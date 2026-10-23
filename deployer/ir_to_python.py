# deployer/ir_to_python.py — the all-in-one Python deployer generator:
# every emitter lives in deployer/ir_common.py and builds through
# the Emitter interface; this module provides the PYTHON rendering of it
# (PythonEmitter) and wraps everything into ONE self-contained Python
# file that picks what to run at runtime (2026-08-19, refactored
# 2026-08-23):
#   --action create|delete   (default create; create aborts on the first
#                             failed command, delete warns and continues)
#   --cluster                only the cluster-wide ovn-nbctl pass
#   --host NAME              only that host's pass
#   --verbose                log every command (DEBUG)
#   --ovn-purge              instead of an action: reset OVS and OVN to a
#                            freshly-installed state (see ovn_purge in the
#                            runtime below)
# With neither --cluster nor --host, everything runs in ONE order for
# `create` — every host first (each installs its OS dependencies and, on
# the central, exposes the NB/SB DBs), then the cluster-wide ovn-nbctl
# pass, which needs both — and the reverse for `delete`: the cluster
# topology first, then every host (2026-09-19). Cluster-only (`--cluster`)
# keeps its plain meaning: run just the cluster pass.
#
# The generated file is NOT a big shell block embedded verbatim: each
# emitted operation becomes a direct
# Python call — a command becomes run_cmd(argv), a file append (e.g. a
# systemd unit) becomes write_file(path, content), and the host's
# packages/images become one INSTALL object run by run_install() — each
# with the runtime's own logging/error policy. That is what makes the
# Python deployment genuinely controllable: no shell syntax (heredocs,
# redirection) survives into the generated file for the runtime to
# re-parse.
# python3 is required on every target host (2026-08-19, confirmed).

from __future__ import annotations

import functools
import pathlib
import re
from collections.abc import Callable, Sequence
from typing import Any

from protocol import generated as pt

from .ir_common import (
    Action,
    Emitter,
    Ref,
    _abstract,
    _configure_dependencies,
    _emit_cluster_body,
    _emit_host_configure,
    _emit_host_script,
    _emit_kernel_router_owner,
    _emit_service_down,
    _emit_service_up,
    _install_plan,
    _job_dependencies,
    _kernel_router_owners,
    _netns_name,
    _python_token,
    _service_faces,
)


def _py_str(s: str) -> str:
    """Embed `s` verbatim as a triple-quoted Python string literal."""
    escaped = s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    # Guard the closing `"""` against merging with a trailing quote in
    # the content (e.g. a wire script ending in `"$container"`) — a real
    # trailing newline keeps the delimiter unambiguous, and write_file()
    # already writes exactly one trailing newline either way.
    if escaped.endswith('"'):
        escaped += "\n"
    return '"""' + escaped + '"""'


def _python_argv_expr(argv: Sequence[str | Ref]) -> str:
    # Render `ip netns exec <ns> ...` as in_netns('<ns>', [...]) so the
    # generated code stays readable.
    if len(argv) >= 4 and argv[0] == "ip" and argv[1] == "netns" and argv[2] == "exec":
        netns = _python_token(argv[3])
        rest = ", ".join(_python_token(token) for token in argv[4:])
        return f"in_netns({netns}, [{rest}])"
    return "[" + ", ".join(_python_token(token) for token in argv) + "]"


class PythonEmitter(Emitter):
    """The Python rendering of the shared emitter interface: a command
    becomes a run_cmd(...) call whose command is the string produced by
    ir_common's own `_sh` (shlex.join) — the SAME function the shell
    front-end uses to render the line — and a file append becomes a
    write_file(...) call; both wrapped in the runtime's own
    logging/error handling. `action` (create/delete) is carried on the
    emitter — the SAME parameter every ir_common emitter branches on —
    so a create and its reverse (delete) live in one emitter function."""

    def __init__(self, label: str, action: Action) -> None:
        self.label = label
        self._indent = 0
        super().__init__(action)

    def _push(self, line: str) -> None:
        # Indent by the emitter's current block depth — begin_block()/
        # end_block() bracket a conditional block (e.g. the docker build).
        # _python_fn adds the function body's own
        # single level on top of this.
        self.lines.append(("    " * self._indent + line) if line else "")

    def begin_block(self, header: str) -> None:
        self._push(header)
        self._indent += 1

    def end_block(self) -> None:
        self._indent -= 1

    def comment(self, text: str) -> None:
        self._push(text)

    def blank(self) -> None:
        self.lines.append("")

    def sh(
        self, argv: Sequence[str | Ref], background: bool = False, optional: bool = False
    ) -> None:
        # `background` is only meaningful in the nested shell router-body
        # emitter (_ShellBody) — the Python runtime has no `&` notion.
        # `optional=True` retires the create-action abort for this one
        # command: it warns and continues (the runtime's own policy).
        # A `Ref` token becomes the bare Python variable of that name, so
        # the runtime passes the variable's VALUE as one argv element.
        abort = "False" if optional else "abort_on_error"
        expr = _python_argv_expr(argv)
        self._push(f"await job.run_cmd({self.label!r}, {expr}, {abort})")

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(f"{var} = await job.capture_cmd({self.label!r}, {expr}, abort_on_error)")

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(f"{var} = await wait_for_chassis(job, {self.label!r}, {expr}, 30)")

    def append(self, path: str, content: str) -> None:
        self._push(
            f"await write_file(job, {self.label!r}, {path!r}, {_py_str(content)}, "
            "abort_on_error)"
        )

    def install_self(self, path: str) -> None:
        # Copy the running generated program to `path` (install_self in
        # the runtime) — the boot restore unit execs that copy.
        self._push(f"await install_self(job, {self.label!r}, {path!r}, abort_on_error)")

    def enable_unit(self, unit: str, optional: bool = True) -> None:
        # `no_start` is a parameter of the generated host function: the
        # boot restore pass enables the fabric units but leaves starting
        # them to systemd, which orders them After= the restore unit.
        abort = "False" if optional else "abort_on_error"
        self.begin_block("if no_start:")
        self._push(f"await job.run_cmd({self.label!r}, ['systemctl', 'enable', {unit!r}], {abort})")
        self.end_block()
        self.begin_block("else:")
        self._push(
            f"await job.run_cmd({self.label!r}, ['systemctl', 'enable', '--now', {unit!r}], "
            f"{abort})"
        )
        # The unit may already be running the PREVIOUS installed copy of
        # this file (install_self has just replaced it). `enable --now`
        # does not restart an active unit, so force the new code in.
        self._push(
            f"await job.run_cmd({self.label!r}, ['systemctl', 'restart', {unit!r}], " f"{abort})"
        )
        self.end_block()


class PythonBodyEmitter(Emitter):
    """One generated job body (a kernel router's or service's up/down),
    as direct Python calls: `sh` becomes job.run_cmd(),
    `append` becomes write_file(), `capture` becomes capture_cmd(). The
    body runs in the generated file itself — no shell script is written
    or exec'd."""

    def __init__(self, label: str, action: Action) -> None:
        self.label = label
        self._indent = 0
        super().__init__(action)

    def _push(self, line: str) -> None:
        self.lines.append(("    " * self._indent + line) if line else "")

    def sh(
        self, argv: Sequence[str | Ref], background: bool = False, optional: bool = False
    ) -> None:
        # Every job is blocking: `background` (a shell `&`) has no
        # meaning here; the command runs and is awaited.
        abort = "False" if optional else "abort_on_error"
        expr = _python_argv_expr(argv)
        self._push(f"await job.run_cmd({self.label!r}, {expr}, {abort})")

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(f"{var} = await job.capture_cmd({self.label!r}, {expr}, abort_on_error)")

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(f"{var} = await wait_for_chassis(job, {self.label!r}, {expr}, 30)")

    def install(self, host: str) -> None:
        self._push(f"await run_install(job, INSTALL[{host!r}], abort_on_error)")

    def append(self, path: str, content: str) -> None:
        self._push(
            f"await write_file(job, {self.label!r}, {path!r}, {_py_str(content)}, "
            "abort_on_error)"
        )

    def py(self, line: str) -> None:
        self._push(line)

    def begin_block(self, header: str) -> None:
        self._push(header)
        self._indent += 1

    def end_block(self) -> None:
        self._indent -= 1

    def comment(self, text: str) -> None:
        self._push(text)

    def blank(self) -> None:
        self.lines.append("")


