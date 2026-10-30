"""The system dependency resolver: makes apt packages present and keeps
them current.

    ResolveSystemDepsReq { deps: [...], timeout? }
      → ResolveSystemDep { dep, state }   one event per dependency, as it settles
      → ResolveSystemDepsRes { deps: [DepStatus] }

All apt work goes through ONE queue drained by ONE worker, so apt never
runs twice at a time from here (another apt on the host —
unattended-upgrades — is waited for by apt itself: `DPkg::Lock::Timeout`).
The queue carries two kinds of jobs:

- install: the missing dependencies of a request. The package lists are
  refreshed first when they are stale (`apt-get update`, at most every
  UPDATE_EVERY, the time persisted in a stamp file across restarts).
- refresh: the regular maintenance, every UPDATE_EVERY — `apt-get update`,
  then `apt-get install --only-upgrade` of every dependency this resolver
  has made ready (it keeps them current, as the old deployer's daily pass
  did). Only once something was requested: with no dependency there is
  nothing to keep current.

Whether a dependency is there is `dpkg-query`'s `install ok installed`;
its version is reported with `ready`. Every apt command is retried with
backoff (RETRIES attempts, 5s doubling up to 300s).

A dependency already queued or being installed for another request is not
queued again: both requests wait for the same result. A request's
`timeout` only bounds how long it waits: what has not settled by then is
reported `timeout`, but the install is never killed (interrupting dpkg
can leave the package database broken) — it finishes in the queue, and a
later request finds the package installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any, Protocol

from .. import command
from .. import generated as g
from ..command import LineSink
from ..message import BodySpec, Msg, ProtocolError, register_bodies
from ..peer import Peer
from ..service import Service
from ..transport import Closed

log = logging.getLogger("rt.sysdeps")

BODIES = {
    "ResolveSystemDepsReq": BodySpec(g.ResolveSystemDepsReq, "request"),
    "ResolveSystemDep": BodySpec(g.ResolveSystemDep, "event"),
    "ResolveSystemDepsRes": BodySpec(g.ResolveSystemDepsRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

# A Debian package name (optionally :arch). Anything else is refused: the
# name ends up in apt's argv, and must never look like an option.
_PACKAGE = re.compile(r"^[a-z0-9][a-z0-9+.-]+(:[a-z0-9-]+)?$")

UPDATE_EVERY = 24 * 60 * 60
# After a failed refresh, try again this much later (not at once).
RETRY_REFRESH = 60 * 60
UPDATE_STAMP = "/var/lib/ovn-fabric/last-apt-update"
RETRIES = 6


class Apt(Protocol):
    """What the resolver needs from the package manager."""

    async def version(self, package: str) -> str | None:
        """The installed version, or None when it is not installed."""

    async def update(self, on_line: LineSink | None = None) -> tuple[bool, str]: ...

    async def install(
        self, packages: Sequence[str], on_line: LineSink | None = None
    ) -> tuple[bool, str]: ...

    async def upgrade(
        self, packages: Sequence[str], on_line: LineSink | None = None
    ) -> tuple[bool, str]: ...


class SystemApt:
    """The host's apt/dpkg."""

    _ENV = {
        "DEBIAN_FRONTEND": "noninteractive",
        "APT_LISTCHANGES_FRONTEND": "none",
        "LC_ALL": "C",
        "TERM": "dumb",
    }
    _OPTIONS = (
        "-o",
        "Dpkg::Use-Pty=0",
        "-o",
        "Dpkg::Progress-Fancy=0",
        # Wait for another apt/dpkg on the host instead of failing.
        "-o",
        "DPkg::Lock::Timeout=600",
    )

    async def version(self, package: str) -> str | None:
        ok, out = await self._run(["dpkg-query", "-W", "-f=${Status} ${Version}", package])
        if not ok or not out.startswith("install ok installed"):
            return None
        return out.rsplit(" ", 1)[-1].strip()

    async def update(self, on_line: LineSink | None = None) -> tuple[bool, str]:
        return await self._run(["apt-get", *self._OPTIONS, "update"], on_line)

    async def install(
        self, packages: Sequence[str], on_line: LineSink | None = None
    ) -> tuple[bool, str]:
        return await self._run(["apt-get", *self._OPTIONS, "install", "-y", *packages], on_line)

    async def upgrade(
        self, packages: Sequence[str], on_line: LineSink | None = None
    ) -> tuple[bool, str]:
        return await self._run(
            ["apt-get", *self._OPTIONS, "install", "--only-upgrade", "-y", *packages], on_line
        )

    async def _run(self, argv: list[str], on_line: LineSink | None = None) -> tuple[bool, str]:
        return await command.run(argv, log=log, env=self._ENV, on_line=on_line)


def _tail(output: str, lines: int = 5) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


@dataclasses.dataclass(frozen=True)
class _Install:
    deps: list[str]


@dataclasses.dataclass(frozen=True)
class _Refresh:
    done: asyncio.Event


