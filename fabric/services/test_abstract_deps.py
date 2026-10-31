from __future__ import annotations

import asyncio
import contextlib
import unittest
from collections.abc import AsyncIterator
from typing import Any

from runtime import generated as rg
from runtime.peer import Peer, RemoteError
from runtime.runtime import Runtime
from runtime.transport import connect

from .. import generated as g
from .abstract_deps import AbstractDeps
from .sysdeps import SystemDeps
from .test_sysdeps import FakeApt, FakeSleep, _stamp

READY, FAILED, TIMEOUT = g.SystemDepState.ready, g.SystemDepState.failed, g.SystemDepState.timeout


@contextlib.asynccontextmanager
async def _client(apt: FakeApt) -> AsyncIterator[Peer]:
    runtime = Runtime()
    runtime.register(
        SystemDeps(apt, stamp=_stamp(0), sleep=FakeSleep()).service(),
        AbstractDeps(runtime.client).service(),
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
                    deps=[g.AbstractDepSpec(name="quantum"), g.AbstractDepSpec(name="jq")],
                )
                request = await client.request(req)
                seen = [msg async for msg in request]
            per_dep = {e.name: e.state for e in _events(seen, g.AbstractDep)}
            self.assertEqual(per_dep, {"quantum": FAILED, "jq": READY})
            self.assertEqual(_events(seen, g.AbstractDepsReady), [])
            logs = [m.body.message for m in seen if isinstance(m.body, rg.LogMsg)]
            self.assertIn("quantum: unknown abstract dependency", logs)

        asyncio.run(run())

    def test_a_dep_listed_twice_is_refused(self) -> None:
        async def run() -> None:
            async with _client(FakeApt()) as client:
                req = g.AbstractDepsReq(
                    kind="AbstractDepsReq",
                    deps=[
                        g.AbstractDepSpec(name="ip", systemDeps=["iproute2"]),
                        g.AbstractDepSpec(name="ip", systemDeps=["iproute2"]),
                    ],
                )
                with self.assertRaises(RemoteError) as caught:
                    await client.ask(req, timeout=5)
            self.assertEqual(caught.exception.code, "bad-request")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
