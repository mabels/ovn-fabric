from __future__ import annotations

import asyncio
import contextlib
import unittest
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from runtime import generated as rg
from runtime.command import Report
from runtime.peer import Peer, RemoteError
from runtime.runtime import Runtime
from runtime.transport import connect

from .. import generated as g
from .abstract_deps import AbstractDeps
from .sysdeps import SystemDeps
from .test_sysdeps import FakeApt, FakeSleep, _stamp

READY, FAILED, TIMEOUT = g.SystemDepState.ready, g.SystemDepState.failed, g.SystemDepState.timeout
DNF = g.PackageManager.dnf


class FakeDnf(FakeApt):
    """A package manager of kind dnf, in memory."""

    name: ClassVar[str] = "dnf"


class FakeSystemd:
    def __init__(self, broken: tuple[str, ...] = ()) -> None:
        self.started: list[list[str]] = []
        self.broken = broken

    async def start(self, units: list[str], report: Report) -> tuple[bool, str]:
        self.started.append(units)
        failing = [unit for unit in units if unit in self.broken]
        if failing:
            return False, f"Job for {failing[0]}.service failed."
        return True, ""


@contextlib.asynccontextmanager
async def _client(apt: FakeApt, systemd: FakeSystemd | None = None) -> AsyncIterator[Peer]:
    runtime = Runtime()
    runtime.register(
        SystemDeps([apt], stamp=_stamp(0), sleep=FakeSleep()).service(),
        AbstractDeps(runtime.client, systemd=systemd or FakeSystemd()).service(),
    )
    stop = asyncio.Event()
    address = f"mem://abstract-{id(runtime)}"
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


def _req(timeout: float | None = None, **deps: list[str]) -> g.AbstractDepsReq:
    return g.AbstractDepsReq(
        kind="AbstractDepsReq",
        packageManager=g.PackageManager.apt,
        deps=[g.AbstractDepSpec(name=name, systemDeps=pkgs) for name, pkgs in deps.items()],
        timeout=timeout,
    )


def _events(seen: list[Any], cls: type) -> list[Any]:
    return [m.body for m in seen if isinstance(m.body, cls)]


