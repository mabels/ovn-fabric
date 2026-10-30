from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from deployer.primitives_host import HostPrimitive
from runtime.message import BodySpec, Msg, from_parts, header, register_bodies, to_parts
from runtime.peer import Peer
from runtime.runtime import Runtime
from runtime.transport import connect

from .. import generated as g
from ..units import to_operation, to_step
from .unit import UnitExecutor, component_name

CREATE, DELETE = g.ApplyAction.create, g.ApplyAction.delete
DONE, FAILED = g.IrApplyOutcome.done, g.IrApplyOutcome.failed


def _cmd(*argv: g.Arg) -> g.StepCommand:
    return g.StepCommand(label="test", argv=list(argv))


def _logged(log: Path, create: str, delete: str, key: str | None = None) -> g.PrimitiveStep:
    """A primitive whose create/delete append a word to `log`."""
    script = f'echo "$0" >> {log}'
    return g.PrimitiveStep(
        kind="Primitive",
        key=key or create,
        nodes=[],
        create=[_cmd("sh", "-c", script, create)],
        delete=[_cmd("sh", "-c", script, delete)],
    )


def _unit(*steps: g.UnitStepSpec) -> g.Unit:
    return g.Unit(name="test: unit", kind="test", steps=list(steps))


@contextlib.asynccontextmanager
async def _client() -> AsyncIterator[Peer]:
    runtime = Runtime()
    runtime.register(UnitExecutor("h").service())
    stop = asyncio.Event()
    address = f"mem://unit-{id(runtime)}"
    server = asyncio.create_task(runtime.serve(address, stop))
    await asyncio.sleep(0)
    client = Peer(await connect(address), "test")
    reader = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        await client.close()
        await reader
        stop.set()
        await server


async def _exec(client: Peer, unit: g.Unit, action: g.ApplyAction, **kw: Any) -> list[Msg[Any]]:
    request = await client.request(
        g.UnitExecReq(kind="UnitExecReq", unit=unit, action=action, **kw)
    )
    return [msg async for msg in request]


class SerializationTest(unittest.TestCase):
    def test_steps_round_trip_through_the_lifecycle_operations(self) -> None:
        steps: list[g.UnitStepSpec] = [
            _logged(Path("/tmp/x"), "a", "-a"),
            g.WriteFileStep(kind="WriteFile", path="/tmp/f", content="x", mode=0o600),
            g.CaptureStep(
                kind="Capture", name="pid", label="pid", argv=["docker", "inspect", "c"], retries=3
            ),
            g.SymlinkNetnsStep(kind="SymlinkNetns", container="c", pid=g.RefArg(ref="pid")),
            g.DirectoryStep(kind="Directory", path="/etc/wireguard"),
            g.DirectoryStep(kind="Directory", path="/var/lib/x", owned=True),
            g.ChmodStep(kind="Chmod", path="/etc/wireguard/x.conf", mode="600"),
            g.RemovePathStep(kind="RemovePath", path="/var/lib/y", recursive=True),
        ]
        for step in steps:
            with self.subTest(kind=step.kind):
                self.assertEqual(to_step(to_operation(step)), step)

    def test_a_typed_primitive_becomes_a_primitive_step(self) -> None:
        step = to_step(HostPrimitive("h").ovn_nbctl_ls("backbone"))
        assert isinstance(step, g.PrimitiveStep)
        self.assertEqual(step.create[0].argv[:2], ["ovn-nbctl", "ls-add"])
        self.assertIn("backbone", step.create[0].argv)
        self.assertTrue(step.delete)  # its own inverse goes along

    def test_a_unit_travels_as_a_message(self) -> None:
        register_bodies({"UnitExecReq": BodySpec(g.UnitExecReq, "request")})
        unit = _unit(
            _logged(Path("/tmp/x"), "a", "-a"),
            g.SymlinkNetnsStep(kind="SymlinkNetns", container="c", pid=g.RefArg(ref="pid")),
        )
        msg = Msg(
            header=header("t", req_id="r"),
            body=g.UnitExecReq(kind="UnitExecReq", unit=unit, action=CREATE),
        )
        self.assertEqual(from_parts(json.loads(json.dumps(to_parts(msg)))), msg)


class ComponentNameTest(unittest.TestCase):
    def test_a_unit_name_becomes_one_token(self) -> None:
        for name, token in (
            ("NIC management-v2 on br-d0868885", "NIC_management-v2_on_br-d0868885"),
            ("router port: lrp-router-home-left (home)", "router_port_lrp-router-home-left_home"),
            ("chassis registration: net-test-hh-b", "chassis_registration_net-test-hh-b"),
        ):
            self.assertEqual(component_name(name), token)


