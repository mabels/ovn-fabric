"""The docker prepare pipeline: docker usable, then every image present.

    DockerPrepareReq { images: [{image, dockerfile?}] }
      → LogMsg …                      progress; pull/build output at debug
      → DockerReady { images }        once docker and every image are ready
      → DockerPrepareRes { images: [ImageStatus] }

1. Docker's system dependencies (DOCKER_DEPS) are resolved by asking the
   sysdeps service — an internal request (the handled request is its
   parentReqId); its progress is relayed as this request's logs.
2. The daemon must answer (`docker info`), polled for a while: right after
   installing it, it may still be starting.
3. Each image: present → ready; else pulled, or built from its Dockerfile.
   One lock per image, so two requests never pull/build the same image at
   once — the second finds it present.

DockerReady is only emitted when everything is ready; a failure is in the
final response.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from .. import command
from .. import generated as g
from ..command import LineSink
from ..message import BodySpec, Msg, ProtocolError, register_bodies
from ..peer import Peer
from ..service import Service

log = logging.getLogger("rt.docker")

BODIES = {
    "DockerPrepareReq": BodySpec(g.DockerPrepareReq, "request"),
    "DockerReady": BodySpec(g.DockerReady, "event"),
    "DockerPrepareRes": BodySpec(g.DockerPrepareRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

# What makes the docker CLI and daemon available (apt packages).
DOCKER_DEPS = ("docker.io",)
# How long to wait for the daemon to answer once installed.
DAEMON_WAIT = 30.0

# An image reference; never something that looks like an option.
_IMAGE = re.compile(r"^[a-z0-9][A-Za-z0-9._/:@-]*$")


class DockerCli(Protocol):
    """What the pipeline needs from docker."""

    async def info(self) -> tuple[bool, str]: ...

    async def present(self, image: str) -> bool: ...

    async def pull(self, image: str, on_line: LineSink | None = None) -> tuple[bool, str]: ...

    async def build(
        self, image: str, dockerfile: str, on_line: LineSink | None = None
    ) -> tuple[bool, str]: ...


class SystemDocker:
    """The host's docker CLI."""

    async def info(self) -> tuple[bool, str]:
        return await command.run(["docker", "info", "--format", "{{.ServerVersion}}"], log=log)

    async def present(self, image: str) -> bool:
        ok, _ = await command.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], log=log
        )
        return ok

    async def pull(self, image: str, on_line: LineSink | None = None) -> tuple[bool, str]:
        return await command.run(["docker", "pull", image], log=log, on_line=on_line)

    async def build(
        self, image: str, dockerfile: str, on_line: LineSink | None = None
    ) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="rt-build-") as context:
            Path(context, "Dockerfile").write_text(dockerfile)
            return await command.run(
                ["docker", "build", "--progress=plain", "-t", image, context],
                log=log,
                on_line=on_line,
            )


def _tail(output: str, lines: int = 5) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


