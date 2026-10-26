from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from deployer.lifecycle import (
    Command,
    LifecycleAction,
    LifecycleController,
    Primitive,
    RawNodeTemplate,
)


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
    ) -> bool:
        self.commands.append((label, tuple(argv), abort_on_error))
        return True

    async def capture_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
    ) -> str:
        return ""


BRIDGE = Primitive(
    key="bridge:br-x",
    nodes=(
        RawNodeTemplate(
            id="bridge:br-x",
            kind="bridge",
            fields={"host": "host:h", "ports": []},
        ),
    ),
    create=(Command("create bridge", ("ovs-vsctl", "add-br", "br-x")),),
    delete=(Command("delete bridge", ("ovs-vsctl", "del-br", "br-x")),),
)


class LifecycleTest(unittest.TestCase):
    def test_create_records_raw_node_and_dumps_snapshot(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(job, LifecycleAction.CREATE, "h", root=root)
            await lc.run(BRIDGE)
            await lc.finish()
            self.assertEqual(len(job.commands), 1)

        root = Path(tempfile.mkdtemp())
        import asyncio

        asyncio.run(run())
        snaps = list((root / "snapshots").glob("*-rawIR.snapshot"))
        self.assertEqual(len(snaps), 1)
        self.assertTrue((root / "rawIR.snapshot").is_symlink())

    def test_delete_removes_node(self) -> None:
        async def run() -> None:
            job = FakeJob()
            create = LifecycleController(job, LifecycleAction.CREATE, "h", root=root)
            await create.run(BRIDGE)
            delete = LifecycleController(job, LifecycleAction.DELETE, "h", root=root)
            await delete.run(BRIDGE)
            await delete.finish()

        root = Path(tempfile.mkdtemp())
        import asyncio

        asyncio.run(run())
        import json

        payload = json.loads((root / "rawIR.snapshot").read_text())
        self.assertEqual(payload["collections"], [])


if __name__ == "__main__":
    unittest.main()
