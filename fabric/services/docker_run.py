"""Running one container.

    DockerRunReq { container: ContainerSpec, follow? }
      → (internal DockerPrepareReq for its image; progress relayed)
      → DockerContainer { name, state: starting }
      → DockerContainer { name, state: started }
    default — the runtime owns it from here:
      → DockerRunRes { name, state: started }
    with follow — the request owns it:
      → LogMsg …                 the container's stdout (info) and stderr (warn)
      → DockerContainer { name, state: stopped, exitCode }
      → DockerRunRes { name, state: stopped, exitCode }

The container is started detached (`docker run -d --name …`); its output
is followed (`docker logs -f`) and its end awaited (`docker wait`); once
it stopped it is removed. Whoever owns it does that:

- by default the runtime: the request ends once it is started; the
  runtime follows it into its own log (fabric.docker_run) and, when the
  runtime stops, stops it;
- with `follow` the request: it lasts until the container stops, and
  ending it early — the client goes away — stops the container.

Either way nothing is left running unwatched.

A name identifies the container: whatever already runs under it is
replaced. The runtime's own container (started by an earlier request) is
stopped by its owner first, which also removes its files; anything else of
that name (a leftover, a container started by hand) is removed with
`docker rm -f`. Either way it is logged as a warning.

Its `files` (e.g. a Corefile) are written under FILES_ROOT/<name>/ and
bind-mounted read-only, one mount per file — whatever else lives in that
container directory is not shadowed. They exist exactly as long as the
container: removed when it ends. A directory already there (no container
of that name runs, that is checked first) is a leftover of a runtime that
ended before it could clean up: it is replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Protocol

from runtime import command
from runtime import generated as rg
from runtime.command import LineSink, Report
from runtime.message import BodySpec, ProtocolError, register_bodies
from runtime.peer import Call, Peer
from runtime.service import Service
from runtime.transport import Closed

from .. import generated as g
from .docker import _IMAGE  # the same image reference rule

log = logging.getLogger("fabric.docker_run")

BODIES = {
    "DockerRunReq": BodySpec(g.DockerRunReq, "request"),
    "DockerContainer": BodySpec(g.DockerContainer, "event"),
    "DockerRunRes": BodySpec(g.DockerRunRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

# A container's files are written under <FILES_ROOT>/<name>/ and mounted
# from there.
FILES_ROOT = "/var/lib/ovn-fabric/containers"

# How long `docker stop` waits before killing, when a request ends early.
STOP_TIMEOUT = 10


class ContainerCli(Protocol):
    """What running a container needs from docker. `report`: where the
    commands go — the request's, while one waits on them."""

    async def exists(self, name: str, *, report: Report = command.nowhere) -> bool: ...

    async def run(self, argv: list[str], *, report: Report = command.nowhere) -> tuple[bool, str]:
        """`docker run -d …` (argv after `docker`); (ok, container id or error)."""

    async def logs(
        self, name: str, on_out: LineSink, on_err: LineSink, *, report: Report = command.nowhere
    ) -> None:
        """Follow the output until the container stops."""

    async def wait(self, name: str, *, report: Report = command.nowhere) -> int:
        """The exit code, once it stopped."""

    async def stop(self, name: str, *, report: Report = command.nowhere) -> None: ...

    async def remove(self, name: str, *, report: Report = command.nowhere) -> None: ...


