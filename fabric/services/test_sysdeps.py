from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import unittest
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from runtime import generated as rg
from runtime.command import report
from runtime.peer import Peer, RemoteError
from runtime.runtime import Runtime
from runtime.transport import connect

from .. import generated as g
from .sysdeps import UPDATE_EVERY, SystemDeps

READY, FAILED, TIMEOUT = g.SystemDepState.ready, g.SystemDepState.failed, g.SystemDepState.timeout


class FakeApt:
    def __init__(self, installed: dict[str, str] | None = None, broken: Sequence[str] = ()) -> None:
        self.installed = dict(installed or {})
        self.broken = set(broken)  # packages whose install fails
        self.calls: list[tuple[str, ...]] = []
        self.gate: asyncio.Event | None = None  # when set: installs wait for it
        self.update_fails = 0  # fail the next N updates
        self.transient = 0  # fail the next N installs with a transient error
        self._active = 0

    async def version(self, package: str) -> str | None:
        return self.installed.get(package)

    async def update(self) -> tuple[bool, str]:
        self.calls.append(("update",))
        report("info", "$ apt-get update")
        if self.update_fails:
            self.update_fails -= 1
            return False, "E: temporary failure resolving"
        return True, ""

    async def install(self, packages: Sequence[str]) -> tuple[bool, str]:
        self.calls.append(("install", *packages))
        report("info", f"$ apt-get install -y {' '.join(packages)}")  # as command.run does
        for package in packages:
            report("debug", f"Setting up {package} (1.0) ...")
        self._active += 1
        try:
            assert self._active == 1, "apt runs twice at a time"
            if self.gate is not None:
                await self.gate.wait()
            await asyncio.sleep(0)
            if self.transient:
                self.transient -= 1
                return False, "E: Could not get lock /var/lib/dpkg/lock-frontend"
            failing = [p for p in packages if p in self.broken]
            if failing:  # apt refuses the whole transaction
                return False, f"E: Unable to locate package {failing[0]}"
            for package in packages:
                self.installed[package] = "1.0"
            return True, ""
        finally:
            self._active -= 1

    async def upgrade(self, packages: Sequence[str]) -> tuple[bool, str]:
        self.calls.append(("upgrade", *packages))
        return True, ""


