from __future__ import annotations

import asyncio
import contextlib
import unittest
from collections.abc import AsyncIterator, Sequence
from typing import Any

from .. import generated as g
from ..command import LineSink
from ..peer import Peer, RemoteError
from ..runtime import Runtime
from ..transport import connect
from .docker import Docker
from .sysdeps import SystemDeps
from .test_sysdeps import FakeApt, FakeSleep, _stamp

READY, FAILED = g.ImageState.ready, g.ImageState.failed


class FakeDocker:
    def __init__(self, present: Sequence[str] = (), broken: Sequence[str] = ()) -> None:
        self.images = set(present)
        self.broken = set(broken)
        self.daemon_up = True
        self.calls: list[tuple[str, ...]] = []
        self.gate: asyncio.Event | None = None

    async def info(self) -> tuple[bool, str]:
        self.calls.append(("info",))
        return (True, "26.1.5") if self.daemon_up else (False, "Cannot connect to the daemon")

    async def present(self, image: str) -> bool:
        return image in self.images

    async def pull(self, image: str, on_line: LineSink | None = None) -> tuple[bool, str]:
        return await self._make("pull", image, on_line)

    async def build(
        self, image: str, dockerfile: str, on_line: LineSink | None = None
    ) -> tuple[bool, str]:
        return await self._make("build", image, on_line)

    async def _make(self, verb: str, image: str, on_line: LineSink | None) -> tuple[bool, str]:
        self.calls.append((verb, image))
        if self.gate is not None:
            await self.gate.wait()
        if on_line is not None:
            on_line(f"{verb}ing {image}")
        if image in self.broken:
            return False, f"Error response from daemon: {image} not found"
        self.images.add(image)
        return True, ""


@contextlib.asynccontextmanager
async def _client(apt: FakeApt, docker: FakeDocker, **options: Any) -> AsyncIterator[Peer]:
    runtime = Runtime()
    runtime.register(
        SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep()).service(),
        Docker(runtime.client, docker, **options).service(),
    )
    stop = asyncio.Event()
    address = f"mem://docker-{id(runtime)}"
    server = asyncio.create_task(runtime.serve(address, stop))
    await asyncio.sleep(0)
    client = Peer(await connect(address), "test")
    reader = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        await client.close()
        await reader
        stop.set()
        await server


def _req(*specs: tuple[str, str | None]) -> g.DockerPrepareReq:
    return g.DockerPrepareReq(
        kind="DockerPrepareReq",
        images=[g.ImageSpec(image=image, dockerfile=dockerfile) for image, dockerfile in specs],
    )


def _states(res: Any) -> dict[str, g.ImageState]:
    assert isinstance(res, g.DockerPrepareRes)
    return {status.image: status.state for status in res.images}


class PipelineTest(unittest.TestCase):
    def test_docker_is_resolved_then_images_are_pulled_and_built(self) -> None:
        async def run() -> None:
            apt, docker = FakeApt(), FakeDocker()
            async with _client(apt, docker) as client:
                request = await client.request(_req(("busybox", None), ("x", "FROM scratch")))
                seen = [msg async for msg in request]
            self.assertIn(("install", "docker.io"), apt.calls)  # through sysdeps
            self.assertEqual(docker.calls, [("info",), ("pull", "busybox"), ("build", "x")])
            self.assertEqual(_states(request.result.body), {"busybox": READY, "x": READY})
            logs = [m.body.message for m in seen if isinstance(m.body, g.LogMsg)]
            self.assertIn("docker.io: ready (1.0)", logs)  # relayed from sysdeps
            self.assertIn("Setting up docker.io (1.0) ...", logs)  # apt's output, relayed
            self.assertIn("pulling busybox", logs)  # docker's output, at debug
            kinds = [m.kind for m in seen]
            self.assertEqual(kinds[-2:], ["DockerReady", "DockerPrepareRes"])
            self.assertEqual({m.req_id for m in seen}, {request.req_id})

        asyncio.run(run())

    def test_a_present_image_is_not_pulled(self) -> None:
        async def run() -> None:
            apt, docker = FakeApt(installed={"docker.io": "26"}), FakeDocker(present=["busybox"])
            async with _client(apt, docker) as client:
                res = await client.ask(_req(("busybox", None)), timeout=5)
            self.assertEqual(_states(res.body), {"busybox": READY})
            self.assertEqual(apt.calls, [])
            self.assertEqual(docker.calls, [("info",)])

        asyncio.run(run())

    def test_a_failing_pull_fails_that_image_and_no_docker_ready(self) -> None:
        async def run() -> None:
            apt, docker = FakeApt(installed={"docker.io": "26"}), FakeDocker(broken=["nope"])
            async with _client(apt, docker) as client:
                request = await client.request(_req(("busybox", None), ("nope", None)))
                with self.assertLogs("rt.docker", "WARNING"):
                    seen = [msg async for msg in request]
            self.assertEqual(_states(request.result.body), {"busybox": READY, "nope": FAILED})
            self.assertNotIn("DockerReady", [m.kind for m in seen])

        asyncio.run(run())

    def test_docker_deps_failing_fail_every_image_without_touching_docker(self) -> None:
        async def run() -> None:
            apt, docker = FakeApt(broken=["docker.io"]), FakeDocker()
            with self.assertLogs("rt.sysdeps", "WARNING"):
                async with _client(apt, docker) as client:
                    res = await client.ask(_req(("busybox", None)), timeout=5)
            self.assertEqual(_states(res.body), {"busybox": FAILED})
            self.assertIn("docker.io failed", res.body.images[0].message)
            self.assertEqual(docker.calls, [])

        asyncio.run(run())

    def test_a_daemon_that_never_answers(self) -> None:
        async def run() -> None:
            apt, docker = FakeApt(installed={"docker.io": "26"}), FakeDocker()
            docker.daemon_up = False
            async with _client(apt, docker, daemon_wait=0) as client:
                res = await client.ask(_req(("busybox", None)), timeout=5)
            self.assertEqual(_states(res.body), {"busybox": FAILED})
            self.assertIn("does not answer", res.body.images[0].message)

        asyncio.run(run())

    def test_concurrent_requests_pull_an_image_once(self) -> None:
        async def run() -> None:
            apt, docker = FakeApt(installed={"docker.io": "26"}), FakeDocker()
            docker.gate = asyncio.Event()
            async with _client(apt, docker) as client:
                first = await client.request(_req(("busybox", None)))
                second = await client.request(_req(("busybox", None)))
                await asyncio.sleep(0.01)
                docker.gate.set()
                a, b = await first.wait(5), await second.wait(5)
            self.assertEqual((_states(a.body), _states(b.body)), ({"busybox": READY},) * 2)
            self.assertEqual(docker.calls.count(("pull", "busybox")), 1)

        asyncio.run(run())

    def test_a_bad_image_reference_is_refused(self) -> None:
        async def run() -> None:
            async with _client(FakeApt(), FakeDocker()) as client:
                with self.assertRaises(RemoteError) as caught:
                    await client.ask(_req(("--privileged", None)), timeout=5)
            self.assertEqual(caught.exception.code, "bad-request")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
