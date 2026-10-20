#!/usr/bin/env python3
# deployer/install_queue.py — the install-queue consumer that runs ON a
# deployed host as `ovn-fabric-install.service`. This file is real, tested,
# type-checked source; the generated deployer installs it on each host
# UNCHANGED (deployer/ir_common.py's _emit_install_queue). It knows
# nothing about any host: everything it does comes from the queue.
#
# Why it exists: deploying a ROUTER can cut the host's own internet access
# while the deploy runs. Installing packages or pulling images inline then
# fails — or stalls — in the middle of the pass. So the deploy installs
# nothing itself: the generated deployer QUEUES what the host needs, and
# this service consumes the queue whenever the internet is there.
#
# The queue is a directory of files, one JOB per file (JSON), each named
# after the moment it was queued (UTC, to the microsecond):
#
#   /var/spool/ovn-fabric/install/20261011T195501.000101Z-package-curl.json
#   /var/spool/ovn-fabric/install/20261011T195501.000417Z-image-coredns_coredns_latest.json
#   /var/spool/ovn-fabric/install/20261011T195533.120008Z-unit-ovn-kernel-x.service.json
#
#   {"kind": "package", "name": "ovs", "packages": ["openvswitch-switch"],
#    "install": ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get",
#                "install", "-y", "openvswitch-switch"],
#    "apt": true}
#   {"kind": "image", "image": "coredns/coredns:latest", "dockerfile": null}
#   {"kind": "unit", "unit": "ovn-kernel-x.service"}
#
# The service runs for good and looks at the directory every POLL_SECONDS:
# whoever wants something installed drops a file there, nothing has to be
# started or told. A job's file is REMOVED when the job is done — and
# "done" is checked on the host first (dpkg, `docker image inspect`,
# `systemctl is-active`), so a job for something the host already has
# costs no connection. What is left in the directory is exactly what the
# host still lacks: `ls` it.
#
# Files are worked on in NAME order — the order they were queued in. And
# what a job builds on comes first: an image waits while a package queued
# before it still has tries left (docker itself may be that package), a
# unit while a package or an image queued before it does. So a unit is
# only (re)started when its image and tools are there, and a restart
# never needs the network (a unit that IS the host's uplink is not torn
# down just to fail again for lack of an image). Only `*.json` is read:
# write a job under another name and rename it, and the consumer never
# sees half a file.
#
# A package/image job is only tried while the host is ONLINE; otherwise
# it waits, for as long as it takes. Online means: a public DNS resolver
# (RESOLVERS — 1.1.1.1 and two others, by ADDRESS) resolves a query sent
# straight to it. Not the host's own resolver, which answers from its
# cache or from a neighbour long after the uplink is gone; and not a
# connection to some mirror, which says more about the mirror. A try
# that fails is repeated with a growing pause; after
# ATTEMPTS of them the job stops holding up the jobs behind it and is only
# retried every SLOW_RETRY seconds (an image nobody publishes must not
# block a router's restart for good). A unit is restarted at most
# ATTEMPTS times and then left alone — one that fails for a reason the
# network cannot fix must not be bounced forever. Rewriting a job's file
# (the next deploy does) starts its count again.
#
#   ovn-fabric-install           the service: poll the queue for good
#   ovn-fabric-install --once    one go over the queue, no waiting,
#                                packages and images only — the deploy
#                                pass runs this in the foreground, so a
#                                host that is online gets its tools
#                                before the pass needs them
#
# Stdlib only, and nothing newer than the python3 a target host ships.

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Literal, TypedDict, Union

QUEUE_DIR = "/var/spool/ovn-fabric/install"


class PackageJob(TypedDict):
    kind: Literal["package"]
    name: str
    # Done when ALL of these dpkg packages are installed.
    packages: list[str]
    # The command that installs them.
    install: list[str]
    # True when `install` is an apt-get call (the package lists are then
    # refreshed first, once per pass).
    apt: bool


