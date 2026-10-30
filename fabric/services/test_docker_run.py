from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from runtime.command import LineSink
from runtime.peer import Peer, RemoteError
from runtime.runtime import Runtime
from runtime.transport import connect

from .. import generated as g
from .abstract_deps import AbstractDeps
from .docker import Docker
from .docker_run import DockerRun, run_argv
from .sysdeps import SystemDeps
from .test_docker import FakeDocker
from .test_sysdeps import FakeApt, FakeSleep, _stamp

S = g.ContainerState


class FakeContainers:
    """One container at a time: prints `out`/`err`, then exits with `code` —
    or, with `forever`, runs until stopped."""

    def __init__(self, out: list[str] = [], err: list[str] = [], code: int = 0) -> None:
        self.out, self.err, self.code = list(out), list(err), code
        self.forever = False
        self.existing: set[str] = set()
        self.calls: list[tuple[str, ...]] = []
        self._stopped = asyncio.Event()
        self.run_ok = True

    async def exists(self, name: str) -> bool:
        return name in self.existing

    async def run(self, argv: list[str]) -> tuple[bool, str]:
        self.calls.append(("run", *argv))
        self._stopped = asyncio.Event()  # a new container
        if not self.run_ok:
            return False, "Error response from daemon: invalid flag"
        return True, "0123456789abcdef"

    async def logs(self, name: str, on_out: LineSink, on_err: LineSink) -> None:
        for line in self.out:
            on_out(line)
        for line in self.err:
            on_err(line)
        if self.forever:
            await self._stopped.wait()

    async def wait(self, name: str) -> int:
        if self.forever:
            await self._stopped.wait()
            return 137
        return self.code

    async def stop(self, name: str) -> None:
        self.calls.append(("stop", name))
        self._stopped.set()

    async def remove(self, name: str) -> None:  # docker rm -f: ends it if it runs
        self.calls.append(("remove", name))
        self.existing.discard(name)
        self._stopped.set()


@contextlib.asynccontextmanager
async def _runtime(
    containers: FakeContainers, docker: FakeDocker, files_root: str | None = None
) -> AsyncIterator[str]:
    runtime = Runtime()
    runtime.register(
        SystemDeps(
            FakeApt(installed={"docker.io": "26"}), stamp=_stamp(0), sleep=FakeSleep()
        ).service(),
        AbstractDeps(runtime.client).service(),
        Docker(runtime.client, docker).service(),
        DockerRun(
            runtime.client, containers, files_root=files_root or tempfile.mkdtemp()
        ).service(),
    )
    stop = asyncio.Event()
    address = f"mem://docker-run-{id(runtime)}"
    server = asyncio.create_task(runtime.serve(address, stop))
    await asyncio.sleep(0)
    try:
        yield address
    finally:
        stop.set()
        await server


@contextlib.asynccontextmanager
async def _client(address: str) -> AsyncIterator[Peer]:
    client = Peer(await connect(address), "test")
    reader = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        await client.close()
        await reader


def _req(follow: bool = True, **spec: Any) -> g.DockerRunReq:
    return g.DockerRunReq(
        kind="DockerRunReq",
        container=g.ContainerSpec(**{"name": "t", "image": "busybox", **spec}),
        follow=follow,
    )