class SystemDeps:
    def __init__(
        self,
        apt: Apt | None = None,
        *,
        stamp: str = UPDATE_STAMP,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._apt: Apt = apt if apt is not None else SystemApt()
        self._stamp = stamp
        self._sleep = sleep
        self._queue: asyncio.Queue[_Install | _Refresh] = asyncio.Queue()
        # Every dependency queued or being installed → its coming status.
        self._pending: dict[str, asyncio.Future[g.DepStatus]] = {}
        # Every dependency queued or being installed → the requests waiting
        # on it (peer, reqId): they get the apt output of its job.
        self._watchers: dict[str, list[tuple[Peer, str]]] = {}
        # Every dependency made ready: what the regular refresh upgrades.
        self._known: set[str] = set()
        # Set once something is known: before that there is nothing to refresh.
        self._something_known = asyncio.Event()
        self._last_update = _read_stamp(stamp)

    def service(self) -> Service:
        return Service(
            name="sysdeps",
            bodies=BODIES,
            handlers={"ResolveSystemDepsReq": self.resolve},
            run=self.run,
        )

    # -- the request ---------------------------------------------------------

    async def resolve(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
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
                version = await self._apt.version(dep)
                if version is not None:
                    settled[dep] = self._ready(dep, version)
                    await self._emit(peer, settled[dep])
                    continue
            future = self._pending.get(dep)  # it may have been queued meanwhile
            if future is None:
                future = self._pending[dep] = asyncio.get_running_loop().create_future()
                missing.append(dep)
            waiting[dep] = future
            assert msg.req_id is not None
            self._watchers.setdefault(dep, []).append((peer, msg.req_id))
        if missing:
            await peer.log("info", f"queued for install: {' '.join(missing)}")
            self._queue.put_nowait(_Install(missing))

        try:
            return await self._wait(peer, req, deps, settled, waiting)
        finally:  # done (or timed out): no more apt output for this request
            for dep in deps:
                watchers = self._watchers.get(dep, [])
                watchers[:] = [w for w in watchers if w != (peer, msg.req_id)]
                if not watchers:
                    self._watchers.pop(dep, None)

    async def _wait(
        self,
        peer: Peer,
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
                    await self._emit(peer, settled[dep])
        for dep in waiting:
            settled[dep] = g.DepStatus(
                dep=dep,
                state=g.SystemDepState.timeout,
                message=f"not settled within {req.timeout:g}s; the install continues",
            )
            await self._emit(peer, settled[dep])
        return g.ResolveSystemDepsRes(
            kind="ResolveSystemDepsRes", deps=[settled[dep] for dep in deps]
        )

    def _ready(self, dep: str, version: str) -> g.DepStatus:
        self._known.add(dep)
        self._something_known.set()
        return g.DepStatus(dep=dep, state=g.SystemDepState.ready, message=version)

    async def _emit(self, peer: Peer, status: g.DepStatus) -> None:
        await peer.emit(
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
        """The only place apt runs: one job at a time, in queue order."""
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
                log.exception("apt job failed")
                if isinstance(job, _Install):
                    self._fail(job.deps, f"{type(exc).__name__}: {exc}")
            finally:
                if isinstance(job, _Refresh):
                    job.done.set()

    async def _install(self, deps: list[str]) -> None:
        async with self._forward(deps) as on_line:
            if time.time() - self._last_update > UPDATE_EVERY:
                await self._update(on_line)
            ok, out = await self._retry("apt-get install", lambda: self._apt.install(deps, on_line))
        for dep in deps:
            version = await self._apt.version(dep)
            if version is not None:
                self._finish(dep, self._ready(dep, version))
                log.info("%s installed (%s)", dep, version)
            else:
                message = _tail(out) if not ok else "apt-get succeeded but it is not installed"
                self._finish(
                    dep, g.DepStatus(dep=dep, state=g.SystemDepState.failed, message=message)
                )
                log.warning("%s not installed: %s", dep, message)

    async def _refresh(self) -> None:
        known = sorted(self._known)
        if not known or not await self._update():
            return
        await self._retry("apt-get --only-upgrade", lambda: self._apt.upgrade(known))

    @contextlib.asynccontextmanager
    async def _forward(self, deps: list[str]) -> AsyncIterator[LineSink]:
        """A line sink for the apt output of a job for `deps`: each line goes,
        in order, as a debug LogMsg to every request waiting on one of them."""
        lines: asyncio.Queue[str | None] = asyncio.Queue()

        async def forward() -> None:
            while (line := await lines.get()) is not None:
                body = g.LogMsg(kind="LogMsg", level=g.LogLevel.debug, message=line)
                targets = {w for dep in deps for w in self._watchers.get(dep, [])}
                for peer, req_id in targets:
                    with contextlib.suppress(Closed):
                        await peer.send(body, req_id=req_id)

        task = asyncio.get_running_loop().create_task(forward())
        try:
            yield lines.put_nowait
        finally:
            lines.put_nowait(None)
            await task

    async def _update(self, on_line: LineSink | None = None) -> bool:
        ok, _ = await self._retry("apt-get update", lambda: self._apt.update(on_line))
        if ok:
            self._last_update = time.time()
            try:
                await asyncio.to_thread(_write_stamp, self._stamp, self._last_update)
            except OSError as exc:
                log.warning("cannot save the apt update time to %s: %s", self._stamp, exc)
        return ok

    async def _retry(
        self, label: str, run: Callable[[], Awaitable[tuple[bool, str]]]
    ) -> tuple[bool, str]:
        delay = 5.0
        for attempt in range(1, RETRIES + 1):
            ok, out = await run()
            if ok:
                return ok, out
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
    return SystemDeps().service()