def _body_fn_name(host_name: str, name: str, action: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "_", f"{host_name}_{name}_{action}")
    return f"_job_{clean}"


def _python_body_fn(name: str, label: str, action: Action, body: Callable[[Emitter], None]) -> str:
    emit = PythonBodyEmitter(label, action)
    body(emit)
    # create aborts on the first failure; delete warns and keeps going —
    # the same policy as the host pass (the old shell scripts' `set -e`
    # only in `up`).
    abort = "False" if action == "delete" else "True"
    lines = [f"    abort_on_error = {abort}"]
    lines += [f"    {line}" if line else "" for line in emit.lines]
    return f"async def {name}(job: Job) -> None:\n" + "\n".join(lines) + "\n"


_GENERATED_RUNTIME = r"""#!/usr/bin/env python3
# Generated by ovn-fabric deployer/ir_to_python.py — DO NOT EDIT.
#
# One file for every pass the generator knows: the cluster-wide ovn-nbctl
# action (run once, from whichever chassis reaches the shared NB DB) and
# each per-host OVS/netns action (run on the host itself), for both
# create and delete. Pick what to run with --action / --cluster / --host;
# with neither --cluster nor --host, create runs every host first (each
# installs its OS dependencies, then brings up OVS/OVN and, on the
# central, exposes the NB/SB DBs) and the cluster-wide ovn-nbctl pass
# LAST — it needs ovn-nbctl installed and the central's DBs reachable, so
# running it first on a fresh host fails (2026-09-19). Delete reverses
# that: the cluster topology goes first, then the hosts.
#
# The IR's dependency/image data is merged into ONE install object per
# host (`BIDIRECTIONAL_DEPS[host]`) and executed directly before OVS/OVN.
#
# --ovn-purge replaces the action: it wipes OVS's database and, on the
# central, OVN's NB/SB databases, and restarts the daemons — back to the
# state right after the packages were installed (see ovn_purge below).
#
# Every command/file-write is a direct Python call into run_cmd()/
# write_file()/run_install() below, each with the same error policy:
# create aborts on the first failure, delete warns and keeps going.

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

_deploy_log = logging.getLogger("ovn-fabric.deployer")


def _job_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ovn-fabric.deployer.{name}")

# __RUNTIME_LIB__


async def _docker_image_exists(image: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/docker",
            "image",
            "inspect",
            image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except OSError:
        return False


def in_netns(netns: str, argv: list[str]) -> list[str]:
    # `ip netns exec <netns> ...` without repeating it in every argv.
    return ["ip", "netns", "exec", netns, *argv]


async def _link_exists(name: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip",
            "link",
            "show",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        return await proc.wait() == 0
    except OSError:
        return False


async def ensure_netns(
    job: Job, label: str, netns: str, abort_on_error: bool
) -> None:
    # A stale netns from a failed previous run makes `ip netns add` fail
    # with "File exists"; recreate it so create is idempotent.
    if os.path.lexists(f"/run/netns/{netns}"):
        await job.run_cmd(label, ["ip", "netns", "delete", netns], False)
    await job.run_cmd(label, ["ip", "netns", "add", netns], abort_on_error)


async def ensure_vlan(
    job: Job,
    label: str,
    parent: str,
    name: str,
    vlan_id: int,
    abort_on_error: bool,
) -> None:
    if await _link_exists(name):
        return
    await job.run_cmd(
        label,
        ["ip", "link", "add", "link", parent, "name", name, "type", "vlan", "id", str(vlan_id)],
        abort_on_error,
    )


async def wait_for_chassis(
    job: Job, label: str, cmd: list[str], attempts: int = 30
) -> str:
    restarted = False
    for attempt in range(attempts):
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        value = out.decode(errors="replace").strip()
        if proc.returncode == 0 and value:
            return value
        if not restarted and attempt + 1 >= attempts // 2:
            # The controller is installed but never registered a chassis.
            # (Re)start it once, then keep waiting.
            await job.run_cmd(
                label,
                ["systemctl", "enable", "--now", "ovn-controller"],
                False,
            )
            restarted = True
        await asyncio.sleep(1)
    message = f"{label}: chassis did not register within {attempts}s"
    _log_at(job, "error", message)
    raise JobError(message, 1)


async def write_sysctl_in_netns(
    job: Job,
    label: str,
    netns: str,
    path: str,
    value: str,
    abort_on_error: bool,
) -> None:
    # No shell redirection: run a tiny Python writer inside the netns.
    # `sysctl` cannot address an iface name containing a dot (eth0.2280).
    proc = await asyncio.create_subprocess_exec(
        "ip",
        "netns",
        "exec",
        netns,
        sys.executable,
        "-c",
        "import sys; open(sys.argv[1], 'w').write(sys.argv[2])",
        path,
        value,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    job.register_process(proc)
    try:
        await proc.communicate()
    finally:
        if proc.returncode is not None:
            job.unregister_process(proc)
    if proc.returncode != 0:
        _log_at(
            job,
            "error" if abort_on_error else "warn",
            f"cannot write {path} in {netns}",
        )
        if abort_on_error:
            raise JobError(f"cannot write {path} in {netns}", proc.returncode or 1)


async def ensure_veth(
    job: Job, label: str, name: str, peer: str, abort_on_error: bool
) -> None:
    if await _link_exists(name):
        return
    await job.run_cmd(
        label, ["ip", "link", "add", name, "type", "veth", "peer", "name", peer],
        abort_on_error,
    )


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as out:
        out.write(content + ("" if content.endswith("\n") else "\n"))


async def _ensure_image(
    job: Job, label: str, image: str, dockerfile: str | None
) -> None:
    # Build the image when the IR declares a Dockerfile; otherwise let
    # `docker run` pull it.
    if dockerfile is None or await _docker_image_exists(image):
        return
    build_dir = tempfile.mkdtemp()
    try:
        await write_file(
            job, label, os.path.join(build_dir, "Dockerfile"), dockerfile, True
        )
        await job.run_cmd(
            label, ["/usr/bin/docker", "build", "-t", image, build_dir], True
        )
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _netns_link(container: str) -> str:
    return f"/var/run/netns/{container}"


async def _symlink_netns(job: Job, label: str, container: str, pid: str) -> None:
    await job.run_cmd(label, ["mkdir", "-p", "/var/run/netns"], True)
    link = _netns_link(container)
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(f"/proc/{pid}/ns/net", link)


async def _docker_veth_up(
    job: Job,
    label: str,
    container: str,
    image: str,
    dockerfile: str | None,
    cmd: list[str],
    netns: str,
    veth: str,
    router_ip: str,
    container_ip: str,
    gateway: str,
    run_flags: list[str] | None = None,
) -> None:
    # One veth injected into the router netns, the peer into the
    # container's netns as eth0 (CNI/Multus-style).
    peer = f"{veth}-c"
    await _ensure_image(job, label, image, dockerfile)
    await job.run_cmd(label, ["/usr/bin/docker", "rm", "-f", container], False)
    await job.run_cmd(
        label,
        [
            "/usr/bin/docker",
            "run",
            "-d",
            "--network",
            "none",
            "--name",
            container,
            *(run_flags or []),
            image,
            *cmd,
        ],
        True,
    )
    pid = await job.capture_cmd(
        label, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", container], True
    )
    await _symlink_netns(job, label, container, pid)
    await job.run_cmd(
        label, ["ip", "link", "add", veth, "type", "veth", "peer", "name", peer], True
    )
    await job.run_cmd(label, ["ip", "link", "set", peer, "netns", container], True)
    await job.run_cmd(label, ["ip", "link", "set", veth, "netns", netns], True)
    await job.run_cmd(
        label, ["ip", "netns", "exec", netns, "ip", "link", "set", veth, "up"], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", netns, "ip", "addr", "add", router_ip, "dev", veth], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", container, "ip", "link", "set", peer, "name", "eth0"], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", container, "ip", "link", "set", "eth0", "up"], True
    )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", container, "ip", "addr", "add", container_ip, "dev", "eth0"],
        True,
    )
    await job.run_cmd(
        label,
        [
            "ip",
            "netns",
            "exec",
            container,
            "ip",
            "route",
            "add",
            "default",
            "via",
            gateway,
            "dev",
            "eth0",
        ],
        True,
    )
    await job.run_cmd(label, ["/usr/bin/docker", "wait", container], True)


async def _docker_owns_start(
    job: Job,
    label: str,
    container: str,
    image: str,
    dockerfile: str | None,
    netns: str,
    dev: str,
    transit_leg: str | None,
    mounts: list[dict] | None = None,
    run_flags: list[str] | None = None,
    identity: dict | None = None,
) -> None:
    # The container owns the router's interfaces ("container IS the
    # router"): move them in, then the body applies the static network.
    await _ensure_image(job, label, image, dockerfile)
    await job.run_cmd(label, ["/usr/bin/docker", "rm", "-f", container], False)
    argv = [
        "/usr/bin/docker",
        "run",
        "-d",
        "--privileged",
        "--network",
        "none",
        "--name",
        container,
    ]
    state_dir = None
    for mount in mounts or []:
        host_path = mount["host"]
        await job.run_cmd(label, ["mkdir", "-p", host_path], True)
        argv += ["-v", f"{host_path}:{mount['container']}"]
        if mount["container"] == "/var/lib/zerotier-one":
            state_dir = host_path
    if identity and state_dir:
        public = os.path.join(state_dir, "identity.public")
        secret = os.path.join(state_dir, "identity.secret")
        await write_file(job, label, public, identity["public"], True)
        await write_file(job, label, secret, identity["secret"], True)
        await job.run_cmd(label, ["chmod", "644", public], True)
        await job.run_cmd(label, ["chmod", "600", secret], True)
    argv += [*(run_flags or []), image, "sleep", "infinity"]
    await job.run_cmd(label, argv, True)
    pid = await job.capture_cmd(
        label, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", container], True
    )
    await _symlink_netns(job, label, container, pid)
    if transit_leg:
        await job.run_cmd(
            label,
            [
                "ip",
                "netns",
                "exec",
                netns,
                "ip",
                "link",
                "set",
                transit_leg,
                "netns",
                container,
            ],
            True,
        )
        await job.run_cmd(
            label, ["ip", "netns", "exec", container, "ip", "link", "set", transit_leg, "up"], True
        )
    await job.run_cmd(
        label, ["ip", "netns", "exec", netns, "ip", "link", "set", dev, "netns", container], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", container, "ip", "link", "set", dev, "up"], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", container, "sysctl", "-w", "net.ipv4.ip_forward=1"], True
    )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", container, "sysctl", "-w", "net.ipv6.conf.all.forwarding=1"],
        True,
    )


async def _docker_exec_wait(
    job: Job, label: str, container: str, cmd: list[str], dev: str
) -> None:
    await job.run_cmd(label, ["/usr/bin/docker", "exec", container, *cmd, dev], True)
    await job.run_cmd(label, ["/usr/bin/docker", "wait", container], True)


async def _zerotier_up(
    job: Job,
    label: str,
    netns: str,
    home: str,
    network: str,
    masq: list[str],
    routes: list[str],
) -> None:
    # `/usr/sbin/zerotier-one <home>` WITHOUT `-d` is the foreground
    # daemon: run it as this job's tracked child, do the wiring while it
    # runs, then await it. The job blocks for the daemon's whole life;
    # Job.stop() kills the process group, so `down` needs no pkill and no
    # PID bookkeeping.
    await job.run_cmd(label, ["mkdir", "-p", home], True)
    daemon = asyncio.create_task(
        job.run_cmd(
            label,
            ["ip", "netns", "exec", netns, "/usr/sbin/zerotier-one", home],
            True,
        )
    )
    try:
        for _ in range(10):
            if await job.capture_cmd(
                label,
                ["ip", "netns", "exec", netns, "zerotier-cli", f"-D{home}", "info"],
                False,
            ):
                break
            await asyncio.sleep(1)
        await job.run_cmd(
            label,
            ["ip", "netns", "exec", netns, "zerotier-cli", f"-D{home}", "join", network],
            True,
        )
        var = ""
        for _ in range(30):
            raw = await job.capture_cmd(
                label,
                [
                    "ip",
                    "netns",
                    "exec",
                    netns,
                    "zerotier-cli",
                    f"-D{home}",
                    "-j",
                    "listnetworks",
                ],
                False,
            )
            if raw:
                try:
                    for entry in json.loads(raw):
                        if entry.get("nwid") == network:
                            var = entry.get("portDeviceName") or ""
                            break
                except ValueError:
                    var = ""
            if var and var != "null":
                break
            await asyncio.sleep(1)
        if not var or var == "null":
            raise RuntimeError(f"zerotier network {network}: no portDeviceName in {netns}")
        await job.run_cmd(
            label, ["ip", "netns", "exec", netns, "ip", "link", "set", var, "up"], True
        )
        for family in masq:
            binary = "iptables" if family == "ipv4" else "ip6tables"
            await job.run_cmd(
                label,
                [
                    "ip",
                    "netns",
                    "exec",
                    netns,
                    binary,
                    "-t",
                    "nat",
                    "-A",
                    "POSTROUTING",
                    "-o",
                    var,
                    "-j",
                    "MASQUERADE",
                ],
                True,
            )
        for dst in routes:
            await job.run_cmd(
                label,
                ["ip", "netns", "exec", netns, "ip", "route", "add", dst, "dev", var],
                True,
            )
    except BaseException:
        daemon.cancel()
        raise
    # Block for the daemon; stop kills it and this returns.
    await daemon


async def write_file(
    job: Job, label: str, path: str, content: str, abort_on_error: bool
) -> None:
    # The append() emitter operation at runtime: write `content` (plus a
    # single trailing newline, so the file always ends in one) to
    # `path`, with the same error policy as run_cmd(). The file I/O runs
    # off the event loop.
    job.debug(f"{label}$ write {path} ({len(content.splitlines())} lines)")
    try:
        await asyncio.to_thread(_write_text, path, content)
    except OSError as exc:
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            job.error(f"cannot write in {label}{detail}: {path}")
            raise JobError(f"cannot write in {label}{detail}: {path}", 127)
        job.warn(f"cannot write in {label}{detail}: {path}")


def _install_copy(src: str, path: str) -> None:
    shutil.copyfile(src, path)
    os.chmod(path, 0o755)


async def install_self(job: Job, label: str, path: str, abort_on_error: bool) -> None:
    # The install_self() emitter operation: copy THIS generated program to
    # `path`, so ovn-fabric-restore.service can re-run the host pass at
    # boot. `__file__` is the script that was invoked; a run from stdin
    # (`python3 -`) has none — that only warns, and the restore unit's
    # ExecCondition then skips itself.
    src = globals().get("__file__")
    if not src or not os.path.isfile(src):
        job.warn(
            f"cannot install the boot restore copy in {label}: "
            "run the deployer from a file, not stdin"
        )
        return
    if os.path.abspath(src) == os.path.abspath(path):
        return
    job.debug(f"{label}$ install {src} -> {path}")
    try:
        await asyncio.to_thread(_install_copy, src, path)
    except OSError as exc:
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            job.error(f"cannot install in {label}{detail}: {path}")
            raise JobError(f"cannot install in {label}{detail}: {path}", 127)
        job.warn(f"cannot install in {label}{detail}: {path}")


INSTALL_INTERVAL = 24 * 60 * 60
APT_UPDATE_STAMP = "/var/lib/ovn-fabric/last-apt-update"


def _read_stamp(path: str) -> float:
    try:
        with open(path, encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return 0.0


def _write_stamp(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(time.time()))


class DepWaiter:
    # Deployer-side dependency waiter, installed as a job's ctx. The
    # runtime only knows ctx and on_emit; the job's on_emit filter feeds
    # every event to waitfor(), which fires the action once all deps are
    # seen.

    def __init__(self, dependencies: list[dict]) -> None:
        self._dependencies = {self._key(dep) for dep in dependencies}
        self._seen: set[tuple] = set()
        self._fired = False

    @staticmethod
    def _key(dep: object) -> tuple:
        if isinstance(dep, dict):
            kind = dep.get("kind")
            if kind == "abstract":
                return ("abstract", dep.get("name"))
            if kind == "docker-image":
                return ("docker-image", dep.get("image"))
            if kind in ("apt-dep", "apt-pkg"):
                return ("apt-dep", dep.get("package") or dep.get("name"))
            if kind in ("sys-cmd-dep", "sys-cmd"):
                return ("sys-cmd-dep", dep.get("name"))
        return ("unknown", repr(dep))

    async def waitfor(self, event: object, action: Any) -> None:
        if self._fired:
            return
        if self._dependencies <= self._seen:
            self._fired = True
            await action()
            return
        if isinstance(event, dict):
            kind = event.get("kind")
            key: tuple | None = None
            if kind == "dependency" and isinstance(event.get("dep"), dict):
                key = self._key(event["dep"])
            elif kind == "apt-dep":
                key = ("apt-dep", event.get("package"))
            elif kind == "sys-cmd-dep":
                key = ("sys-cmd-dep", event.get("name"))
            elif kind == "abstract-dep":
                key = ("abstract", event.get("name"))
            if key is not None and key in self._dependencies:
                self._seen.add(key)
        if not self._dependencies or self._dependencies <= self._seen:
            self._fired = True
            await action()


def register(
    job: Job, dependencies: list[dict], action: Any, keep_alive: bool = False
) -> None:
    # The job has no on_start work: it registers a DepWaiter with its
    # abstract deps and, once they are fulfilled, runs the job content.
    waiter = DepWaiter(dependencies)
    job.ctx = waiter
    job.hold()

    async def on_event(job: Job, event: object, ctx: object = None) -> None:
        async def run() -> None:
            try:
                await action(job)
            except BaseException:
                job.release()
                raise
            if keep_alive:
                # A router/service job stays alive after its setup; its
                # down body runs only when the tree/service is stopped.
                job.hold()
            else:
                job.release()

        await waiter.waitfor(event, run)

    job.on_emit(on_event)

    async def dependency_timeout() -> None:
        if DEPENDENCY_TIMEOUT <= 0:
            return
        await asyncio.sleep(DEPENDENCY_TIMEOUT)
        if not waiter._fired and not job._stopped:
            job.error(
                f"{job.name}: dependencies were not fulfilled within "
                f"{DEPENDENCY_TIMEOUT}s; the install cannot complete "
                f"(missing packages or no network)"
            )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(dependency_timeout())


def package_waiters(host: str) -> dict[str, DepWaiter]:
    # One SEPARATE DepWaiter instance per abstract dependency in
    # BIDIRECTIONAL_DEPS, keyed by the abstract name and initialized with
    # that entry's concrete deps.
    waiters: dict[str, DepWaiter] = {}
    for name, entry in BIDIRECTIONAL_DEPS.get(host, {}).items():
        if entry["abstract"].get("kind") != "abstract":
            continue
        waiters[name] = DepWaiter(entry["concrete"])
    return waiters


async def _apt_retry(
    job: Job, label: str, cmd: list[str], attempts: int = 6
) -> bool:
    # apt-get may hit the network; bound each attempt and retry with an
    # exponential backoff. Each attempt is a fresh run_cmd (the old one is
    # killed on timeout).
    delay = 5.0
    for attempt in range(1, attempts + 1):
        if await job.run_cmd(label, cmd, False, timeout=600):
            return True
        if attempt < attempts:
            job.warn(
                f"{label}: attempt {attempt}/{attempts} failed; retrying in {delay:g}s"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300.0)
    job.error(f"{label}: gave up after {attempts} attempts")
    return False


async def _dpkg_version(job: Job, name: str) -> str | None:
    status = await job.capture_cmd(
        "dpkg", ["dpkg-query", "-W", "-f=${Status} ${Version}", name], False
    )
    # `unknown ok not-installed` also contains the substring
    # "installed" — only the exact installed state counts.
    if not status.startswith("install ok installed"):
        return None
    return status.rsplit(" ", 1)[-1]


async def _install_packages(job: Job, deps: dict) -> None:
    label = "install-packages"
    apt_packages: list[str] = []
    sys_cmds: list[dict] = []
    for entry in deps.values():
        for dep in entry["concrete"]:
            if dep["kind"] == "apt-dep" and dep["package"] not in apt_packages:
                apt_packages.append(dep["package"])
            elif dep["kind"] == "sys-cmd-dep":
                sys_cmds.append(dep)

    installed = [
        name for name in apt_packages if await _dpkg_version(job, name) is not None
    ]
    missing = [name for name in apt_packages if name not in installed]

    update_ok = True
    if apt_packages:
        now = time.time()
        last = await asyncio.to_thread(_read_stamp, APT_UPDATE_STAMP)
        if now - last < INSTALL_INTERVAL:
            job.debug(
                f"apt update is fresh ({(now - last) / 3600:.1f}h old); skipping"
            )
        else:
            update_ok = await _apt_retry(
            job,
            label,
            [
                "apt-get",
                "-o",
                "Dpkg::Use-Pty=0",
                "-o",
                "Dpkg::Progress-Fancy=0",
                "update",
            ],
        )
            if update_ok:
                try:
                    await asyncio.to_thread(_write_stamp, APT_UPDATE_STAMP)
                except OSError as exc:
                    job.warn(f"cannot save apt update timestamp: {exc}")
            else:
                job.error(f"{label}: apt-get update failed; skipping install")

    if update_ok and installed:
        await _apt_retry(
            job,
            label,
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "APT_LISTCHANGES_FRONTEND=none",
                "LC_ALL=C",
                "TERM=dumb",
                "apt-get",
                "-o",
                "Dpkg::Use-Pty=0",
                "-o",
                "Dpkg::Progress-Fancy=0",
                "install",
                "--only-upgrade",
                "-y",
                *sorted(installed),
            ],
        )

    apt_ok = update_ok
    if apt_ok and missing:
        apt_ok = await _apt_retry(
            job,
            label,
            [
                "env",
                "DEBIAN_FRONTEND=noninteractive",
                "APT_LISTCHANGES_FRONTEND=none",
                "LC_ALL=C",
                "TERM=dumb",
                "apt-get",
                "-o",
                "Dpkg::Use-Pty=0",
                "-o",
                "Dpkg::Progress-Fancy=0",
                "install",
                "-y",
                *sorted(missing),
            ],
        )

    installed_sys: set[str] = set()
    for dep in sorted(sys_cmds, key=lambda entry: entry.get("order", 0)):
        if not apt_ok:
            job.warn(
                f"cannot install {dep['name']}: apt dependencies failed"
            )
            continue
        if await _apt_retry(job, label, dep["cmd"]):
            installed_sys.add(dep["name"])

    # Concrete events only; the packages job ctx translates them.
    for name in apt_packages:
        if await _dpkg_version(job, name) is not None:
            job.emit(
                {"kind": "apt-dep", "package": name, "state": "installed"},
                queue=True,
            )
    for name in installed_sys:
        job.emit(
                {"kind": "sys-cmd-dep", "name": name, "state": "installed"},
                queue=True,
            )


async def _install_image(job: Job, entry: dict) -> None:
    label = f"image:{entry['image']}"
    if await _docker_image_exists(entry["image"]):
        job.debug(f"{entry['image']} already exists")
        job.emit(
            {
                "kind": "dependency",
                "dep": {"kind": "docker-image", "image": entry["image"]},
                "state": "installed",
            },
            queue=True,
        )
        return
    if entry.get("dockerfile") is None:
        ok = await job.run_cmd(
            label, ["/usr/bin/docker", "pull", entry["image"]], False, timeout=600
        )
        if not ok:
            job.warn(f"could not pull {entry['image']}")
            return
        job.info(f"pulled {entry['image']}")
    else:
        build_dir = tempfile.mkdtemp()
        try:
            await write_file(
                job,
                label,
                os.path.join(build_dir, "Dockerfile"),
                entry["dockerfile"],
                True,
            )
            ok = await job.run_cmd(
                label,
                ["/usr/bin/docker", "build", "-t", entry["image"], build_dir],
                False,
                timeout=600,
            )
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)
        if not ok:
            job.warn(f"could not build {entry['image']}")
            return
        job.info(f"built {entry['image']}")
    job.emit(
        {
            "kind": "dependency",
            "dep": {"kind": "docker-image", "image": entry["image"]},
            "state": "installed",
        },
        queue=True,
    )


async def _docker_ready(job: Job) -> bool:
    delay = 1.0
    for _ in range(8):
        if await job.capture_cmd(
            "docker", ["/usr/bin/docker", "version"], False
        ):
            job.emit(
                {"kind": "abstract-dep", "name": "docker", "state": "ready"},
                queue=True,
            )
            return True
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
    return False


# ── --ovn-purge ───────────────────────────────────────────────────
# Reset OVS and/or OVN on THIS machine to a freshly-installed state: stop
# the daemons, delete the database files, start the daemons again so each
# one's own init recreates an empty, schema-only database.
#   ovs=True      OVS's conf.db — every bridge/port/interface/external-id
#                 of this chassis, including its OVN registration.
#   central=True  OVN's NB and SB databases — the logical topology of the
#                 ENTIRE cluster, every chassis, not just this host. The
#                 files live on the central, so this part only does
#                 anything when it runs there.
# What create set up outside the databases (netns, veths, systemd units,
# containers) is NOT touched — that is --action delete's job. Afterwards
# everything create registered is gone with the databases: run create
# again to bring the fabric back.
#
# The chassis's system-id is read first and written back afterwards. It IS
# the chassis's identity in the SB database (Chassis.name); left to
# regenerate, every gateway-chassis pin made against the old id would
# silently reference a chassis that no longer exists.
#
# A daemon that is not running (or not installed) must not stop the
# purge, so the stops only warn; a database file that cannot be removed
# or a daemon that does not come back aborts.
OVS_DB = "/var/lib/openvswitch/conf.db"
OVN_DBS = [
    "/var/lib/ovn/ovnnb_db.db",
    "/var/lib/ovn/ovnsb_db.db",
    "/var/lib/ovn/.ovnnb_db.db.~lock~",
    "/var/lib/ovn/.ovnsb_db.db.~lock~",
]
OVN_CENTRAL_DB_SERVICES = ["ovn-ovsdb-server-nb", "ovn-ovsdb-server-sb"]


async def ovn_purge(job: Job, label: str, ovs: bool, central: bool) -> None:
    system_id = ""
    if ovs:
        system_id = (
            await job.capture_cmd(
                label,
                [
                    "ovs-vsctl",
                    "--if-exists",
                    "get",
                    "open_vswitch",
                    ".",
                    "external_ids:system-id",
                ],
                False,
            )
        ).strip('"')
        await job.run_cmd(label, ["systemctl", "stop", "ovn-controller"], False)
    if central:
        await job.run_cmd(
            label,
            ["systemctl", "stop", "ovn-northd", *OVN_CENTRAL_DB_SERVICES],
            False,
        )
    if ovs:
        await job.run_cmd(
            label, ["systemctl", "stop", "ovs-vswitchd", "ovsdb-server"], False
        )
        await job.run_cmd(label, ["rm", "-f", OVS_DB], True)
    if central:
        await job.run_cmd(label, ["rm", "-f", *OVN_DBS], True)
    if ovs:
        await job.run_cmd(
            label, ["systemctl", "start", "ovsdb-server", "ovs-vswitchd"], True
        )
        if system_id:
            setting = f"external_ids:system-id={system_id}"
            await job.run_cmd(
                label,
                ["ovs-vsctl", "set", "open_vswitch", ".", setting],
                True,
            )
            await write_file(
                job, label, "/etc/openvswitch/system-id.conf", system_id, True
            )
    if central:
        await job.run_cmd(
            label,
            ["systemctl", "start", *OVN_CENTRAL_DB_SERVICES, "ovn-northd"],
            True,
        )
    if ovs:
        await job.run_cmd(label, ["systemctl", "start", "ovn-controller"], True)


__CLUSTER_FUNCS__


__HOST_FUNCS__

HOSTS = {
__HOSTS__
}

SERVICES = __SERVICES__

BIDIRECTIONAL_DEPS = {
__BIDIRECTIONAL_DEPS__
}


def concrete_deps(kind: str, host: str | None = None) -> list[dict]:
    # Unique concrete dependencies of one kind from BIDIRECTIONAL_DEPS.
    # kind: "apt-dep" | "sys-cmd-dep" | "docker-image".
    hosts = [host] if host is not None else list(BIDIRECTIONAL_DEPS)
    seen: set[tuple] = set()
    out: list[dict] = []
    for host_name in hosts:
        for entry in BIDIRECTIONAL_DEPS.get(host_name, {}).values():
            for dep in entry["concrete"]:
                if dep["kind"] != kind:
                    continue
                key = (
                    dep["kind"],
                    dep.get("package"),
                    dep.get("name"),
                    dep.get("image"),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(dep)
    return out


def apt_deps(host: str | None = None) -> list[dict]:
    return concrete_deps("apt-dep", host)


def sys_cmd_deps(host: str | None = None) -> list[dict]:
    return concrete_deps("sys-cmd-dep", host)


def docker_image_deps(host: str | None = None) -> list[dict]:
    return concrete_deps("docker-image", host)


_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
_deploy_log = logging.getLogger("ovn-fabric.deployer")


async def _handle_log(job: Job, event: object) -> None:
    # The ONE central log sink: every job's `log()`/`warn()`/... bubbles
    # up here with the job name already attached.
    if not isinstance(event, dict) or event.get("kind") != "log":
        return
    level = _LOG_LEVELS.get(event.get("level"), logging.INFO)
    _deploy_log.log(level, "%s: %s", event.get("name", "?"), event.get("message", ""))


async def install_zerotier(job: Job) -> None:
    # Custom installer, separate from the apt/sys-cmd system-deps job.
    # The job is gated on the concrete curl dependency; only when that is
    # ready does it run the vendor installer and announce the abstract
    # zerotier dependency.
    await job.run_cmd(
        "install-zerotier",
        ["sh", "-c", "curl -s https://install.zerotier.com | bash"],
        True,
        600,
    )
    job.emit(
        {"kind": "abstract-dep", "name": "zerotier", "state": "installed"},
        queue=True,
    )


def _resolve_dependency_job(
    root: Job,
    host: str,
    image_deps: list[dict],
    repeat: bool,
    with_images: bool = True,
    custom_installers: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    waiters = package_waiters(host)
    packages = root.job("install-packages", initiate=True, ctx=waiters)

    async def packages_start(job: Job) -> None:
        async def translate(job: Job, event: object, ctx: object = None) -> None:
            waiters_ctx = ctx if isinstance(ctx, dict) else {}
            for abstract, waiter in waiters_ctx.items():
                async def emit(name: str = abstract) -> None:
                    job.emit(
                        {"kind": "abstract-dep", "name": name, "state": "installed"},
                        queue=True,
                    )

                await waiter.waitfor(event, emit)

        job.on_emit(translate)
        while True:
            await _install_packages(job, BIDIRECTIONAL_DEPS[host])
            if not repeat:
                break
            await asyncio.sleep(INSTALL_INTERVAL)

    packages.on_start(packages_start)

    for name, dependencies in (custom_installers or {}).items():
        installer = root.job(f"install-{name}", initiate=True)
        if name == "zerotier":
            register(installer, dependencies, install_zerotier)

    if not with_images:
        return

    # One STATIC job per docker-image, each gated on the docker abstract
    # dependency. Each emits its own queued docker-image ready event.
    images = [
        entry["concrete"][0]
        for entry in BIDIRECTIONAL_DEPS.get(host, {}).values()
        if entry["abstract"].get("kind") == "docker-image"
    ]
    for entry in images:
        # Name the job after the image itself; the BIDIRECTIONAL filter
        # already gives the complete, unique list.
        image_job = root.job(f"image:{entry['image']}", initiate=True)

        async def install_image(job: Job, image: dict = entry) -> None:
            await _install_image(job, image)

        register(image_job, image_deps, install_image)


def _run_common(
    root: Job,
    host: str,
    image_deps: list[dict],
    configure_deps: list[dict],
    custom_installers: dict[str, list[dict]],
) -> Job:
    root.on_emit(_handle_log)
    _resolve_dependency_job(
        root, host, image_deps, repeat=True, custom_installers=custom_installers
    )

    async def configure(job: Job) -> None:
        await CONFIGURE[host](job)
        job.emit(
            {"kind": "abstract-dep", "name": "configure", "state": "ready"},
            queue=True,
        )

    configure_job = root.job("configure", initiate=True)
    register(configure_job, configure_deps, configure)
    return configure_job


def _action_jobs(
    root: Job,
    host: str,
    image_deps: list[dict],
    dependencies: list[dict],
    name: str,
    action: Any,
) -> Job:
    root.on_emit(_handle_log)
    _resolve_dependency_job(root, host, image_deps, repeat=False)
    job = root.job(name, initiate=True)
    register(job, dependencies, action)
    return job


def _python_job(
    parent: Job, name: str, dependencies: list[str], up: object, down: object
) -> Job:
    # One kernel router/service job, gated on its IR-derived deps (which
    # always include configure). DepWait is the generated deployer's.
    job = parent.job(name, initiate=True)
    register(job, dependencies, up, keep_alive=True)
    job.on_stop(down)
    return job



__JOB_FUNCS__

JOB_BODIES = {
__JOB_BODIES__
}

CONFIGURE = {
__CONFIGURE__
}

__TREE_FUNCS__

__CREATE_FUNCS__

__DELETE_FUNCS__

RUN_JOBS = {
__TREES__
}

CREATE_JOBS = {
__CREATE_JOBS__
}

DELETE_JOBS = {
__DELETE_JOBS__
}


async def _run_host(host: str) -> int:
    root = RUN_JOBS[host]()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    running = asyncio.create_task(root.run())
    stopped = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({running, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if stopped in done:
        await root.stop()
    else:
        stopped.cancel()
    await running
    return 0


DEPENDENCY_TIMEOUT = 180


async def _run_job_set(factory: Any, label: str) -> None:
    root = factory()
    if DEPENDENCY_TIMEOUT <= 0:
        await root.run()
        return
    try:
        await asyncio.wait_for(root.run(), timeout=DEPENDENCY_TIMEOUT)
    except asyncio.TimeoutError:
        await root.stop()
        print(
            f"{RED}error: {label} did not complete within "
            f"{DEPENDENCY_TIMEOUT}s; dependencies could not be installed "
            f"(missing packages or no network){RESET}",
            file=sys.stderr,
        )
        raise SystemExit(1)


async def purge(host, cluster_only: bool) -> int:
    async def run_one(name: str) -> None:
        root = Job(f"ovn-fabric(purge:{name})")
        _resolve_dependency_job(root, name, [], repeat=False, with_images=False)
        job = root.job("ovn-fabric(purge)", initiate=True)

        async def action(j: Job) -> None:
            await _purge(j, name, cluster_only)

        register(job, [{"kind": "abstract", "name": "ovn"}], action)
        await _run_job_set(lambda: root, f"purge on {name}")

    if cluster_only:
        central = next(
            (name for name, data in HOSTS.items() if data["role"] == "central"),
            None,
        )
        if central is None:
            print(f"{RED}error: no central host for --cluster{RESET}", file=sys.stderr)
            return 2
        await run_one(central)
        return 0
    if host is not None:
        if host not in HOSTS:
            print(
                f"{RED}error: unknown host {host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        if HOSTS[host]["role"] is None:
            print(
                f"{YELLOW}host:{host} is not part of the OVN cluster — nothing to purge{RESET}"
            )
            return 0
        await run_one(host)
        return 0
    for name in HOSTS:
        if HOSTS[name]["role"] is None:
            print(
                f"{YELLOW}host:{name} is not part of the OVN cluster — nothing to purge{RESET}"
            )
            continue
        await run_one(name)
    return 0


async def _purge(job: Job, host, cluster_only: bool) -> int:
    if cluster_only:
        await ovn_purge(job, "cluster (purge)", ovs=False, central=True)
        return 0
    if host is not None and host not in HOSTS:
        print(
            f"{RED}error: unknown host {host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
            file=sys.stderr,
        )
        return 2
    for name in HOSTS if host is None else [host]:
        role = HOSTS[name]["role"]
        if role is None:
            print(
                f"{YELLOW}host:{name} is not part of the OVN cluster — nothing to purge{RESET}"
            )
            continue
        await ovn_purge(job, f"host:{name} (purge)", ovs=True, central=role == "central")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ovn-fabric generated deployer")
    parser.add_argument(
        "--action",
        choices=["create", "delete", "run"],
        help="create (default) aborts on the first failed command; delete warns and continues",
    )
    parser.add_argument(
        "--cluster",
        action="store_true",
        help="run only the cluster-wide ovn-nbctl pass",
    )
    parser.add_argument("--host", metavar="NAME", help="run only this host's pass")
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        metavar="SECONDS",
        help="dependency/action timeout in seconds (0 disables it; default 180)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="alias for --action run (the generated service uses --action run)",
    )
    parser.add_argument(
        "--job",
        metavar="NAME",
        help="run one generated kernel router/service body (the systemd units call this)",
    )
    parser.add_argument(
        "--job-action",
        choices=["up", "down"],
        help="which body of --job to run",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="echo every command before running it",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="write and enable the fabric's units but do not start them now (systemd starts "
        "them after this pass; ovn-fabric-restore.service uses it at boot)",
    )
    parser.add_argument(
        "--ovn-purge",
        action="store_true",
        help="INSTEAD of an action: wipe the OVS database and, on the central, OVN's NB/SB "
        "databases, and restart the daemons (a freshly-installed state). With --host only "
        "that host's part; with --cluster only the NB/SB databases (run it on the central)",
    )
    args = parser.parse_args()
    global DEPENDENCY_TIMEOUT
    DEPENDENCY_TIMEOUT = args.timeout
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        # Verbose: timestamp every record, at every level. Normal: keep
        # the compact level/message form.
        format=(
            "%(asctime)s.%(msecs)03d %(levelname)s %(message)s"
            if args.verbose
            else "%(levelname)s %(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if args.job is not None:
        if args.job_action is None:
            parser.error("--job needs --job-action")
        if args.job not in JOB_BODIES:
            print(
                f"{RED}error: unknown job {args.job!r}; known jobs: {', '.join(JOB_BODIES)}{RESET}",
                file=sys.stderr,
            )
            return 2
        up, down = JOB_BODIES[args.job]

        async def run_job() -> None:
            job = Job(args.job, logger_factory=_job_logger)
            action = up if args.job_action == "up" else down

            async def start(h: Job) -> None:
                await action(h)

            job.on_start(start)
            # start() only schedules; run() observes the started set.
            await job.run()

        asyncio.run(run_job())
        return 0
    if args.job_action is not None:
        parser.error("--job-action needs --job")
    if args.action == "run" or args.run:
        if args.host is None:
            parser.error("--action run needs --host")
        if args.cluster or args.ovn_purge:
            parser.error("--action run replaces the other actions")
        if args.host not in RUN_JOBS:
            print(
                f"{RED}error: unknown host {args.host!r}; "
                f"known hosts: {', '.join(RUN_JOBS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        return asyncio.run(_run_host(args.host))
    if args.ovn_purge:
        # A purge is its own run, never a silent prelude or epilogue to
        # create/delete: say so instead of ignoring an --action.
        if args.action is not None:
            parser.error("--ovn-purge replaces the action; run --action as a separate step")
        return asyncio.run(purge(args.host, args.cluster))
    args.action = args.action or "create"
    abort_on_error = args.action == "create"

    if args.host is not None:
        if args.host not in HOSTS:
            print(
                f"{RED}error: unknown host {args.host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        jobs = CREATE_JOBS if args.action == "create" else DELETE_JOBS
        asyncio.run(
            _run_job_set(jobs[args.host], f"{args.action} on {args.host}")
        )
    elif args.cluster:
        cluster = cluster_create if args.action == "create" else cluster_delete

        async def run_cluster() -> None:
            job = Job("ovn-fabric", logger_factory=_job_logger)
            await job.start()
            try:
                await cluster(job, abort_on_error)
            finally:
                await job.stop()

        asyncio.run(run_cluster())
    elif args.action == "create":
        async def run_create() -> None:
            for name in CREATE_JOBS:
                await _run_job_set(CREATE_JOBS[name], f"create on {name}")

        asyncio.run(run_create())
    else:
        async def run_delete() -> None:
            order = sorted(
                DELETE_JOBS, key=lambda name: HOSTS[name]["role"] != "central"
            )
            for name in order:
                await _run_job_set(DELETE_JOBS[name], f"delete on {name}")

        asyncio.run(run_delete())
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def _python_fn(
    name: str,
    label: str,
    action: Action,
    body: Callable[[Emitter], None],
    extra_params: str = "",
) -> str:
    """One generated function: run `body(emit)` through a PythonEmitter
    carrying `action` (the ONE create/delete parameter) and render its
    lines as an indented function body. `extra_params` appends parameters
    after `abort_on_error` — the per-host functions take `no_start` (see
    --no-start); the cluster pass takes none."""
    emit = PythonEmitter(label, action)
    body(emit)
    body_lines = [f"    {line}" if line else "" for line in emit.lines]
    return f"async def {name}(job, abort_on_error{extra_params}):\n" + "\n".join(body_lines) + "\n"


def _host_fn_name(host_name: str, action: str) -> str:
    return f"_host_{re.sub(r'[^A-Za-z0-9]', '_', host_name)}_{action}"


def _run_fn_name(host_name: str) -> str:
    return f"_run_{re.sub(r'[^A-Za-z0-9]', '_', host_name)}"


def _runtime_code() -> str:
    """The runtime library, inlined into the generated single file.

    The deployer is copied to hosts that do not have the ovn-fabric
    package, so `runtime/service.py` is embedded — not imported. Its
    module docstring and `from __future__` are dropped (the latter must
    be the first statement of a module).
    """
    path = pathlib.Path(__file__).resolve().parents[1] / "runtime" / "service.py"
    source = path.read_text(encoding="utf-8")
    if source.startswith('"""'):
        end = source.index('"""', 3) + 3
        source = source[end:]
    source = source.replace("from __future__ import annotations\n", "", 1)
    return source.strip("\n")


