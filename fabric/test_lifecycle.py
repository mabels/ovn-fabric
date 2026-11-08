from __future__ import annotations

import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from unittest import mock

from fabric.context import Ctx, Options
from fabric.lifecycle import (
    Command,
    LifecycleAction,
    LifecycleController,
    Primitive,
    RawNodeTemplate,
    SymlinkNetnsOperation,
    WriteFileOperation,
)


class FakeJob:
    def __init__(self, capture: str = "", fail: Sequence[str] = ()) -> None:
        self.commands: list[tuple[str, tuple[str, ...], bool]] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self._capture = capture
        self._fail = set(fail)

    async def run_cmd(
        self,
        label: str,
        cmd: list[str],
        abort_on_error: bool = True,
        timeout: float | None = None,
    ) -> bool:
        self.commands.append((label, tuple(cmd), abort_on_error))
        if label not in self._fail:
            return True
        if abort_on_error:
            raise RuntimeError(f"failed in {label}")
        self.warn(f"failed in {label}")
        return False

    async def capture_cmd(
        self,
        label: str,
        cmd: list[str],
        abort_on_error: bool = True,
        timeout: float | None = None,
    ) -> str:
        return self._capture

    def info(self, message: str) -> None:
        self.infos.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def _primitive(key: str, create: Sequence[str], delete: Sequence[str]) -> Primitive:
    return Primitive(
        key=key,
        nodes=(RawNodeTemplate(id=key, kind="test", fields={}),),
        create_cmds=(Command(f"{key} (create)", tuple(create)),),
        delete_cmds=(Command(f"{key} (delete)", tuple(delete)),),
    )


BRIDGE = _primitive("bridge:br-x", ("ovs-vsctl", "add-br", "br-x"), ("ovs-vsctl", "del-br", "br-x"))
PORT = _primitive(
    "port:eth0",
    ("ovs-vsctl", "add-port", "br-x", "eth0"),
    ("ovs-vsctl", "del-port", "br-x", "eth0"),
)


def _controller(
    job: FakeJob, action: LifecycleAction, ctx: Ctx | None = None
) -> LifecycleController:
    return LifecycleController(ctx or Ctx(), job, action, "h", name="job/h")