class ImageJob(TypedDict):
    kind: Literal["image"]
    image: str
    # The Dockerfile to build it from; None means `docker pull`.
    dockerfile: str | None


class UnitJob(TypedDict):
    """A systemd unit that should be running: done when it is active;
    (re)started when it is failed or was never started."""

    kind: Literal["unit"]
    unit: str


# `Union`, not `|`: this line runs, and on a host's python3 older than 3.10
# `A | B` of two classes does not.
Job = Union[PackageJob, ImageJob, UnitJob]  # noqa: UP007

POLL_SECONDS = 10
# Who is asked whether the host is online (see online()): public DNS
# resolvers, by address.
RESOLVERS = ["1.1.1.1", "8.8.8.8", "9.9.9.9"]
DNS_PORT = 53
# How long they have to answer.
ONLINE_TIMEOUT = 3
# One install/build/pull/restart may not hang forever on a connection
# that died under it.
COMMAND_TIMEOUT = 900
# Tries (per version of a job's file) before a package/image job stops
# holding up the jobs behind it, and before a unit is left alone.
ATTEMPTS = 3
# How often a package/image job is still retried after that.
SLOW_RETRY = 600
# A file younger than this that does not parse is still being written.
SETTLE_SECONDS = 2


def log(message: str) -> None:
    print(f"ovn-fabric-install: {message}", flush=True)


