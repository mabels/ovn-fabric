from __future__ import annotations

import asyncio
import time
import unittest
from collections.abc import Sequence
from typing import Any
from unittest import mock

from deployer.dependency import install_image, install_packages


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.captures: list[tuple[str, ...]] = []
        self.events: list[dict[str, Any]] = []
        self.messages: list[str] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        **kwargs: Any,
    ) -> bool:
        self.commands.append(tuple(argv))
        return True

    async def capture_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
    ) -> str:
        self.captures.append(tuple(argv))
        return ""

    def emit(self, event: dict[str, Any], queue: bool = False) -> None:
        self.events.append(event)

    def debug(self, message: str) -> None:
        self.messages.append(message)

    def info(self, message: str) -> None:
        self.messages.append(message)

    def warn(self, message: str) -> None:
        self.messages.append(message)

    def error(self, message: str) -> None:
        self.messages.append(message)


class DependencyDryRunTest(unittest.TestCase):
    def test_packages_answer_installed_without_apt_or_dpkg(self) -> None:
        async def run() -> None:
            job = FakeJob()
            deps = {
                "ovs": {
                    "abstract": {"kind": "abstract", "name": "ovs"},
                    "concrete": [
                        {"kind": "apt-dep", "package": "openvswitch-switch"},
                        {"kind": "sys-cmd-dep", "name": "ovs-vsctl", "cmd": ["true"]},
                    ],
                }
            }
            await install_packages(job, deps, dry_run=True)
            self.assertEqual(job.commands, [])
            self.assertEqual(job.captures, [])
            self.assertEqual(
                job.events,
                [
                    {
                        "kind": "apt-dep",
                        "package": "openvswitch-switch",
                        "state": "installed",
                    },
                    {"kind": "sys-cmd-dep", "name": "ovs-vsctl", "state": "installed"},
                ],
            )
            self.assertIn("dry-run: apt-dep openvswitch-switch installed", job.messages)
            self.assertIn("dry-run: sys-cmd-dep ovs-vsctl installed", job.messages)

        asyncio.run(run())

    def test_image_answers_installed_without_docker(self) -> None:
        async def run() -> None:
            job = FakeJob()
            await install_image(
                job,
                {"image": "ovn-fabric-dns", "dockerfile": "FROM scratch"},
                dry_run=True,
            )
            self.assertEqual(job.commands, [])
            self.assertEqual(
                job.events,
                [
                    {
                        "kind": "dependency",
                        "dep": {"kind": "docker-image", "image": "ovn-fabric-dns"},
                        "state": "installed",
                    }
                ],
            )
            self.assertIn("dry-run: docker-image ovn-fabric-dns installed", job.messages)

        asyncio.run(run())


class DependencyResolvedLogTest(unittest.TestCase):
    def test_resolved_packages_are_logged_at_info(self) -> None:
        async def run() -> None:
            job = FakeJob()

            async def capture_cmd(
                label: str, argv: Sequence[str], abort_on_error: bool = True
            ) -> str:
                return "install ok installed 3.3.0"

            job.capture_cmd = capture_cmd  # type: ignore[method-assign]
            deps = {
                "ovs": {
                    "abstract": {"kind": "abstract", "name": "ovs"},
                    "concrete": [
                        {"kind": "apt-dep", "package": "openvswitch-switch"},
                        {"kind": "sys-cmd-dep", "name": "ovs-vsctl", "cmd": ["true"]},
                    ],
                }
            }
            with mock.patch("deployer.dependency._read_stamp", return_value=time.time()):
                await install_packages(job, deps)
            self.assertIn("apt-dep openvswitch-switch resolved (3.3.0)", job.messages)
            self.assertIn("sys-cmd-dep ovs-vsctl resolved", job.messages)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