class SystemContainers:
    """The host's docker CLI."""

    async def exists(self, name: str, *, report: Report = command.nowhere) -> bool:
        ok, _ = await command.run(["docker", "container", "inspect", name], log=log, report=report)
        return ok

    async def run(self, argv: list[str], *, report: Report = command.nowhere) -> tuple[bool, str]:
        return await command.run(["docker", *argv], log=log, report=report)

    async def logs(
        self, name: str, on_out: LineSink, on_err: LineSink, *, report: Report = command.nowhere
    ) -> None:
        await command.run(
            ["docker", "logs", "-f", name], log=log, on_line=on_out, on_err=on_err, report=report
        )

    async def wait(self, name: str, *, report: Report = command.nowhere) -> int:
        ok, out = await command.run(["docker", "wait", name], log=log, report=report)
        try:
            return int(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return -1

    async def stop(self, name: str, *, report: Report = command.nowhere) -> None:
        await command.run(["docker", "stop", "-t", str(STOP_TIMEOUT), name], log=log, report=report)

    async def remove(self, name: str, *, report: Report = command.nowhere) -> None:
        await command.run(["docker", "rm", "-f", name], log=log, report=report)


def run_argv(spec: g.ContainerSpec, files: Sequence[tuple[str, str]] = ()) -> list[str]:
    """`docker run -d …` for `spec` (argv after `docker`); `files` are
    (host path, container path) of its written files, mounted read-only."""
    argv = ["run", "-d", "--name", spec.name]
    if spec.privileged:
        argv.append("--privileged")
    if spec.network is not None:
        argv += ["--network", spec.network]
    for var in spec.env or []:
        argv += ["-e", f"{var.name}={var.value}"]
    for mount in spec.mounts or []:
        argv += ["-v", f"{mount.host}:{mount.container}" + (":ro" if mount.readOnly else "")]
    for host_path, container_path in files:
        argv += ["-v", f"{host_path}:{container_path}:ro"]
    argv += list(spec.flags or [])
    argv += [spec.image, *(spec.command or [])]
    return argv


class DockerRun:
    def __init__(
        self,
        client: Callable[[], Awaitable[Peer]],
        containers: ContainerCli | None = None,
        *,
        files_root: str = FILES_ROOT,
    ) -> None:
        self._client = client  # the runtime's internal connection, to ask docker prepare
        self._files_root = Path(files_root)
        self._containers: ContainerCli = (
            containers if containers is not None else SystemContainers()
        )
        # The containers the runtime owns (started without `follow`): name →
        # the task that watches it until it stops.
        self._owned: dict[str, asyncio.Task[None]] = {}
        # The containers a `follow` request owns: name → set once that
        # request has cleaned up.
        self._following: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def service(self) -> Service:
        return Service(
            name="docker_run",
            bodies=BODIES,
            handlers={"DockerRunReq": self.run},
            run=self.keep,
        )

    async def keep(self) -> None:
        """Keep the owned containers while the runtime runs; when it stops,
        stop them — nothing is left running unwatched."""
        try:
            await asyncio.Event().wait()
        finally:
            tasks = list(self._owned.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.DockerRunReq)
        spec = req.container
        if not _NAME.match(spec.name):
            raise ProtocolError("bad-request", f"not a container name: {spec.name!r}")
        if not _IMAGE.match(spec.image):
            raise ProtocolError("bad-request", f"not an image reference: {spec.image!r}")
        for file in spec.files or []:
            path = PurePosixPath(file.path)
            if not path.is_absolute() or ".." in path.parts:
                raise ProtocolError("bad-request", f"not an absolute file path: {file.path!r}")

        problem = await self._prepare(call, spec)
        if problem is not None:
            return await self._end(call, spec.name, g.ContainerState.failed, message=problem)
        # One start per name at a time: replace whatever runs under it, then
        # start the new one (the lock is held until it is registered).
        lock = self._locks.setdefault(spec.name, asyncio.Lock())
        await lock.acquire()
        locked = True
        try:
            await self._replace(call, spec.name)
            await self._state(call, spec.name, g.ContainerState.starting)
            files_dir = self._files_root / spec.name
            if files_dir.exists():
                # Nothing runs under this name any more (replaced above): a
                # leftover of a runtime that ended before it could clean up.
                log.warning("%s: replacing the leftover %s", spec.name, files_dir)
                await call.log("warn", f"replacing the leftover {files_dir}")
            try:
                files = await asyncio.to_thread(_write_files, files_dir, spec.files or [])
            except OSError as exc:
                return await self._end(
                    call, spec.name, g.ContainerState.failed, message=f"writing its files: {exc}"
                )
            handed_over = False
            done = asyncio.Event()
            try:
                ok, out = await self._containers.run(run_argv(spec, files), report=call.report)
                if not ok:
                    return await self._end(
                        call,
                        spec.name,
                        g.ContainerState.failed,
                        message=f"docker run: {out.strip()}",
                    )
                if not req.follow:
                    # The runtime owns it from here, with its files.
                    self._own(spec.name, files_dir)
                    handed_over = True
                    return await self._end(
                        call, spec.name, g.ContainerState.started, message=out.strip()[:12]
                    )
                self._following[spec.name] = done
                lock.release()
                locked = False
                return await self._follow(call, spec, out)
            finally:  # the files live exactly as long as the container
                if not handed_over:
                    await asyncio.to_thread(shutil.rmtree, files_dir, True)
                if self._following.get(spec.name) is done:
                    del self._following[spec.name]
                done.set()
        finally:
            if locked:
                lock.release()

    async def _replace(self, call: Call, name: str) -> None:
        """Make the name free: a container of the runtime is stopped by its
        owner (which also removes its files); anything else of that name —
        a leftover, a container started by hand — is removed."""
        owned = self._owned.get(name)
        following = self._following.get(name)
        if owned is not None:
            what = "the runtime's running container"
        elif following is not None:
            what = "the running container of another request"
        elif await self._containers.exists(name, report=call.report):
            what = "an existing container"
        else:
            return
        log.warning("%s: replacing %s", name, what)
        await call.log("warn", f"{name}: replacing {what}")
        if owned is not None:
            owned.cancel()  # its watcher stops and removes it, with its files
            await asyncio.gather(owned, return_exceptions=True)
        elif following is not None:
            # that request sees it stop, and cleans up
            await self._containers.remove(name, report=call.report)
            await following.wait()
        else:
            await self._containers.remove(name, report=call.report)

    async def _follow(self, call: Call, spec: g.ContainerSpec, started: str) -> g.DockerRunRes:
        """The request owns the container: stream it until it stops."""
        try:
            await self._state(
                call, spec.name, g.ContainerState.started, message=started.strip()[:12]
            )
            async with _forward(call, spec.name) as (on_out, on_err):
                following = asyncio.get_running_loop().create_task(
                    self._containers.logs(spec.name, on_out, on_err, report=call.report)
                )
                code = await self._containers.wait(spec.name, report=call.report)
                await following  # the rest of the output, up to the end
        except BaseException:
            # The request ended early: the container does not outlive it.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._stop(spec.name))
            raise
        await self._containers.remove(spec.name, report=call.report)
        return await self._end(call, spec.name, g.ContainerState.stopped, exit_code=code)

    async def _prepare(self, call: Call, spec: g.ContainerSpec) -> str | None:
        """None when the image is present, else why not."""
        client = await self._client()
        image = g.ImageSpec(image=spec.image, dockerfile=spec.dockerfile)
        request = await client.request(
            g.DockerPrepareReq(kind="DockerPrepareReq", images=[image]), parent=call.req_id
        )
        async for event in request:  # relay docker prepare's progress as our own
            if isinstance(event.body, rg.LogMsg):
                await call.emit(event.body)
        res = request.result.body
        if isinstance(res, rg.ErrorRes):
            return f"docker prepare: {res.code}: {res.message}"
        assert isinstance(res, g.DockerPrepareRes)
        (status,) = res.images
        if status.state is not g.ImageState.ready:
            return f"image {status.image}: {status.message or status.state.value}"
        return None

    async def _stop(self, name: str) -> None:
        log.info("%s: the request ended; stopping the container", name)
        await self._containers.stop(name)
        await self._containers.remove(name)

    def _own(self, name: str, files_dir: Path) -> None:
        # The watcher belongs to the runtime, not to the request that started
        # it: it gets no Call, so nothing it does goes to that request.
        task = asyncio.get_running_loop().create_task(
            self._watch(name, files_dir), name=f"container:{name}"
        )
        self._owned[name] = task
        task.add_done_callback(lambda _: self._owned.pop(name, None))

    async def _watch(self, name: str, files_dir: Path) -> None:
        """An owned container: its output into the runtime's log, until it
        stops; then it is removed with its files."""
        stopping = False
        try:
            following = asyncio.get_running_loop().create_task(
                self._containers.logs(
                    name,
                    lambda line: log.info("%s: %s", name, line),
                    lambda line: log.warning("%s: %s", name, line),
                )
            )
            code = await self._containers.wait(name)
            await following
            log.info("%s: stopped (exit %d)", name, code)
        except asyncio.CancelledError:
            stopping = True
            raise
        finally:
            with contextlib.suppress(Exception):
                if stopping:
                    log.info("%s: the runtime stops; stopping the container", name)
                    await asyncio.shield(self._containers.stop(name))
                await asyncio.shield(self._containers.remove(name))
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, files_dir, True))

    async def _state(
        self, call: Call, name: str, state: g.ContainerState, message: str | None = None
    ) -> None:
        with contextlib.suppress(Closed):
            await call.emit(
                g.DockerContainer(kind="DockerContainer", name=name, state=state, message=message)
            )

    async def _end(
        self,
        call: Call,
        name: str,
        state: g.ContainerState,
        *,
        exit_code: int | None = None,
        message: str | None = None,
    ) -> g.DockerRunRes:
        with contextlib.suppress(Closed):
            await call.emit(
                g.DockerContainer(
                    kind="DockerContainer",
                    name=name,
                    state=state,
                    exitCode=exit_code,
                    message=message,
                )
            )
        return g.DockerRunRes(
            kind="DockerRunRes", name=name, state=state, exitCode=exit_code, message=message
        )