def run(argv: list[str]) -> bool:
    log("$ " + " ".join(argv))
    try:
        return subprocess.run(argv, timeout=COMMAND_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        log(f"timed out after {COMMAND_TIMEOUT}s: {argv[0]}")
    except OSError as exc:
        log(f"cannot run {argv[0]}: {exc.strerror}")
    return False


def quiet(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """A local, network-free query — None when the tool itself is missing."""
    try:
        return subprocess.run(argv, capture_output=True, text=True)
    except OSError:
        return None


def _resolved(reply: bytes, ident: int) -> bool:
    """Is `reply` a resolver's answer to OUR query — and an answer that
    resolves it (no error, at least one record)?"""
    if len(reply) < 12:
        return False
    reply_id, flags, _questions, answers = struct.unpack(">HHHH", reply[:8])
    return reply_id == ident and bool(flags & 0x8000) and flags & 0x000F == 0 and answers > 0


def online() -> bool:
    """Is the internet there? One DNS query — the root zone's name
    servers, which every resolver knows — goes to each of RESOLVERS at
    once; the host is online when one of them resolves it within
    ONLINE_TIMEOUT."""
    ident = int.from_bytes(os.urandom(2), "big")
    # Header: id, flags (recursion desired), one question; the question:
    # the root name, type NS, class IN.
    query = struct.pack(">HHHHHH", ident, 0x0100, 1, 0, 0, 0) + b"\x00" + struct.pack(">HH", 2, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            asked = 0
            for resolver in RESOLVERS:
                try:
                    sock.sendto(query, (resolver, DNS_PORT))
                    asked += 1
                except OSError:
                    pass  # no route to this one
            if not asked:
                return False
            deadline = time.monotonic() + ONLINE_TIMEOUT
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                sock.settimeout(left)
                reply, sender = sock.recvfrom(512)
                if sender[0] in RESOLVERS and _resolved(reply, ident):
                    return True
    except OSError:  # includes the timeout
        return False


# ── the queue ─────────────────────────────────────────────────────────


def _is_strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def parse_job(raw: object) -> Job | None:
    """A queue file's content as a job — None when it is not one. The
    shape is checked field by field: a file that is not what the
    generator writes must not be half-understood."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "package":
        name, packages, install = raw.get("name"), raw.get("packages"), raw.get("install")
        apt = raw.get("apt")
        if (
            isinstance(name, str)
            and _is_strings(packages)
            and _is_strings(install)
            and install
            and isinstance(apt, bool)
        ):
            return json.loads(json.dumps(raw))  # type: ignore[no-any-return]
        return None
    if kind == "image":
        image, dockerfile = raw.get("image"), raw.get("dockerfile")
        if (
            isinstance(image, str)
            and "dockerfile" in raw
            and (dockerfile is None or isinstance(dockerfile, str))
        ):
            return json.loads(json.dumps(raw))  # type: ignore[no-any-return]
        return None
    if kind == "unit" and isinstance(raw.get("unit"), str):
        return json.loads(json.dumps(raw))  # type: ignore[no-any-return]
    return None


class Entry(TypedDict):
    path: str
    # The file's modification time — its "version": a rewritten file is a
    # new job as far as the tries go.
    mtime: float
    job: Job


def read_queue() -> tuple[list[Entry], list[str]]:
    """(the jobs in file-name order; the files that are not jobs)."""
    try:
        names = sorted(name for name in os.listdir(QUEUE_DIR) if name.endswith(".json"))
    except OSError:
        return [], []
    entries: list[Entry] = []
    broken: list[str] = []
    for name in names:
        path = os.path.join(QUEUE_DIR, name)
        try:
            mtime = os.stat(path).st_mtime
            with open(path, encoding="utf-8") as handle:
                job = parse_job(json.load(handle))
        except FileNotFoundError:
            continue  # consumed or replaced under us — not an error
        except (OSError, ValueError):
            job = None
        if job is not None:
            entries.append({"path": path, "mtime": mtime, "job": job})
        elif time.time() - mtime >= SETTLE_SECONDS:
            broken.append(path)
    return entries, broken


# What a job builds on: one of a higher rank waits for the unfinished
# ones of a lower rank queued before it.
RANK = {"package": 0, "image": 1, "unit": 2}


def describe(job: Job) -> str:
    if job["kind"] == "package":
        return f"package {job['name']}"
    if job["kind"] == "image":
        return f"image {job['image']}"
    return f"unit {job['unit']}"


def finish(path: str, job: Job) -> None:
    log(f"done: {describe(job)}")
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


# ── one job ───────────────────────────────────────────────────────────


def package_installed(package: str) -> bool:
    proc = quiet(["dpkg-query", "-W", "-f=${Status}", package])
    return proc is not None and proc.returncode == 0 and proc.stdout == "install ok installed"


def unit_state(unit: str) -> str:
    proc = quiet(["systemctl", "is-active", unit])
    return proc.stdout.strip() if proc is not None else "unknown"


def done(job: Job) -> bool:
    """Checked on the host itself — never needs the network."""
    if job["kind"] == "package":
        return all(package_installed(package) for package in job["packages"])
    if job["kind"] == "image":
        proc = quiet(["docker", "image", "inspect", job["image"]])
        return proc is not None and proc.returncode == 0
    return unit_state(job["unit"]) == "active"


def perform(job: Job) -> None:
    if job["kind"] == "package":
        run(job["install"])
    elif job["kind"] == "image":
        if job["dockerfile"] is None:
            run(["docker", "pull", job["image"]])
        else:
            with tempfile.TemporaryDirectory() as context:
                with open(os.path.join(context, "Dockerfile"), "w", encoding="utf-8") as out:
                    out.write(job["dockerfile"] + "\n")
                run(["docker", "build", "-t", job["image"], context])
    else:
        run(["systemctl", "restart", job["unit"]])


# ── one pass over the queue ───────────────────────────────────────────


class Attempt(TypedDict):
    mtime: float
    count: int
    # Not again before this time (time.monotonic()).
    due: float


class Pass(TypedDict):
    # Jobs still in the queue that are on their way: waiting for the
    # internet, for what they build on, for a unit that is starting, or
    # between two tries.
    waiting: list[str]
    # Jobs that used up their tries (a package/image is still retried
    # every SLOW_RETRY, a unit no more), and files that are not jobs.
    stuck: list[str]


def ready(job: Job, asked: list[bool]) -> bool:
    """Can the job be tried right now? A unit unless systemd is still
    starting it; a package/image when the host is online — asked at most
    once per pass (`asked` holds the answer), and not at all in a pass
    that has nothing to install."""
    if job["kind"] == "unit":
        return unit_state(job["unit"]) != "activating"
    if not asked:
        asked.append(online())
    return asked[0]


def work(tried: dict[str, Attempt], units: bool) -> Pass:
    """Go over the queue once: drop every job that is already done and
    try what is due. `tried` remembers the tries per file between passes;
    `units` is False for `--once`, which leaves the unit jobs to the
    service."""
    entries, broken = read_queue()
    result: Pass = {"waiting": [], "stuck": list(broken)}
    for path in list(tried):
        if not any(entry["path"] == path for entry in entries):
            del tried[path]

    # The lowest rank among the jobs so far that are not done and still
    # have tries left: every later job of a higher rank waits for them.
    holding = len(RANK)
    refreshed = False
    asked: list[bool] = []
    for entry in entries:
        path, job = entry["path"], entry["job"]
        if job["kind"] == "unit" and not units:
            continue
        if done(job):
            finish(path, job)
            continue
        name = describe(job)
        attempt = tried.get(path)
        if attempt is None or attempt["mtime"] != entry["mtime"]:
            attempt = tried[path] = {"mtime": entry["mtime"], "count": 0, "due": 0.0}
        rank = RANK[job["kind"]]
        if rank > holding:
            result["waiting"].append(name)  # what it builds on comes first
            continue
        if attempt["count"] >= ATTEMPTS:
            # Out of tries: holds nothing up any more. A unit is left
            # alone; a package/image gets one try per SLOW_RETRY.
            slow = job["kind"] != "unit" and attempt["due"] <= time.monotonic()
            if not (slow and ready(job, asked)):
                result["stuck"].append(name)
                continue
        elif attempt["due"] > time.monotonic() or not ready(job, asked):
            result["waiting"].append(name)
            holding = min(holding, rank)
            continue
        if job["kind"] == "package" and job["apt"] and not refreshed:
            # A failed refresh is not the end: the install says whether
            # the lists the host already has were good enough.
            run(["apt-get", "update"])
            refreshed = True
        attempt["count"] += 1
        perform(job)
        if done(job):
            finish(path, job)
        elif attempt["count"] >= ATTEMPTS:
            attempt["due"] = time.monotonic() + SLOW_RETRY
            result["stuck"].append(name)
        else:
            pause = min(POLL_SECONDS * 2 ** attempt["count"], SLOW_RETRY)
            attempt["due"] = time.monotonic() + pause
            result["waiting"].append(name)
            holding = min(holding, rank)
    return result


def once() -> int:
    """Jobs left in the queue are not a failure of this go — they are what
    the service is for — so the exit status is 0 either way."""
    result = work({}, units=False)
    left = result["waiting"] + result["stuck"]
    if left:
        log(f"left for ovn-fabric-install.service: {', '.join(left)}")
    else:
        log("nothing left to install")
    return 0


def serve() -> int:
    """Poll the queue for good. Says what it is waiting on or stuck with
    when that changes, not on every poll."""
    tried: dict[str, Attempt] = {}
    announced: Pass = {"waiting": [], "stuck": []}
    log(f"watching {QUEUE_DIR} every {POLL_SECONDS}s")
    while True:
        result = work(tried, units=True)
        if result != announced:
            if result["waiting"]:
                log(f"waiting on: {', '.join(result['waiting'])}")
            if result["stuck"]:
                log(f"stuck, left in {QUEUE_DIR}: {', '.join(result['stuck'])}")
            if not result["waiting"] and not result["stuck"]:
                log("the queue is empty")
            announced = result
        time.sleep(POLL_SECONDS)


def main(argv: list[str]) -> int:
    if argv == ["--once"]:
        return once()
    if argv:
        print("usage: ovn-fabric-install [--once]", file=sys.stderr)
        return 2
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
