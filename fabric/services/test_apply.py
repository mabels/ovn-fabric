from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest import mock

from fabric.ir_fixture import NODES
from protocol.hydrate import hydrate_nodes
from runtime import generated as rg
from runtime.message import Msg, plain
from runtime.peer import Call, Peer, RemoteError
from runtime.runtime import Runtime
from runtime.service import Service
from runtime.test_message import IR_SAMPLE
from runtime.transport import connect

from .. import generated as g
from ..desired import desired_rawir
from ..plan import plan_operations, plan_units
from . import abstract_deps, apply, unit
from .apply import Apply, Failed
from .ir import IrStore
from .test_ir import _put

HOST = "net-test-hh-b"  # the host in IR_SAMPLE
PLAN = [u.name for u in plan_units(hydrate_nodes(IR_SAMPLE), HOST)]
DONE, FAILED, SKIPPED = g.ApplyState.done, g.ApplyState.failed, g.ApplyState.skipped


class Fakes:
    """The abstract deps service and the unit executor, recorded."""

    def __init__(self, fail: str | None = None, deps_fail: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.skips: dict[str, list[int]] = {}  # unit -> the steps it was told to skip
        self.fail, self.deps_fail = fail, deps_fail

    async def deps(self, call: Call) -> object:
        self.calls.append(("deps", [d.name for d in call.body.deps]))
        state = g.SystemDepState.failed if self.deps_fail else g.SystemDepState.ready
        return g.AbstractDepsRes(
            kind="AbstractDepsRes",
            deps=[g.AbstractDepStatus(name=d.name, state=state, deps=[]) for d in call.body.deps],
        )

    async def unit(self, call: Call) -> object:
        req = call.body
        inputs = {v.name: json.loads(v.json) for v in req.inputs or []}
        self.calls.append((req.unit.name, (req.action.value, inputs, req.dryRun)))
        self.skips[req.unit.name] = list(req.skip or [])
        await call.log("info", f"{req.action.value} unit: {req.unit.name}")
        failed = req.unit.name == self.fail
        outputs = {**inputs, f"seen-{len(self.calls)}": req.unit.name}
        return g.UnitExecRes(
            kind="UnitExecRes",
            unit=req.unit.name,
            state=g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done,
            message="boom" if failed else None,
            outputs=[g.UnitValue(name=k, json=json.dumps(v)) for k, v in outputs.items()],
        )

    def services(self) -> list[Service]:
        return [
            Service(
                name="abstract_deps",
                bodies=abstract_deps.BODIES,
                handlers={"AbstractDepsReq": self.deps},
            ),
            Service(name="unit", bodies=unit.BODIES, handlers={"UnitExecReq": self.unit}),
        ]


@contextlib.asynccontextmanager
async def _client(fakes: Fakes, host: str = HOST) -> AsyncIterator[tuple[Peer, Apply]]:
    runtime = Runtime()
    apply = Apply(runtime.client, host)
    runtime.register(IrStore(tempfile.mkdtemp()).service(), apply.service(), *fakes.services())
    stop = asyncio.Event()
    address = f"mem://apply-{id(runtime)}"
    server = asyncio.create_task(runtime.serve(address, stop))
    await asyncio.sleep(0)
    client = Peer(await connect(address), "test")
    reader = asyncio.create_task(client.run())
    try:
        await client.ask(_put("draft", IR_SAMPLE), timeout=5)
        yield client, apply
    finally:
        await client.close()
        await reader
        stop.set()
        await server


def _owned(live: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Live nodes as the collector hands them: each with its owner (ours,
    unless a fixture says else)."""
    return {node_id: {"owner": "ovn-fabric", **node} for node_id, node in live.items()}


def _apply(
    action: str = "create", ref: str = "draft", host: str | None = None, dry: bool = False
) -> g.IrApplyReq:
    return g.IrApplyReq(
        kind="IrApplyReq", ref=ref, action=g.ApplyAction(action), host=host, dryRun=dry or None
    )


async def _run(client: Peer, req: g.IrApplyReq) -> tuple[g.IrApplyRes, list[Msg[Any]]]:
    request = await client.request(req)
    seen = [msg async for msg in request]
    assert isinstance(seen[-1].body, g.IrApplyRes)
    return seen[-1].body, seen


class ApplyTest(unittest.TestCase):
    def test_create_resolves_deps_first_then_runs_the_units_in_order(self) -> None:
        async def run() -> None:
            fakes = Fakes()
            async with _client(fakes) as (client, _a):
                res, seen = await _run(client, _apply())
            self.assertEqual(fakes.calls[0][0], "deps")
            self.assertIn("ovn", fakes.calls[0][1])
            self.assertEqual([c[0] for c in fakes.calls[1:]], PLAN)
            self.assertEqual(
                (res.state, [u.state for u in res.units]),
                (g.IrApplyOutcome.done, [DONE] * len(PLAN)),
            )
            progress = [m.body.unit for m in seen if isinstance(m.body, g.IrApplyProgress)]
            self.assertEqual([p.state for p in progress[:2]], [g.ApplyState.running, DONE])
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            self.assertIn(f"create unit: {PLAN[0]}", logs)  # the executor's logs, relayed

        asyncio.run(run())

    def test_what_a_unit_captures_goes_to_the_next(self) -> None:
        async def run() -> None:
            fakes = Fakes()
            async with _client(fakes) as (client, _a):
                await _run(client, _apply())
            first_inputs, second_inputs = fakes.calls[1][1][1], fakes.calls[2][1][1]
            self.assertEqual(first_inputs, {})
            self.assertEqual(second_inputs, {"seen-2": PLAN[0]})

        asyncio.run(run())

    def test_delete_runs_the_units_rewound_without_deps(self) -> None:
        async def run() -> None:
            fakes = Fakes()
            async with _client(fakes) as (client, _a):
                res, _seen = await _run(client, _apply("delete"))
            self.assertEqual([c[0] for c in fakes.calls], PLAN[::-1])
            self.assertEqual({c[1][0] for c in fakes.calls}, {"delete"})
            self.assertEqual(res.state, g.IrApplyOutcome.done)

        asyncio.run(run())

    def test_a_failed_unit_stops_a_create_but_not_a_delete(self) -> None:
        async def run() -> None:
            for action, expected in (
                ("create", [FAILED, SKIPPED]),
                ("delete", None),
            ):
                fakes = Fakes(fail=PLAN[0])
                async with _client(fakes) as (client, _a):
                    res, _seen = await _run(client, _apply(action))
                states = {u.name: u.state for u in res.units}
                self.assertEqual(res.state, g.IrApplyOutcome.failed)
                if expected is not None:
                    self.assertEqual([states[PLAN[0]], states[PLAN[1]]], expected)
                    self.assertEqual(res.units[0].message, "boom")
                else:  # delete: the failed one is reported, the others still ran
                    self.assertEqual(states[PLAN[1]], DONE)
                    self.assertEqual(len([c for c in fakes.calls if c[0] != "deps"]), len(PLAN))

        asyncio.run(run())

    def test_a_failed_workload_only_skips_itself(self) -> None:
        def unit_(name: str, scope: str) -> g.Unit:
            return g.Unit(name=name, kind="test", scope=scope, steps=[])

        plan = [
            unit_("chassis registration", "configure"),
            unit_("workload: ingress", "ingress"),
            unit_("NIC of ingress", "ingress"),
            unit_("workload: dns", "dns"),
            unit_("NIC of dns", "dns"),
        ]

        async def run() -> None:
            fakes = Fakes(fail="workload: ingress")
            with mock.patch("fabric.services.apply.plan_units", return_value=plan):
                async with _client(fakes) as (client, _a):
                    res, _seen = await _run(client, _apply())
            states = {u.name: u.state for u in res.units}
            self.assertEqual(
                states,
                {
                    "chassis registration": DONE,
                    "workload: ingress": FAILED,
                    "NIC of ingress": SKIPPED,
                    "workload: dns": DONE,  # independent: it still runs
                    "NIC of dns": DONE,
                },
            )
            skipped = next(u for u in res.units if u.state is SKIPPED)
            self.assertEqual(skipped.message, "needs workload: ingress, which failed")
            self.assertEqual(res.state, g.IrApplyOutcome.failed)

        asyncio.run(run())

    def test_unready_deps_fail_the_create_before_any_unit(self) -> None:
        async def run() -> None:
            fakes = Fakes(deps_fail=True)
            async with _client(fakes) as (client, _a):
                res, _seen = await _run(client, _apply())
            self.assertEqual((res.state, res.units), (g.IrApplyOutcome.failed, []))
            self.assertEqual([c[0] for c in fakes.calls], ["deps"])

        asyncio.run(run())

    def test_a_dry_run_installs_nothing_and_asks_the_executor_to_describe(self) -> None:
        async def run() -> None:
            fakes = Fakes()
            async with _client(fakes) as (client, _a):
                res, _seen = await _run(client, _apply(dry=True))
            self.assertNotIn("deps", [c[0] for c in fakes.calls])
            self.assertEqual({c[1][2] for c in fakes.calls}, {True})
            self.assertEqual(res.state, g.IrApplyOutcome.done)

        asyncio.run(run())

    def test_an_unknown_ref_or_a_foreign_host_is_refused(self) -> None:
        async def run() -> None:
            async with _client(Fakes()) as (client, _a):
                for req, code in (
                    (_apply(ref="nope"), "not-found"),
                    (_apply(host="elsewhere"), "bad-request"),
                ):
                    with self.subTest(code=code), self.assertRaises(RemoteError) as caught:
                        await client.ask(req, timeout=5)
                    self.assertEqual(caught.exception.code, code)

        asyncio.run(run())

    def test_one_apply_at_a_time(self) -> None:
        async def run() -> None:
            async with _client(Fakes()) as (client, apply_service):
                async with apply_service._lock:  # an apply is running
                    second = await client.request(_apply(dry=True))
                    await asyncio.sleep(0.05)
                    self.assertIsNone(second._final)  # it waits
                final = await second.wait(timeout=5)
                self.assertEqual(final.body.state, g.IrApplyOutcome.done)

        asyncio.run(run())


class DeployedTest(unittest.TestCase):
    """The store's name `deployed`: the IR on this host."""

    def test_create_names_it_deployed_and_its_delete_drops_the_name(self) -> None:
        async def run() -> None:
            async with _client(Fakes()) as (client, _apply_service):
                await _run(client, _apply("create"))
                got = await client.ask(g.IrGetReq(kind="IrGetReq", ref="deployed"), timeout=5)
                draft = await client.ask(g.IrGetReq(kind="IrGetReq", ref="draft"), timeout=5)
                self.assertEqual(got.body.info.hash, draft.body.info.hash)
                await _run(client, _apply("delete"))
                with self.assertRaises(RemoteError):
                    await client.ask(g.IrGetReq(kind="IrGetReq", ref="deployed"), timeout=5)
                # the IR itself stays: its own name still points to it
                await client.ask(g.IrGetReq(kind="IrGetReq", ref="draft"), timeout=5)

        asyncio.run(run())

    def test_a_delete_with_keep_deployed_keeps_the_name(self) -> None:
        async def run() -> None:
            async with _client(Fakes()) as (client, _apply_service):
                await _run(client, _apply("create"))
                deployed = await client.ask(g.IrGetReq(kind="IrGetReq", ref="deployed"), timeout=5)
                await _run(client, dataclasses.replace(_apply("delete"), keepDeployed=True))
                kept = await client.ask(g.IrGetReq(kind="IrGetReq", ref="deployed"), timeout=5)
                self.assertEqual(kept.body.info.hash, deployed.body.info.hash)

        asyncio.run(run())

    def test_a_dry_run_deploys_nothing(self) -> None:
        async def run() -> None:
            async with _client(Fakes()) as (client, _apply_service):
                await _run(client, _apply("create", dry=True))
                with self.assertRaises(RemoteError):
                    await client.ask(g.IrGetReq(kind="IrGetReq", ref="deployed"), timeout=5)

        asyncio.run(run())

    def test_run_dry_run_reports_the_deployed_ir_in_rawir(self) -> None:
        async def run() -> None:
            live = {
                "ovn.ls:elsewhere": {"id": "ovn.ls:elsewhere", "kind": "ovn.ls"},
                "link:*global*|docker0": {"id": "x", "kind": "link", "owner": "docker"},
            }
            async with _client(Fakes()) as (client, _apply_service):
                await _run(client, _apply("create"))
                with mock.patch("fabric.observed.snapshot", return_value=_owned(live)):
                    res, seen = await _run(client, _apply("run", ref="deployed", dry=True))
                self.assertEqual(res.state, g.IrApplyOutcome.done)
                self.assertEqual(res.units, [])
                logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
                self.assertTrue(any("RawIR nodes" in line for line in logs), logs)
                # someone else's: counted, not compared
                self.assertIn("left alone, someone else's: docker 1", logs)
                self.assertTrue(any("only observed 1" in line for line in logs), logs)

        asyncio.run(run())


CENTRAL = "central-1"
LS_BACKBONE = {
    "id": "ovn.ls:backbone",
    "kind": "ovn.ls",
    "name": "backbone",
    "externalIds": {},
    "portRefs": [],
}


def _central_ir() -> list[dict[str, Any]]:
    """The deployer fixtures' IR (it has logical switches), as plain JSON."""
    return [cast(dict[str, Any], plain(node)) for node in NODES]


class FailedTest(unittest.TestCase):
    def _unit(self, name: str, scope: str) -> g.Unit:
        return g.Unit(name=name, kind="x", scope=scope, steps=[])

    def test_an_unchecked_unit_holds_back_its_scope_not_the_foundation(self) -> None:
        failed = Failed()
        failed.hold(self._unit("workload: w", "w"))
        failed.hold(self._unit("root namespace: h", "configure"))
        self.assertTrue(failed.blocks(self._unit("NIC: n", "w")))
        self.assertEqual(
            failed.why(self._unit("NIC: n", "w")), "needs workload: w, which run cannot check"
        )
        self.assertFalse(failed.blocks(self._unit("workload: other", "other")))
        self.assertFalse(failed.blocks(self._unit("logical switch: s", "cluster")))


class RunTest(unittest.TestCase):
    """run: per unit, from the live RawIR — run what is gone, leave what is
    there (fabric/desired.py's choose)."""

    def _run_on(self, live: dict[str, dict[str, Any]]) -> tuple[Fakes, g.IrApplyRes, list[str]]:
        async def run() -> tuple[Fakes, g.IrApplyRes, list[str]]:
            fakes = Fakes()
            async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                await client.ask(_put("central", _central_ir()), timeout=5)
                with mock.patch("fabric.observed.snapshot", return_value=_owned(live)):
                    res, seen = await _run(client, _apply("run", ref="central"))
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            return fakes, res, logs

        return asyncio.run(run())

    @staticmethod
    def _all_there() -> dict[str, dict[str, Any]]:
        """The fixtures' desired RawIR — plus the router port whose chassis
        is only known at run time (desired_rawir cannot predict it)."""
        live = desired_rawir(plan_operations(NODES, CENTRAL)).nodes
        port = "ovn.lrp:router-home|lrp-router-home-left"
        live[port] = {"id": port, "kind": "ovn.lrp"}
        return live

    def test_what_is_there_is_left_alone_only_reads_run(self) -> None:
        live = self._all_there()
        fakes, res, logs = self._run_on(live)
        self.assertEqual(res.state, g.IrApplyOutcome.done)
        ran = [name for name, _ in fakes.calls]
        self.assertIn("chassis id: chassis-1 (resolved on this host)", ran)  # a read
        self.assertNotIn("logical switch: backbone", ran)
        # the root netns runs nothing: as a read, it does no harm
        self.assertIn("root namespace: central-1", ran)
        self.assertNotIn("router port: lrp-router-home-left (home)", ran)
        self.assertTrue(any(line.startswith("units: ") for line in logs), logs)

    def test_what_differs_is_replaced_before_the_units_run(self) -> None:
        # ADR 0011 step 4: a router port there with another MAC is removed
        # first; its unit then finds it missing and makes it again.
        port = "ovn.lrp:router-home|lrp-router-home-right"
        live = self._all_there()
        live[port] = {**live[port], "mac": "00:00:00:00:09:09"}
        after = {k: v for k, v in live.items() if k != port}

        async def run() -> tuple[Fakes, g.IrApplyRes, list[str]]:
            fakes = Fakes()
            async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                await client.ask(_put("central", _central_ir()), timeout=5)
                with mock.patch(
                    "fabric.observed.snapshot", side_effect=[_owned(live), _owned(after)]
                ):
                    res, seen = await _run(client, _apply("run", ref="central"))
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            return fakes, res, logs

        fakes, res, logs = asyncio.run(run())
        self.assertEqual(res.state, g.IrApplyOutcome.done)
        self.assertIn(f"replace {port} (differs in mac)", logs)
        ran = [name for name, _ in fakes.calls]
        self.assertLess(
            ran.index("replace what differs"),
            ran.index("router port: lrp-router-home-right (backbone)"),
        )

    def test_with_no_cleanup_nothing_is_replaced(self) -> None:
        port = "ovn.lrp:router-home|lrp-router-home-right"
        live = self._all_there()
        live[port] = {**live[port], "mac": "00:00:00:00:09:09"}

        async def run() -> tuple[Fakes, list[str]]:
            fakes = Fakes()
            async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                await client.ask(_put("central", _central_ir()), timeout=5)
                with mock.patch("fabric.observed.snapshot", return_value=_owned(live)):
                    req = _apply("run", ref="central")
                    req = dataclasses.replace(req, noCleanup=True)
                    _res, seen = await _run(client, req)
            return fakes, [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]

        fakes, logs = asyncio.run(run())
        self.assertIn("replacing what differs skipped (--no-cleanup)", logs)
        self.assertNotIn("replace what differs", [name for name, _ in fakes.calls])

    def test_a_unit_that_is_gone_runs_completely(self) -> None:
        live = self._all_there()
        del live["ovn.ls:backbone"]
        fakes, res, logs = self._run_on(live)
        self.assertEqual(res.state, g.IrApplyOutcome.done)
        ran = dict(fakes.calls)
        self.assertEqual(ran["logical switch: backbone"][0], "create")  # run creates
        self.assertEqual(fakes.skips["logical switch: backbone"], [])  # all of it
        self.assertNotIn("logical switch: home", ran)
        graph = next(line for line in logs if line.startswith("reconciled graph:"))
        self.assertIn("applied 1, missing 0", graph)

    def _cleaned(
        self, fail: str | None = None, **req: Any
    ) -> tuple[Fakes, g.IrApplyRes, list[str]]:
        leftover = {"id": "ovn.ls:gone", "kind": "ovn.ls", "name": "gone"}
        live = {**self._all_there(), "ovn.ls:gone": leftover}

        async def run() -> tuple[Fakes, g.IrApplyRes, list[str]]:
            fakes = Fakes(fail=fail)
            async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                await client.ask(_put("central", _central_ir()), timeout=5)
                with mock.patch("fabric.observed.snapshot", return_value=_owned(live)):
                    res, seen = await _run(
                        client, dataclasses.replace(_apply("run", ref="central"), **req)
                    )
            return fakes, res, [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]

        return asyncio.run(run())

    def test_leftovers_are_removed_at_the_end_by_default(self) -> None:
        fakes, res, logs = self._cleaned()
        self.assertEqual(res.state, g.IrApplyOutcome.done)
        self.assertEqual(fakes.calls[-1][0], "cleanup: leftovers")
        self.assertEqual(fakes.calls[-1][1][0], "delete")
        self.assertIn("cleanup: remove ovn.ls:gone", logs)
        self.assertNotIn("cleanup: remove ovn.ls:backbone", logs)  # fulfilled by a step

    def test_a_run_names_its_ir_deployed(self) -> None:
        async def run() -> None:
            async with _client(Fakes(), host=CENTRAL) as (client, _apply_service):
                put = await client.ask(_put("central", _central_ir()), timeout=5)
                with mock.patch("fabric.observed.snapshot", return_value=_owned({})):
                    await _run(client, _apply("run", ref="central"))
                got = await client.ask(g.IrGetReq(kind="IrGetReq", ref="deployed"), timeout=5)
                self.assertEqual(got.body.info.hash, put.body.hash)

        asyncio.run(run())

    def test_no_cleanup_keeps_them(self) -> None:
        fakes, _res, logs = self._cleaned(noCleanup=True)
        self.assertNotIn("cleanup: leftovers", [name for name, _ in fakes.calls])
        self.assertIn("leftovers kept (--no-cleanup)", logs)

    def test_a_failed_unit_keeps_them(self) -> None:
        fakes, res, logs = self._cleaned(fail="chassis id: chassis-1 (resolved on this host)")
        self.assertEqual(res.state, g.IrApplyOutcome.failed)
        self.assertNotIn("cleanup: leftovers", [name for name, _ in fakes.calls])
        self.assertIn("a unit failed: leftovers are not removed", logs)

    def test_create_and_delete_are_unconditional(self) -> None:
        # Only run looks at the live RawIR: create and delete run every
        # step, also where everything is there (or gone) already.
        for action in ("create", "delete"):
            with self.subTest(action=action):

                async def run(action: str = action) -> Fakes:
                    fakes = Fakes()
                    async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                        await client.ask(_put("central", _central_ir()), timeout=5)
                        with mock.patch(
                            "fabric.observed.snapshot", return_value=_owned(self._all_there())
                        ) as snapshot:
                            await _run(client, _apply(action, ref="central"))
                        self.assertFalse(snapshot.called)
                    return fakes

                fakes = asyncio.run(run())
                self.assertIn("logical switch: backbone", fakes.skips)
                self.assertTrue(all(skip == [] for skip in fakes.skips.values()))

    def test_without_the_live_view_run_changes_nothing(self) -> None:
        async def run() -> None:
            fakes = Fakes()
            async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                await client.ask(_put("central", _central_ir()), timeout=5)
                with mock.patch("fabric.observed.snapshot", side_effect=OSError("no ip")):
                    with self.assertRaisesRegex(RemoteError, "cannot collect the live RawIR"):
                        await client.ask(_apply("run", ref="central"), timeout=5)
            self.assertEqual([c[0] for c in fakes.calls], ["deps"])  # no unit ran

        asyncio.run(run())

    def test_run_resolves_the_dependencies_first(self) -> None:
        async def run() -> None:
            fakes = Fakes(deps_fail=True)
            async with _client(fakes, host=CENTRAL) as (client, _apply_service):
                await client.ask(_put("central", _central_ir()), timeout=5)
                with (
                    mock.patch("fabric.observed.snapshot", return_value={}) as snapshot,
                    # the fixtures' host declares none: give it one
                    mock.patch.object(
                        apply, "abstract_deps", return_value=[{"name": "ovn-central"}]
                    ),
                ):
                    res, _seen = await _run(client, _apply("run", ref="central"))
            self.assertEqual((res.state, res.units), (g.IrApplyOutcome.failed, []))
            self.assertEqual([c[0] for c in fakes.calls], ["deps"])
            self.assertFalse(snapshot.called)  # not ready: nothing collected either

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