class AbstractDepsTest(unittest.TestCase):
    def test_each_abstract_dep_and_all_together(self) -> None:
        async def run() -> None:
            apt = FakeApt(installed={"iproute2": "6.1"})
            async with _client(apt) as client:
                request = await client.request(
                    _req(
                        ovn=["ovn-central", "ovn-host"], ip=["iproute2"], ovs=["openvswitch-switch"]
                    )
                )
                seen = [msg async for msg in request]
            # One apt run for everything missing (iproute2 was there).
            self.assertEqual(
                apt.calls, [("install", "ovn-central", "ovn-host", "openvswitch-switch")]
            )
            per_dep = {e.name: e.state for e in _events(seen, g.AbstractDep)}
            self.assertEqual(per_dep, {"ovn": READY, "ip": READY, "ovs": READY})
            ovn = next(e for e in _events(seen, g.AbstractDep) if e.name == "ovn")
            self.assertEqual([d.dep for d in ovn.deps], ["ovn-central", "ovn-host"])
            kinds = [m.kind for m in seen]
            self.assertEqual(kinds[-2:], ["AbstractDepsReady", "AbstractDepsRes"])
            self.assertEqual(_events(seen, g.AbstractDepsReady)[0].names, ["ovn", "ip", "ovs"])
            res = request.result.body
            self.assertEqual([s.name for s in res.deps], ["ovn", "ip", "ovs"])  # request order

        asyncio.run(run())

    def test_a_settled_dep_is_announced_before_the_rest(self) -> None:
        async def run() -> None:
            apt = FakeApt(installed={"docker.io": "26"})
            apt.gate = asyncio.Event()
            async with _client(apt) as client:
                request = await client.request(_req(docker=["docker.io"], ovn=["ovn-host"]))
                first: list[str] = []
                async for msg in request:
                    if isinstance(msg.body, g.AbstractDep):
                        first.append(msg.body.name)
                        apt.gate.set()  # ovn only installs after docker was announced
            self.assertEqual(first, ["docker", "ovn"])

        asyncio.run(run())

    def test_a_failed_package_fails_its_abstract_dep_only(self) -> None:
        async def run() -> None:
            apt = FakeApt(broken=["ovn-central"])
            async with _client(apt) as client:
                with self.assertLogs("fabric.sysdeps", "WARNING"):
                    request = await client.request(
                        _req(ovn=["ovn-central", "ovn-host"], ip=["iproute2"])
                    )
                    seen = [msg async for msg in request]
            per_dep = {e.name: e.state for e in _events(seen, g.AbstractDep)}
            self.assertEqual(per_dep, {"ovn": FAILED, "ip": READY})
            self.assertEqual(_events(seen, g.AbstractDepsReady), [])
            ovn = next(s for s in request.result.body.deps if s.name == "ovn")
            self.assertEqual(
                {d.dep: d.state for d in ovn.deps}, {"ovn-central": FAILED, "ovn-host": READY}
            )

        asyncio.run(run())

    def test_a_timeout_is_the_abstract_deps_state(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            apt.gate = asyncio.Event()  # never opened: the install hangs
            async with _client(apt) as client:
                res = await client.ask(_req(timeout=0.05, ovs=["openvswitch-switch"]), timeout=5)
                apt.gate.set()
            self.assertEqual([(s.name, s.state) for s in res.body.deps], [("ovs", TIMEOUT)])

        asyncio.run(run())

    def test_without_system_deps_it_is_ready_at_once(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            async with _client(apt) as client:
                res = await client.ask(_req(nothing=[]), timeout=5)
            self.assertEqual([(s.name, s.state) for s in res.body.deps], [("nothing", READY)])
            self.assertEqual(apt.calls, [])

        asyncio.run(run())

    def test_a_bare_name_uses_the_table_and_an_explicit_list_overrides_it(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            async with _client(apt) as client:
                req = g.AbstractDepsReq(
                    kind="AbstractDepsReq",
                    packageManager=g.PackageManager.apt,
                    deps=[
                        g.AbstractDepSpec(name="docker"),
                        g.AbstractDepSpec(name="ovn", systemDeps=["ovn-central", "ovn-host"]),
                    ],
                )
                res = await client.ask(req, timeout=5)
            deps = {s.name: [d.dep for d in s.deps] for s in res.body.deps}
            self.assertEqual(deps, {"docker": ["docker.io"], "ovn": ["ovn-central", "ovn-host"]})

        asyncio.run(run())

    def test_an_unknown_name_without_system_deps_fails(self) -> None:
        async def run() -> None:
            apt = FakeApt()
            async with _client(apt) as client:
                req = g.AbstractDepsReq(
                    kind="AbstractDepsReq",
                    packageManager=g.PackageManager.apt,
                    deps=[g.AbstractDepSpec(name="quantum"), g.AbstractDepSpec(name="ping")],
                )
                request = await client.request(req)
                seen = [msg async for msg in request]
            per_dep = {e.name: e.state for e in _events(seen, g.AbstractDep)}
            self.assertEqual(per_dep, {"quantum": FAILED, "ping": READY})
            self.assertEqual(_events(seen, g.AbstractDepsReady), [])
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            self.assertIn("quantum: unknown abstract dependency", logs)

        asyncio.run(run())

    def test_a_dep_listed_twice_is_refused(self) -> None:
        async def run() -> None:
            async with _client(FakeApt()) as client:
                req = g.AbstractDepsReq(
                    kind="AbstractDepsReq",
                    packageManager=g.PackageManager.apt,
                    deps=[
                        g.AbstractDepSpec(name="ip", systemDeps=["iproute2"]),
                        g.AbstractDepSpec(name="ip", systemDeps=["iproute2"]),
                    ],
                )
                with self.assertRaises(RemoteError) as caught:
                    await client.ask(req, timeout=5)
            self.assertEqual(caught.exception.code, "bad-request")

        asyncio.run(run())


class ServicesTest(unittest.TestCase):
    def test_on_dnf_its_units_are_started_once_its_packages_are_there(self) -> None:
        async def run() -> None:
            systemd = FakeSystemd()
            async with _client(FakeDnf(installed={"openvswitch": "3.6"}), systemd) as client:
                res = await client.ask(_by_name("ovs", "ping", manager=DNF), timeout=5)
            ovs, ping = res.body.deps
            self.assertEqual(ovs.state, READY)
            self.assertEqual(
                [(d.dep, d.state) for d in ovs.deps],
                [("openvswitch", READY), ("service:openvswitch", READY)],
            )
            self.assertEqual([d.dep for d in ping.deps], ["iputils"])  # no units
            self.assertEqual(systemd.started, [["openvswitch"]])

        asyncio.run(run())

    def test_a_unit_that_does_not_start_fails_the_dependency(self) -> None:
        async def run() -> None:
            systemd = FakeSystemd(broken=("ovn-northd",))
            async with _client(FakeDnf(), systemd) as client:
                res = await client.ask(_by_name("ovn-central", manager=DNF), timeout=5)
            (central,) = res.body.deps
            self.assertEqual(central.state, FAILED)
            self.assertIn("ovn-northd.service failed", central.deps[-1].message)

        asyncio.run(run())

    def test_on_apt_nothing_is_started(self) -> None:
        async def run() -> None:
            systemd = FakeSystemd()
            async with _client(FakeApt(), systemd) as client:
                res = await client.ask(_by_name("ovs"), timeout=5)
            self.assertEqual(res.body.deps[0].state, READY)
            self.assertEqual(systemd.started, [])  # Debian starts them on install

        asyncio.run(run())


def _by_name(*names: str, manager: g.PackageManager = g.PackageManager.apt) -> g.AbstractDepsReq:
    return g.AbstractDepsReq(
        kind="AbstractDepsReq",
        packageManager=manager,
        deps=[g.AbstractDepSpec(name=name) for name in names],
    )


if __name__ == "__main__":
    unittest.main()