def _service_manifest(nodes: list[pt.Model]) -> dict[str, list[dict[str, str]]]:
    """The kernel routers/workloads each host owns, as the generated
    tree's `SERVICES` manifest (logical name, kind)."""
    out: dict[str, list[dict[str, str]]] = {}
    for node in nodes:
        if node.kind != "infra.host":
            continue
        entries: list[dict[str, str]] = []
        for owner in _kernel_router_owners(node.id, nodes):
            name = owner.key.name
            entries.append(
                {
                    "name": name,
                    "kind": "router",
                }
            )
        for n in nodes:
            if n.kind in ("kernel.app.container", "kernel.app.service") and n.data.host == node.id:
                name = n.key.name
                entries.append(
                    {
                        "name": name,
                        "kind": "service",
                    }
                )
        out[node.key.host] = entries
    return out


def generate_python_deployer(nodes: list[pt.Model]) -> str:
    """The all-in-one Python deployer: every action of every pass, as
    native Python functions over the shared emitters (2026-08-19,
    refactored 2026-08-23). Kernel routers and services are generated BODY
    functions (run_cmd/write_file calls), not shell scripts: their systemd
    units exec this file's `--job NAME --job-action up|down`."""
    service_manifest = _service_manifest(nodes)
    install_plans = {
        node.key.host: _install_plan(node, nodes) for node in nodes if node.kind == "infra.host"
    }
    bidirectional: dict[str, dict[str, dict[str, Any]]] = {}
    for host, plan in install_plans.items():
        per_host: dict[str, dict[str, Any]] = {}
        for name, resolved in plan.get("resolved", {}).items():
            if resolved["kind"] == "apt":
                concrete = [
                    {"kind": "apt-dep", "package": package} for package in resolved["packages"]
                ]
            else:
                concrete = []
                for required in resolved.get("requires", []):
                    required_resolved = plan.get("resolved", {}).get(required)
                    if required_resolved and required_resolved["kind"] == "apt":
                        concrete.extend(
                            {
                                "kind": "apt-dep",
                                "package": package,
                                "order": 0,
                            }
                            for package in required_resolved["packages"]
                        )
                concrete.append(
                    {
                        "kind": "sys-cmd-dep",
                        "name": name,
                        "cmd": resolved["cmd"],
                        "order": 10,
                    }
                )
            per_host[name] = {
                "abstract": {"kind": "abstract", "name": name},
                "concrete": concrete,
            }
        for entry in plan["jobs"]:
            if entry["kind"] != "image":
                continue
            descriptor = {
                "kind": "docker-image",
                "image": entry["image"],
                "dockerfile": entry["dockerfile"],
                "order": 20,
            }
            per_host[f"docker-image:{entry['image']}"] = {
                "abstract": {"kind": "docker-image", "image": entry["image"]},
                "concrete": [descriptor],
            }
        bidirectional[host] = per_host

    host_defs: list[str] = []
    host_entries: list[str] = []
    job_funcs: list[str] = []
    job_entries: list[str] = []
    configure_entries: list[str] = []
    tree_defs: list[str] = []
    tree_entries: list[str] = []
    create_defs: list[str] = []
    create_entries: list[str] = []
    delete_defs: list[str] = []
    delete_entries: list[str] = []
    for node in nodes:
        if node.kind != "infra.host":
            continue
        host_name = node.key.host
        for action in ("create", "delete"):
            fn = _host_fn_name(host_name, action)
            host_defs.append(
                _python_fn(
                    fn,
                    f"host:{host_name} ({action})",
                    action,
                    functools.partial(_emit_host_script, node, nodes),
                    extra_params=", no_start",
                )
            )
        role = node.data.ovnRole.value if node.data.ovnRole is not None else None
        host_entries.append(
            f"    {host_name!r}: {{\n"
            f'        "create": {_host_fn_name(host_name, "create")},\n'
            f'        "delete": {_host_fn_name(host_name, "delete")},\n'
            f'        "role": {role!r},\n'
            "    },"
        )

        configure_fn = _body_fn_name(host_name, "configure", "up")
        job_funcs.append(
            _python_body_fn(
                configure_fn,
                f"{host_name}:configure",
                "create",
                functools.partial(_emit_host_configure, node, nodes),
            )
        )
        configure_entries.append(f"    {host_name!r}: {configure_fn},")
        # The service's ExecStart/ExecStop run this job: `up` drives the
        # tree (and blocks until SIGTERM), `down` is a no-op because
        # systemd's SIGTERM has already made the tree run its down bodies.
        tree_job = f"ovn-fabric-{host_name}"
        up_fn = _body_fn_name(host_name, "run", "up")
        down_fn = _body_fn_name(host_name, "run", "down")
        job_funcs.append(
            f"async def {up_fn}(job: Job) -> None:\n" f"    await _run_host({host_name!r})\n"
        )
        down_all = _body_fn_name(host_name, "all", "down")
        job_funcs.append(f"async def {down_fn}(job: Job) -> None:\n" f"    await {down_all}(job)\n")
        job_entries.append(f"    {tree_job!r}: ({up_fn}, {down_fn}),")

        bodies: dict[str, tuple[str, str]] = {}
        for owner in _kernel_router_owners(node.id, nodes):
            router = owner.key.name
            netns = _netns_name(router)
            functions: list[str] = []
            for body_action, label in (("create", "up"), ("delete", "down")):
                fn = _body_fn_name(host_name, router, label)
                job_funcs.append(
                    _python_body_fn(
                        fn,
                        f"{host_name}:{router} ({label})",
                        body_action,
                        functools.partial(
                            _emit_kernel_router_owner, owner, netns, nodes, body_action
                        ),
                    )
                )
                functions.append(fn)
            bodies[router] = (functions[0], functions[1])
        for n in nodes:
            if n.kind not in ("kernel.app.container", "kernel.app.service"):
                continue
            if n.data.host != node.id:
                continue
            name = n.key.name
            faces = _service_faces(name, host_name, nodes)
            functions = []
            for body_action, label in (("create", "up"), ("delete", "down")):
                fn = _body_fn_name(host_name, name, label)
                emit_fn = _emit_service_up if label == "up" else _emit_service_down
                job_funcs.append(
                    _python_body_fn(
                        fn,
                        f"{host_name}:{name} ({label})",
                        body_action,
                        functools.partial(emit_fn, n, faces),
                    )
                )
                functions.append(fn)
            bodies[name] = (functions[0], functions[1])

        down_calls = [f"    await {down}(job)" for _up, down in reversed(list(bodies.values()))]
        job_funcs.append(
            f"async def {down_all}(job: Job) -> None:\n"
            + ("\n".join(down_calls) if down_calls else "    return")
            + "\n"
        )

        plan = install_plans[host_name]
        image_deps = (
            [{"kind": "abstract", "name": "docker"}]
            if any(entry["kind"] == "image" for entry in plan["jobs"])
            else []
        )
        custom_installers: dict[str, list[dict[str, str]]] = {}
        if "zerotier" in (node.data.dependencies or []):
            custom_installers["zerotier"] = [{"kind": "apt-dep", "package": "curl"}]
        configure_deps = _configure_dependencies(node, nodes)
        job_deps = _job_dependencies(node, nodes)
        services = service_manifest[host_name]
        tree_fn = _run_fn_name(host_name)
        tree_lines = [
            f"def {tree_fn}() -> Job:",
            "    root = Job('ovn-fabric', logger_factory=_job_logger)",
            f"    configure = _run_common(root, {host_name!r}, "
            f"{image_deps!r}, {configure_deps!r}, {custom_installers!r})",
        ]
        for svc in services:
            up, down = bodies[svc["name"]]
            deps = job_deps.get(svc["name"], ["configure"])
            tree_lines.append(
                f"    _python_job(configure, {svc['name']!r}, {deps!r}, {up}, {down})"
            )
        tree_lines.append("    return root")
        tree_defs.append("\n".join(tree_lines))
        tree_entries.append(f"    {host_name!r}: {tree_fn},")

        all_deps = [
            entry["abstract"]
            for entry in bidirectional[host_name].values()
            if entry["abstract"].get("kind") == "abstract"
        ]
        create_deps = configure_deps
        cluster = node.data.ovnRole == pt.OvnRole.central
        create_fn = _run_fn_name(host_name).replace("_run_", "_create_")
        create_lines = [
            f"def {create_fn}() -> Job:",
            "    root = Job('ovn-fabric', logger_factory=_job_logger)",
            f"    _resolve_dependency_job(root, {host_name!r}, [], "
            f"repeat=False, with_images=False, "
            f"custom_installers={custom_installers!r})",
            "    async def create_action(job: Job) -> None:",
            f"        await HOSTS[{host_name!r}]['create'](job, True, False)",
            f"        await CONFIGURE[{host_name!r}](job)",
            "        job.emit({'kind': 'abstract-dep', 'name': 'create-ready', "
            "'state': 'ready'}, queue=True)",
            "    create_job = root.job('ovn-fabric(create)', initiate=True)",
            f"    register(create_job, {create_deps!r}, create_action)",
        ]
        if cluster:
            create_lines += [
                "    async def cluster_action(job: Job) -> None:",
                "        await cluster_create(job, True)",
                "    cluster_job = root.job('cluster-create', initiate=True)",
                f"    register(cluster_job, "
                f"[{_abstract('ovn')!r}, {_abstract('create-ready')!r}], cluster_action)",
            ]
        create_lines.append("    return root")
        create_defs.append("\n".join(create_lines))
        create_entries.append(f"    {host_name!r}: {create_fn},")

        delete_fn = _run_fn_name(host_name).replace("_run_", "_delete_")
        delete_lines = [
            f"def {delete_fn}() -> Job:",
            "    root = Job('ovn-fabric', logger_factory=_job_logger)",
            f"    _resolve_dependency_job(root, {host_name!r}, [], "
            f"repeat=False, with_images=False, "
            f"custom_installers={custom_installers!r})",
        ]
        delete_deps = list(all_deps)
        if cluster:
            delete_lines += [
                "    async def cluster_action(job: Job) -> None:",
                "        await cluster_delete(job, False)",
                "        job.emit({'kind': 'abstract-dep', "
                "'name': 'cluster-deleted', 'state': 'ready'}, queue=True)",
                "    cluster_job = root.job('cluster-delete', initiate=True)",
                f"    register(cluster_job, [{_abstract('ovn')!r}], cluster_action)",
            ]
            delete_deps.append({"kind": "abstract", "name": "cluster-deleted"})
        down_calls = [f"        await {down}(job)" for _up, down in reversed(list(bodies.values()))]
        delete_lines += [
            "    async def delete_action(job: Job) -> None:",
            f"        await HOSTS[{host_name!r}]['delete'](job, False, False)",
            *down_calls,
            "    delete_job = root.job('ovn-fabric(delete)', initiate=True)",
            f"    register(delete_job, {delete_deps!r}, delete_action)",
            "    return root",
        ]
        delete_defs.append("\n".join(delete_lines))
        delete_entries.append(f"    {host_name!r}: {delete_fn},")
        for name, (up, down) in bodies.items():
            job_entries.append(f"    {name!r}: ({up}, {down}),")

    cluster_defs = "\n\n".join(
        _python_fn(
            f"cluster_{action}",
            f"cluster ({action})",
            action,
            lambda emit: _emit_cluster_body(nodes, emit),
        )
        for action in ("create", "delete")
    )

    return (
        _GENERATED_RUNTIME.replace("__CLUSTER_FUNCS__", cluster_defs)
        .replace("__HOST_FUNCS__", "\n\n".join(host_defs))
        .replace("__HOSTS__", "\n".join(host_entries))
        .replace("__SERVICES__", repr(service_manifest))
        .replace(
            "__INSTALL__",
            "\n".join(f"    {host!r}: {plan!r}," for host, plan in install_plans.items()),
        )
        .replace(
            "__BIDIRECTIONAL_DEPS__",
            "\n".join(f"    {host!r}: {per_host!r}," for host, per_host in bidirectional.items()),
        )
        .replace("__JOB_FUNCS__", "\n\n".join(job_funcs))
        .replace("__JOB_BODIES__", "\n".join(job_entries))
        .replace("__CONFIGURE__", "\n".join(configure_entries))
        .replace("__TREE_FUNCS__", "\n\n".join(tree_defs))
        .replace("__TREES__", "\n".join(tree_entries))
        .replace("__CREATE_FUNCS__", "\n\n".join(create_defs))
        .replace("__DELETE_FUNCS__", "\n\n".join(delete_defs))
        .replace("__CREATE_JOBS__", "\n".join(create_entries))
        .replace("__DELETE_JOBS__", "\n".join(delete_entries))
        .replace("# __RUNTIME_LIB__", _runtime_code())
    )
