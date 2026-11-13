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
   (fabric/deps.py) through the abstract deps service — the first thing,
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
import contextlib
import dataclasses
import json
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

from protocol import generated as pt
from protocol.hydrate import hydrate_nodes
from raw_reconciler.natural_key import all_keys
from raw_reconciler.owners import OVN_FABRIC, mark_host, ours
from runtime import generated as rg
from runtime.message import BodySpec, ProtocolError, plain, register_bodies
from runtime.peer import Call, Peer, RemoteError
from runtime.service import Service
from runtime.transport import Closed

from .. import cleanup, observed, release
from .. import generated as g
from ..addresses import AddressError, Addresses, live_view
from ..boot import AllocatorState, boot_id, remove_old_oneshot
from ..decide import STARTS_AFRESH
from ..deps import abstract_deps
from ..desired import (
    Reconciled,
    StepState,
    changed_by_steps,
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
from ..supervise import POLL, SETTLE, Supervisor, docker_states, watch_docker

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
    "StatusReq": BodySpec(g.StatusReq, "request"),
    "StatusRes": BodySpec(g.StatusRes, "response"),
}

# What a background job's units say when a newer apply released it.
RELEASED = "released: a newer apply took over"
register_bodies(BODIES)  # clients decode these too


# The scopes every other one needs: a failure there stops a create.
FOUNDATION = ("configure", "cluster")

# All images at once unless a run bounds it (--image-jobs).
ALL_AT_ONCE = 1_000


@dataclasses.dataclass(frozen=True)
class Split:
    """A plan split by what its units need (ADR 0012 §3): the scopes with an
    image go to the background — its image, then the rest of the scope (the
    container and everything in it); the network is everything else, and
    decides the run."""

    network: list[g.Unit]
    images: dict[str, list[g.Unit]]  # scope -> its image units
    workloads: dict[str, list[g.Unit]]  # scope -> its other units, in plan order

    @property
    def background(self) -> list[str]:
        return [
            unit.name
            for scope, units in self.images.items()
            for unit in [*units, *self.workloads.get(scope, [])]
        ]


def split_plan(plan: list[g.Unit]) -> Split:
    with_image = {u.scope or "" for u in plan if u.kind == "image"}
    images: dict[str, list[g.Unit]] = {}
    workloads: dict[str, list[g.Unit]] = {}
    for unit in plan:
        scope = unit.scope or ""
        if scope not in with_image:
            continue
        (images if unit.kind == "image" else workloads).setdefault(scope, []).append(unit)
    return Split(
        network=[u for u in plan if (u.scope or "") not in with_image],
        images=images,
        workloads=workloads,
    )


