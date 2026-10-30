from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from deployer.context import Ctx
from deployer.lifecycle import LifecycleAction, LifecycleController, Operation
from deployer.primitives_host import HostPrimitive


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
    ) -> bool:
        self.commands.append(tuple(argv))
        return True

    async def capture_cmd(
        self, label: str, argv: Sequence[str], abort_on_error: bool = True
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
    lc.lazy.clear()


class FilesystemPrimitiveTest(unittest.TestCase):
    def test_mkdir_chmod_and_remove(self) -> None:
        async def run() -> None:
            job = FakeJob()
            host = HostPrimitive("h")
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, host.make_dir("/etc/wireguard"))
            await _now(lc, host.chmod("/etc/wireguard/x.conf", "600"))
            self.assertIn("fs.dir:/etc/wireguard", lc.graph.nodes)
            self.assertIn("fs.chmod:/etc/wireguard/x.conf", lc.graph.nodes)

            delete = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(delete, host.remove_file("/var/run/netns/x"))
            await _now(delete, host.remove_tree("/var/lib/ovn-fabric/x"))
            self.assertEqual(
                job.commands[-2:],
                [
                    ("rm", "-f", "/var/run/netns/x"),
                    ("rm", "-rf", "/var/lib/ovn-fabric/x"),
                ],
            )

        asyncio.run(run())
