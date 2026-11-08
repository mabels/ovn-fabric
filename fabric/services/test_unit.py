from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import tempfile
import typing
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest import mock

from runtime import generated as rg
from runtime.message import BodySpec, Msg, from_parts, header, register_bodies, to_parts
from runtime.peer import Peer
from runtime.runtime import Runtime
from runtime.transport import connect

from .. import decide
from .. import generated as g
from ..primitives.host import HostPrimitive
from ..units import _KINDS, to_operation, to_step
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

    def test_every_step_kind_of_the_schema_has_its_entry(self) -> None:
        schema = {
            typing.get_args(typing.get_type_hints(cls)["kind"])[0]
            for cls in typing.get_args(g.UnitStepSpec)
        }
        self.assertEqual(set(_KINDS), schema)

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
            logs = [m.body for m in created if isinstance(m.body, rg.LogMsg)]
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

    def test_a_failed_capture_says_why_from_its_stderr(self) -> None:
        async def run() -> None:
            script = "echo noise; echo 'Error: No such object: c' >&2; exit 1"
            capture = g.CaptureStep(
                kind="Capture", name="pid", label="pid", argv=["sh", "-c", script]
            )
            async with _client() as client:
                with self.assertLogs("fabric.unit", "WARNING"):
                    seen = await _exec(client, _unit(capture), CREATE)
            res = seen[-1].body
            self.assertEqual(res.state, FAILED)
            self.assertTrue(res.message.endswith("failed: Error: No such object: c"))
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            self.assertIn("Error: No such object: c", logs)  # in the unit's debug output too

        asyncio.run(run())

    def test_a_command_running_too_long_is_killed_and_fails_its_unit(self) -> None:
        async def run() -> None:
            slow = g.PrimitiveStep(
                kind="Primitive",
                key="slow",
                nodes=[],
                create=[g.StepCommand(label="slow", argv=["sleep", "60"])],
                delete=[],
            )
            own = g.PrimitiveStep(
                kind="Primitive",
                key="own",
                nodes=[],
                create=[g.StepCommand(label="own", argv=["sleep", "0.5"], timeout=0.2)],
                delete=[],
            )
            async with _client() as client:
                with self.assertLogs("fabric.unit", "WARNING"):
                    # The run's limit …
                    res = (await _exec(client, _unit(slow), CREATE, commandTimeout=0.2))[-1].body
                    self.assertEqual(res.state, FAILED)
                    self.assertIn("timed out after 0.2s: sleep 60", res.message)
                    # … and a command's own, over the run's.
                    res = (await _exec(client, _unit(own), CREATE, commandTimeout=60))[-1].body
                    self.assertEqual(res.state, FAILED)
                    self.assertIn("timed out after 0.2s: sleep 0.5", res.message)

        asyncio.run(run())

    def test_skipped_steps_do_not_run(self) -> None:
        async def run() -> None:
            log = Path(tempfile.mkdtemp()) / "log"
            unit = _unit(_logged(log, "a", "-a"), _logged(log, "b", "-b"))
            async with _client() as client:
                res = (await _exec(client, unit, CREATE, skip=[0]))[-1].body
            self.assertEqual(res.state, DONE)
            self.assertEqual(log.read_text().split(), ["b"])  # step 0 is already there

        asyncio.run(run())

    def test_dry_run_describes_and_runs_nothing(self) -> None:
        async def run() -> None:
            log = Path(tempfile.mkdtemp()) / "log"
            unit = _unit(_logged(log, "a", "-a"), _logged(log, "b", "-b"))
            async with _client() as client:
                seen = await _exec(client, unit, DELETE, dryRun=True)
            self.assertFalse(log.exists())
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            self.assertEqual(logs[0], "delete (2 steps)")
            lines = [line for line in logs if " --- " in line]  # the described operations
            self.assertEqual(len(lines), 2)
            self.assertIn("-b", lines[0])  # rewound
            self.assertIn("-a", lines[1])

        asyncio.run(run())


class DecideTest(unittest.TestCase):
    """A step with its own decider (fabric/decide.py): run decides it in
    the executor, create applies it."""

    def test_run_skips_applies_and_replaces_as_decided_create_applies(self) -> None:
        by_key = {"s": decide.Decision.skip, "r": decide.Decision.replace}

        async def run(log: Path) -> tuple[list[str], list[str], Any]:
            steps = []
            for word in ("s", "r", "a"):
                step = _logged(log, word, f"-{word}")
                step.decide = g.AlwaysDecide(kind="always")
                steps.append(step)
            unit = _unit(*steps)

            async def decider(spec: Any, decided: decide.Decided, lc: Any) -> decide.Decision:
                # the step's key is the word: decide by it
                key = next(s.key for s in steps if s.key not in decided.by_key)
                return by_key.get(key, decide.Decision.apply)

            with mock.patch.dict(decide.DECIDERS, {"always": decider}):
                async with _client() as client:
                    res = (await _exec(client, unit, CREATE, decide=True))[-1].body
                    ran = log.read_text().split()
                    log.write_text("")
                    await _exec(client, unit, CREATE)
                    created = log.read_text().split()
            return ran, created, res

        with tempfile.TemporaryDirectory() as tmp:
            ran, created, res = asyncio.run(run(Path(tmp) / "log"))
        self.assertEqual(ran, ["-r", "r", "a"])  # s skipped; r undone, then done
        self.assertEqual(created, ["s", "r", "a"])  # create: every step, undecided
        self.assertEqual(
            [(d.index, d.decision.value) for d in res.decisions],
            [(0, "skip"), (1, "replace"), (2, "apply")],
        )