class Docker:
    def __init__(
        self,
        client: Callable[[], Awaitable[Peer]],
        docker: DockerCli | None = None,
        *,
        deps: Sequence[str] = DOCKER_DEPS,
        daemon_wait: float = DAEMON_WAIT,
    ) -> None:
        self._client = client  # the runtime's internal connection, to ask sysdeps
        self._docker: DockerCli = docker if docker is not None else SystemDocker()
        self._deps = list(deps)
        self._daemon_wait = daemon_wait
        self._locks: dict[str, asyncio.Lock] = {}

    def service(self) -> Service:
        return Service(name="docker", bodies=BODIES, handlers={"DockerPrepareReq": self.prepare})

    async def prepare(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.DockerPrepareReq)
        bad = [spec.image for spec in req.images if not _IMAGE.match(spec.image)]
        if bad:
            raise ProtocolError("bad-request", f"not an image reference: {', '.join(bad)}")

        problem = await self._docker_ready(peer)
        if problem is not None:
            return g.DockerPrepareRes(
                kind="DockerPrepareRes",
                images=[self._failed(spec.image, problem) for spec in req.images],
            )
        statuses = [await self._image(peer, spec) for spec in req.images]
        if all(status.state is g.ImageState.ready for status in statuses):
            await peer.emit(g.DockerReady(kind="DockerReady", images=statuses))
        return g.DockerPrepareRes(kind="DockerPrepareRes", images=statuses)

    # -- 1 + 2: docker itself ------------------------------------------------

    async def _docker_ready(self, peer: Peer) -> str | None:
        """None when docker is usable, else why not."""
        await peer.log("info", f"resolving docker's dependencies: {' '.join(self._deps)}")
        client = await self._client()
        request = await client.request(
            g.ResolveSystemDepsReq(kind="ResolveSystemDepsReq", deps=self._deps)
        )
        async for event in request:  # relay sysdeps' progress as our own
            if isinstance(event.body, g.LogMsg):
                await peer.emit(event.body)
            elif isinstance(event.body, g.ResolveSystemDep):
                note = f" ({event.body.message})" if event.body.message else ""
                await peer.log("info", f"{event.body.dep}: {event.body.state.value}{note}")
        res = request.result.body
        if isinstance(res, g.ErrorRes):
            return f"sysdeps: {res.code}: {res.message}"
        assert isinstance(res, g.ResolveSystemDepsRes)
        missing = [s for s in res.deps if s.state is not g.SystemDepState.ready]
        if missing:
            return "; ".join(f"{s.dep} {s.state.value}: {s.message or ''}".strip() for s in missing)

        deadline = asyncio.get_running_loop().time() + self._daemon_wait
        while True:
            ok, out = await self._docker.info()
            if ok:
                await peer.log("info", f"docker daemon {out.strip()} is ready")
                return None
            if asyncio.get_running_loop().time() >= deadline:
                return f"the docker daemon does not answer: {_tail(out)}"
            await asyncio.sleep(1)

    # -- 3: one image --------------------------------------------------------

    async def _image(self, peer: Peer, spec: g.ImageSpec) -> g.ImageStatus:
        async with self._locks.setdefault(spec.image, asyncio.Lock()):
            if await self._docker.present(spec.image):
                await peer.log("info", f"{spec.image}: present")
                return g.ImageStatus(image=spec.image, state=g.ImageState.ready, message="present")
            verb = "pull" if spec.dockerfile is None else "build"
            await peer.log("info", f"{spec.image}: {verb}")
            async with _forward(peer) as on_line:
                if spec.dockerfile is None:
                    ok, out = await self._docker.pull(spec.image, on_line)
                else:
                    ok, out = await self._docker.build(spec.image, spec.dockerfile, on_line)
            if not ok:
                log.warning("%s: %s failed: %s", spec.image, verb, _tail(out))
                return self._failed(spec.image, f"{verb} failed: {_tail(out)}")
            await peer.log("info", f"{spec.image}: {verb} done")
            return g.ImageStatus(image=spec.image, state=g.ImageState.ready, message=verb)

    @staticmethod
    def _failed(image: str, message: str) -> g.ImageStatus:
        return g.ImageStatus(image=image, state=g.ImageState.failed, message=message)


@contextlib.asynccontextmanager
async def _forward(peer: Peer) -> AsyncIterator[LineSink]:
    """A line sink: each line goes, in order, as a debug LogMsg of the
    request being handled."""
    lines: asyncio.Queue[str | None] = asyncio.Queue()

    async def forward() -> None:
        while (line := await lines.get()) is not None:
            await peer.log("debug", line)

    task = asyncio.get_running_loop().create_task(forward())  # inherits the request
    try:
        yield lines.put_nowait
    finally:
        lines.put_nowait(None)
        await task


def service(client: Callable[[], Awaitable[Peer]]) -> Service:
    return Docker(client).service()
