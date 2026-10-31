from __future__ import annotations

import asyncio
import stat
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

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
        timeout: float | None = None,
    ) -> bool:
        self.commands.append(tuple(argv))
        return True

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


class FilesystemPrimitiveTest(unittest.TestCase):
    def test_mkdir_chmod_and_remove_are_done_in_python(self) -> None:
        async def run() -> None:
            root = Path(tempfile.mkdtemp())
            job = FakeJob()
            host = HostPrimitive("h")
            wg = root / "etc/wireguard"
            conf = wg / "x.conf"
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, host.make_dir(str(wg)))
            await _now(lc, host.make_dir(str(wg)))  # one already there is fine
            conf.write_text("[Interface]\n")
            await _now(lc, host.chmod(str(conf), "600"))
            self.assertEqual(stat.S_IMODE(conf.stat().st_mode), 0o600)
            self.assertIn(f"fs.dir:{wg}", lc.graph.nodes)
            self.assertIn(f"fs.chmod:{conf}", lc.graph.nodes)

            state = root / "var/lib/x"
            (state / "data").mkdir(parents=True)
            netns = root / "run/netns/x"
            netns.parent.mkdir(parents=True)
            netns.touch()
            delete = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(delete, host.make_dir(str(wg)))  # shared, not empty: stays
            self.assertTrue(conf.exists())
            await _now(delete, host.remove_file(str(netns)))
            await _now(delete, host.remove_file(str(netns)))  # gone already is fine
            await _now(delete, host.state_dir(str(state)))  # owned: with its content
            self.assertFalse(netns.exists())
            self.assertFalse(state.exists())
            conf.unlink()
            await _now(delete, host.make_dir(str(wg)))  # empty now: removed
            self.assertFalse(wg.exists())
            self.assertEqual(job.commands, [])  # no mkdir/rm/chmod processes

        asyncio.run(run())