class WireguardDeciderTest(unittest.TestCase):
    def _decide(self, running: str, on_disk: str) -> decide.Decision:
        spec = g.WireguardDecide(
            kind="wireguard", netns="ns", iface="wg0", confPath="/etc/wg0.conf", confSha256="abc"
        )

        class Probe:
            async def capture_cmd(self, label: str, argv: Any, **kw: Any) -> str:
                return running if "wg" in argv else on_disk

        return asyncio.run(decide.decide(spec, decide.Decided(), Probe()))  # type: ignore[arg-type]

    def test_the_spec_hashes_the_config_as_its_file_is_written(self) -> None:
        tunnel = HostPrimitive("h").netns("ns").wireguard("wg0", "/etc/wg0.conf", "[Interface]")
        step = to_step(tunnel, decide.spec(decide.Decide.wireguard, tunnel))
        assert step.kind == "Primitive" and step.decide is not None
        self.assertEqual(
            step.decide.confSha256,  # type: ignore[union-attr]
            hashlib.sha256(b"[Interface]\n").hexdigest(),  # WriteFile adds the newline
        )
        self.assertIsNone(to_step(tunnel).decide)  # type: ignore[union-attr]

    def test_down_applies_same_config_skips_other_config_replaces(self) -> None:
        self.assertEqual(self._decide("", ""), decide.Decision.apply)
        self.assertEqual(self._decide("wg0", "abc  /etc/wg0.conf"), decide.Decision.skip)
        self.assertEqual(self._decide("wg1 wg0", "def  /etc/wg0.conf"), decide.Decision.replace)


class _Probe:
    """A controller answering a decider's probes by the command's words."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.ran: list[list[str]] = []

    async def capture_cmd(self, label: str, argv: Any, **kw: Any) -> str:
        return next((out for word, out in self.answers.items() if word in argv), "")

    async def run_cmd(self, label: str, argv: Any, **kw: Any) -> bool:
        self.ran.append(list(argv))
        return True


def _decided(
    spec: Any, probe: _Probe, earlier: dict[str, decide.Decision] | None = None
) -> decide.Decision:
    handle = decide.Decided(dict(earlier or {}))
    return asyncio.run(decide.decide(spec, handle, probe))  # type: ignore[arg-type]


class DockerDeciderTest(unittest.TestCase):
    container = g.DockerContainerDecide(kind="docker.container", name="c", image="img", spec="s1")

    def test_a_container_runs_restarts_or_stays(self) -> None:
        self.assertEqual(_decided(self.container, _Probe({})), decide.Decision.apply)
        same = _Probe(
            {"{{.State.Running}} {{.Image}} " + decide._LABEL: "true sha:1 s1", "{{.Id}}": "sha:1"}
        )
        self.assertEqual(_decided(self.container, same), decide.Decision.skip)
        other_spec = _Probe({"c": "true sha:1 s0", "{{.Id}}": "sha:1"})
        self.assertEqual(_decided(self.container, other_spec), decide.Decision.replace)
        new_image = _Probe(
            {"{{.State.Running}} {{.Image}} " + decide._LABEL: "true sha:1 s1", "{{.Id}}": "sha:2"}
        )
        self.assertEqual(_decided(self.container, new_image), decide.Decision.replace)
        # its image is built anew in this unit
        built = {"docker.image:img": decide.Decision.apply}
        self.assertEqual(_decided(self.container, same, built), decide.Decision.replace)

    def test_an_image_is_built_when_its_dockerfile_changed_a_pull_pulls(self) -> None:
        image = g.DockerImageDecide(kind="docker.image", image="img", spec="h1")
        self.assertEqual(_decided(image, _Probe({"inspect": "h1"})), decide.Decision.skip)
        self.assertEqual(_decided(image, _Probe({"inspect": "h0"})), decide.Decision.apply)
        probe = _Probe({})
        pull = g.DockerPullDecide(kind="docker.pull", image="img")
        self.assertEqual(_decided(pull, probe), decide.Decision.skip)
        self.assertEqual(probe.ran, [["/usr/bin/docker", "pull", "img"]])

    def test_an_exec_runs_with_its_container_started(self) -> None:
        exec_ = g.DockerExecDecide(kind="docker.exec", container="c")
        for container, expected in (
            (decide.Decision.apply, decide.Decision.apply),
            (decide.Decision.replace, decide.Decision.apply),
            (decide.Decision.skip, decide.Decision.skip),
        ):
            got = _decided(exec_, _Probe({}), {"docker.container:c": container})
            self.assertEqual(got, expected)

    def test_a_fresh_container_runs_the_unit_s_later_steps_again(self) -> None:
        async def run(log: Path) -> list[str]:
            container = _logged(log, "container", "-container")
            container.decide = g.DockerContainerDecide(
                kind="docker.container", name="c", image="img", spec="s"
            )
            address = _logged(log, "address", "-address")  # found by apply: in skip
            unit = _unit(container, address)

            async def fresh(spec: Any, decided: decide.Decided, lc: Any) -> decide.Decision:
                return decide.Decision.apply

            with mock.patch.dict(decide.DECIDERS, {"docker.container": fresh}):
                async with _client() as client:
                    await _exec(client, unit, CREATE, decide=True, skip=[1])
            return log.read_text().split()

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(asyncio.run(run(Path(tmp) / "log")), ["container", "address"])


if __name__ == "__main__":
    unittest.main()