class FakeSleep:
    """Retries' backoff returns at once (recorded); the refresher's long
    wait for the next day blocks until cancelled."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if delay > 10_000:  # the wait for the next day's refresh
            await asyncio.Event().wait()


def _stamp(age: float) -> str:
    path = Path(tempfile.mkdtemp()) / "last-apt-update"
    path.write_text(str(time.time() - age))
    return str(path)


@contextlib.asynccontextmanager
async def _client(sysdeps: SystemDeps) -> AsyncIterator[Peer]:
    runtime = Runtime()
    runtime.register(sysdeps.service())
    stop = asyncio.Event()
    address = f"mem://sysdeps-{id(sysdeps)}"
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


def _req(*deps: str, timeout: float | None = None) -> g.ResolveSystemDepsReq:
    return g.ResolveSystemDepsReq(kind="ResolveSystemDepsReq", deps=list(deps), timeout=timeout)


def _states(res: Any) -> dict[str, g.SystemDepState]:
    assert isinstance(res, g.ResolveSystemDepsRes)
    return {status.dep: status.state for status in res.deps}


class ResolveTest(unittest.TestCase):
    def test_an_installed_dep_is_ready_without_apt(self) -> None:
        async def run() -> None:
            apt = FakeApt(installed={"curl": "8.5.0"})
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep())) as client:
                request = await client.request(_req("curl"))
                seen = [msg async for msg in request]
            self.assertEqual(apt.calls, [])
            event, final = seen
            self.assertEqual((event.body.dep, event.body.state), ("curl", READY))
            self.assertEqual(event.req_id, request.req_id)
            self.assertEqual(final.body.deps[0].message, "8.5.0")  # the version

        asyncio.run(run())

    def test_a_missing_dep_is_installed_after_a_stale_update(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            stamp = _stamp(UPDATE_EVERY + 1)
            async with _client(SystemDeps(apt, stamp=stamp, sleep=FakeSleep())) as client:
                res = await client.ask(_req("jq"), timeout=5)
            self.assertEqual(_states(res.body), {"jq": READY})
            self.assertIn(("install", "jq"), apt.calls)
            self.assertLess(apt.calls.index(("update",)), apt.calls.index(("install", "jq")))
            self.assertGreater(float(Path(stamp).read_text()), time.time() - 60)

        asyncio.run(run())

    def test_fresh_lists_are_not_updated_again(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            async with _client(SystemDeps(apt, stamp=_stamp(60), sleep=FakeSleep())) as client:
                await client.ask(_req("jq"), timeout=5)
            self.assertEqual(apt.calls, [("install", "jq")])

        asyncio.run(run())

    def test_concurrent_requests_are_serialized_and_share_a_dep(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            apt.gate = asyncio.Event()
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep())) as client:
                first = await client.request(_req("curl", "jq"))
                second = await client.request(_req("jq", "iproute2"))
                await asyncio.sleep(0.01)
                apt.gate.set()
                a, b = await first.wait(5), await second.wait(5)
            self.assertEqual(_states(a.body), {"curl": READY, "jq": READY})
            self.assertEqual(_states(b.body), {"jq": READY, "iproute2": READY})
            # jq was installed once, with the first request.
            self.assertEqual(apt.calls, [("install", "curl", "jq"), ("install", "iproute2")])

        asyncio.run(run())

    def test_a_transient_failure_is_retried_with_backoff(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            apt.transient = 6  # every attempt
            sleep = FakeSleep()
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=sleep)) as client:
                with self.assertLogs("fabric.sysdeps", "WARNING"):
                    res = await client.ask(_req("jq"), timeout=5)
            self.assertEqual(_states(res.body), {"jq": FAILED})
            self.assertIn("Could not get lock", res.body.deps[0].message)
            self.assertEqual(apt.calls.count(("install", "jq")), 6)
            self.assertEqual([d for d in sleep.delays if d < 10_000], [5, 10, 20, 40, 80])

        asyncio.run(run())

    def test_an_unknown_package_is_not_retried(self) -> None:
        async def run() -> None:
            apt = FakeApt(broken=["nope"])
            sleep = FakeSleep()
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=sleep)) as client:
                with self.assertLogs("fabric.sysdeps", "WARNING"):
                    res = await client.ask(_req("nope"), timeout=5)
            self.assertEqual(_states(res.body), {"nope": FAILED})
            self.assertIn("Unable to locate package nope", res.body.deps[0].message)
            self.assertEqual(apt.calls.count(("install", "nope")), 1)
            self.assertEqual([d for d in sleep.delays if d < 10_000], [])

        asyncio.run(run())

    def test_one_bad_package_only_fails_itself(self) -> None:
        async def run() -> None:
            apt = FakeApt(broken=["nope"])
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep())) as client:
                with self.assertLogs("fabric.sysdeps", "WARNING"):
                    res = await client.ask(_req("jq", "nope", "curl"), timeout=5)
            self.assertEqual(_states(res.body), {"jq": READY, "nope": FAILED, "curl": READY})
            self.assertEqual(
                apt.calls,
                [
                    ("install", "jq", "nope", "curl"),
                    ("install", "jq"),
                    ("install", "nope"),
                    ("install", "curl"),
                ],
            )

        asyncio.run(run())

    def test_a_timeout_reports_and_the_install_still_finishes(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            apt.gate = asyncio.Event()
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep())) as client:
                res = await client.ask(_req("jq", timeout=0.05), timeout=5)
                self.assertEqual(_states(res.body), {"jq": TIMEOUT})
                apt.gate.set()
                again = await client.ask(_req("jq"), timeout=5)
            self.assertEqual(_states(again.body), {"jq": READY})
            self.assertEqual(apt.calls.count(("install", "jq")), 1)

        asyncio.run(run())

    def test_a_bad_package_name_is_refused(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep())) as client:
                with self.assertRaises(RemoteError) as caught:
                    await client.ask(_req("curl", "-o=APT::Get::Foo"), timeout=5)
            self.assertEqual(caught.exception.code, "bad-request")
            self.assertEqual(apt.calls, [])

        asyncio.run(run())


class OutputTest(unittest.TestCase):
    def test_the_apt_output_reaches_every_request_waiting_on_the_job(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            apt.gate = asyncio.Event()
            async with _client(SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep())) as client:
                first = await client.request(_req("curl", "jq"))
                second = await client.request(_req("jq"))  # joins the same job
                await asyncio.sleep(0.01)
                apt.gate.set()
                a = [msg async for msg in first]
                b = [msg async for msg in second]

            def lines(seen: list[Any]) -> list[str]:
                return [
                    m.body.message
                    for m in seen
                    if isinstance(m.body, rg.LogMsg) and m.body.level is rg.LogLevel.debug
                ]

            self.assertEqual(lines(a), ["Setting up curl (1.0) ...", "Setting up jq (1.0) ..."])
            self.assertEqual(lines(b), lines(a))
            # The output comes before the dependency's status.
            kinds = [m.kind for m in a]
            self.assertLess(
                max(i for i, m in enumerate(a) if isinstance(m.body, rg.LogMsg)),
                kinds.index("ResolveSystemDep"),
            )

        asyncio.run(run())


class RefreshTest(unittest.TestCase):
    def test_the_regular_refresh_updates_and_upgrades_what_it_knows(self) -> None:
        async def run() -> None:
            apt = FakeApt(installed={"curl": "8.5.0"})
            sysdeps = SystemDeps(apt, stamp=_stamp(60), sleep=FakeSleep())
            async with _client(sysdeps) as client:
                await client.ask(_req("curl"), timeout=5)
            apt.calls.clear()
            await sysdeps._refresh()
            self.assertEqual(apt.calls, [("update",), ("upgrade", "curl")])

        asyncio.run(run())

    def test_nothing_requested_means_no_apt_at_all(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            async with _client(SystemDeps(apt, stamp=_stamp(UPDATE_EVERY + 1), sleep=FakeSleep())):
                await asyncio.sleep(0.05)
            self.assertEqual(apt.calls, [])

        asyncio.run(run())

    def test_the_first_ready_dep_starts_a_due_refresh(self) -> None:
        async def run() -> None:
            apt = FakeApt(installed={"curl": "8.5.0"})
            stale = _stamp(UPDATE_EVERY + 1)
            async with _client(SystemDeps(apt, stamp=stale, sleep=FakeSleep())) as client:
                await client.ask(_req("curl"), timeout=5)  # installed: no apt for the request
                for _ in range(100):
                    if ("upgrade", "curl") in apt.calls:
                        break
                    await asyncio.sleep(0.01)
            self.assertEqual(apt.calls, [("update",), ("upgrade", "curl")])

        asyncio.run(run())

    def test_a_failed_refresh_is_retried_later_not_at_once(self) -> None:
        async def run() -> None:
            apt = FakeApt(installed={"curl": "8.5.0"})
            apt.update_fails = 6  # every attempt of the first refresh
            sleep = FakeSleep()
            with self.assertLogs("fabric.sysdeps", "WARNING"):
                stale = _stamp(UPDATE_EVERY + 1)
                async with _client(SystemDeps(apt, stamp=stale, sleep=sleep)) as client:
                    await client.ask(_req("curl"), timeout=5)
                    for _ in range(200):
                        if apt.calls.count(("update",)) >= 7:
                            break
                        await asyncio.sleep(0.01)
            # 6 attempts with backoff, then the refresh waits an hour, then again.
            self.assertEqual(sleep.delays[:6], [5, 10, 20, 40, 80, 3600])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
