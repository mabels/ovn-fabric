# deployer/test_install_queue.py — stdlib unittest for the install-queue
# consumer (deployer/install_queue.py), the file a host pass installs as
# /usr/local/sbin/ovn-fabric-install.
#
# The queue is a REAL directory (a temp one, with real files named the way
# the generated deployer names them); the HOST is faked: which packages
# and images it has, what state its units are in, what it can reach, and
# its clock. Nothing is installed, pulled, restarted or connected to —
# except RunTest, which runs `true`/`false`, and OnlineTest, which asks
# resolvers of its own on 127.0.0.1.

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest import mock

from . import install_queue as iq


def package(name: str, *packages: str) -> iq.PackageJob:
    wanted = list(packages) or [name]
    return {
        "kind": "package",
        "name": name,
        "packages": wanted,
        "install": ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", *wanted],
        "apt": True,
    }


def image(name: str, dockerfile: str | None = None) -> iq.ImageJob:
    return {"kind": "image", "image": name, "dockerfile": dockerfile}


def unit(name: str) -> iq.UnitJob:
    return {"kind": "unit", "unit": name}


class FakeClock:
    """install_queue's `time`: sleeping only moves the clock."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return time.time() + self.elapsed

    def monotonic(self) -> float:
        return 5000.0 + self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds


class FakeHost:
    """What the consumer asks a host and does to it."""

    def __init__(self) -> None:
        self.packages: set[str] = set()
        self.images: set[str] = set()
        self.units: dict[str, str] = {}
        self.is_online = False
        # How often the consumer asked whether the host is online.
        self.asked = 0
        # Installs/pulls/restarts that fail: the package, image or unit.
        self.broken: set[str] = set()
        self.ran: list[str] = []
        self.dockerfiles: dict[str, str] = {}

    def quiet(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        if argv[0] == "dpkg-query":
            installed = argv[-1] in self.packages
            return subprocess.CompletedProcess(
                argv, 0 if installed else 1, "install ok installed" if installed else "", ""
            )
        if argv[:3] == ["docker", "image", "inspect"]:
            if "docker.io" not in self.packages:
                return None  # no docker binary on this host
            return subprocess.CompletedProcess(argv, 0 if argv[3] in self.images else 1, "", "")
        if argv[:2] == ["systemctl", "is-active"]:
            state = self.units.get(argv[2], "inactive")
            return subprocess.CompletedProcess(
                argv, 0 if state == "active" else 3, state + "\n", ""
            )
        raise AssertionError(f"unexpected query: {argv}")

    def run(self, argv: list[str]) -> bool:
        self.ran.append(" ".join(argv))
        if argv == ["apt-get", "update"]:
            return True
        if "apt-get" in argv:
            wanted = argv[argv.index("-y") + 1 :]
            if self.broken.intersection(wanted):
                return False
            self.packages.update(wanted)
            return True
        if argv[:2] == ["docker", "pull"]:
            if argv[2] in self.broken:
                return False
            self.images.add(argv[2])
            return True
        if argv[:3] == ["docker", "build", "-t"]:
            with open(os.path.join(argv[4], "Dockerfile"), encoding="utf-8") as handle:
                self.dockerfiles[argv[3]] = handle.read()
            self.images.add(argv[3])
            return True
        if argv[:2] == ["systemctl", "restart"]:
            ok = argv[2] not in self.broken
            self.units[argv[2]] = "active" if ok else "failed"
            return ok
        raise AssertionError(f"unexpected command: {argv}")

    def online(self) -> bool:
        self.asked += 1
        return self.is_online


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.host = FakeHost()
        # docker itself is there unless a test takes it away.
        self.host.packages.add("docker.io")
        self.clock = FakeClock()
        self.out = io.StringIO()
        self._queued = 0
        for name, fake in (
            ("QUEUE_DIR", self.dir),
            ("quiet", self.host.quiet),
            ("run", self.host.run),
            ("online", self.host.online),
            ("time", self.clock),
        ):
            patcher = mock.patch.object(iq, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        redirect = contextlib.redirect_stdout(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def queue(self, job: iq.Job) -> str:
        """Queue `job` the way the generated deployer does: a file named
        after the moment it is queued (here: one microsecond after the
        one before it)."""
        self._queued += 1
        label = iq.describe(job).replace(" ", "-").replace("/", "_")
        path = os.path.join(self.dir, f"20261011T195501.{self._queued:06d}Z-{label}.json")
        with open(path, "w", encoding="utf-8") as out:
            json.dump(job, out)
        return path

    def left(self) -> list[str]:
        """What is still queued, by description, in queue order."""
        entries, _ = iq.read_queue()
        return [iq.describe(entry["job"]) for entry in entries]

    def online(self) -> None:
        self.host.is_online = True


class ParseJobTest(unittest.TestCase):
    def test_the_three_kinds_of_job(self) -> None:
        jobs: list[iq.Job] = [
            package("ovn", "ovn-central", "ovn-host"),
            image("coredns/coredns:latest"),
            image("ovn-fabric-dns", "FROM alpine:latest"),
            unit("ovn-kernel-x.service"),
        ]
        for job in jobs:
            self.assertEqual(iq.parse_job(json.loads(json.dumps(job))), job)

    def test_anything_else_is_not_a_job(self) -> None:
        # A file that is not what the generator writes is left alone, not
        # half-understood — least of all its `install` command.
        broken: list[Any] = [
            None,
            [],
            "package",
            {},
            {"kind": "script", "run": "rm -rf /"},
            {**package("ovs"), "install": []},
            {**package("ovs"), "install": "apt-get install -y ovs"},
            {**package("ovs"), "packages": [1]},
            {**package("ovs"), "apt": "yes"},
            {**image("ubuntu"), "dockerfile": 3},
            {"kind": "image", "image": "ubuntu"},
            {"kind": "image", "dockerfile": None},
            {"kind": "unit"},
            {"kind": "unit", "unit": ["a.service"]},
        ]
        for raw in broken:
            self.assertIsNone(iq.parse_job(raw), raw)


class ReadQueueTest(QueueCase):
    def test_jobs_come_in_the_order_they_were_queued(self) -> None:
        # The name starts with the time the job was queued — so name order
        # is queue order, whatever the rest of the name says.
        self.queue(unit("z.service"))
        self.queue(package("ovs"))
        self.queue(image("alpine"))
        self.assertEqual(self.left(), ["unit z.service", "package ovs", "image alpine"])

    def test_a_missing_or_empty_queue_is_no_jobs(self) -> None:
        self.assertEqual(iq.read_queue(), ([], []))
        with mock.patch.object(iq, "QUEUE_DIR", os.path.join(self.dir, "nope")):
            self.assertEqual(iq.read_queue(), ([], []))

    def test_only_json_files_are_read(self) -> None:
        # The deployer writes a job under another name and renames it.
        with open(os.path.join(self.dir, ".unit-x.json.tmp"), "w", encoding="utf-8") as out:
            out.write('{"kind": "unit", "un')
        self.clock.elapsed = 60
        self.assertEqual(iq.read_queue(), ([], []))

    def test_a_file_that_is_not_a_job_is_reported_once_it_has_settled(self) -> None:
        path = os.path.join(self.dir, "20261011T195501.000001Z-unit-x.json")
        with open(path, "w", encoding="utf-8") as out:
            out.write('{"kind": "unit", "un')
        # Just written: someone may still be writing it.
        self.assertEqual(iq.read_queue(), ([], []))
        self.clock.elapsed = iq.SETTLE_SECONDS + 1
        self.assertEqual(iq.read_queue(), ([], [path]))
        # It is left where it is, and does not stop the jobs around it.
        self.queue(unit("y.service"))
        self.host.units["y.service"] = "failed"
        result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": [], "stuck": [path]})
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.host.units["y.service"], "active")


class WorkTest(QueueCase):
    def test_a_job_that_is_already_done_is_removed_without_a_connection(self) -> None:
        self.host.packages.add("openvswitch-switch")
        self.host.images.add("alpine")
        self.host.units["x.service"] = "active"
        self.queue(package("ovs", "openvswitch-switch"))
        self.queue(image("alpine"))
        self.queue(unit("x.service"))
        result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": [], "stuck": []})
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(self.host.ran, [])
        self.assertEqual(self.host.asked, 0)

    def test_online_the_whole_queue_is_done_in_one_pass_in_order(self) -> None:
        self.online()
        self.host.units["x.service"] = "failed"
        self.queue(package("curl"))
        self.queue(package("ovn", "ovn-central", "ovn-host"))
        self.queue(image("coredns/coredns:latest"))
        self.queue(unit("x.service"))
        result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": [], "stuck": []})
        apt = "env DEBIAN_FRONTEND=noninteractive apt-get install -y "
        self.assertEqual(
            self.host.ran,
            [
                # The package lists are refreshed once, not per package.
                "apt-get update",
                apt + "curl",
                apt + "ovn-central ovn-host",
                "docker pull coredns/coredns:latest",
                "systemctl restart x.service",
            ],
        )
        # Whether the host is online is asked once per pass, not per job.
        self.assertEqual(self.host.asked, 1)
        self.assertEqual(os.listdir(self.dir), [])
        self.assertIn("ovn-fabric-install: done: package ovn\n", self.out.getvalue())

    def test_a_package_is_done_only_when_all_its_packages_are(self) -> None:
        self.online()
        self.host.packages.add("ovn-host")
        self.queue(package("ovn", "ovn-central", "ovn-host"))
        iq.work({}, units=True)
        self.assertIn("ovn-central", self.host.packages)

    def test_an_install_that_is_not_apt_does_not_refresh_the_package_lists(self) -> None:
        self.online()
        job: iq.PackageJob = {
            "kind": "package",
            "name": "zerotier",
            "packages": ["zerotier-one"],
            "install": ["sh", "-c", "curl -s https://install.zerotier.com | bash"],
            "apt": False,
        }
        self.queue(job)
        with mock.patch.object(iq, "run", return_value=True) as run:
            iq.work({}, units=True)
        run.assert_called_once_with(job["install"])

    def test_an_image_with_a_dockerfile_is_built_from_it(self) -> None:
        self.online()
        self.queue(image("ovn-fabric-dns", "FROM alpine:latest\nRUN apk add --no-cache dnsmasq"))
        self.assertEqual(iq.work({}, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(
            self.host.dockerfiles,
            {"ovn-fabric-dns": "FROM alpine:latest\nRUN apk add --no-cache dnsmasq\n"},
        )
        self.assertFalse(any("pull" in line for line in self.host.ran))

    def test_offline_nothing_is_tried_and_everything_waits(self) -> None:
        self.host.units["x.service"] = "failed"
        self.queue(package("ovs"))
        self.queue(image("alpine"))
        self.queue(unit("x.service"))
        tried: dict[str, iq.Attempt] = {}
        for _ in range(20):
            result = iq.work(tried, units=True)
            self.clock.sleep(iq.POLL_SECONDS)
        self.assertEqual(
            result, {"waiting": ["package ovs", "image alpine", "unit x.service"], "stuck": []}
        )
        self.assertEqual(self.host.ran, [])
        # Waiting for the internet uses up no tries...
        self.assertEqual({attempt["count"] for attempt in tried.values()}, {0})
        # ...so it all still happens when the internet is back.
        self.online()
        self.assertEqual(iq.work(tried, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(self.host.units["x.service"], "active")
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_pass_with_nothing_to_install_does_not_ask_whether_it_is_online(self) -> None:
        # Restarting a unit needs no internet — and no DNS query either.
        self.host.units["x.service"] = "failed"
        self.queue(unit("x.service"))
        self.assertEqual(iq.work({}, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(self.host.asked, 0)
        self.assertEqual(self.host.units["x.service"], "active")

    def test_a_unit_waits_for_the_packages_and_images_queued_before_it(self) -> None:
        # A restart must not need the network: the unit is only restarted
        # once what it builds on is there.
        self.online()
        self.host.broken.add("alpine")
        self.host.units["x.service"] = "failed"
        self.queue(package("ovs"))
        self.queue(image("alpine"))
        self.queue(unit("x.service"))
        tried: dict[str, iq.Attempt] = {}
        result = iq.work(tried, units=True)
        self.assertEqual(result, {"waiting": ["image alpine", "unit x.service"], "stuck": []})
        self.assertEqual(self.host.units["x.service"], "failed")
        self.host.broken.clear()
        self.clock.sleep(2 * iq.POLL_SECONDS)
        self.assertEqual(iq.work(tried, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(self.host.ran[-2:], ["docker pull alpine", "systemctl restart x.service"])

    def test_an_image_waits_for_the_packages_queued_before_it(self) -> None:
        # docker itself may be one of them.
        self.online()
        self.host.packages.discard("docker.io")
        self.host.broken.add("docker.io")
        self.queue(package("docker", "docker.io"))
        self.queue(image("alpine"))
        result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": ["package docker", "image alpine"], "stuck": []})
        self.assertFalse(any("pull" in line for line in self.host.ran))

    def test_a_job_does_not_wait_for_what_was_queued_after_it(self) -> None:
        # Order is the order of queueing: this unit was queued BEFORE the
        # package, so it does not build on it.
        self.host.units["x.service"] = "failed"
        self.queue(unit("x.service"))
        self.queue(package("ovs"))
        result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": ["package ovs"], "stuck": []})
        self.assertEqual(self.host.ran, ["systemctl restart x.service"])

    def test_jobs_of_one_kind_do_not_wait_for_each_other(self) -> None:
        self.online()
        self.host.broken.add("traceroute")
        self.queue(package("traceroute"))
        self.queue(package("ovs"))
        result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": ["package traceroute"], "stuck": []})
        self.assertIn("ovs", self.host.packages)

    def test_a_failed_try_is_repeated_with_a_growing_pause(self) -> None:
        self.online()
        self.host.broken.add("alpine")
        self.queue(image("alpine"))
        tried: dict[str, iq.Attempt] = {}
        at: list[float] = []
        for _ in range(12):
            before = len(self.host.ran)
            iq.work(tried, units=True)
            if len(self.host.ran) > before:
                at.append(self.clock.elapsed)
            self.clock.sleep(iq.POLL_SECONDS)
        # Polled every 10s; tried at once, then 20s and 40s later.
        self.assertEqual(at, [0, 20, 60])
        self.assertEqual(self.host.ran, ["docker pull alpine"] * iq.ATTEMPTS)

    def test_a_job_out_of_tries_stops_holding_up_the_jobs_behind_it(self) -> None:
        # An image nobody publishes must not block a router's restart for
        # good.
        self.online()
        self.host.broken.add("PLACEHOLDER")
        self.host.units["x.service"] = "failed"
        self.queue(image("PLACEHOLDER"))
        self.queue(unit("x.service"))
        tried: dict[str, iq.Attempt] = {}
        results: list[iq.Pass] = []
        for _ in range(8):
            results.append(iq.work(tried, units=True))
            self.clock.sleep(iq.POLL_SECONDS)
        self.assertEqual(
            results[0], {"waiting": ["image PLACEHOLDER", "unit x.service"], "stuck": []}
        )
        self.assertEqual(results[-1], {"waiting": [], "stuck": ["image PLACEHOLDER"]})
        self.assertEqual(self.host.units["x.service"], "active")
        # The pass that used up the image's last try is the one that
        # restarts the unit.
        self.assertEqual(
            self.host.ran[-2:], ["docker pull PLACEHOLDER", "systemctl restart x.service"]
        )
        self.assertEqual(self.left(), ["image PLACEHOLDER"])

    def test_a_package_or_image_out_of_tries_is_still_retried_slowly(self) -> None:
        self.online()
        self.host.broken.add("alpine")
        self.queue(image("alpine"))
        tried: dict[str, iq.Attempt] = {}
        for _ in range(10):
            iq.work(tried, units=True)
            self.clock.sleep(iq.POLL_SECONDS)
        self.assertEqual(len(self.host.ran), iq.ATTEMPTS)
        # ...the registry has it now.
        self.host.broken.clear()
        self.clock.sleep(iq.SLOW_RETRY)
        self.assertEqual(iq.work(tried, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_slow_retry_also_waits_for_the_internet(self) -> None:
        self.online()
        self.host.broken.add("alpine")
        self.queue(image("alpine"))
        tried: dict[str, iq.Attempt] = {}
        for _ in range(10):
            iq.work(tried, units=True)
            self.clock.sleep(iq.POLL_SECONDS)
        self.host.is_online = False
        self.clock.sleep(iq.SLOW_RETRY)
        self.assertEqual(iq.work(tried, units=True), {"waiting": [], "stuck": ["image alpine"]})
        self.assertEqual(len(self.host.ran), iq.ATTEMPTS)

    def test_a_unit_that_keeps_failing_is_left_alone(self) -> None:
        # It fails for a reason the network cannot fix: do not bounce it
        # forever.
        self.host.units["x.service"] = "failed"
        self.host.broken.add("x.service")
        self.queue(unit("x.service"))
        tried: dict[str, iq.Attempt] = {}
        for _ in range(200):
            result = iq.work(tried, units=True)
            self.clock.sleep(iq.POLL_SECONDS)
        self.assertEqual(self.host.ran, ["systemctl restart x.service"] * iq.ATTEMPTS)
        self.assertEqual(result, {"waiting": [], "stuck": ["unit x.service"]})
        # ...but it is not forgotten: once it is up, its job is done.
        self.host.units["x.service"] = "active"
        self.assertEqual(iq.work(tried, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(os.listdir(self.dir), [])

    def test_a_unit_that_was_never_started_is_started(self) -> None:
        self.queue(unit("x.service"))  # the fake host reports it "inactive"
        iq.work({}, units=True)
        self.assertEqual(self.host.ran, ["systemctl restart x.service"])

    def test_a_unit_that_is_still_starting_is_not_restarted(self) -> None:
        self.host.units["x.service"] = "activating"
        self.queue(unit("x.service"))
        tried: dict[str, iq.Attempt] = {}
        self.assertEqual(iq.work(tried, units=True), {"waiting": ["unit x.service"], "stuck": []})
        self.assertEqual(self.host.ran, [])
        self.assertEqual(tried[self.left_paths()[0]]["count"], 0)

    def left_paths(self) -> list[str]:
        return [entry["path"] for entry in iq.read_queue()[0]]

    def test_a_rewritten_job_file_gets_its_tries_back(self) -> None:
        self.host.units["x.service"] = "failed"
        self.host.broken.add("x.service")
        path = self.queue(unit("x.service"))
        tried: dict[str, iq.Attempt] = {}
        for _ in range(50):
            iq.work(tried, units=True)
            self.clock.sleep(iq.POLL_SECONDS)
        self.assertEqual(len(self.host.ran), iq.ATTEMPTS)
        # Someone queues it again (the same file, written anew).
        stamp = os.stat(path).st_mtime + 100
        os.utime(path, (stamp, stamp))
        self.host.broken.clear()
        self.assertEqual(iq.work(tried, units=True), {"waiting": [], "stuck": []})
        self.assertEqual(self.host.units["x.service"], "active")

    def test_the_tries_of_a_file_that_is_gone_are_forgotten(self) -> None:
        self.online()
        self.host.broken.add("alpine")
        path = self.queue(image("alpine"))
        tried: dict[str, iq.Attempt] = {}
        iq.work(tried, units=True)
        self.assertEqual(list(tried), [path])
        os.remove(path)
        iq.work(tried, units=True)
        self.assertEqual(tried, {})

    def test_without_units_the_unit_jobs_are_not_even_looked_at(self) -> None:
        self.host.units["x.service"] = "active"
        self.queue(unit("x.service"))
        self.assertEqual(iq.work({}, units=False), {"waiting": [], "stuck": []})
        self.assertEqual(self.left(), ["unit x.service"])

    def test_a_job_whose_tool_is_missing_is_a_failed_try_not_a_crash(self) -> None:
        # No docker on the host (and no package job that brings it).
        self.host.packages.discard("docker.io")
        self.online()
        self.queue(image("alpine"))
        with mock.patch.object(iq, "run", return_value=False):
            result = iq.work({}, units=True)
        self.assertEqual(result, {"waiting": ["image alpine"], "stuck": []})


class OnceTest(QueueCase):
    def test_online_it_installs_everything_but_leaves_the_units(self) -> None:
        # The deploy pass runs this in the foreground BEFORE it starts the
        # units — the units are not its business.
        self.online()
        self.host.units["x.service"] = "failed"
        self.queue(package("ovs"))
        self.queue(image("alpine"))
        self.queue(unit("x.service"))
        self.assertEqual(iq.main(["--once"]), 0)
        self.assertEqual(self.left(), ["unit x.service"])
        self.assertEqual(self.host.units["x.service"], "failed")
        self.assertEqual(self.clock.sleeps, [])

    def test_offline_it_returns_at_once_and_says_what_is_left(self) -> None:
        self.queue(package("ovs"))
        self.queue(image("alpine"))
        # What is left is the service's job, not a failed deploy step.
        self.assertEqual(iq.main(["--once"]), 0)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.host.ran, [])
        self.assertIn(
            "left for ovn-fabric-install.service: package ovs, image alpine", self.out.getvalue()
        )

    def test_a_failed_install_is_tried_once_and_left_to_the_service(self) -> None:
        self.online()
        self.host.broken.add("ovs")
        self.queue(package("ovs"))
        self.assertEqual(iq.main(["--once"]), 0)
        self.assertIn("left for ovn-fabric-install.service: package ovs", self.out.getvalue())
        self.assertEqual(len([line for line in self.host.ran if "install" in line]), 1)

    def test_an_empty_queue_is_fine(self) -> None:
        self.assertEqual(iq.main(["--once"]), 0)


class Stop(Exception):
    """Ends a serve() loop from inside the fake clock."""


class ServeTest(QueueCase):
    def serve(self, polls: int, between: dict[int, Any] | None = None) -> None:
        """Run the service for `polls` polls; `between[n]()` runs while it
        sleeps after poll n (1-based)."""
        sleep = self.clock.sleep

        def fake_sleep(seconds: float) -> None:
            sleep(seconds)
            count = len(self.clock.sleeps)
            if between and count in between:
                between[count]()
            if count >= polls:
                raise Stop

        with mock.patch.object(self.clock, "sleep", fake_sleep):
            with self.assertRaises(Stop):
                iq.main([])

    def test_it_polls_the_directory_every_ten_seconds_for_good(self) -> None:
        # An empty queue is not the end: the next deploy — or anyone —
        # just drops a file.
        self.serve(polls=30)
        self.assertEqual(self.clock.sleeps, [10] * 30)
        self.assertEqual(iq.POLL_SECONDS, 10)

    def test_a_job_dropped_into_the_directory_is_found_on_the_next_poll(self) -> None:
        self.online()
        self.host.units["x.service"] = "failed"

        def deploy_ends() -> None:
            self.queue(unit("x.service"))

        self.queue(package("ovs"))
        self.serve(polls=3, between={2: deploy_ends})
        self.assertEqual(
            self.host.ran[-2:],
            [
                "env DEBIAN_FRONTEND=noninteractive apt-get install -y ovs",
                "systemctl restart x.service",
            ],
        )
        self.assertEqual(os.listdir(self.dir), [])

    def test_the_internet_coming_back_is_noticed_by_polling(self) -> None:
        self.host.units["router.service"] = "failed"
        self.queue(image("alpine"))
        self.queue(unit("router.service"))
        self.serve(polls=40, between={30: self.online})
        self.assertEqual(self.host.ran, ["docker pull alpine", "systemctl restart router.service"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_it_says_what_it_waits_on_when_that_changes_not_on_every_poll(self) -> None:
        self.queue(image("alpine"))
        self.serve(polls=40, between={30: self.online})
        log = self.out.getvalue()
        self.assertEqual(log.count("waiting on: image alpine"), 1)
        self.assertEqual(log.count("the queue is empty"), 1)
        self.assertIn(f"watching {self.dir} every 10s", log)

    def test_it_says_what_is_stuck(self) -> None:
        self.host.units["x.service"] = "failed"
        self.host.broken.add("x.service")
        self.queue(unit("x.service"))
        self.serve(polls=20)
        self.assertEqual(self.out.getvalue().count(f"stuck, left in {self.dir}: unit x.service"), 1)


class MainTest(unittest.TestCase):
    def test_anything_but_once_is_a_usage_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(iq.main(["--help"]), 2)
        self.assertIn("usage: ovn-fabric-install [--once]", stderr.getvalue())


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        redirect = contextlib.redirect_stdout(io.StringIO())
        self.out = redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_success_and_failure(self) -> None:
        self.assertTrue(iq.run(["true"]))
        self.assertFalse(iq.run(["false"]))

    def test_a_missing_tool_is_a_failure_not_a_crash(self) -> None:
        self.assertFalse(iq.run(["ovn-fabric-no-such-tool"]))
        self.assertIn("cannot run ovn-fabric-no-such-tool", self.out.getvalue())
        self.assertIsNone(iq.quiet(["ovn-fabric-no-such-tool"]))

    def test_a_command_that_hangs_is_cut_off(self) -> None:
        # A connection that died under an install must not stall the queue.
        hung = subprocess.TimeoutExpired(["apt-get"], iq.COMMAND_TIMEOUT)
        with mock.patch("subprocess.run", side_effect=hung) as run:
            self.assertFalse(iq.run(["apt-get", "install", "-y", "ovs"]))
        self.assertEqual(run.call_args.kwargs, {"timeout": iq.COMMAND_TIMEOUT})
        self.assertIn("timed out after 900s: apt-get", self.out.getvalue())


class Resolver:
    """A DNS resolver of the test's own on 127.0.0.1: answers ONE query
    with `reply(query)` — or with nothing when that returns None."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.queries: list[bytes] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(2)
        self.port: int = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        try:
            query, sender = self.sock.recvfrom(512)
        except OSError:
            return
        self.queries.append(query)
        answer = self.reply(query)
        if answer is not None:
            self.sock.sendto(answer, sender)

    def close(self) -> None:
        self.thread.join()
        self.sock.close()