class RunTest(unittest.TestCase):
    def test_lifecycle_output_and_exit_code(self) -> None:
        async def run() -> None:
            containers = FakeContainers(out=["hello"], err=["oops"], code=3)
            docker = FakeDocker()
            async with _runtime(containers, docker) as address, _client(address) as client:
                request = await client.request(_req(command=["sh", "-c", "x"]))
                seen = [msg async for msg in request]
            self.assertIn(("pull", "busybox"), docker.calls)  # prepared first
            states = [m.body.state for m in seen if isinstance(m.body, g.DockerContainer)]
            self.assertEqual(states, [S.starting, S.started, S.stopped])
            logs = [(m.body.level, m.body.message) for m in seen if isinstance(m.body, g.LogMsg)]
            self.assertIn((g.LogLevel.info, "hello"), logs)
            self.assertIn((g.LogLevel.warn, "oops"), logs)
            by = {m.body.message: m.body.component for m in seen if isinstance(m.body, g.LogMsg)}
            self.assertEqual((by["hello"], by["oops"]), ("t", "t"))  # the container's output
            res = request.result.body
            self.assertEqual((res.state, res.exitCode), (S.stopped, 3))
            self.assertEqual(containers.calls[-1], ("remove", "t"))
            self.assertEqual({m.req_id for m in seen}, {request.req_id})

        asyncio.run(run())

    def test_by_default_the_request_ends_started_and_the_runtime_owns_it(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            containers = FakeContainers(out=["serving"])
            containers.forever = True
            file = g.ContainerFile(path="/etc/x", content="x")
            async with _runtime(containers, FakeDocker(), root) as address:
                with self.assertLogs("fabric.docker_run", "INFO") as logs:
                    async with _client(address) as client:
                        request = await client.request(_req(follow=False, files=[file]))
                        seen = [msg async for msg in request]
                    # the client is gone: the container keeps running, followed by the runtime
                    await asyncio.sleep(0.05)
                    self.assertNotIn(("stop", "t"), containers.calls)
                    self.assertTrue((Path(root) / "t/etc/x").exists())
                # the runtime stopped: so did the container, removed with its files
            states = [m.body.state for m in seen if isinstance(m.body, g.DockerContainer)]
            self.assertEqual(states, [S.starting, S.started])
            self.assertEqual(request.result.body.state, S.started)
            self.assertNotIn(
                "serving", [m.body.message for m in seen if isinstance(m.body, g.LogMsg)]
            )
            self.assertIn("t: serving", [r.getMessage() for r in logs.records])
            self.assertIn(("stop", "t"), containers.calls)
            self.assertEqual(containers.calls[-1], ("remove", "t"))
            self.assertFalse((Path(root) / "t").exists())

        asyncio.run(run())

    def test_an_owned_container_that_stops_is_removed_with_its_files(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            containers = FakeContainers(code=0)
            file = g.ContainerFile(path="/etc/x", content="x")
            async with _runtime(containers, FakeDocker(), root) as address:
                async with _client(address) as client:
                    res = await client.ask(_req(follow=False, files=[file]), timeout=5)
                for _ in range(100):
                    if not (Path(root) / "t").exists():
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(res.body.state, S.started)
                self.assertIn(("remove", "t"), containers.calls)
                self.assertNotIn(("stop", "t"), containers.calls)  # it ended by itself
                self.assertFalse((Path(root) / "t").exists())

        asyncio.run(run())

    def test_the_run_argv_carries_the_ir_options(self) -> None:
        spec = g.ContainerSpec(
            name="dns",
            image="coredns",
            command=["-conf", "/etc/Corefile"],
            privileged=True,
            network="none",
            env=[g.EnvVar(name="A", value="1")],
            mounts=[g.Mount(host="/srv/x", container="/x", readOnly=True)],
            flags=["--dns", "1.1.1.1"],
        )
        self.assertEqual(
            run_argv(spec),
            [
                "run", "-d", "--name", "dns", "--privileged", "--network", "none",
                "-e", "A=1", "-v", "/srv/x:/x:ro", "--dns", "1.1.1.1",
                "coredns", "-conf", "/etc/Corefile",
            ],
        )  # fmt: skip

    def test_ending_the_request_early_stops_the_container(self) -> None:
        async def run() -> None:
            containers = FakeContainers(out=["up"])
            containers.forever = True
            async with _runtime(containers, FakeDocker()) as address:
                async with _client(address) as client:
                    request = await client.request(_req())
                    async for msg in request:
                        if isinstance(msg.body, g.DockerContainer) and msg.body.state is S.started:
                            break
                # the client went away
                for _ in range(100):
                    if ("remove", "t") in containers.calls:
                        break
                    await asyncio.sleep(0.01)
            self.assertIn(("stop", "t"), containers.calls)
            self.assertIn(("remove", "t"), containers.calls)

        asyncio.run(run())

    def test_an_existing_container_of_that_name_is_replaced(self) -> None:
        async def run() -> None:
            containers = FakeContainers()
            containers.existing.add("t")  # a leftover, or started by hand
            async with _runtime(containers, FakeDocker()) as address, _client(address) as client:
                with self.assertLogs("fabric.docker_run", "WARNING"):
                    res = await client.ask(_req(), timeout=5)
            self.assertEqual(res.body.state, S.stopped)
            run_at = next(i for i, c in enumerate(containers.calls) if c[0] == "run")
            self.assertEqual(containers.calls[0], ("remove", "t"))
            self.assertLess(0, run_at)

        asyncio.run(run())

    def test_replacing_the_runtimes_own_container(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            containers = FakeContainers()
            containers.forever = True
            file = g.ContainerFile(path="/etc/x", content="old")
            async with _runtime(containers, FakeDocker(), root) as address:
                async with _client(address) as client:
                    first = await client.ask(_req(follow=False, files=[file]), timeout=5)
                    self.assertEqual(first.body.state, S.started)
                    new = g.ContainerFile(path="/etc/x", content="new")
                    with self.assertLogs("fabric.docker_run", "WARNING"):
                        second = await client.ask(_req(follow=False, files=[new]), timeout=5)
                    self.assertEqual(second.body.state, S.started)
                    # the old one was stopped and removed by its owner, before the new run
                    runs = [i for i, c in enumerate(containers.calls) if c[0] == "run"]
                    self.assertIn(("stop", "t"), containers.calls[runs[0] : runs[1]])
                    self.assertEqual((Path(root) / "t/etc/x").read_text(), "new")

        asyncio.run(run())

    def test_replacing_the_container_of_a_following_request(self) -> None:
        async def run() -> None:
            containers = FakeContainers()
            containers.forever = True
            async with _runtime(containers, FakeDocker()) as address:
                async with _client(address) as a, _client(address) as b:
                    following = await a.request(_req(follow=True))
                    async for msg in following:
                        if isinstance(msg.body, g.DockerContainer) and msg.body.state is S.started:
                            break
                    with self.assertLogs("fabric.docker_run", "WARNING"):
                        second = await b.ask(_req(follow=False), timeout=5)
                    old = await following.wait(timeout=5)
            self.assertEqual(second.body.state, S.started)
            self.assertEqual(old.body.state, S.stopped)  # the first request saw it end

        asyncio.run(run())

    def test_an_image_that_cannot_be_prepared_fails_without_running(self) -> None:
        async def run() -> None:
            containers = FakeContainers()
            async with _runtime(containers, FakeDocker(broken=["busybox"])) as address:
                async with _client(address) as client:
                    with self.assertLogs("fabric.docker", "WARNING"):
                        res = await client.ask(_req(), timeout=5)
            self.assertEqual(res.body.state, S.failed)
            self.assertIn("pull failed", res.body.message)
            self.assertEqual(containers.calls, [])

        asyncio.run(run())

    def test_a_failing_docker_run(self) -> None:
        async def run() -> None:
            containers = FakeContainers()
            containers.run_ok = False
            async with _runtime(containers, FakeDocker()) as address, _client(address) as client:
                res = await client.ask(_req(), timeout=5)
            self.assertEqual(res.body.state, S.failed)
            self.assertIn("invalid flag", res.body.message)

        asyncio.run(run())

    def test_files_are_written_mounted_read_only_and_removed(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            containers = FakeContainers()
            seen: list[str] = []

            async def logs(name: str, on_out: LineSink, on_err: LineSink) -> None:
                seen.append((Path(root) / "dns/etc/coredns/Corefile").read_text())

            containers.logs = logs  # type: ignore[method-assign]
            corefile = g.ContainerFile(path="/etc/coredns/Corefile", content=".:53 {\n}\n")
            async with _runtime(containers, FakeDocker(), root) as address:
                async with _client(address) as client:
                    res = await client.ask(_req(name="dns", files=[corefile]), timeout=5)
            self.assertEqual(res.body.state, S.stopped)
            self.assertEqual(seen, [".:53 {\n}\n"])  # there while it ran
            run_call = next(c for c in containers.calls if c[0] == "run")
            self.assertIn(f"{root}/dns/etc/coredns/Corefile:/etc/coredns/Corefile:ro", run_call)
            self.assertFalse((Path(root) / "dns").exists())  # gone with the container

        asyncio.run(run())

    def test_a_leftover_files_directory_is_replaced(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            (Path(root) / "t/etc").mkdir(parents=True)
            (Path(root) / "t/etc/stale").write_text("old")
            containers = FakeContainers()
            seen: list[list[str]] = []

            async def logs(name: str, on_out: LineSink, on_err: LineSink) -> None:
                seen.append(sorted(p.name for p in (Path(root) / "t/etc").iterdir()))

            containers.logs = logs  # type: ignore[method-assign]
            file = g.ContainerFile(path="/etc/x", content="new")
            async with _runtime(containers, FakeDocker(), root) as address:
                async with _client(address) as client:
                    with self.assertLogs("fabric.docker_run", "WARNING"):
                        res = await client.ask(_req(files=[file]), timeout=5)
            self.assertEqual(res.body.state, S.stopped)
            self.assertEqual(seen, [["x"]])  # the stale file is gone

        asyncio.run(run())

    def test_files_are_removed_when_docker_run_fails(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            containers = FakeContainers()
            containers.run_ok = False
            file = g.ContainerFile(path="/etc/x", content="x")
            async with _runtime(containers, FakeDocker(), root) as address:
                async with _client(address) as client:
                    res = await client.ask(_req(files=[file]), timeout=5)
            self.assertEqual(res.body.state, S.failed)
            self.assertFalse((Path(root) / "t").exists())

        asyncio.run(run())

    def test_a_relative_or_escaping_file_path_is_refused(self) -> None:
        async def run() -> None:
            async with _runtime(FakeContainers(), FakeDocker()) as address:
                async with _client(address) as client:
                    for path in ("etc/x", "/etc/../../x"):
                        file = g.ContainerFile(path=path, content="")
                        with self.subTest(path=path), self.assertRaises(RemoteError):
                            await client.ask(_req(files=[file]), timeout=5)

        asyncio.run(run())

    def test_a_bad_name_is_refused(self) -> None:
        async def run() -> None:
            async with _runtime(FakeContainers(), FakeDocker()) as address:
                async with _client(address) as client:
                    with self.assertRaises(RemoteError) as caught:
                        await client.ask(_req(name="-x"), timeout=5)
            self.assertEqual(caught.exception.code, "bad-request")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