def _write_files(directory: Path, files: Sequence[g.ContainerFile]) -> list[tuple[str, str]]:
    """Write a container's files under a fresh `directory` (a leftover is
    replaced); (host path, container path) of each."""
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True)
    out: list[tuple[str, str]] = []
    for file in files:
        host_path = directory / file.path.lstrip("/")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(file.content)
        out.append((str(host_path), file.path))
    return out


@contextlib.asynccontextmanager
async def _forward(call: Call, component: str) -> AsyncIterator[tuple[LineSink, LineSink]]:
    """Two line sinks — stdout and stderr — whose lines go, in order, as
    info/warn LogMsg of the request being handled, from `component` (the
    container)."""
    lines: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

    async def forward() -> None:
        while (item := await lines.get()) is not None:
            level, line = item
            with contextlib.suppress(Closed):
                await call.log(level, line, component)

    task = asyncio.get_running_loop().create_task(forward())
    try:
        yield (
            lambda line: lines.put_nowait(("info", line)),
            lambda line: lines.put_nowait(("warn", line)),
        )
    finally:
        lines.put_nowait(None)
        await task


def service(client: Callable[[], Awaitable[Peer]], *, files_root: str = FILES_ROOT) -> Service:
    return DockerRun(client, files_root=files_root).service()
