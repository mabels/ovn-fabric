"""Jobs: long requests that belong to the runtime, not to a connection
(ADR 0012 §7).

A service names the requests it wants run this way (`Service.detached`).
The runtime starts such a request's handler as a task of its own; what the
handler emits (log lines, progress) goes into the job, which keeps it and
hands it to every follower — first the connection that sent the request,
later anyone sending `FollowReq {reqId}`. When a follower's connection
closes, only that follower goes; the job runs on.

Finished jobs are kept (the last `KEEP_FINISHED`) so a run can be looked at
after it ended; the journal (runtime/journal.py) has every line anyway.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from . import generated as g
from .message import ProtocolError

log = logging.getLogger("runtime.jobs")

KEEP_FINISHED = 20
# What a job keeps for a follower; past it the oldest go (the journal has
# them).
KEEP_MESSAGES = 50_000


class Job:
    def __init__(self, req_id: str, request: str) -> None:
        self.req_id = req_id
        self.request = request
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.messages: list[object] = []
        self.dropped = 0
        self.final: object | None = None
        self._changed = asyncio.Condition()

    @property
    def state(self) -> str:
        if self.final is None:
            return "running"
        return "failed" if isinstance(self.final, g.ErrorRes) else "done"

    def add(self, body: object) -> None:
        self.messages.append(body)
        if len(self.messages) > KEEP_MESSAGES:
            del self.messages[0]
            self.dropped += 1
        asyncio.get_running_loop().create_task(self._notify())

    async def finish(self, body: object) -> None:
        self.final = body
        self.finished_at = time.time()
        await self._notify()

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def follow(self, emit: Callable[[object], Awaitable[None]]) -> object:
        """Hand every message to `emit`, from the first kept one on, until the
        job is done; its final response."""
        seen = self.dropped
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: seen < self.dropped + len(self.messages) or self.final is not None
                )
                start = max(seen - self.dropped, 0)
                batch = self.messages[start:]
                seen = self.dropped + len(self.messages)
                final = self.final
            for body in batch:
                await emit(body)
            if final is not None and seen == self.dropped + len(self.messages):
                return final

    def info(self) -> g.JobInfo:
        return g.JobInfo(
            reqId=self.req_id,
            request=self.request,
            state=g.JobState(self.state),
            startedAt=self.started_at,
            finishedAt=self.finished_at,
            messages=len(self.messages),
        )


class JobOutbox:
    """A detached Call's outbox (runtime/forward.py's shape): everything it
    emits goes into its job."""

    def __init__(self, job: Job) -> None:
        self._job = job

    def put(self, body: object) -> bool:
        self._job.add(body)
        return True

    async def close(self) -> None:
        return None

    def abort(self) -> None:
        return None


class Jobs:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def start(self, job: Job, run: Callable[[], Awaitable[object]]) -> None:
        """Run `run` for `job` as a task of the runtime; its result (or its
        failure, as an ErrorRes) is the job's final response."""
        self._jobs[job.req_id] = job

        async def body() -> None:
            try:
                result = await run()
            except asyncio.CancelledError:
                await job.finish(g.ErrorRes(kind="ErrorRes", code="cancelled", message="stopped"))
                raise
            except ProtocolError as exc:
                result = g.ErrorRes(kind="ErrorRes", code=exc.code, message=str(exc))
            except Exception as exc:  # noqa: BLE001 — every failure is the job's answer
                log.exception("job %s (%s) failed", job.req_id, job.request)
                result = g.ErrorRes(
                    kind="ErrorRes", code="failed", message=f"{type(exc).__name__}: {exc}"
                )
            await job.finish(result)
            self._forget_old()

        task = asyncio.get_running_loop().create_task(body(), name=f"job {job.req_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def get(self, req_id: str) -> Job:
        job = self._jobs.get(req_id)
        if job is None:
            raise ProtocolError("not-found", f"no job {req_id!r}")
        return job

    def infos(self) -> list[g.JobInfo]:
        return [job.info() for job in sorted(self._jobs.values(), key=lambda j: j.started_at)]

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _forget_old(self) -> None:
        done = sorted(
            (job for job in self._jobs.values() if job.final is not None),
            key=lambda job: job.finished_at or 0.0,
        )
        for job in done[:-KEEP_FINISHED]:
            del self._jobs[job.req_id]


async def follow(job: Job, emit: Callable[[object], Awaitable[None]]) -> object:
    """Follow `job` as a request handler does: its final response, an
    ErrorRes raised again as the same error."""
    final = await job.follow(emit)
    if isinstance(final, g.ErrorRes):
        raise ProtocolError(final.code, final.message)
    return final
