"""The ovn-fabric runtime.

A small async job tree. Builders register lifecycle and event hooks;
`run()` drives start/stop.

    root = Job("ovn-fabric")

    deps = root.job("installQueue", initiate=True)
    deps.on_start(install_missing)        # async def install_missing(job)
    deps.on_stop(stop_deps)               # async def stop_deps(job)

    service = deps.job("ovn-kernel-one", initiate=True)
    service.on_start(start_router)
    service.on_stop(stop_router)
    service.on_emit(on_dependency_event)  # async def (job, event)

    root.on_emit(log_event)               # async def (job, event)
    service.warn("no uplink yet")         # {"kind": "log", "name": "ovn-kernel-one", ...}

    asyncio.run(root.run())

Semantics:

- Starting: a job starts once its parent's start hooks have settled
  (succeeded or failed). `initiate=True` starts it right away instead, so
  a hanging parent can be bypassed. A child added with `job()` while the
  tree is running is started immediately (a dynamic job).
- Hooks: every hook runs in its own task, so a slow or failing job never
  blocks a sibling. Failures are logged, not raised.
- Events: `emit()` broadcasts to the `on_emit` hooks of EVERY job in the
  tree, the emitter included. Log events (`kind == "log"`) instead travel
  UP to the ancestors only, so the root can do the central logging. The
  runtime itself emits `{"kind": "runtime", "state": "start" | "ready" |
  "stop", "job": name}`.
- Stopping: `stop()` cancels the job's in-flight hooks, kills its
  processes, runs its stop hooks, then stops its children (last first).
  It then stops the parent too, unless `no_parent=True`.
- `run()` starts the tree, stops each leaf as soon as its start hooks have
  settled, and returns once nothing is running any more (or the root was
  stopped).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import sys
from collections.abc import Callable, Coroutine, Iterator
from typing import Any, Literal, TypedDict

# The event payload is opaque to the runtime, except for the log shape
# `Job.log()` emits.
Event = Any
LogLevel = Literal["debug", "info", "warn", "error"]
LOG_LEVELS: tuple[LogLevel, ...] = ("debug", "info", "warn", "error")


class LogEvent(TypedDict):
    kind: Literal["log"]
    level: LogLevel
    # The name of the job that logged, added by `Job.log()`.
    name: str
    message: str


JobState = Literal["pending", "starting", "ready", "stopping", "stopped"]


class ProcessStatus(TypedDict):
    pid: int
    # The command line, shell-quoted ("" if it was registered without one).
    cmd: str


class JobStatus(TypedDict):
    name: str
    state: JobState
    initiate: bool
    # Hook tasks still running (start/emit hooks, the readiness watcher).
    tasks: int
    # The processes this job would kill on stop().
    processes: list[ProcessStatus]
    children: list[JobStatus]


StartHook = Callable[["Job"], Coroutine[Any, Any, None]]
StopHook = Callable[["Job"], Coroutine[Any, Any, None]]
EmitHook = Callable[..., Coroutine[Any, Any, None]]
Action = Callable[["Job"], Coroutine[Any, Any, None]]
Configure = Callable[["Job"], None]

log = logging.getLogger("ovn-fabric.runtime")


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("job hook failed", exc_info=task.exception())


class Job:
    """One node of the job tree, and the handle hooks receive."""

    def __init__(
        self,
        name: str,
        *,
        initiate: bool = False,
        parent: Job | None = None,
        ctx: Any = None,
    ) -> None:
        self.name = name
        self.initiate = initiate
        self.parent = parent
        # Optional per-job context, handed to on_emit hooks that accept it.
        self.ctx: Any = ctx if ctx is not None or parent is None else parent.ctx
        self.children: list[Job] = []

        self._start_hooks: list[StartHook] = []
        self._stop_hooks: list[StopHook] = []
        self._emit_hooks: list[EmitHook] = []

        # Hook tasks running on behalf of this job; stop() cancels them.
        self._tasks: set[asyncio.Task[None]] = set()
        # Processes started by this job (run_cmd), with their command line;
        # stop() kills them.
        self._processes: dict[asyncio.subprocess.Process, str] = {}
        # Set once this job's start hooks have settled.
        self._ready = asyncio.Event()
        # Only used on the root: "something changed", wakes up run().
        self._wakeup = asyncio.Event()
        self._started = False
        self._stopped = False
        # A held job is waiting (e.g. on a dependency event): run() must
        # not treat it as a completed leaf.
        self._held = False
        # Set once _stop() has finished (it may run in another task).
        self._stop_done = asyncio.Event()

    # -- building -----------------------------------------------------------

    def job(
        self,
        name: str,
        initiate: bool = False,
        configure: Configure | None = None,
        ctx: Any = None,
    ) -> Job:
        """Add a child job. On a running tree it starts right away."""
        child = Job(name, initiate=initiate, parent=self, ctx=ctx)
        self.children.append(child)
        if configure is not None:
            configure(child)
        if self._started and not self._stopped:
            child._spawn(child._start())
            self._root._wake()
        return child

    def on_start(self, hook: StartHook) -> StartHook:
        self._start_hooks.append(hook)
        return hook

    def on_stop(self, hook: StopHook) -> StopHook:
        self._stop_hooks.append(hook)
        return hook

    def on_emit(self, hook: EmitHook) -> EmitHook:
        self._emit_hooks.append(hook)
        return hook

    # -- events and logging -------------------------------------------------

    def emit(self, event: Event) -> None:
        """Deliver `event` without waiting: each hook runs in its own task.

        Log events go to the ancestors; everything else to every job."""
        if not self._root._started:
            raise RuntimeError(f"{self.name}: emit() before start()")
        if isinstance(event, dict) and event.get("kind") == "log":
            self._trace("emit called (log -> root): %r", event)
            receivers: Iterator[Job] = self._ancestors()
        else:
            self._trace("emit called: %r", event)
            receivers = self._root._subtree()
        for job in receivers:
            for hook in job._emit_hooks:
                if job.ctx is None:
                    job._spawn(hook(job, event))
                else:
                    job._spawn(hook(job, event, job.ctx))

    def log(self, level: LogLevel, message: str) -> None:
        """Emit a log event for this job; it bubbles up to the root."""
        if level not in LOG_LEVELS:
            raise ValueError(f"unknown log level {level!r}")
        event: LogEvent = {"kind": "log", "level": level, "name": self.name, "message": message}
        self.emit(event)

    def debug(self, message: str) -> None:
        self.log("debug", message)

    def info(self, message: str) -> None:
        self.log("info", message)

    def warn(self, message: str) -> None:
        self.log("warn", message)

    def error(self, message: str) -> None:
        self.log("error", message)

    # -- processes ----------------------------------------------------------

    async def run_cmd(
        self,
        label: str,
        cmd: list[str],
        abort_on_error: bool = True,
        timeout: float | None = None,
    ) -> bool:
        """Run `cmd` and wait for it. stop() kills it if it is still running.

        Only the calling hook waits; sibling hooks run in their own tasks.
        `timeout` kills the process group and applies `abort_on_error`.
        Returns whether the command exited 0."""
        ok, _ = await self._exec(label, cmd, abort_on_error, capture=False, timeout=timeout)
        return ok

    async def capture_cmd(
        self,
        label: str,
        cmd: list[str],
        abort_on_error: bool = True,
        timeout: float | None = None,
    ) -> str:
        """Like run_cmd(), but return stdout ("" on failure)."""
        _ok, stdout = await self._exec(label, cmd, abort_on_error, capture=True, timeout=timeout)
        return stdout.decode(errors="replace").strip()

    def register_process(
        self, proc: asyncio.subprocess.Process, cmd: list[str] | None = None
    ) -> None:
        """Bind a process to this job so stop() kills it. `cmd` is only
        for status()."""
        if not self._root._started:
            raise RuntimeError(f"{self.name}: child process before start()")
        self._processes[proc] = shlex.join(cmd) if cmd is not None else ""

    def unregister_process(self, proc: asyncio.subprocess.Process) -> None:
        self._processes.pop(proc, None)

    # -- driving the tree ---------------------------------------------------

    def hold(self) -> None:
        """Keep this job alive while it waits for an external event."""
        self._held = True
        self._wake()

    def release(self) -> None:
        self._held = False
        self._wake()

    async def start(self) -> None:
        """Start the tree. Returns once the hooks are scheduled, not done."""
        self._trace("start called")
        if self._started:
            raise RuntimeError(f"{self.name}: start() called twice")
        await self._start()

    async def stop(self, no_parent: bool = False) -> None:
        """Stop this job and its children, then (by default) the parent."""
        self._trace("stop called (no_parent=%s)", no_parent)
        await self._stop()
        if self.parent is not None and not no_parent:
            await self.parent.stop()

    async def run(self) -> None:
        """Start the tree and drive it until nothing is left running."""
        await self.start()
        while not self._stopped:
            for job in list(self._subtree()):
                if not job.children and job._ready.is_set() and not job._held:
                    await job._stop()
            if not self._busy():
                break
            await self._wakeup.wait()
            self._wakeup.clear()
        await self._stop()
        # A stop started from a hook runs in that hook's task; don't return
        # (and let the loop cancel it) before it has finished.
        await asyncio.gather(*(job._stop_done.wait() for job in self._subtree() if job._stopped))

    # -- inspection ---------------------------------------------------------

    @property
    def state(self) -> JobState:
        if self._stop_done.is_set():
            return "stopped"
        if self._stopped:
            return "stopping"
        if self._ready.is_set():
            return "ready"
        if self._started:
            return "starting"
        return "pending"

    def status(self) -> JobStatus:
        """A snapshot of this job and its subtree."""
        return {
            "name": self.name,
            "state": self.state,
            "initiate": self.initiate,
            "tasks": len(self._tasks),
            "processes": [
                {"pid": proc.pid, "cmd": cmd}
                for proc, cmd in sorted(self._processes.items(), key=lambda item: item[0].pid)
            ],
            "children": [child.status() for child in self.children],
        }

    def format_status(self) -> str:
        """status() as an indented tree, one job per line."""
        return "\n".join(_format_status(self.status(), depth=0))

    # -- internals ----------------------------------------------------------

    @property
    def _root(self) -> Job:
        job = self
        while job.parent is not None:
            job = job.parent
        return job

    def _ancestors(self) -> Iterator[Job]:
        job = self.parent
        while job is not None:
            yield job
            job = job.parent

    def _subtree(self) -> Iterator[Job]:
        yield self
        for child in self.children:
            yield from child._subtree()

    def _busy(self) -> bool:
        """True while a job in this subtree is still starting (or waiting for
        its parent), has processes running, or is in the middle of stopping."""
        if self._held:
            return True
        if self._stopped:
            running = not self._stop_done.is_set()
        else:
            running = not self._ready.is_set() or bool(self._processes)
        return running or any(child._busy() for child in self.children)

    def _wake(self) -> None:
        self._root._wakeup.set()

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_task_failure)
        return task

    def _trace(self, message: str, *args: object) -> None:
        """Runtime-internal debug log (the Python logger, not an event)."""
        log.debug("%s: " + message, self.name, *args)

    def _lifecycle(self, state: Literal["start", "ready", "stop"]) -> None:
        self.emit({"kind": "runtime", "state": state, "job": self.name})

    async def _start(self) -> None:
        if not self.initiate and self.parent is not None:
            self._trace("start waits for %s to be ready", self.parent.name)
            await self.parent._ready.wait()
        if self._stopped:
            self._trace("start skipped: already stopped")
            return
        self._trace("enter start")
        # No await from here on: job() relies on _started meaning "the
        # children list has been launched".
        self._started = True
        self._lifecycle("start")
        hooks = [self._spawn(hook(self)) for hook in self._start_hooks]
        self._spawn(self._become_ready(hooks))
        for child in self.children:
            child._spawn(child._start())
        self._trace("left start: %d start hook(s) running", len(hooks))

    async def _become_ready(self, hooks: list[asyncio.Task[None]]) -> None:
        if hooks:
            # wait(), not gather(): cancelling this watcher must not cancel
            # the start hooks (a hook may call stop() itself).
            await asyncio.wait(hooks)
        self._ready.set()
        self._wake()
        self._lifecycle("ready")

    async def _stop(self) -> None:
        if self._stopped:
            self._trace("stop skipped: already stopped")
            return
        self._trace("enter stop")
        self._stopped = True
        self._held = False
        self._wake()
        try:
            if self._root._started:
                self._lifecycle("stop")
                # Let the stop observers start before our tasks are cancelled.
                await asyncio.sleep(0)
            await self._cancel_tasks()
            # Kill what the cancelled start hooks left running BEFORE the stop
            # hooks, and again afterwards for anything the stop hooks started.
            await self._kill_processes()
            await self._run_stop_hooks()
            await self._kill_processes()
            for child in reversed(self.children):
                await child._stop()
        finally:
            self._stop_done.set()
            self._wake()
            self._trace("left stop")

    async def _cancel_tasks(self) -> None:
        # The current task may be one of ours: a hook that called stop().
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_stop_hooks(self) -> None:
        results = await asyncio.gather(
            *(hook(self) for hook in self._stop_hooks), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                log.error("%s: stop hook failed", self.name, exc_info=result)

    async def _kill_processes(self) -> None:
        for proc in list(self._processes):
            await _kill(proc)
        self._processes.clear()

    async def _exec(
        self,
        label: str,
        cmd: list[str],
        abort_on_error: bool,
        *,
        capture: bool,
        timeout: float | None = None,
    ) -> tuple[bool, bytes]:
        """Run `cmd` bound to this job; return (ok, stdout)."""
        line = shlex.join(cmd)
        self.debug(f"{label}$ {line}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE if capture else None,
                # Its own process group, so _kill() takes everything it spawned.
                start_new_session=True,
            )
        except OSError as exc:
            self._trace("run_cmd %s failed to start: %s", label, exc)
            detail = f" ({exc.strerror})" if exc.strerror else ""
            self._fail(abort_on_error, 127, f"cannot run in {label}{detail}: {line}")
            return False, b""
        self.register_process(proc, cmd)
        self._trace("run_cmd %s started: pid %d: %s", label, proc.pid, line)
        try:
            if timeout is None:
                stdout, _ = await proc.communicate()
            else:
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    self._trace("run_cmd %s timed out after %ss: pid %d", label, timeout, proc.pid)
                    await _kill(proc)
                    self.unregister_process(proc)
                    self._fail(
                        abort_on_error,
                        124,
                        f"timed out after {timeout:g}s in {label}: {line}",
                    )
                    return False, b""
        except asyncio.CancelledError:
            self._trace("run_cmd %s cancelled: pid %d left for stop() to kill", label, proc.pid)
            raise
        finally:
            # On cancellation leave it registered: stop() must kill it.
            if proc.returncode is not None:
                self.unregister_process(proc)
        self._trace("run_cmd %s completed: pid %d exit %d", label, proc.pid, proc.returncode)
        if proc.returncode:
            self._fail(abort_on_error, proc.returncode, f"failed in {label}: {line}")
            return False, stdout or b""
        return True, stdout or b""

    def _fail(self, abort: bool, exit_code: int, message: str) -> None:
        if abort:
            self.error(message)
            sys.exit(exit_code)
        self.warn(message)


def _format_status(status: JobStatus, depth: int) -> Iterator[str]:
    line = f"{'  ' * depth}{status['name']}: {status['state']}"
    if status["initiate"]:
        line += " initiate"
    if status["tasks"]:
        line += f" tasks={status['tasks']}"
    yield line
    for proc in status["processes"]:
        yield f"{'  ' * (depth + 1)}$ [{proc['pid']}] {proc['cmd']}"
    for child in status["children"]:
        yield from _format_status(child, depth + 1)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the process group; SIGKILL it if it is still alive after 5 s."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.wait_for(proc.wait(), 5)
    except ProcessLookupError:
        pass
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()


async def run_cmd(
    job: Job,
    label: str,
    cmd: list[str],
    abort_on_error: bool,
    timeout: float | None = None,
) -> bool:
    return await job.run_cmd(label, cmd, abort_on_error, timeout=timeout)


async def capture_cmd(
    job: Job,
    label: str,
    cmd: list[str],
    abort_on_error: bool,
    timeout: float | None = None,
) -> str:
    return await job.capture_cmd(label, cmd, abort_on_error, timeout=timeout)