class LifecycleTest(unittest.TestCase):
    def test_queue_runs_create_forward_and_delete_reversed(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = _controller(job, LifecycleAction.CREATE)
            await lc.run(BRIDGE)
            await lc.run(PORT)
            self.assertEqual(job.commands, [])
            await lc.execute()
            self.assertEqual(
                [command[1] for command in job.commands],
                [
                    ("ovs-vsctl", "add-br", "br-x"),
                    ("ovs-vsctl", "add-port", "br-x", "eth0"),
                ],
            )

            # The SAME declaration, DELETE: the controller reverses it and
            # picks each primitive's delete.
            job_delete = FakeJob()
            lc_delete = _controller(job_delete, LifecycleAction.DELETE)
            await lc_delete.run(BRIDGE)
            await lc_delete.run(PORT)
            await lc_delete.execute()
            self.assertEqual(
                [command[1] for command in job_delete.commands],
                [
                    ("ovs-vsctl", "del-port", "br-x", "eth0"),
                    ("ovs-vsctl", "del-br", "br-x"),
                ],
            )

        asyncio.run(run())

    def test_create_aborts_and_delete_keeps_going(self) -> None:
        async def run() -> None:
            job = FakeJob(fail=["port:eth0 (create)"])
            lc = _controller(job, LifecycleAction.CREATE)
            await lc.run(PORT)
            await lc.run(BRIDGE)
            with self.assertRaises(RuntimeError):
                await lc.execute()
            self.assertEqual(len(job.commands), 1)
            self.assertTrue(job.commands[0][2], "CREATE must abort on failure")

            job = FakeJob(fail=["port:eth0 (delete)"])
            lc = _controller(job, LifecycleAction.DELETE)
            await lc.run(BRIDGE)
            await lc.run(PORT)
            await lc.execute()
            self.assertEqual(
                [(command[1][1], command[2]) for command in job.commands],
                [("del-port", False), ("del-br", False)],
            )
            self.assertEqual(job.warnings, ["failed in port:eth0 (delete)"])
            self.assertEqual(
                job.infos,
                [
                    "delete Primitive[port:eth0]: ovs-vsctl del-port br-x eth0",
                    "delete Primitive[bridge:br-x]: ovs-vsctl del-br br-x",
                ],
            )

        asyncio.run(run())

    def test_delete_logs_a_raising_operation_and_continues(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = _controller(job, LifecycleAction.DELETE)
            await lc.run(BRIDGE)
            # A directory where a file is expected: unlink raises.
            await lc.run(WriteFileOperation(tempfile.mkdtemp(), ""))
            await lc.execute()
            self.assertEqual([command[1][1] for command in job.commands], ["del-br"])
            self.assertEqual(len(job.warnings), 1)
            self.assertIn("WriteFileOperation", job.warnings[0])

        asyncio.run(run())

    def test_create_records_and_delete_removes_raw_nodes(self) -> None:
        async def run() -> None:
            lc = _controller(FakeJob(), LifecycleAction.CREATE)
            await lc.run(BRIDGE)
            await lc.execute()
            self.assertIn("bridge:br-x", lc.graph.nodes)

            lc.action = LifecycleAction.DELETE
            await lc.execute()
            self.assertNotIn("bridge:br-x", lc.graph.nodes)

        asyncio.run(run())

    def test_queued_capture_resolves_lazy_refs(self) -> None:
        async def run() -> None:
            job = FakeJob(capture="uuid-1")
            lc = _controller(job, LifecycleAction.CREATE)
            ref = lc.capture("chassis", "lookup", ["ovn-sbctl", "find", "Chassis"])
            await lc.run(
                Primitive(
                    key="object",
                    nodes=(),
                    create_cmds=(
                        Command("use", cast(tuple[str, ...], ("ovn-nbctl", "set", "x", ref))),
                    ),
                    delete_cmds=(),
                )
            )
            await lc.execute()
            self.assertEqual(job.commands[-1][1], ("ovn-nbctl", "set", "x", "uuid-1"))

        asyncio.run(run())

    def test_capture_publishes_a_keyed_result_into_the_run_wide_store(self) -> None:
        async def run() -> None:
            ctx = Ctx()
            producer = _controller(FakeJob(capture="uuid-2"), LifecycleAction.CREATE, ctx)
            chassis = producer.wait_capture(
                "chassis",
                "lookup",
                ["ovn-sbctl", "find", "Chassis"],
                result_key="CHASSIS_ID",
            )
            await producer.execute()
            self.assertEqual(ctx.store["chassis"], {"CHASSIS_ID": "uuid-2"})

            # A second controller of the same run reads the value.
            job = FakeJob()
            consumer = _controller(job, LifecycleAction.CREATE, ctx)
            await consumer.run(
                Primitive(
                    key="object",
                    nodes=(),
                    create_cmds=(
                        Command("use", cast(tuple[str, ...], ("ovn-nbctl", "set", "x", chassis))),
                    ),
                    delete_cmds=(),
                )
            )
            await consumer.execute()
            self.assertEqual(job.commands[-1][1], ("ovn-nbctl", "set", "x", "uuid-2"))

        asyncio.run(run())

    def test_a_required_capture_without_a_value_fails_create(self) -> None:
        async def run() -> None:
            ctx = Ctx()
            lc = _controller(FakeJob(capture=""), LifecycleAction.CREATE, ctx)
            lc.wait_capture("chassis", "lookup", ["ovn-sbctl", "find", "Chassis"], retries=2)
            with mock.patch("asyncio.sleep", new_callable=mock.AsyncMock):
                with self.assertRaisesRegex(RuntimeError, "gave no value after 3 attempts"):
                    await lc.execute()
            self.assertNotIn("chassis", ctx.store)

            optional = _controller(FakeJob(capture=""), LifecycleAction.CREATE, ctx)
            optional.capture("maybe", "lookup", ["true"], optional=True)
            await optional.execute()
            self.assertIsNone(ctx.store["maybe"])

        asyncio.run(run())

    def test_a_failing_capture_is_retried_and_only_its_last_failure_is_final(self) -> None:
        class Flaky(FakeJob):
            def __init__(self) -> None:
                super().__init__()
                self.aborts: list[bool] = []

            async def capture_cmd(
                self,
                label: str,
                cmd: list[str],
                abort_on_error: bool = True,
                timeout: float | None = None,
            ) -> str:
                self.aborts.append(abort_on_error)
                return "uuid-3" if len(self.aborts) == 3 else ""

        async def run() -> None:
            ctx = Ctx()
            job = Flaky()
            lc = _controller(job, LifecycleAction.CREATE, ctx)
            lc.wait_capture("chassis", "lookup", ["ovn-sbctl"], retries=3, result_key="ID")
            with mock.patch("asyncio.sleep", new_callable=mock.AsyncMock):
                await lc.execute()
            self.assertEqual(ctx.store["chassis"], {"ID": "uuid-3"})
            self.assertEqual(job.aborts, [False, False, False])  # a failure is not final yet

            job = Flaky()
            lc = _controller(job, LifecycleAction.CREATE, ctx)
            lc.wait_capture("chassis", "lookup", ["ovn-sbctl"], retries=1)
            with mock.patch("asyncio.sleep", new_callable=mock.AsyncMock):
                with self.assertRaises(RuntimeError):
                    await lc.execute()
            self.assertEqual(job.aborts, [False, True])  # the last one aborts on failure

        asyncio.run(run())

    def test_dry_run_describes_the_ordered_queue_and_runs_nothing(self) -> None:
        async def run() -> None:
            job = FakeJob()
            ctx = Ctx(Options(dry_run=True))
            lc = _controller(job, LifecycleAction.DELETE, ctx)
            await lc.run(BRIDGE)
            await lc.run(PORT)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                await lc.execute()
            self.assertEqual(job.commands, [])
            self.assertEqual(
                out.getvalue().splitlines(),
                [
                    "job/h --- Primitive[port:eth0] --- ovs-vsctl del-port br-x eth0",
                    "job/h --- Primitive[bridge:br-x] --- ovs-vsctl del-br br-x",
                ],
            )

        asyncio.run(run())

    def test_write_file_create_overwrites_and_delete_removes_it(self) -> None:
        async def run() -> None:
            path = Path(tempfile.mkdtemp()) / "unit.service"
            path.write_text("left over")
            write = WriteFileOperation(str(path), "[Unit]", mode=0o600)
            create = _controller(FakeJob(), LifecycleAction.CREATE)
            await create.run(write)
            await create.execute()
            self.assertEqual(path.read_text(), "[Unit]\n")  # what was there is overwritten
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])  # no temporary left
            await create.execute()  # again: still fine

            delete = _controller(FakeJob(), LifecycleAction.DELETE)
            await delete.run(write)
            await delete.execute()
            self.assertFalse(path.exists())
            # Deleting what is already gone is not an error.
            await delete.execute()

        asyncio.run(run())


class SymlinkNetnsTest(unittest.TestCase):
    def test_a_link_to_a_process_that_is_gone_is_made_anew(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "c"
            old.symlink_to("/proc/999999/ns/net")
            operation = SymlinkNetnsOperation("c", os.getpid())

            async def run() -> None:
                with (
                    mock.patch.object(SymlinkNetnsOperation, "path", old),
                    mock.patch.object(
                        SymlinkNetnsOperation, "_running", mock.AsyncMock(return_value=True)
                    ),
                ):
                    await operation.create(mock.Mock())

            asyncio.run(run())
            self.assertEqual(os.readlink(old), f"/proc/{os.getpid()}/ns/net")


if __name__ == "__main__":
    unittest.main()