class ExecuteTest(unittest.TestCase):
    def test_create_runs_forward_and_delete_rewound(self) -> None:
        async def run() -> None:
            log = Path(tempfile.mkdtemp()) / "log"
            unit = _unit(_logged(log, "a", "-a"), _logged(log, "b", "-b"))
            async with _client() as client:
                created = await _exec(client, unit, CREATE)
                self.assertEqual(log.read_text().split(), ["a", "b"])
                deleted = await _exec(client, unit, DELETE)
            self.assertEqual(log.read_text().split(), ["a", "b", "-b", "-a"])
            for seen in (created, deleted):
                self.assertEqual(seen[-1].body.state, DONE)
            # every command reached the requester — from the unit
            logs = [m.body for m in created if isinstance(m.body, g.LogMsg)]
            self.assertTrue(any(log.message.startswith("$ sh -c") for log in logs))
            self.assertEqual({log.component for log in logs}, {"test_unit"})

        asyncio.run(run())

    def test_a_capture_feeds_a_ref_and_comes_back_as_output(self) -> None:
        async def run() -> None:
            log = Path(tempfile.mkdtemp()) / "log"
            capture = g.CaptureStep(kind="Capture", name="pid", label="pid", argv=["echo", "4242"])
            use = g.PrimitiveStep(
                kind="Primitive",
                key="use",
                nodes=[],
                create=[
                    _cmd(
                        "sh",
                        "-c",
                        f'echo "$0 $1" >> {log}',
                        g.RefArg(ref="pid"),
                        g.RefArg(ref="given"),
                    )
                ],
                delete=[],
            )
            given = g.UnitValue(name="given", json=json.dumps("from-before"))
            async with _client() as client:
                seen = await _exec(client, _unit(capture, use), CREATE, inputs=[given])
            self.assertEqual(log.read_text().split(), ["4242", "from-before"])
            outputs = {v.name: json.loads(v.json) for v in seen[-1].body.outputs}
            self.assertEqual(outputs, {"pid": "4242", "given": "from-before"})

        asyncio.run(run())

    def test_create_stops_at_the_first_failure_delete_keeps_going(self) -> None:
        async def run() -> None:
            log = Path(tempfile.mkdtemp()) / "log"
            broken = g.PrimitiveStep(
                kind="Primitive",
                key="broken",
                nodes=[],
                create=[_cmd("false")],
                delete=[_cmd("false")],
            )
            unit = _unit(_logged(log, "a", "-a"), broken, _logged(log, "c", "-c"))
            async with _client() as client:
                with self.assertLogs("fabric.unit", "WARNING"):
                    created = (await _exec(client, unit, CREATE))[-1].body
                self.assertEqual((created.state, log.read_text().split()), (FAILED, ["a"]))
                self.assertIn("false", created.message)
                with self.assertLogs("fabric.unit", "WARNING"):
                    deleted = (await _exec(client, unit, DELETE))[-1].body
            self.assertEqual(deleted.state, DONE)  # best effort: reported, not fatal
            self.assertEqual(log.read_text().split(), ["a", "-c", "-a"])

        asyncio.run(run())

    def test_write_file_overwrites_and_is_deleted(self) -> None:
        async def run() -> None:
            path = Path(tempfile.mkdtemp()) / "f"
            path.write_text("left over")
            unit = _unit(g.WriteFileStep(kind="WriteFile", path=str(path), content="x"))
            async with _client() as client:
                self.assertEqual((await _exec(client, unit, CREATE))[-1].body.state, DONE)
                self.assertEqual(path.read_text(), "x\n")
                again = (await _exec(client, unit, CREATE))[-1].body
                self.assertEqual(again.state, DONE)  # overwritten again
                await _exec(client, unit, DELETE)
            self.assertFalse(path.exists())

        asyncio.run(run())

    def test_the_netns_of_a_container_that_is_not_running_is_not_linked(self) -> None:
        async def run() -> None:
            capture = g.CaptureStep(kind="Capture", name="pid", label="pid", argv=["echo", "0"])
            link = g.SymlinkNetnsStep(
                kind="SymlinkNetns", container="rt-test-exited", pid=g.RefArg(ref="pid")
            )
            async with _client() as client:
                with self.assertLogs("fabric.unit", "WARNING"):
                    res = (await _exec(client, _unit(capture, link), CREATE))[-1].body
            self.assertEqual(res.state, FAILED)
            self.assertIn("container rt-test-exited is not running", res.message)
            self.assertFalse(Path("/var/run/netns/rt-test-exited").exists())

        asyncio.run(run())

    def test_dry_run_describes_and_runs_nothing(self) -> None:
        async def run() -> None:
            log = Path(tempfile.mkdtemp()) / "log"
            unit = _unit(_logged(log, "a", "-a"), _logged(log, "b", "-b"))
            async with _client() as client:
                seen = await _exec(client, unit, DELETE, dryRun=True)
            self.assertFalse(log.exists())
            logs = [m.body.message for m in seen if isinstance(m.body, g.LogMsg)]
            self.assertEqual(logs[0], "delete (2 steps)")
            lines = [line for line in logs if " --- " in line]  # the described operations
            self.assertEqual(len(lines), 2)
            self.assertIn("-b", lines[0])  # rewound
            self.assertIn("-a", lines[1])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