@dataclasses.dataclass
class _Track:
    """An apply as `fabric status` shows it."""

    req_id: str
    action: g.ApplyAction
    hash: str
    background: bool
    started: float
    finished: float | None = None
    state: g.TrackState = g.TrackState.running
    units: dict[str, g.ApplyUnit] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _Background:
    """The background job of the last create or run (ADR 0012 §3)."""

    key: str  # action|hash|host: the same work
    req_id: str
    units: list[str]
    task: asyncio.Task[None]  # follows it to its end


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
    def __init__(
        self,
        client: Callable[[], Awaitable[Peer]],
        host: str | None = None,
        state_root: str | None = None,
    ) -> None:
        self._client = client  # the runtime's internal connection, to ask the IR service
        self.host = host or socket.gethostname()
        # The allocator's memory and the boot id (ADR 0012 §6); none in tests.
        self._state = AllocatorState(state_root) if state_root is not None else None
        self._replay: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()  # one apply at a time
        # The background job, and the work it may go on with: a newer apply
        # with other work releases it (ADR 0012 §5) — it stops before its
        # next unit.
        self._background: _Background | None = None
        self._background_key: str | None = None
        # What `fabric status` shows: the last apply, the last background
        # job; the applies the runtime started itself (boot, repair).
        self._tracks: dict[str, _Track] = {}
        self._last_apply: str | None = None
        self._last_background: str | None = None
        self._own: dict[str, g.ApplyWhy] = {}
        # Keeping the applied IR's containers alive (fabric/supervise.py).
        self._supervisor = Supervisor()
        self._supervising = False
        self._wake = asyncio.Event()
        self._containers: tuple[str, dict[str, str]] | None = None  # hash -> its containers

    def service(self) -> Service:
        # A run belongs to the runtime (ADR 0012 §7): it goes on when the
        # connection that sent it closes; `fabric follow` attaches again.
        # So is the background job one leaves behind (ADR 0012 §3).
        return Service(
            name="apply",
            bodies=BODIES,
            handlers={"IrApplyReq": self.apply, "StatusReq": self.status},
            detached=frozenset({"IrApplyReq"}),
            run=self._at_start if self._state is not None else None,
        )

    def _remembered(self) -> dict[str, dict[str, dict[str, int]]]:
        return self._state.remembered() if self._state is not None else {}

    def _remember(self, addrs: Addresses, digest: str) -> None:
        if self._state is not None:
            self._state.save(addrs.placed(), digest)

    async def _at_start(self) -> None:
        """Boot (ADR 0012 §6): after a reboot the kernel's part is gone — the
        deployed IR is run again (network, then its background job), without
        cleanup; after a restart of the runtime only, everything is there."""
        assert self._state is not None
        await self._boot()
        await self._supervise()

    async def _boot(self) -> None:
        assert self._state is not None
        try:
            if await asyncio.to_thread(remove_old_oneshot):
                log.info("boot: the old ovn-fabric-reconcile.service removed")
            before = self._state.load().get("bootId")
            now = boot_id()
            if before is None:
                log.info("boot: no boot recorded yet — nothing replayed")
                await asyncio.to_thread(self._state.save, None)
                return
            if before == now:
                log.info("boot: the same boot (%s) — the runtime restarted, nothing replayed", now)
                return
            log.info("boot: rebooted (%s, was %s) — replaying the deployed IR", now, before)
            client = await self._started_client()
            request = await client.request(
                g.IrApplyReq(
                    kind="IrApplyReq", ref=DEPLOYED, action=g.ApplyAction.run, noCleanup=True
                )
            )
            self._own[request.req_id] = g.ApplyWhy.boot
            log.info("boot: replay is job %s — fabric follow %s", request.req_id, request.req_id)
            async for _event in request:
                pass
            res = request.result.body
            if isinstance(res, rg.ErrorRes):
                log.warning("boot: replay: %s: %s", res.code, res.message)
            elif isinstance(res, g.IrApplyRes):
                log.info("boot: replay %s (network)", res.state.value)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the runtime serves on regardless
            log.exception("boot: replay failed")

    # -- keeping containers alive (fabric/supervise.py) --

    async def _supervise(self) -> None:
        """Look at the applied IR's containers on every docker event (a
        container died, stopped, went) and every POLL seconds; bring back
        what is down, while no apply runs."""
        loop = asyncio.get_running_loop()
        watcher = loop.create_task(watch_docker(self._wake.set))
        self._supervising = True
        try:
            while True:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), POLL)
                self._wake.clear()
                await asyncio.sleep(SETTLE)  # a container replaced on purpose is back by then
                try:
                    await self._keep_alive()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — supervising goes on
                    log.exception("supervise: failed")
        finally:
            self._supervising = False
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher

    def _busy(self) -> bool:
        """An apply or a background job runs: it stops and starts containers
        on purpose."""
        running = self._background is not None and not self._background.task.done()
        return self._lock.locked() or running

    async def _keep_alive(self) -> None:
        if self._busy():
            return  # looked at again when it is done (it wakes us)
        wanted = await self._wanted_containers()
        self._supervisor.want(wanted)
        if not wanted:
            return
        states = await asyncio.to_thread(docker_states)
        if states is None:
            log.warning("supervise: docker does not answer")
            return
        now = time.time()
        self._supervisor.healthy(states, now)
        due = self._supervisor.due(states, now)
        if not due:
            waits = [
                self._supervisor.restarts[n].next_at - now
                for n in self._supervisor.down(states)
                if n in self._supervisor.restarts
            ]
            if waits:  # one keeps dying: look again when its wait is over
                asyncio.get_running_loop().call_later(max(min(waits), 0.0), self._wake.set)
            return
        for name in due:
            tries = self._supervisor.restarts.get(name)
            log.warning(
                "supervise: %s is %s — bringing it back (%s, try %d)",
                name,
                states.get(name, "missing"),
                wanted[name],
                (tries.count if tries else 0) + 1,
            )
        self._supervisor.started(due, now)
        client = await self._client()
        request = await client.request(
            g.IrApplyReq(kind="IrApplyReq", ref=DEPLOYED, action=g.ApplyAction.run, noCleanup=True)
        )
        self._own[request.req_id] = g.ApplyWhy.repair
        log.info("supervise: repair is job %s — fabric follow %s", request.req_id, request.req_id)
        async for _event in request:
            pass

    async def _wanted_containers(self) -> dict[str, str]:
        """container -> its unit, of the IR applied here — only while it is
        the deployed one (after a delete nothing is kept alive)."""
        applied = self._state.applied() if self._state is not None else None
        if applied is None:
            return {}
        try:
            info, ir = await self._resolve(None, DEPLOYED)
        except ProtocolError:
            return {}
        if info.hash != applied or self.host not in info.hosts:
            return {}
        if self._containers is not None and self._containers[0] == info.hash:
            return self._containers[1]
        nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
        try:
            addrs = Addresses.resolve(nodes, remembered=self._remembered())
            operations = plan_operations(nodes, self.host, addrs)
        except (AddressError, PlanError) as exc:
            log.warning("supervise: %s: %s", info.hash, exc)
            return {}
        # As fabric/cleanup.py wanted_containers: the steps that start one.
        found = {
            str(getattr(op, "name")): unit
            for unit, op in operations
            if type(op).__name__ == "DockerPrimitive"
        }
        self._containers = (info.hash, found)
        return found

    # -- status --

    async def status(self, call: Call) -> object:
        try:
            deployed: str | None = (await self._resolve(call, DEPLOYED))[0].hash
        except ProtocolError:
            deployed = None
        wanted = await self._wanted_containers()
        states = (await asyncio.to_thread(docker_states) if wanted else {}) or {}
        containers = []
        for name, unit in sorted(wanted.items()):
            tries = self._supervisor.restarts.get(name)
            containers.append(
                g.ContainerStatus(
                    name=name,
                    unit=unit,
                    state=states.get(name, "missing"),
                    restarts=tries.count if tries else 0,
                    lastRestart=tries.last if tries else None,
                    nextRestart=tries.next_at if tries and tries.next_at > time.time() else None,
                )
            )
        return g.StatusRes(
            kind="StatusRes",
            host=self.host,
            bootId=boot_id(),
            deployed=deployed,
            applied=self._state.applied() if self._state is not None else None,
            lastApply=self._track_info(self._last_apply),
            background=self._track_info(self._last_background),
            supervising=self._supervising,
            containers=containers,
        )

    def _track_info(self, req_id: str | None) -> g.ApplyTrack | None:
        track = self._tracks.get(req_id or "")
        if track is None:
            return None
        why = self._own.get(track.req_id) or (
            g.ApplyWhy.background if track.background else g.ApplyWhy.client
        )
        return g.ApplyTrack(
            reqId=track.req_id,
            action=track.action,
            hash=track.hash,
            why=why,
            state=track.state,
            startedAt=track.started,
            finishedAt=track.finished,
            units=list(track.units.values()),
        )

    async def _started_client(self) -> Peer:
        """The internal connection, once the runtime listens."""
        for _ in range(200):
            try:
                return await self._client()
            except (Closed, OSError):
                await asyncio.sleep(0.05)
        return await self._client()

    async def apply(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.IrApplyReq)
        if req.dryRun:
            return await self._apply(call, req)
        track = _Track(call.req_id, req.action, req.ref, bool(req.background), time.time())
        self._tracks[call.req_id] = track
        if req.background:
            self._last_background = call.req_id
        else:
            self._last_apply = call.req_id
        kept = {self._last_apply, self._last_background}
        self._tracks = {k: t for k, t in self._tracks.items() if k in kept}
        self._own = {k: w for k, w in self._own.items() if k in kept or k == call.req_id}
        try:
            res = await self._apply(call, req)
        except BaseException:
            track.state, track.finished = g.TrackState.failed, time.time()
            raise
        finally:
            self._wake.set()  # what it did to the containers: look
        state = g.TrackState.done
        if isinstance(res, g.IrApplyRes):
            if any(u.message == RELEASED for u in res.units):
                state = g.TrackState.released
            elif res.state is g.IrApplyOutcome.failed:
                state = g.TrackState.failed
        track.state, track.finished = state, time.time()
        return res

    async def _apply(self, call: Call, req: g.IrApplyReq) -> object:
        info, ir = await self._resolve(call, req.ref)
        track = self._tracks.get(call.req_id)
        if track is not None:
            track.hash = info.hash
        host = req.host or self.host
        if host not in info.hosts:
            raise ProtocolError(
                "bad-request",
                f"{host} is not a host of {info.hash} (its hosts: {', '.join(info.hosts) or '-'})",
            )
        if req.background:
            # Not under the lock: the next apply goes ahead, and releases it.
            return await self._background_part(call, req, info, ir, host)
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
                addrs = Addresses.resolve(nodes, remembered=self._remembered())
                plan = plan_units(nodes, host, addrs)
            except (AddressError, PlanError) as exc:
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
            background: g.BackgroundJob | None = None
            if req.dryRun:
                results = await self._run(call, plan if creating else plan[::-1], req, host)
            elif not creating:
                await self._release(call)
                results = await self._run(call, plan[::-1], req, host)
                if self._state is not None:  # nothing applied: nothing kept alive
                    self._state.deleted()
            else:
                # The network first; what has an image goes on as a job.
                await self._release_other(call, self._work(req.action, info.hash, host))
                split = split_plan(plan)
                values: list[g.UnitValue] = []
                self._remember(addrs, info.hash)
                results = await self._run(call, split.network, req, host, values_out=values)
                background = await self._start_background(
                    call, req, info.hash, host, split.background, values, results
                )
            if not req.dryRun and (creating or not req.keepDeployed):
                await self._mark_deployed(call, ir, info.hash, creating)
            failed = any(r.state is g.ApplyState.failed for r in results)
            outcome = g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done
            return self._result(info.hash, req, host, outcome, results, background)

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
        inputs: list[g.UnitValue] | None = None,
        values_out: list[g.UnitValue] | None = None,
        stop: Callable[[], str | None] | None = None,
    ) -> list[g.ApplyUnit]:
        """Run `units` through the unit executor. create and delete run every
        step, unconditionally (a create fails on what is already there, a
        delete removes best effort). `choose` is run's: asked right before
        each unit, with the values known by then — which steps to skip, or
        None to leave the unit alone. `inputs`: values captured before (a
        background job gets the run's); `values_out` receives the values
        captured at the end; `stop`, asked before each unit, ends it early
        (a newer run released this job)."""
        client = await self._client()
        store: list[g.UnitValue] = list(inputs or [])
        results: list[g.ApplyUnit] = []
        failed = Failed()
        for unit in units:
            stopped = stop() if stop is not None else None
            if stopped is not None:
                left = g.ApplyUnit(name=unit.name, state=g.ApplyState.skipped, message=stopped)
                results.append(left)
                await self._progress(call, left)
                continue
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
        if values_out is not None:
            values_out[:] = store
        return results

    async def _progress(self, call: Call, unit: g.ApplyUnit) -> None:
        track = self._tracks.get(call.req_id)
        if track is not None and track.background:
            track.units[unit.name] = unit
        await call.emit(g.IrApplyProgress(kind="IrApplyProgress", unit=unit))

    @staticmethod
    def _result(
        digest: str,
        req: g.IrApplyReq,
        host: str,
        outcome: g.IrApplyOutcome,
        units: list[g.ApplyUnit],
        background: g.BackgroundJob | None = None,
    ) -> g.IrApplyRes:
        return g.IrApplyRes(
            kind="IrApplyRes",
            hash=digest,
            action=req.action,
            host=host,
            state=outcome,
            units=units,
            background=background,
        )

    @staticmethod
    def _work(action: g.ApplyAction, digest: str, host: str) -> str:
        return f"{action.value}|{digest}|{host}"

    async def _start_background(
        self,
        call: Call,
        req: g.IrApplyReq,
        digest: str,
        host: str,
        units: list[str],
        values: list[g.UnitValue],
        network: list[g.ApplyUnit],
    ) -> g.BackgroundJob | None:
        """Hand what has an image to a job of its own (ADR 0012 §3): the same
        request with `background`, the values the network captured as its
        inputs — once the network is there. One of an earlier apply with the
        same work goes on (§5)."""
        if not units:
            await self._release(call)
            return None
        if any(r.state is g.ApplyState.failed for r in network):
            await call.log("warn", f"the network failed: {len(units)} background units not started")
            await self._release(call)
            return None
        key = self._work(req.action, digest, host)
        old = self._background
        if old is not None and not old.task.done() and old.key == key:
            await call.log("info", f"background job {old.req_id}: the same work — it goes on")
            return g.BackgroundJob(reqId=old.req_id, units=old.units, kept=True)
        await self._release(call)
        self._background_key = key  # before it starts: it checks against it
        client = await self._client()
        request = await client.request(
            g.IrApplyReq(
                kind="IrApplyReq",
                ref=digest,
                action=req.action,
                host=host,
                background=True,
                inputs=values,
                imageJobs=req.imageJobs,
                commandTimeout=req.commandTimeout,
            ),
            parent=call.req_id,
        )

        async def to_its_end() -> None:
            with contextlib.suppress(Exception):
                async for _event in request:
                    pass

        self._background = _Background(
            key, request.req_id, units, asyncio.get_running_loop().create_task(to_its_end())
        )
        await call.log(
            "info",
            f"background job {request.req_id}: {len(units)} units (images, workloads) — "
            f"fabric follow {request.req_id}",
        )
        return g.BackgroundJob(reqId=request.req_id, units=units)

    async def _release_other(self, call: Call, key: str) -> None:
        """At an apply's start: a background job of other work is released."""
        if self._background is not None and self._background.key != key:
            await self._release(call)

    async def _release(self, call: Call) -> None:
        """Release the background job: it stops before its next unit; wait
        for the units it is in (a pull, a container) to end."""
        self._background_key = None
        old, self._background = self._background, None
        if old is None or old.task.done():
            return
        await call.log(
            "info", f"background job {old.req_id} released — waiting for its current units"
        )
        await old.task

    async def _background_part(
        self, call: Call, req: g.IrApplyReq, info: g.IrInfo, ir: g.IrCollection, host: str
    ) -> g.IrApplyRes:
        """The job a create or a run leaves behind: the scopes with an image."""
        key = self._work(req.action, info.hash, host)

        def stop() -> str | None:
            return None if self._background_key == key else RELEASED

        if req.action is g.ApplyAction.run:
            return await self._converge(call, req, info, ir, host, stop=stop)
        if req.action is not g.ApplyAction.create or req.dryRun:
            raise ProtocolError("bad-request", "background: only of a create or a run")
        nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
        try:
            addrs = Addresses.resolve(nodes, remembered=self._remembered())
            split = split_plan(plan_units(nodes, host, addrs))
        except (AddressError, PlanError) as exc:
            raise ProtocolError("bad-ir", str(exc)) from None
        await call.log("info", f"background of create {info.hash} on {host}")
        results = await self._run_background(call, req, host, split, stop)
        failed = any(r.state is g.ApplyState.failed for r in results)
        outcome = g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done
        return self._result(info.hash, req, host, outcome, results)

    async def _run_background(
        self,
        call: Call,
        req: g.IrApplyReq,
        host: str,
        split: Split,
        stop: Callable[[], str | None],
        choose: Choose | None = None,
    ) -> list[g.ApplyUnit]:
        """The images, all at once (or `imageJobs` at a time, ADR 0012 §4);
        each scope's other units, in plan order, once its image is there."""
        slots = asyncio.Semaphore(req.imageJobs or ALL_AT_ONCE)

        async def image(units: list[g.Unit]) -> list[g.ApplyUnit]:
            async with slots:
                return await self._run(
                    call, units, req, host, choose=choose, inputs=req.inputs, stop=stop
                )

        loop = asyncio.get_running_loop()
        images = {scope: loop.create_task(image(units)) for scope, units in split.images.items()}
        values = list(req.inputs or [])
        results: list[g.ApplyUnit] = []
        for scope, task in images.items():
            pulled = await task
            results.extend(pulled)
            units = split.workloads.get(scope, [])
            if any(r.state is g.ApplyState.failed for r in pulled):
                for unit in units:
                    left = g.ApplyUnit(
                        name=unit.name, state=g.ApplyState.skipped, message="its image failed"
                    )
                    results.append(left)
                    await self._progress(call, left)
                continue
            out: list[g.UnitValue] = []
            results.extend(
                await self._run(
                    call, units, req, host, choose=choose, inputs=values, stop=stop, values_out=out
                )
            )
            values = out
        return results

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
        self,
        call: Call,
        req: g.IrApplyReq,
        info: g.IrInfo,
        ir: g.IrCollection,
        host: str,
        stop: Callable[[], str | None] | None = None,
    ) -> g.IrApplyRes:
        """run: bring the host to its IR — its dependencies first, then unit
        by unit in plan order. Right
        before a unit runs, its steps are checked against the live RawIR
        (their learned footprint, with the values captured so far) and
        `choose` decides: run all of it, only its missing steps, only its
        reads — or leave it alone (fabric/desired.py). At the end, what the
        first look found to be ours and no step fulfilled is removed, unless
        a unit failed or noCleanup asks to keep it.

        The network part decides it; the scopes with an image go on as the
        background job (ADR 0012 §3), which runs this again with `stop`:
        a look of its own, then only those scopes — no release, no
        replacing, no cleanup (the network part did them)."""
        background = stop is not None
        if host != self.host:
            raise ProtocolError("bad-request", f"run needs the live RawIR: only on {self.host}")
        nodes = hydrate_nodes(plain(ir.collection))  # type: ignore[arg-type]
        # First the host's dependencies (as create): the tools the live
        # RawIR is collected with, and the services the plan needs.
        problem = None if background else await self._deps(call, nodes, host)
        if problem is not None:
            await call.log("warn", problem)
            return self._result(info.hash, req, host, g.IrApplyOutcome.failed, [])
        try:
            live = await asyncio.to_thread(observed.snapshot)
        except Exception as exc:  # noqa: BLE001 — without the live view, change nothing
            raise ProtocolError("failed", f"cannot collect the live RawIR: {exc}") from None
        # The addresses first (ADR 0011 §3): a name keeps the number it holds
        # live; only new names get free ones, and nothing live is handed out.
        try:
            addrs = Addresses.resolve(nodes, *live_view(nodes, live), remembered=self._remembered())
        except AddressError as exc:
            raise ProtocolError("bad-ir", str(exc)) from None
        if not background:
            self._remember(addrs, info.hash)
        for net_id, owner, text, how in addrs.allocated():
            await call.log("info" if how == "new" else "debug", f"address {how}: {owner} {text}")
        try:
            plan = plan_units(nodes, host, addrs)
            operations = plan_operations(nodes, host, addrs)
        except PlanError as exc:
            raise ProtocolError("bad-ir", str(exc)) from None
        host_node = next(n for n in nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host)
        _mark_host(live, host_node, operations)
        # Release before claim (ADR 0010): a leftover holding a single
        # resource the plan claims goes first — and out of the first look,
        # so the step claiming it runs.
        released: list[g.ApplyUnit] = []
        found = (
            []
            if background
            else await self._releases(call, live, [resolve(op, {}) for _unit, op in operations])
        )
        if found and req.noCleanup:
            await call.log("info", "release before claim skipped (--no-cleanup)")
        elif found:
            held = {item.node_id: live[item.node_id] for item in found}
            released.append(
                await self._remove(
                    call, req, host, "release before claim", cleanup.removal_order(held.values())
                )
            )
            for node_id in held:
                live.pop(node_id, None)
        # Fields steps change on nodes others add: not the adder's to compare.
        changed_elsewhere = changed_by_steps([op for _unit, op in operations])
        # Replace what differs (ADR 0011 step 4): a node a step makes that is
        # there with other own fields (a router port with another address) is
        # removed before the units run, and its unit makes it anew — one way
        # to apply a change, the same for every kind.
        differing = (
            []
            if background
            else await self._differing(call, live, plan, operations, changed_elsewhere)
        )
        if differing and req.noCleanup:
            await call.log("info", "replacing what differs skipped (--no-cleanup)")
        elif differing:
            released.append(
                await self._remove(
                    call,
                    req,
                    host,
                    "replace what differs",
                    cleanup.removal_order(live[i] for i in differing),
                )
            )
            # What went with them (a bridge's ports, a port's routes) is
            # missing now too: look again, so their units make it.
            try:
                live = await asyncio.to_thread(observed.snapshot)
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError("failed", f"cannot collect the live RawIR: {exc}") from None
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

        # A unit that (re)starts a container gives it a new netns: what the
        # first look found in the old one is gone. The unit after it is
        # decided on a fresh look — a workload's NIC after its container
        # was started afresh (a changed file) is missing, not there.
        starts_container = {
            name
            for name, ops in by_unit.items()
            if any(type(op).__name__ == "DockerPrimitive" for op in ops)
        }
        stale = {"since": ""}

        async def look_again(after: str) -> None:
            nonlocal live
            try:
                fresh = await asyncio.to_thread(observed.snapshot)
            except Exception as exc:  # noqa: BLE001 — decide on the old look
                await call.log("warn", f"cannot look again after {after}: {exc}")
                return
            _mark_host(fresh, host_node, operations)
            live = fresh
            later.update(
                zip(
                    (u.name for u in plan),
                    consumed_later([by_unit[u.name] for u in plan], live),
                    strict=True,
                )
            )
            await call.log("debug", f"looked again: {after} started a container")

        async def choose_unit(unit: g.Unit, values: dict[str, Any]) -> Pick:
            values_of["latest"] = values
            if stale["since"]:
                after, stale["since"] = stale["since"], ""
                await look_again(after)
            steps = [resolve(operation, values) for operation in by_unit[unit.name]]
            checked = [
                (
                    (decider, {})
                    if decider is not None
                    else step_state(step, live, later[unit.name], changed_elsewhere)
                )
                for step, decider in zip(steps, _deciders(unit), strict=True)
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
            if unit.name in starts_container:
                stale["since"] = unit.name
            ran_of[unit.name] = [
                step
                for i, (step, (state, _)) in enumerate(zip(steps, checked, strict=True))
                if i not in choice.skip and state is StepState.missing
            ]
            return Pick(choice.skip)

        split = split_plan(plan)
        values: list[g.UnitValue] = []
        if stop is None:
            await self._release_other(call, self._work(req.action, info.hash, host))
        if stop is not None:
            await call.log("info", f"background of run {info.hash} on {host}")
            results = await self._run_background(call, req, host, split, stop, choose_unit)
        else:
            await call.log(
                "info",
                f"run {info.hash} on {host}: {len(split.network)} units "
                f"(+{len(split.background)} in the background)",
            )
            results = [
                *released,
                *await self._run(
                    call, split.network, req, host, choose=choose_unit, values_out=values
                ),
            ]
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
        counts = graph.counts()
        await call.log(
            "info",
            f"reconciled graph: {len(graph.nodes)} nodes — present {counts['present']}, "
            f"applied {counts['applied']}, missing {counts['missing']}, "
            f"observed {counts['observed']}",
        )
        for node_id, state in sorted(graph.state.items()):
            await call.log("debug", f"{state.value}: {node_id}")
        if background:
            failed = any(r.state is g.ApplyState.failed for r in results)
            outcome = g.IrApplyOutcome.failed if failed else g.IrApplyOutcome.done
            return self._result(info.hash, req, host, outcome, results)
        # What is on the host belongs to this IR now.
        await self._mark_deployed(call, ir, info.hash, True)
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
        job = await self._start_background(
            call, req, info.hash, host, split.background, values, results
        )
        return self._result(info.hash, req, host, outcome, results, job)

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
        return await self._remove(call, req, host, "cleanup: leftovers", left)

    @staticmethod
    async def _differing(
        call: Call,
        live: dict[str, dict[str, Any]],
        plan: list[g.Unit],
        operations: list[tuple[str, Any]],
        changed_elsewhere: dict[str, set[str]],
    ) -> list[str]:
        """The live nodes the plan makes that are there with other own
        fields — logged; not removed yet. A step whose values are only known
        at run time cannot be told: it is not replaced."""
        by_unit: dict[str, list[Any]] = collections.defaultdict(list)
        for unit_name, operation in operations:
            # unresolved: a value only known at run time stays unknown
            by_unit[unit_name].append(operation)
        later = consumed_later([by_unit[unit.name] for unit in plan], live)
        out: list[str] = []
        for unit, consumed in zip(plan, later, strict=True):
            for step in by_unit[unit.name]:
                state, differ = step_state(step, live, consumed, changed_elsewhere)
                if state is not StepState.present:
                    continue
                for node_id, fields in sorted(differ.items()):
                    if node_id in live and node_id not in out:
                        await call.log(
                            "info", f"replace {node_id} (differs in {', '.join(fields)})"
                        )
                        out.append(node_id)
        await call.log("info", f"replace what differs: {len(out)}")
        return out

    async def _remove(
        self, call: Call, req: g.IrApplyReq, host: str, name: str, nodes: list[Any]
    ) -> g.ApplyUnit:
        """Remove `nodes` (in the order given) as one unit, best effort."""
        await self._progress(call, g.ApplyUnit(name=name, state=g.ApplyState.running))
        client = await self._client()
        unit = cleanup.unit(nodes)
        res = await client.ask(
            g.UnitExecReq(
                kind="UnitExecReq",
                unit=dataclasses.replace(unit, name=name),
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
            operations=len(nodes),
            message=res.body.message,
        )
        await self._progress(call, result)
        return result

    @staticmethod
    async def _releases(
        call: Call, live: dict[str, dict[str, Any]], steps: list[Any]
    ) -> list[release.Release]:
        """The leftovers holding a single resource the plan claims (ADR
        0010) — logged; not removed yet."""
        fulfilled, _untranslated = crossed_out(steps, live)
        left = [node["id"] for node in cleanup.leftovers(ours(live), fulfilled)]
        found = release.find(steps, live, left)
        await call.log("info", f"release before claim: {len(found)}")
        for item in found:
            await call.log(
                "info",
                f"release {item.node_id} (holds {item.key}, claimed by {item.claimed_by})",
            )
        return found

    @staticmethod
    async def _leftovers(
        call: Call, live: dict[str, dict[str, Any]], steps: list[Any]
    ) -> list[Any]:
        """The nodes of ours in the first look no step fulfils, logged."""
        fulfilled, untranslated = crossed_out(steps, live)
        if untranslated:
            await call.log("warn", "no RawIR for these steps: " + ", ".join(untranslated))
        # A container of ours no step starts any more (its router or
        # workload left the IR) goes too.
        left = cleanup.leftovers(ours(live), fulfilled, cleanup.wanted_containers(steps))
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
        # The single resources and their natural keys (ADR 0010), with the
        # addresses a node holds (ADR 0011 §3a).
        keys = {i: k for i, node in live.items() if (k := all_keys(node, live))}
        await call.log("info", f"natural keys: {sum(map(len, keys.values()))} single resources")
        for node_id, node_keys in sorted(keys.items()):
            await call.log("debug", f"natural key: {node_id} = {', '.join(node_keys)}")
        # As run: the addresses placed against the live view (ADR 0011 §3).
        try:
            addrs = Addresses.resolve(nodes, *live_view(nodes, live), remembered=self._remembered())
        except AddressError as exc:
            raise ProtocolError("bad-ir", str(exc)) from None
        operations = plan_operations(nodes, host, addrs)
        await self._releases(call, live, [resolve(op, {}) for _unit, op in operations])
        by_unit: dict[str, list[Any]] = collections.defaultdict(list)
        for unit_name, operation in operations:
            by_unit[unit_name].append(operation)
        changed_elsewhere = changed_by_steps([op for _unit, op in operations])
        choices: collections.Counter[str] = collections.Counter()
        units = plan_units(nodes, host, addrs)
        later = consumed_later([by_unit[unit.name] for unit in units], live)
        held = Failed()
        for unit, consumed in zip(units, later, strict=True):
            if held.blocks(unit):
                choices["waits for a unit run cannot check"] += 1
                await call.log("info", f"{unit.name}: would wait — {held.why(unit)}")
                continue
            steps = by_unit[unit.name]
            states = [
                (
                    decider
                    if decider is not None
                    else step_state(step, live, consumed, changed_elsewhere)[0]
                )
                for step, decider in zip(steps, _deciders(unit), strict=True)
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

    async def _resolve(self, call: Call | None, ref: str) -> tuple[g.IrInfo, g.IrCollection]:
        """The stored IR `ref` names (a hash or a name): its info and nodes."""
        client = await self._client()
        try:
            res = await client.ask(
                g.IrGetReq(kind="IrGetReq", ref=ref),
                timeout=None,
                parent=call.req_id if call is not None else None,
            )
        except RemoteError as exc:  # e.g. not-found: the requester's answer too
            raise ProtocolError(exc.code, exc.message) from None
        assert isinstance(res.body, g.IrGetRes)
        return res.body.info, res.body.ir


def _deciders(unit: g.Unit) -> list[StepState | None]:
    """Per step: that it decides itself on the host (fabric/decide.py), and
    whether it may start afresh what later steps build on; None: run's check
    of its footprint tells."""
    out: list[StepState | None] = []
    for step in unit.steps:
        spec = step.decide if step.kind == "Primitive" else None
        if spec is None:
            out.append(None)
        else:
            afresh = spec.kind in STARTS_AFRESH
            out.append(StepState.starts_afresh if afresh else StepState.decides)
    return out


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


def service(
    client: Callable[[], Awaitable[Peer]], host: str | None = None, state_root: str | None = None
) -> Service:
    return Apply(client, host, state_root).service()
