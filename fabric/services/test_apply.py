from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from collections.abc import AsyncIterator
from typing import Any
from unittest import mock

from protocol.hydrate import hydrate_nodes
from runtime import generated as rg
from runtime.message import Msg
from runtime.peer import Call, Peer, RemoteError
from runtime.runtime import Runtime
from runtime.service import Service
from runtime.test_message import IR_SAMPLE
from runtime.transport import connect

from .. import generated as g
from ..plan import plan_units
from . import abstract_deps, unit
from .apply import Apply
from .ir import IrStore
from .test_ir import _put

HOST = "net-test-hh-b"  # the host in IR_SAMPLE
PLAN = [u.name for u in plan_units(hydrate_nodes(IR_SAMPLE), HOST)]
DONE, FAILED, SKIPPED = g.ApplyState.done, g.ApplyState.failed, g.ApplyState.skipped


class Fakes:
    """The abstract deps service and the unit executor, recorded."""

    def __init__(self, fail: str | None = None, deps_fail: bool = False) -> None:
        self.calls: list[tuple[str, Any]] = []
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


if __name__ == "__main__":
    unittest.main()
