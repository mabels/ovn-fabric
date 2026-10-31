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
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from deployer.deps import abstract_deps
from protocol import generated as pt
from protocol.hydrate import hydrate_nodes
from runtime import generated as rg
from runtime.message import BodySpec, Msg, ProtocolError, plain, register_bodies
from runtime.peer import Peer, RemoteError
from runtime.service import Service

from .. import generated as g
from ..plan import PlanError, plan_units

log = logging.getLogger("fabric.apply")

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

    def add(self, unit: g.Unit) -> None:
        scope = unit.scope or ""
        if scope in FOUNDATION or not scope:
            self._foundation = self._foundation or unit.name
        else:
            self._scopes.setdefault(scope, unit.name)

    def blocks(self, unit: g.Unit) -> bool:
        return self._foundation is not None or (unit.scope or "") in self._scopes

    def why(self, unit: g.Unit) -> str:
        failed = self._foundation or self._scopes.get(unit.scope or "")
        return f"needs {failed}, which failed"


class Apply:
    def __init__(self, client: Callable[[], Awaitable[Peer]], host: str | None = None) -> None:
        self._client = client  # the runtime's internal connection, to ask the IR service
        self.host = host or socket.gethostname()
        self._lock = asyncio.Lock()  # one apply at a time

    def service(self) -> Service:
        return Service(name="apply", bodies=BODIES, handlers={"IrApplyReq": self.apply})

    async def apply(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.IrApplyReq)
        info, ir = await self._resolve(req.ref)
        host = req.host or self.host
        if host not in info.hosts:
            raise ProtocolError(
                "bad-request",
                f"{host} is not a host of {info.hash} (its hosts: {', '.join(info.hosts) or '-'})",
            )
        if self._lock.locked():
            await peer.log("info", "another apply runs: waiting for it")
        async with self._lock:
            mode = " (dry run)" if req.dryRun else ""
            # The deployer's declarations take the IR as its own types.
            nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
            try:
                plan = plan_units(nodes, host)
            except PlanError as exc:
                raise ProtocolError("bad-ir", str(exc)) from None
            await peer.log(
                "info",
                f"{req.action.value} {info.hash} on {host}{mode}: {len(ir.collection)} nodes, "
                f"{len(plan)} units",
            )
            creating = req.action is g.ApplyAction.create
            if creating and not req.dryRun:
                problem = await self._deps(peer, nodes, host)
                if problem is not None:
                    await peer.log("warn", problem)
                    return self._result(info.hash, req, host, g.IrApplyOutcome.failed, [])
            results = await self._run(peer, plan if creating else plan[::-1], req, host)
            failed = any(r.state is g.ApplyState.failed for r in results)
            outcome = g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done
            return self._result(info.hash, req, host, outcome, results)

    async def _deps(self, peer: Peer, nodes: list[pt.Model], host: str) -> str | None:
        """Resolve the host's abstract dependencies; None when all are ready."""
        host_node = next(n for n in nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host)
        deps, notes = abstract_deps(host_node)
        for note in notes:
            await peer.log("info", f"deps: {note}")
        client = await self._client()
        request = await client.request(
            g.AbstractDepsReq(
                kind="AbstractDepsReq",
                deps=[g.AbstractDepSpec(name=d["name"], systemDeps=d["systemDeps"]) for d in deps],
            )
        )
        async for event in request:  # its progress, as ours
            if isinstance(event.body, rg.LogMsg):
                await peer.emit(event.body)
        res = request.result.body
        if isinstance(res, rg.ErrorRes):
            return f"abstract deps: {res.code}: {res.message}"
        assert isinstance(res, g.AbstractDepsRes)
        missing = [
            f"{s.name} {s.state.value}" for s in res.deps if s.state is not g.SystemDepState.ready
        ]
        return f"dependencies not ready: {', '.join(missing)}" if missing else None

    async def _run(
        self, peer: Peer, units: list[g.Unit], req: g.IrApplyReq, host: str
    ) -> list[g.ApplyUnit]:
        client = await self._client()
        store: list[g.UnitValue] = []
        results: list[g.ApplyUnit] = []
        failed = Failed()
        for unit in units:
            if req.action is g.ApplyAction.create and failed.blocks(unit):
                skipped = g.ApplyUnit(
                    name=unit.name, state=g.ApplyState.skipped, message=failed.why(unit)
                )
                results.append(skipped)
                await self._progress(peer, skipped)
                continue
            await self._progress(peer, g.ApplyUnit(name=unit.name, state=g.ApplyState.running))
            request = await client.request(
                g.UnitExecReq(
                    kind="UnitExecReq",
                    unit=unit,
                    action=req.action,
                    inputs=store,
                    host=host,
                    dryRun=req.dryRun,
                    commandTimeout=req.commandTimeout,
                )
            )
            async for event in request:  # the executor's logs, as ours
                if isinstance(event.body, rg.LogMsg):
                    await peer.emit(event.body)
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
            await self._progress(peer, result)
            if result.state is g.ApplyState.failed:
                failed.add(unit)
        return results

    @staticmethod
    async def _progress(peer: Peer, unit: g.ApplyUnit) -> None:
        await peer.emit(g.IrApplyProgress(kind="IrApplyProgress", unit=unit))

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

    async def _resolve(self, ref: str) -> tuple[g.IrInfo, g.IrCollection]:
        """The stored IR `ref` names (a hash or a name): its info and nodes."""
        client = await self._client()
        try:
            res = await client.ask(g.IrGetReq(kind="IrGetReq", ref=ref), timeout=None)
        except RemoteError as exc:  # e.g. not-found: the requester's answer too
            raise ProtocolError(exc.code, exc.message) from None
        assert isinstance(res.body, g.IrGetRes)
        return res.body.info, res.body.ir


def service(client: Callable[[], Awaitable[Peer]], host: str | None = None) -> Service:
    return Apply(client, host).service()
