"""The system dependency resolver: makes packages present and keeps them
current, through the host's package manager (packages.py: apt or dnf,
found when the runtime starts).

    ResolveSystemDepsReq { deps: [...], timeout? }
      → ResolveSystemDep { dep, state }   one event per dependency, as it settles
      → ResolveSystemDepsRes { deps: [DepStatus] }

All package work goes through ONE queue drained by ONE worker, so the
package manager never runs twice at a time from here (another one on the
host — unattended-upgrades — is waited for by the manager itself). The
queue carries two kinds of jobs:

- install: the missing dependencies of a request. The package index is
  refreshed first when it is stale (at most every UPDATE_EVERY, the time
  persisted in a stamp file across restarts).
- refresh: the regular maintenance, every UPDATE_EVERY — refresh the
  index, then upgrade every dependency this resolver has made ready (it
  keeps them current, as the old deployer's daily pass did). Only once
  something was requested: with no dependency there is nothing to keep
  current.

Whether a dependency is there, and its version, is the manager's `version`;
the version is reported with `ready`. Every package command is retried
with backoff (RETRIES attempts, 5s doubling up to 300s) — unless the
manager says its failure is permanent (an unknown package): that is not
retried. A manager refuses a whole install for one bad package, so a
failed batch is retried package by package: one bad package only fails
itself.

A dependency already queued or being installed for another request is not
queued again: both requests wait for the same result. A request's
`timeout` only bounds how long it waits: what has not settled by then is
reported `timeout`, but the install is never killed (interrupting dpkg or
rpm can leave the package database broken) — it finishes in the queue,
and a later request finds the package installed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence

from runtime import command
from runtime.command import Report
from runtime.message import BodySpec, ProtocolError, register_bodies
from runtime.peer import Call
from runtime.service import Service

from .. import generated as g
from .packages import MANAGERS, PackageManager

log = logging.getLogger("fabric.sysdeps")

BODIES = {
    "ResolveSystemDepsReq": BodySpec(g.ResolveSystemDepsReq, "request"),
    "ResolveSystemDep": BodySpec(g.ResolveSystemDep, "event"),
    "ResolveSystemDepsRes": BodySpec(g.ResolveSystemDepsRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

# A package name (Debian's may carry :arch, rpm's capitals and `_`). Anything
# else is refused: the name ends up in the manager's argv, and must never
# look like an option.
_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_+.-]+(:[a-z0-9-]+)?$")

UPDATE_EVERY = 24 * 60 * 60
# After a failed refresh, try again this much later (not at once).
RETRY_REFRESH = 60 * 60
# When the index was last refreshed, per manager ("last-apt-update").
UPDATE_STAMP = "/var/lib/ovn-fabric/last-{manager}-update"
RETRIES = 6


def _tail(output: str, lines: int = 5) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


@dataclasses.dataclass(frozen=True)
class _Install:
    deps: list[str]


@dataclasses.dataclass(frozen=True)
class _Refresh:
    done: asyncio.Event


class SystemDeps:
    """The service: one resolver per package manager, each with its own
    queue and worker. A request goes to the one it names (the host's
    config chose it); a manager without an implementation is refused."""

    def __init__(
        self,
        managers: Sequence[PackageManager],
        *,
        stamp: str = UPDATE_STAMP,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._resolvers = {
            manager.name: _Resolver(manager, stamp=stamp.format(manager=manager.name), sleep=sleep)
            for manager in managers
        }

    def service(self) -> Service:
        return Service(
            name="sysdeps",
            bodies=BODIES,
            handlers={"ResolveSystemDepsReq": self.resolve},
            run=self.run,
        )

    async def resolve(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.ResolveSystemDepsReq)
        resolver = self._resolvers.get(req.packageManager.value)
        if resolver is None:
            known = ", ".join(sorted(self._resolvers))
            raise ProtocolError(
                "bad-request", f"no package manager {req.packageManager.value!r} (known: {known})"
            )
        return await resolver.resolve(call)

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            for resolver in self._resolvers.values():
                tg.create_task(resolver.run())


class _Resolver:
    """Resolving through one package manager: its queue, its worker, its
    regular refresh."""

    def __init__(
        self,
        packages: PackageManager,
        *,
        stamp: str,
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._packages = packages
        self._stamp = stamp
        self._sleep = sleep
        self._queue: asyncio.Queue[_Install | _Refresh] = asyncio.Queue()
        # Every dependency queued or being installed → its coming status.
        self._pending: dict[str, asyncio.Future[g.DepStatus]] = {}
        # Every dependency queued or being installed → the requests waiting
        # on it: they get the package manager's output of its job.
        self._watchers: dict[str, list[Call]] = {}
        # Every dependency made ready: what the regular refresh upgrades.
        self._known: set[str] = set()
        # Set once something is known: before that there is nothing to refresh.
        self._something_known = asyncio.Event()
        self._last_update = _read_stamp(self._stamp)

    # -- the request ---------------------------------------------------------

    async def resolve(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.ResolveSystemDepsReq)
        deps = list(dict.fromkeys(req.deps))  # deduplicated, in request order
        bad = [dep for dep in deps if not _PACKAGE.match(dep)]
        if bad:
            raise ProtocolError("bad-request", f"not a package name: {', '.join(bad)}")

        settled: dict[str, g.DepStatus] = {}
        waiting: dict[str, asyncio.Future[g.DepStatus]] = {}
        missing: list[str] = []
        for dep in deps:
            if dep not in self._pending:
                version = await self._packages.version(dep, report=call.report)
                if version is not None:
                    settled[dep] = self._ready(dep, version)
                    await self._emit(call, settled[dep])
                    continue
            future = self._pending.get(dep)  # it may have been queued meanwhile
            if future is None:
                future = self._pending[dep] = asyncio.get_running_loop().create_future()
                missing.append(dep)
            waiting[dep] = future
            self._watchers.setdefault(dep, []).append(call)
        if missing:
            await call.log("info", f"queued for install: {' '.join(missing)}")
            self._queue.put_nowait(_Install(missing))

        try:
            return await self._wait(call, req, deps, settled, waiting)
        finally:  # done (or timed out): no more package output for this request
            for dep in deps:
                watchers = self._watchers.get(dep, [])
                watchers[:] = [w for w in watchers if w is not call]
                if not watchers:
                    self._watchers.pop(dep, None)

    async def _wait(
        self,
        call: Call,
        req: g.ResolveSystemDepsReq,
        deps: list[str],
        settled: dict[str, g.DepStatus],
        waiting: dict[str, asyncio.Future[g.DepStatus]],
    ) -> g.ResolveSystemDepsRes:
        deadline = None if req.timeout is None else time.monotonic() + req.timeout
        while waiting:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, _ = await asyncio.wait(
                set(waiting.values()), timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                break  # the timeout; the futures stay with the queue
            for dep, future in list(waiting.items()):
                if future in done:
                    settled[dep] = future.result()
                    del waiting[dep]
                    await self._emit(call, settled[dep])
        for dep in waiting:
            settled[dep] = g.DepStatus(
                dep=dep,
                state=g.SystemDepState.timeout,
                message=f"not settled within {req.timeout:g}s; the install continues",
            )
            await self._emit(call, settled[dep])
        return g.ResolveSystemDepsRes(
            kind="ResolveSystemDepsRes", deps=[settled[dep] for dep in deps]
        )

    def _ready(self, dep: str, version: str) -> g.DepStatus:
        self._known.add(dep)
        self._something_known.set()
        return g.DepStatus(dep=dep, state=g.SystemDepState.ready, message=version)

    async def _emit(self, call: Call, status: g.DepStatus) -> None:
        await call.emit(
            g.ResolveSystemDep(
                kind="ResolveSystemDep", dep=status.dep, state=status.state, message=status.message
            )
        )

    # -- the long-running part -----------------------------------------------

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._worker())
            tg.create_task(self._refresher())

    async def _refresher(self) -> None:
        """Queue the regular refresh whenever it is due — but only once a
        dependency has been made ready: with nothing requested there is
        nothing to keep current (an install refreshes stale lists itself)."""
        await self._something_known.wait()
        while True:
            due = self._last_update + UPDATE_EVERY - time.time()
            if due > 0:
                await self._sleep(due)
                continue
            job = _Refresh(asyncio.Event())
            self._queue.put_nowait(job)
            await job.done.wait()
            if self._last_update + UPDATE_EVERY <= time.time():  # it failed
                await self._sleep(RETRY_REFRESH)

    async def _worker(self) -> None:
        """The only place the package manager runs: one job at a time, in
        queue order."""
        while True:
            job = await self._queue.get()
            try:
                if isinstance(job, _Install):
                    await self._install(job.deps)
                else:
                    await self._refresh()
            except asyncio.CancelledError:
                if isinstance(job, _Install):
                    self._fail(job.deps, "the resolver stopped")
                raise
            except Exception as exc:  # noqa: BLE001 — a job never kills the worker
                log.exception("%s job failed", self._packages.name)
                if isinstance(job, _Install):
                    self._fail(job.deps, f"{type(exc).__name__}: {exc}")
            finally:
                if isinstance(job, _Refresh):
                    job.done.set()

    async def _install(self, deps: list[str]) -> None:
        report = self._report_to(deps)
        if time.time() - self._last_update > UPDATE_EVERY:
            await self._update(report)
        ok, out = await self._retry(
            f"{self._packages.name} install", lambda: self._packages.install(deps, report=report)
        )
        outputs = {dep: (ok, out) for dep in deps}
        if not ok and len(deps) > 1:
            # The manager refuses the whole transaction for one bad package: try
            # each on its own, so one only fails itself.
            log.warning("installing %s together failed; one by one", " ".join(deps))
            for dep in deps:
                outputs[dep] = await self._retry(
                    f"{self._packages.name} install {dep}",
                    lambda dep=dep: self._packages.install([dep], report=report),  # type: ignore[misc]
                )
        # The check after the install is reported to the waiting requests
        # too; their statuses only go out after it (below).
        results: list[g.DepStatus] = []
        for dep in deps:
            version = await self._packages.version(dep, report=report)
            if version is not None:
                results.append(self._ready(dep, version))
                log.info("%s installed (%s)", dep, version)
            else:
                dep_ok, dep_out = outputs[dep]
                message = (
                    _tail(dep_out)
                    if not dep_ok
                    else f"{self._packages.name} install succeeded but it is not installed"
                )
                results.append(g.DepStatus(dep=dep, state=g.SystemDepState.failed, message=message))
                log.warning("%s not installed: %s", dep, message)
        for status in results:
            self._finish(status.dep, status)

    async def _refresh(self) -> None:
        known = sorted(self._known)
        if not known or not await self._update(command.nowhere):  # no one waits on it
            return
        await self._retry(f"{self._packages.name} upgrade", lambda: self._packages.upgrade(known))

    def _report_to(self, deps: list[str]) -> Report:
        """A Report for a job for `deps`: each line goes, in order, to every
        request waiting on one of them — the worker runs outside any
        request, so it fans out itself."""

        def report(level: str, line: str) -> None:
            for call in {call for dep in deps for call in self._watchers.get(dep, [])}:
                call.report(level, line)

        return report

    async def _update(self, report: Report) -> bool:
        ok, _ = await self._retry(
            f"{self._packages.name} refresh", lambda: self._packages.refresh(report=report)
        )
        if ok:
            self._last_update = time.time()
            try:
                await asyncio.to_thread(_write_stamp, self._stamp, self._last_update)
            except OSError as exc:
                log.warning("cannot save the index refresh time to %s: %s", self._stamp, exc)
        return ok

    async def _retry(
        self, label: str, run: Callable[[], Awaitable[tuple[bool, str]]]
    ) -> tuple[bool, str]:
        delay = 5.0
        for attempt in range(1, RETRIES + 1):
            ok, out = await run()
            if ok:
                return ok, out
            if self._packages.permanent(out):  # retrying cannot help
                log.warning("%s failed: %s", label, _tail(out))
                return False, out
            if attempt < RETRIES:
                log.warning(
                    "%s: attempt %d/%d failed; retrying in %gs", label, attempt, RETRIES, delay
                )
                await self._sleep(delay)
                delay = min(delay * 2, 300.0)
        log.error("%s: gave up after %d attempts: %s", label, RETRIES, _tail(out))
        return False, out

    def _finish(self, dep: str, status: g.DepStatus) -> None:
        future = self._pending.pop(dep, None)
        if future is not None and not future.done():
            future.set_result(status)

    def _fail(self, deps: list[str], message: str) -> None:
        for dep in deps:
            self._finish(dep, g.DepStatus(dep=dep, state=g.SystemDepState.failed, message=message))


def _read_stamp(path: str) -> float:
    try:
        with open(path, encoding="utf-8") as handle:
            return float(handle.read().strip())
    except (OSError, ValueError):
        return 0.0


def _write_stamp(path: str, when: float) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(when))


def service() -> Service:
    return SystemDeps(list(MANAGERS.values())).service()
