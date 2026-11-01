"""The docker prepare pipeline: docker usable, then every image present.

    DockerPrepareReq { images: [{image, dockerfile?}] }
      → LogMsg …                      progress; pull/build output at debug
      → DockerReady { images }        once docker and every image are ready
      → DockerPrepareRes { images: [ImageStatus] }

1. Docker's dependencies (DOCKER_DEPS, abstract: `docker`) are resolved by
   asking the abstract deps service — an internal request (the handled request is its
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
import logging
import re
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol

from runtime import command
from runtime import generated as rg
from runtime.command import Report
from runtime.message import BodySpec, ProtocolError, register_bodies
from runtime.peer import Call, Peer
from runtime.service import Service

from .. import generated as g

log = logging.getLogger("fabric.docker")

BODIES = {
    "DockerPrepareReq": BodySpec(g.DockerPrepareReq, "request"),
    "DockerReady": BodySpec(g.DockerReady, "event"),
    "DockerPrepareRes": BodySpec(g.DockerPrepareRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

# What makes the docker CLI and daemon available (abstract dependencies).
DOCKER_DEPS = ("docker",)
# How long to wait for the daemon to answer once installed.
DAEMON_WAIT = 30.0

# An image reference; never something that looks like an option.
_IMAGE = re.compile(r"^[a-z0-9][A-Za-z0-9._/:@-]*$")


class DockerCli(Protocol):
    """What the pipeline needs from docker."""

    # `report`: where the commands and their output go (the request's).

    async def info(self, report: Report) -> tuple[bool, str]: ...

    async def present(self, image: str, report: Report) -> bool: ...

    async def pull(self, image: str, report: Report) -> tuple[bool, str]: ...

    async def build(self, image: str, dockerfile: str, report: Report) -> tuple[bool, str]: ...


class SystemDocker:
    """The host's docker CLI."""

    async def info(self, report: Report) -> tuple[bool, str]:
        return await command.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"], log=log, report=report
        )

    async def present(self, image: str, report: Report) -> bool:
        ok, _ = await command.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], log=log, report=report
        )
        return ok

    async def pull(self, image: str, report: Report) -> tuple[bool, str]:
        return await command.run(["docker", "pull", image], log=log, report=report)

    async def build(self, image: str, dockerfile: str, report: Report) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory(prefix="rt-build-") as context:
            Path(context, "Dockerfile").write_text(dockerfile)
            return await command.run(
                ["docker", "build", "--progress=plain", "-t", image, context],
                log=log,
                report=report,
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

    async def prepare(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.DockerPrepareReq)
        bad = [spec.image for spec in req.images if not _IMAGE.match(spec.image)]
        if bad:
            raise ProtocolError("bad-request", f"not an image reference: {', '.join(bad)}")

        problem = await self._docker_ready(call, req.packageManager)
        if problem is not None:
            return g.DockerPrepareRes(
                kind="DockerPrepareRes",
                images=[self._failed(spec.image, problem) for spec in req.images],
            )
        statuses = [await self._image(call, spec) for spec in req.images]
        if all(status.state is g.ImageState.ready for status in statuses):
            await call.emit(g.DockerReady(kind="DockerReady", images=statuses))
        return g.DockerPrepareRes(kind="DockerPrepareRes", images=statuses)

    # -- 1 + 2: docker itself ------------------------------------------------

    async def _docker_ready(self, call: Call, manager: g.PackageManager | None) -> str | None:
        """None when docker is usable, else why not. Its packages are made
        present when the request names the host's package manager;
        without one docker must already be installed."""
        if manager is not None:
            problem = await self._packages_ready(call, manager)
            if problem is not None:
                return problem
        deadline = asyncio.get_running_loop().time() + self._daemon_wait
        while True:
            ok, out = await self._docker.info(call.report)
            if ok:
                await call.log("info", f"docker daemon {out.strip()} is ready")
                return None
            if asyncio.get_running_loop().time() >= deadline:
                return f"the docker daemon does not answer: {_tail(out)}"
            await asyncio.sleep(1)

    async def _packages_ready(self, call: Call, manager: g.PackageManager) -> str | None:
        """None when docker's abstract dependencies are ready, else why not."""
        await call.log("info", f"resolving docker's dependencies: {' '.join(self._deps)}")
        client = await self._client()
        request = await client.request(
            g.AbstractDepsReq(
                kind="AbstractDepsReq",
                packageManager=manager,
                deps=[g.AbstractDepSpec(name=dep) for dep in self._deps],
            ),
            parent=call.req_id,
        )
        async for event in request:  # relay their progress as our own
            if isinstance(event.body, rg.LogMsg):
                await call.emit(event.body)
            elif isinstance(event.body, g.AbstractDep):
                packages = ", ".join(
                    f"{d.dep} {d.state.value}" + (f" {d.message}" if d.message else "")
                    for d in event.body.deps
                )
                await call.log("info", f"{event.body.name}: {event.body.state.value} ({packages})")
        res = request.result.body
        if isinstance(res, rg.ErrorRes):
            return f"abstract deps: {res.code}: {res.message}"
        assert isinstance(res, g.AbstractDepsRes)
        missing = [s for s in res.deps if s.state is not g.SystemDepState.ready]
        if missing:
            return "; ".join(
                f"{s.name} {s.state.value}: "
                + ", ".join(f"{d.dep} {d.state.value}: {d.message or ''}".strip() for d in s.deps)
                for s in missing
            )
        return None

    # -- 3: one image --------------------------------------------------------

    async def _image(self, call: Call, spec: g.ImageSpec) -> g.ImageStatus:
        async with self._locks.setdefault(spec.image, asyncio.Lock()):
            if await self._docker.present(spec.image, call.report):
                await call.log("info", f"{spec.image}: present")
                return g.ImageStatus(image=spec.image, state=g.ImageState.ready, message="present")
            verb = "pull" if spec.dockerfile is None else "build"
            await call.log("info", f"{spec.image}: {verb}")
            if spec.dockerfile is None:
                ok, out = await self._docker.pull(spec.image, call.report)
            else:
                ok, out = await self._docker.build(spec.image, spec.dockerfile, call.report)
            if not ok:
                log.warning("%s: %s failed: %s", spec.image, verb, _tail(out))
                return self._failed(spec.image, f"{verb} failed: {_tail(out)}")
            await call.log("info", f"{spec.image}: {verb} done")
            return g.ImageStatus(image=spec.image, state=g.ImageState.ready, message=verb)

    @staticmethod
    def _failed(image: str, message: str) -> g.ImageStatus:
        return g.ImageStatus(image=image, state=g.ImageState.failed, message=message)


def service(client: Callable[[], Awaitable[Peer]]) -> Service:
    return Docker(client).service()
