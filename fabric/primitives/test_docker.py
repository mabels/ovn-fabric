from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from fabric.context import Ctx
from fabric.lifecycle import LifecycleAction, LifecycleController, Operation
from fabric.primitives.docker import files_hash
from fabric.primitives.host import HostPrimitive


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
        timeout: float | None = None,
    ) -> bool:
        self.commands.append((label, tuple(argv)))
        return tuple(argv[:3]) != ("/usr/bin/docker", "image", "inspect")

    async def capture_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        timeout: float | None = None,
    ) -> str:
        return ""

    def info(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        pass


async def _now(lc: LifecycleController, operation: Operation) -> None:
    """Run one operation right away, in the controller's direction."""
    await lc.run(operation)
    await lc.execute()
    lc.queue.clear()


class DockerPrimitiveTest(unittest.TestCase):
    def test_container_run_remove_and_netns(self) -> None:
        async def run() -> None:
            job = FakeJob()
            host = HostPrimitive("h")
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            docker = host.docker(
                "ovn-fabric-dns",
                "ovn-fabric-dns",
                command=("/usr/sbin/dnsmasq", "--no-daemon"),
                privileged=True,
                environment=(("A", "1"),),
                volumes=(("/var/lib/ovn-fabric/dns", "/etc/dns:ro"),),
                run_flags=("--dns", "1.1.1.1"),
            )
            await _now(lc, docker)

            self.assertIn("docker.container:ovn-fabric-dns", lc.graph.nodes)
            node = lc.graph.nodes["docker.container:ovn-fabric-dns"]
            self.assertEqual(node["netnsRef"], "netns:ovn-fabric-dns")
            self.assertFalse(docker.get_netns().owned)

            argv = [command[1] for command in job.commands]
            self.assertEqual(argv[0], ("/usr/bin/docker", "rm", "-f", "ovn-fabric-dns"))
            self.assertIn(
                (
                    "/usr/bin/docker",
                    "run",
                    "-d",
                    "--privileged",
                    "--network",
                    "none",
                    "--name",
                    "ovn-fabric-dns",
                    "--label",
                    f"ovn-fabric.spec={docker.spec}",  # what it runs with, hashed
                    "-e",
                    "A=1",
                    "-v",
                    "/var/lib/ovn-fabric/dns:/etc/dns:ro",
                    "--dns",
                    "1.1.1.1",
                    "ovn-fabric-dns",
                    "/usr/sbin/dnsmasq",
                    "--no-daemon",
                ),
                argv,
            )

        asyncio.run(run())

    def test_image_build_and_exists(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(
                lc,
                HostPrimitive("h").docker_image(
                    "ovn-fabric-dns",
                    "/var/lib/ovn-fabric/build/dns/Dockerfile",
                    "/var/lib/ovn-fabric/build/dns",
                    "abc",
                ),
            )
            argv = [command[1] for command in job.commands]
            self.assertIn(
                (
                    "/usr/bin/docker",
                    "build",
                    "-t",
                    "ovn-fabric-dns",
                    "--label",
                    "ovn-fabric.spec=abc",
                    "-f",
                    "/var/lib/ovn-fabric/build/dns/Dockerfile",
                    "/var/lib/ovn-fabric/build/dns",
                ),
                argv,
            )
            self.assertIn("docker.image:ovn-fabric-dns", lc.graph.nodes)

        asyncio.run(run())

    def test_a_process_started_in_the_container_ends_with_it(self) -> None:
        async def run() -> None:
            app = HostPrimitive("h").docker("c", "img").exec_detached(["/sbin/dhcpcd", "eth0"])
            for action in (LifecycleAction.CREATE, LifecycleAction.DELETE):
                job = FakeJob()
                await _now(LifecycleController(Ctx(), job, action, "h"), app)
                if action is LifecycleAction.CREATE:
                    self.assertEqual(
                        job.commands[-1][1],
                        ("/usr/bin/docker", "exec", "-d", "c", "/sbin/dhcpcd", "eth0"),
                    )
                else:
                    self.assertEqual(job.commands, [])  # removing the container ends it

        asyncio.run(run())

    def test_delete_removes_the_container(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(lc, HostPrimitive("h").docker("c", "img"))
            self.assertEqual(
                job.commands[-1][1],
                ("/usr/bin/docker", "rm", "-f", "c"),
            )

        asyncio.run(run())


class SpecTest(unittest.TestCase):
    def test_a_changed_file_changes_the_spec_the_run_arguments_stay(self) -> None:
        host = HostPrimitive("h")
        one = host.docker("c", "img", files=files_hash("identity-1"))
        other = host.docker("c", "img", files=files_hash("identity-2"))
        self.assertNotEqual(one.spec, other.spec)
        self.assertEqual(one, host.docker("c", "img", files=files_hash("identity-1")))
        self.assertNotIn("identity-1", " ".join(one._run_argv()))


if __name__ == "__main__":
    unittest.main()
