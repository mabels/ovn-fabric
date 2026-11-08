"""Applying a stored IR on this host.

    IrApplyReq { ref, action: create|delete, host?, dryRun?, commandTimeout? }
      → IrApplyProgress { unit }       as each unit of work changes state
      → IrApplyRes { hash, action, host, state, units }

`ref` is resolved once, at the start, through the IR service (an internal
IrGetReq): what is applied is that hash, even if the name moves meanwhile.
The host is the request's, else this runtime's own (`serve --host`, else
its hostname); it must be one of the IR's infra.hosts. One apply runs at a
time — a second one waits — so a create and a delete never overlap.

What an apply does (the apply task owns the order; the unit executor runs
each unit):

1. create only (not on delete, not dry): the host's abstract dependencies
   (deployer/deps.py) through the abstract deps service — the first thing,
   before any unit;
2. the plan: the IR cut into units of work on this host (fabric/plan.py);
3. the units, one after the other, each as an internal UnitExecReq — in
   plan order on create, rewound on delete (the executor rewinds the steps
   of each). What a unit captures goes to the next ones (its `outputs`
   become their `inputs`). On create a failed unit skips what needs it
   (`skipped`): the rest of its router or workload — or, failing in
   configure or cluster, everything after it. On delete every unit runs.

Progress: IrApplyProgress per unit (running, then how it ended); the
executor's logs — a unit's start at info, its commands at debug — reach
the requester as they come.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from deployer.deps import abstract_deps
from protocol import generated as pt
from protocol.hydrate import hydrate_nodes
from raw_reconciler.owners import OVN_FABRIC, mark_host, ours
from runtime import generated as rg
from runtime.message import BodySpec, ProtocolError, plain, register_bodies
from runtime.peer import Call, Peer, RemoteError
from runtime.service import Service

from .. import cleanup, observed
from .. import generated as g
from ..desired import (
    Reconciled,
    StepState,
    choose,
    compare,
    consumed_later,
    crossed_out,
    desired_rawir,
    missing_nodes,
    step_state,
)
from ..lifecycle import LifecycleAction, resolve
from ..plan import PlanError, plan_operations, plan_units

log = logging.getLogger("fabric.apply")

# The IR store's name for the IR deployed on this host: the last one a create
# or a run was fed (refs.json points it to that IR's hash).
DEPLOYED = "deployed"


@dataclasses.dataclass(frozen=True)
class Pick:
    """run's choice for a unit (see Apply._run): the steps to skip, or
    None to leave it alone; `hold`: left alone unchecked — the units after
    it in its scope wait (Failed.hold)."""

    skip: list[int] | None
    hold: bool = False


Choose = Callable[[g.Unit, dict[str, Any]], Awaitable[Pick]]

BODIES = {
    "IrApplyReq": BodySpec(g.IrApplyReq, "request"),
    "IrApplyProgress": BodySpec(g.IrApplyProgress, "event"),
    "IrApplyRes": BodySpec(g.IrApplyRes, "response"),
}
register_bodies(BODIES)  # clients decode these too


# The scopes every other one needs: a failure there stops a create.
FOUNDATION = ("configure", "cluster")


class Failed:
    """Which units a create can no longer run, after failures: a unit needs
    the units before it in its scope, and every scope needs the foundation
    (configure, cluster). So a failed router or workload only skips the rest
    of itself — a failed ingress does not keep the DNS from coming up."""

    def __init__(self) -> None:
        self._foundation: str | None = None  # the failed foundation unit
        self._scopes: dict[str, str] = {}  # failed scope -> its failed unit
        self._held: dict[str, str] = {}  # scope -> the unit run cannot check

    def add(self, unit: g.Unit) -> None:
        scope = unit.scope or ""
        if scope in FOUNDATION or not scope:
            self._foundation = self._foundation or unit.name
        else:
            self._scopes.setdefault(scope, unit.name)

    def hold(self, unit: g.Unit) -> None:
        """run left `unit` alone, unable to tell whether it is there: the
        units after it in its scope (a workload's NICs) wait — not in the
        foundation, where everything else would wait too."""
        scope = unit.scope or ""
        if scope and scope not in FOUNDATION:
            self._held.setdefault(scope, unit.name)

    def blocks(self, unit: g.Unit) -> bool:
        scope = unit.scope or ""
        return self._foundation is not None or scope in self._scopes or scope in self._held

    def why(self, unit: g.Unit) -> str:
        failed = self._foundation or self._scopes.get(unit.scope or "")
        if failed is None:
            return f"needs {self._held[unit.scope or '']}, which run cannot check"
        return f"needs {failed}, which failed"


class Apply:
    def __init__(self, client: Callable[[], Awaitable[Peer]], host: str | None = None) -> None:
        self._client = client  # the runtime's internal connection, to ask the IR service
        self.host = host or socket.gethostname()
        self._lock = asyncio.Lock()  # one apply at a time

    def service(self) -> Service:
        return Service(name="apply", bodies=BODIES, handlers={"IrApplyReq": self.apply})

    async def apply(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.IrApplyReq)
        info, ir = await self._resolve(call, req.ref)
        host = req.host or self.host
        if host not in info.hosts:
            raise ProtocolError(
                "bad-request",
                f"{host} is not a host of {info.hash} (its hosts: {', '.join(info.hosts) or '-'})",
            )
        if req.action is g.ApplyAction.run and req.dryRun:
            return await self._run_dry(call, req, info, ir, host)
        if self._lock.locked():
            await call.log("info", "another apply runs: waiting for it")
        if req.action is g.ApplyAction.run:
            async with self._lock:
                return await self._converge(call, req, info, ir, host)
        async with self._lock:
            mode = " (dry run)" if req.dryRun else ""
            # The deployer's declarations take the IR as its own types.
            nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
            try:
                plan = plan_units(nodes, host)
            except PlanError as exc:
                raise ProtocolError("bad-ir", str(exc)) from None
            await call.log(
                "info",
                f"{req.action.value} {info.hash} on {host}{mode}: {len(ir.collection)} nodes, "
                f"{len(plan)} units",
            )
            creating = req.action is g.ApplyAction.create
            if creating and not req.dryRun:
                problem = await self._deps(call, nodes, host)
                if problem is not None:
                    await call.log("warn", problem)
                    return self._result(info.hash, req, host, g.IrApplyOutcome.failed, [])
            results = await self._run(call, plan if creating else plan[::-1], req, host)
            if not req.dryRun and (creating or not req.keepDeployed):
                await self._mark_deployed(call, ir, info.hash, creating)
            failed = any(r.state is g.ApplyState.failed for r in results)
            outcome = g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done
            return self._result(info.hash, req, host, outcome, results)

    async def _deps(self, call: Call, nodes: list[pt.Model], host: str) -> str | None:
        """Resolve the host's abstract dependencies; None when all are ready."""
        host_node = next(n for n in nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host)
        deps = abstract_deps(host_node)
        client = await self._client()
        request = await client.request(
            g.AbstractDepsReq(
                kind="AbstractDepsReq",
                # The host's config names it (the IR's infra.host os).
                packageManager=g.PackageManager(host_node.data.os.packageManager.value),
                deps=[g.AbstractDepSpec(name=d["name"]) for d in deps],
            ),
            parent=call.req_id,
        )
        async for event in request:  # its progress, as ours
            if isinstance(event.body, rg.LogMsg):
                await call.emit(event.body)
        res = request.result.body
        if isinstance(res, rg.ErrorRes):
            return f"abstract deps: {res.code}: {res.message}"
        assert isinstance(res, g.AbstractDepsRes)
        missing = [
            f"{s.name} {s.state.value}" for s in res.deps if s.state is not g.SystemDepState.ready
        ]
        return f"dependencies not ready: {', '.join(missing)}" if missing else None

    async def _run(
        self,
        call: Call,
        units: list[g.Unit],
        req: g.IrApplyReq,
        host: str,
        choose: Choose | None = None,
    ) -> list[g.ApplyUnit]:
        """Run `units` through the unit executor. create and delete run every
        step, unconditionally (a create fails on what is already there, a
        delete removes best effort). `choose` is run's: asked right before
        each unit, with the values known by then — which steps to skip, or
        None to leave the unit alone."""
        client = await self._client()
        store: list[g.UnitValue] = []
        results: list[g.ApplyUnit] = []
        failed = Failed()
        for unit in units:
            if req.action is not g.ApplyAction.delete and failed.blocks(unit):
                skipped = g.ApplyUnit(
                    name=unit.name, state=g.ApplyState.skipped, message=failed.why(unit)
                )
                results.append(skipped)
                await self._progress(call, skipped)
                continue
            skip: list[int] = []
            if choose is not None:
                values = {value.name: json.loads(value.json) for value in store}
                chosen = await choose(unit, values)
                if chosen.skip is None:
                    if chosen.hold:
                        failed.hold(unit)
                    left = g.ApplyUnit(name=unit.name, state=g.ApplyState.skipped)
                    results.append(left)
                    await self._progress(call, left)
                    continue
                skip = chosen.skip
            await self._progress(call, g.ApplyUnit(name=unit.name, state=g.ApplyState.running))
            request = await client.request(
                g.UnitExecReq(
                    kind="UnitExecReq",
                    unit=unit,
                    # run converges by creating what is missing
                    action=(
                        g.ApplyAction.create if req.action is g.ApplyAction.run else req.action
                    ),
                    inputs=store,
                    host=host,
                    dryRun=req.dryRun,
                    commandTimeout=req.commandTimeout,
                    skip=skip or None,
                    # run: steps with their own decider are decided there
                    decide=True if req.action is g.ApplyAction.run else None,
                ),
                parent=call.req_id,
            )
            async for event in request:  # the executor's logs, as ours
                if isinstance(event.body, rg.LogMsg):
                    await call.emit(event.body)
            res = request.result.body
            if isinstance(res, rg.ErrorRes):
                result = g.ApplyUnit(
                    name=unit.name, state=g.ApplyState.failed, message=f"{res.code}: {res.message}"
                )
            else:
                assert isinstance(res, g.UnitExecRes)
                store = res.outputs
                done = res.state is g.IrApplyOutcome.done
                result = g.ApplyUnit(
                    name=unit.name,
                    state=g.ApplyState.done if done else g.ApplyState.failed,
                    operations=len(unit.steps),
                    message=res.message,
                )
            results.append(result)
            await self._progress(call, result)
            if result.state is g.ApplyState.failed:
                failed.add(unit)
        return results

    @staticmethod
    async def _progress(call: Call, unit: g.ApplyUnit) -> None:
        await call.emit(g.IrApplyProgress(kind="IrApplyProgress", unit=unit))

    @staticmethod
    def _result(
        digest: str,
        req: g.IrApplyReq,
        host: str,
        outcome: g.IrApplyOutcome,
        units: list[g.ApplyUnit],
    ) -> g.IrApplyRes:
        return g.IrApplyRes(
            kind="IrApplyRes",
            hash=digest,
            action=req.action,
            host=host,
            state=outcome,
            units=units,
        )

    async def _mark_deployed(
        self, call: Call, ir: g.IrCollection, digest: str, creating: bool
    ) -> None:
        """The IR store's name DEPLOYED points to the IR on this host: set
        once a create or a run ran its units (what is on the host belongs to it, even
        where a unit failed), dropped once that IR is deleted again — unless
        the delete asks to keep it (keepDeployed)."""
        client = await self._client()
        if creating:
            await client.ask(
                g.IrPutReq(kind="IrPutReq", name=DEPLOYED, ir=ir), timeout=None, parent=call.req_id
            )
            return
        try:
            deployed, _ = await self._resolve(call, DEPLOYED)
        except ProtocolError:
            return  # nothing deployed
        if deployed.hash == digest:
            await client.ask(
                g.IrDeleteReq(kind="IrDeleteReq", ref=DEPLOYED), timeout=None, parent=call.req_id
            )

    async def _converge(
        self, call: Call, req: g.IrApplyReq, info: g.IrInfo, ir: g.IrCollection, host: str
    ) -> g.IrApplyRes:
        """run: bring the host to its IR — its dependencies first, then unit
        by unit in plan order. Right
        before a unit runs, its steps are checked against the live RawIR
        (their learned footprint, with the values captured so far) and
        `choose` decides: run all of it, only its missing steps, only its
        reads — or leave it alone (fabric/desired.py). At the end, what the
        first look found to be ours and no step fulfilled is removed, unless
        a unit failed or noCleanup asks to keep it."""
        if host != self.host:
            raise ProtocolError("bad-request", f"run needs the live RawIR: only on {self.host}")
        nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
        # First the host's dependencies (as create): the tools the live
        # RawIR is collected with, and the services the plan needs.
        problem = await self._deps(call, nodes, host)
        if problem is not None:
            await call.log("warn", problem)
            return self._result(info.hash, req, host, g.IrApplyOutcome.failed, [])
        try:
            plan = plan_units(nodes, host)
            operations = plan_operations(nodes, host)
        except PlanError as exc:
            raise ProtocolError("bad-ir", str(exc)) from None
        try:
            live = await asyncio.to_thread(observed.snapshot)
        except Exception as exc:  # noqa: BLE001 — without the live view, change nothing
            raise ProtocolError("failed", f"cannot collect the live RawIR: {exc}") from None
        host_node = next(n for n in nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host)
        _mark_host(live, host_node, operations)
        graph = Reconciled.of(ours(live), desired_rawir(operations).nodes)
        by_unit: dict[str, list[Any]] = collections.defaultdict(list)
        for unit_name, operation in operations:
            by_unit[unit_name].append(operation)
        # What later units take away (a link moved into a container).
        later = dict(
            zip(
                (unit.name for unit in plan),
                consumed_later([by_unit[unit.name] for unit in plan], live),
                strict=True,
            )
        )
        # The resolved steps a unit created, that have a footprint.
        ran_of: dict[str, list[Any]] = {}
        choices: collections.Counter[str] = collections.Counter()

        # The values captured so far: what the steps resolve with at the end.
        values_of: dict[str, dict[str, Any]] = {"latest": {}}

        async def choose_unit(unit: g.Unit, values: dict[str, Any]) -> Pick:
            values_of["latest"] = values
            steps = [resolve(operation, values) for operation in by_unit[unit.name]]
            checked = [
                ((StepState.read, {}) if decides else step_state(step, live, later[unit.name]))
                for step, decides in zip(steps, _self_deciding(unit), strict=True)
            ]
            choice = choose([state for state, _ in checked])
            choices[choice.why] += 1
            states = ", ".join(
                f"{type(step).__name__} {state.value}"
                for step, (state, _) in zip(steps, checked, strict=True)
            )
            level = "info" if choice.run else "debug"
            verdict = "runs" if choice.run else "left alone"
            await call.log(level, f"{unit.name}: {verdict} — {choice.why} ({states})")
            for _state, differ in checked:
                for node_id, fields in differ.items():
                    await call.log(
                        "warn", f"{node_id}: present, but differs in {', '.join(fields)}"
                    )
            if not choice.run:
                return Pick(None, hold=choice.unchecked)
            ran_of[unit.name] = [
                step
                for i, (step, (state, _)) in enumerate(zip(steps, checked, strict=True))
                if i not in choice.skip and state is StepState.missing
            ]
            return Pick(choice.skip)

        await call.log("info", f"run {info.hash} on {host}: {len(plan)} units")
        results = await self._run(call, plan, req, host, choose=choose_unit)
        for result in results:  # what run did, into the reconciled graph
            for step in ran_of.get(result.name, []):
                if result.state is g.ApplyState.done:
                    graph.applied(step)
                else:
                    graph.failed(step)
        await call.log(
            "info",
            "units: " + ", ".join(f"{why} {n}" for why, n in sorted(choices.items())),
        )
        # What is on the host belongs to this IR now.
        await self._mark_deployed(call, ir, info.hash, True)
        counts = graph.counts()
        await call.log(
            "info",
            f"reconciled graph: {len(graph.nodes)} nodes — present {counts['present']}, "
            f"applied {counts['applied']}, missing {counts['missing']}, "
            f"observed {counts['observed']}",
        )
        for node_id, state in sorted(graph.state.items()):
            await call.log("debug", f"{state.value}: {node_id}")
        failed = any(r.state is g.ApplyState.failed for r in results)
        if failed:
            await call.log("warn", "a unit failed: leftovers are not removed")
        elif req.noCleanup:
            await call.log("info", "leftovers kept (--no-cleanup)")
        else:
            final = values_of["latest"]
            steps = [resolve(operation, final) for _unit, operation in operations]
            results.append(await self._cleanup(call, req, host, live, steps))
        failed = any(r.state is g.ApplyState.failed for r in results)
        outcome = g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done
        return self._result(info.hash, req, host, outcome, results)

    async def _cleanup(
        self,
        call: Call,
        req: g.IrApplyReq,
        host: str,
        live: dict[str, dict[str, Any]],
        steps: list[Any],
    ) -> g.ApplyUnit:
        """The end of a run: what the first look found to be ours and no
        step fulfilled is removed (fabric/cleanup.py)."""
        left = await self._leftovers(call, live, steps)
        name = "cleanup: leftovers"
        await self._progress(call, g.ApplyUnit(name=name, state=g.ApplyState.running))
        client = await self._client()
        res = await client.ask(
            g.UnitExecReq(
                kind="UnitExecReq",
                unit=cleanup.unit(left),
                action=g.ApplyAction.delete,
                host=host,
                commandTimeout=req.commandTimeout,
            ),
            timeout=None,
            parent=call.req_id,
        )
        assert isinstance(res.body, g.UnitExecRes)
        done = res.body.state is g.IrApplyOutcome.done
        result = g.ApplyUnit(
            name=name,
            state=g.ApplyState.done if done else g.ApplyState.failed,
            operations=len(left),
            message=res.body.message,
        )
        await self._progress(call, result)
        return result

    @staticmethod
    async def _leftovers(
        call: Call, live: dict[str, dict[str, Any]], steps: list[Any]
    ) -> list[Any]:
        """The nodes of ours in the first look no step fulfils, logged."""
        fulfilled, untranslated = crossed_out(steps, live)
        if untranslated:
            await call.log("warn", "no RawIR for these steps: " + ", ".join(untranslated))
        left = cleanup.leftovers(ours(live), fulfilled)
        await call.log("info", f"cleanup: {len(left)} leftovers")
        for node in left:
            await call.log("info", f"cleanup: remove {node['id']}")
        return left

    async def _run_dry(
        self, call: Call, req: g.IrApplyReq, info: g.IrInfo, ir: g.IrCollection, host: str
    ) -> g.IrApplyRes:
        """run, as a dry run: what the IR is in RawIR on this host (the
        desired side of a reconcile), and what cannot be predicted yet."""
        if not req.dryRun:
            raise ProtocolError("bad-request", "run applies nothing yet: use it with --dry-run")
        nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
        host_node = next(n for n in nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host)
        try:
            operations = plan_operations(nodes, host)
            desired = desired_rawir(operations)
        except PlanError as exc:
            raise ProtocolError("bad-ir", str(exc)) from None
        kinds = collections.Counter(node["kind"] for node in desired.nodes.values())
        await call.log(
            "info",
            f"run {info.hash} on {host} (dry run): {len(desired.nodes)} RawIR nodes — "
            + ", ".join(f"{kind} {n}" for kind, n in sorted(kinds.items())),
        )
        await call.log("info", f"changes on {len(desired.changes)} nodes no primitive adds")
        if desired.not_learned:
            await call.log(
                "warn",
                "not learned yet (primate): "
                + ", ".join(f"{name} ×{n}" for name, n in desired.not_learned.most_common()),
            )
            await call.log(
                "info",
                "  because of: "
                + ", ".join(f"{why} ×{n}" for why, n in desired.not_learned_why.most_common()),
            )
        if desired.runtime_values:
            await call.log(
                "info", f"{len(desired.runtime_values)} with a value known only at run time"
            )
        if desired.not_rawir:
            await call.log(
                "info",
                "no RawIR: " + ", ".join(f"{k} ×{n}" for k, n in desired.not_rawir.most_common()),
            )
        for node_id, unit in desired.conflicts:
            await call.log("warn", f"{node_id}: added differently again by {unit}")
        live = await self._compare(call, desired.nodes, host_node, operations)
        if live is not None:
            await self._would_create(call, nodes, host, live)
        return self._result(info.hash, req, host, g.IrApplyOutcome.done, [])

    async def _would_create(
        self,
        call: Call,
        nodes: list[pt.Model],
        host: str,
        live: dict[str, dict[str, Any]],
    ) -> None:
        """What run would do now, per unit (as _converge chooses). Steps with
        a value only known at run time cannot be checked here: they count as
        unknown (run checks them once the values are captured)."""
        operations = plan_operations(nodes, host)
        by_unit: dict[str, list[Any]] = collections.defaultdict(list)
        for unit_name, operation in operations:
            by_unit[unit_name].append(operation)
        choices: collections.Counter[str] = collections.Counter()
        units = plan_units(nodes, host)
        later = consumed_later([by_unit[unit.name] for unit in units], live)
        held = Failed()
        for unit, consumed in zip(units, later, strict=True):
            if held.blocks(unit):
                choices["waits for a unit run cannot check"] += 1
                await call.log("info", f"{unit.name}: would wait — {held.why(unit)}")
                continue
            steps = by_unit[unit.name]
            states = [
                StepState.read if decides else step_state(step, live, consumed)[0]
                for step, decides in zip(steps, _self_deciding(unit), strict=True)
            ]
            choice = choose(states)
            choices[choice.why] += 1
            if choice.unchecked:
                held.hold(unit)
            verdict = "would run" if choice.run else "left alone"
            await call.log("info", f"{unit.name}: {verdict} — {choice.why}")
            for step, state in zip(steps, states, strict=True):
                await call.log("debug", f"{unit.name}:   {type(step).__name__} {state.value}")
                if state in (StepState.partial, StepState.missing):
                    level = "info" if state is StepState.partial else "debug"
                    for node_id in missing_nodes(step, live):
                        await call.log(level, f"{unit.name}:   missing {node_id}")
            for index, step in enumerate(steps):
                if choice.run and index not in choice.skip:
                    commands = "; ".join(step.describe(LifecycleAction.CREATE))
                    await call.log("debug", f"{unit.name}: would run: {commands}")
        await call.log(
            "info",
            "units: " + ", ".join(f"{why} {n}" for why, n in sorted(choices.items())),
        )
        # With no values captured yet; what a step's node ids hold does not
        # depend on them.
        await self._leftovers(call, live, [resolve(op, {}) for _unit, op in operations])

    async def _compare(
        self,
        call: Call,
        desired: dict[str, dict[str, Any]],
        host_node: pt.InfraHostNode,
        operations: list[tuple[str, Any]],
    ) -> dict[str, dict[str, Any]] | None:
        """The live RawIR — ours of it — against the desired: per kind, what
        is applied, to apply, only observed, or differs — ids at debug; how
        many nodes are someone else's, by owner. Returns the live RawIR as
        collected, owners marked (None when it cannot be collected)."""
        try:
            collected = await asyncio.to_thread(observed.snapshot)
        except Exception as exc:  # noqa: BLE001 — a report, not an apply: say why, go on
            await call.log("warn", f"cannot collect the live RawIR: {type(exc).__name__}: {exc}")
            return None
        _mark_host(collected, host_node, operations)
        live = ours(collected)
        others = collections.Counter(
            node["owner"] for node in collected.values() if node["owner"] != OVN_FABRIC
        )
        await call.log(
            "info",
            "left alone, someone else's: "
            + ", ".join(f"{owner} {n}" for owner, n in sorted(others.items())),
        )
        result = compare(desired, live)
        await call.log(
            "info",
            f"live: {len(live)} RawIR nodes of ours — applied {len(result.applied)}, "
            f"to apply {len(result.to_apply)}, only observed {len(result.only_observed)}, "
            f"differ {len(result.differ)}",
        )
        kinds = sorted({n["kind"] for n in [*desired.values(), *live.values()]})
        for kind in kinds:

            def count(ids: list[str]) -> int:
                return sum(
                    1 for i in ids if (desired.get(i) or live.get(i) or {}).get("kind") == kind
                )

            await call.log(
                "info",
                f"  {kind}: applied {count(result.applied)}, to apply {count(result.to_apply)}, "
                f"only observed {count(result.only_observed)}, differ {count(list(result.differ))}",
            )
        for label, ids in (
            ("to apply", result.to_apply),
            ("only observed", result.only_observed),
            ("applied", result.applied),
        ):
            for node_id in ids:
                await call.log("debug", f"{label}: {node_id}")
        for node_id, fields in result.differ.items():
            await call.log("debug", f"differ: {node_id}: {', '.join(fields)}")
        return collected

    async def _resolve(self, call: Call, ref: str) -> tuple[g.IrInfo, g.IrCollection]:
        """The stored IR `ref` names (a hash or a name): its info and nodes."""
        client = await self._client()
        try:
            res = await client.ask(
                g.IrGetReq(kind="IrGetReq", ref=ref), timeout=None, parent=call.req_id
            )
        except RemoteError as exc:  # e.g. not-found: the requester's answer too
            raise ProtocolError(exc.code, exc.message) from None
        assert isinstance(res.body, g.IrGetRes)
        return res.body.info, res.body.ir


def _self_deciding(unit: g.Unit) -> list[bool]:
    """Per step: it has its own decider (fabric/decide.py) — the executor
    decides it from its live state; for run's check it is like a read."""
    return [step.kind == "Primitive" and step.decide is not None for step in unit.steps]


# Where an operation names an interface of the host it uses but does not
# create (a VLAN's NIC, the AF_XDP NIC): class name -> its fields.
_PARENTS: dict[str, tuple[str, ...]] = {
    "VlanPrimitive": ("parent",),
    "NetnsLinkPrimitive": ("parent",),
    "OVSAfxdpPort": ("port",),
}


def _mark_host(
    live: dict[str, dict[str, Any]], host_node: pt.InfraHostNode, operations: list[tuple[str, Any]]
) -> None:
    """The host's own in the live RawIR: its interfaces (topology:
    Host.reconcile — the loopback among them, in every netns) and the
    physical parents the plan uses, with what is on them."""
    parents = {
        value
        for _unit, operation in operations
        for field in _PARENTS.get(type(operation).__name__, ())
        if isinstance(value := getattr(operation, field, None), str)
    }
    mark_host(live, host_node.data.reconcile.hostInterfaces, parents)


def service(client: Callable[[], Awaitable[Peer]], host: str | None = None) -> Service:
    return Apply(client, host).service()