def answer(query: bytes, *, rcode: int = 0, records: int = 13, ident: int | None = None) -> bytes:
    """A response header for `query` (the records themselves are not
    looked at by the consumer)."""
    (query_id,) = struct.unpack(">H", query[:2])
    flags = 0x8180 | rcode
    return (
        struct.pack(">HHHHHH", query_id if ident is None else ident, flags, 1, records, 0, 0)
        + query[12:]
    )


class OnlineTest(unittest.TestCase):
    def ask(self, reply: Any) -> tuple[bool, Resolver]:
        resolver = Resolver(reply)
        self.addCleanup(resolver.close)
        with (
            mock.patch.object(iq, "RESOLVERS", ["127.0.0.1"]),
            mock.patch.object(iq, "DNS_PORT", resolver.port),
            mock.patch.object(iq, "ONLINE_TIMEOUT", 0.3),
        ):
            return iq.online(), resolver

    def test_online_is_a_resolver_resolving_a_query(self) -> None:
        result, resolver = self.ask(answer)
        self.assertTrue(result)
        # What is asked: one question, recursion desired — the root
        # zone's name servers (name ".", type NS, class IN).
        (query,) = resolver.queries
        self.assertEqual(struct.unpack(">HHHHHH", query[:12])[1:], (0x0100, 1, 0, 0, 0))
        self.assertEqual(query[12:], b"\x00\x00\x02\x00\x01")

    def test_no_answer_is_offline(self) -> None:
        started = time.monotonic()
        result, _ = self.ask(lambda query: None)
        self.assertFalse(result)
        self.assertLess(time.monotonic() - started, 2)

    def test_an_answer_that_does_not_resolve_is_offline(self) -> None:
        # SERVFAIL: the resolver is there, the internet behind it is not.
        self.assertFalse(self.ask(lambda query: answer(query, rcode=2))[0])
        self.assertFalse(self.ask(lambda query: answer(query, records=0))[0])

    def test_an_answer_to_another_query_or_garbage_is_ignored(self) -> None:
        self.assertFalse(self.ask(lambda query: answer(query, ident=query[0] ^ 0xFF))[0])
        self.assertFalse(self.ask(lambda query: b"hello")[0])

    def test_the_resolvers_are_asked_by_address(self) -> None:
        # The host's own resolver — and its cache — must not be part of
        # the question.
        self.assertEqual(iq.RESOLVERS[0], "1.1.1.1")
        for resolver in iq.RESOLVERS:
            socket.inet_aton(resolver)  # raises on anything but an address

    def test_no_route_at_all_is_offline_at_once(self) -> None:
        unreachable = OSError(101, "Network is unreachable")
        with mock.patch("socket.socket") as factory:
            sock = factory.return_value.__enter__.return_value
            sock.sendto.side_effect = unreachable
            self.assertFalse(iq.online())
            sock.recvfrom.assert_not_called()


if __name__ == "__main__":
    unittest.main()
