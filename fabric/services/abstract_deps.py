"""Abstract dependencies: "ovn", "docker", … made ready through their
system dependencies.

    AbstractDepsReq { deps: [{name, systemDeps: [...]}], timeout? }
      → (internal ResolveSystemDepsReq for all their system deps at once;
         its logs relayed)
      → AbstractDep { name, state, deps }   one per abstract dep, as it settles
      → AbstractDepsReady { names }         once every one is ready
      → AbstractDepsRes { deps: [AbstractDepStatus] }

What an abstract dependency means (its system dependencies) is this
service's table (SYSTEM_DEPS, Debian/Ubuntu packages); a request entry may
name them itself instead (the IR's per-host choice: `ovn` on the central
is ovn-central + ovn-host). A name the table does not know, without its
own system dependencies, fails. Other services ask by abstract name —
docker prepare asks for `docker`. All system dependencies go to sysdeps in one request, so they
share its queue and one apt run; an abstract dependency settles as soon as
all of its own have, so an early one is announced without waiting for the
rest. Its state is the worst of its system dependencies' (failed, then
timeout, then ready); one with none is ready at once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from runtime.message import BodySpec, Msg, ProtocolError, register_bodies
from runtime.peer import Peer
from runtime.service import Service

from .. import generated as g

BODIES = {
    "AbstractDepsReq": BodySpec(g.AbstractDepsReq, "request"),
    "AbstractDep": BodySpec(g.AbstractDep, "event"),
    "AbstractDepsReady": BodySpec(g.AbstractDepsReady, "event"),
    "AbstractDepsRes": BodySpec(g.AbstractDepsRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

# What an abstract dependency means on Debian/Ubuntu (the deployer's
# _PACKAGE_BY_DEP). `ovn` is ovn-controller (every OVN host runs one); the
# central additionally needs ovn-central, which its request names itself.
SYSTEM_DEPS: dict[str, list[str]] = {
    "ovn": ["ovn-host"],
    "ovs": ["openvswitch-switch"],
    "ip": ["iproute2"],
    "iptables": ["iptables"],
    "dhclient": ["isc-dhcp-client"],
    "dhcpcd": ["dhcpcd"],
    "wireguard": ["wireguard-tools"],
    "docker": ["docker.io"],
    "jq": ["jq"],
    "arp": ["net-tools"],
    "ping": ["iputils-ping"],
    "traceroute": ["traceroute"],
    "curl": ["curl"],
    "python": ["python-is-python3"],
}

_WORST = [g.SystemDepState.failed, g.SystemDepState.timeout, g.SystemDepState.ready]


def _state(statuses: list[g.DepStatus]) -> g.SystemDepState:
    states = {status.state for status in statuses}
    return next((state for state in _WORST if state in states), g.SystemDepState.ready)


class AbstractDeps:
    def __init__(
        self,
        client: Callable[[], Awaitable[Peer]],
        table: Mapping[str, list[str]] = SYSTEM_DEPS,
    ) -> None:
        self._client = client  # the runtime's internal connection, to ask sysdeps
        self._table = table

    def service(self) -> Service:
        return Service(
            name="abstract_deps", bodies=BODIES, handlers={"AbstractDepsReq": self.resolve}
        )

    async def resolve(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.AbstractDepsReq)
        names = [spec.name for spec in req.deps]
        if len(set(names)) != len(names):
            raise ProtocolError("bad-request", "an abstract dependency is listed twice")
        settled: dict[str, g.DepStatus] = {}
        announced: dict[str, g.AbstractDepStatus] = {}
        wanted: dict[str, list[str]] = {}
        for spec in req.deps:
            deps = spec.systemDeps if spec.systemDeps is not None else self._table.get(spec.name)
            if deps is None:
                announced[spec.name] = g.AbstractDepStatus(
                    name=spec.name, state=g.SystemDepState.failed, deps=[]
                )
                await peer.log("warn", f"{spec.name}: unknown abstract dependency")
                await peer.emit(
                    g.AbstractDep(
                        kind="AbstractDep", name=spec.name, state=g.SystemDepState.failed, deps=[]
                    )
                )
                continue
            wanted[spec.name] = list(dict.fromkeys(deps))

        async def announce_settled() -> None:
            for name, deps in wanted.items():
                if name in announced or not all(dep in settled for dep in deps):
                    continue
                statuses = [settled[dep] for dep in deps]
                status = announced[name] = g.AbstractDepStatus(
                    name=name, state=_state(statuses), deps=statuses
                )
                await peer.emit(
                    g.AbstractDep(
                        kind="AbstractDep", name=name, state=status.state, deps=status.deps
                    )
                )

        await announce_settled()  # the ones without system dependencies
        system = list(dict.fromkeys(dep for deps in wanted.values() for dep in deps))
        if system:
            await self._system(peer, system, req.timeout, settled, announce_settled)

        res = [announced[name] for name in names]
        if all(status.state is g.SystemDepState.ready for status in res):
            await peer.emit(g.AbstractDepsReady(kind="AbstractDepsReady", names=names))
        return g.AbstractDepsRes(kind="AbstractDepsRes", deps=res)

    async def _system(
        self,
        peer: Peer,
        deps: list[str],
        timeout: float | None,
        settled: dict[str, g.DepStatus],
        announce_settled: Callable[[], Awaitable[None]],
    ) -> None:
        client = await self._client()
        request = await client.request(
            g.ResolveSystemDepsReq(kind="ResolveSystemDepsReq", deps=deps, timeout=timeout)
        )
        async for event in request:
            if isinstance(event.body, g.LogMsg):  # sysdeps' progress, as our own
                await peer.emit(event.body)
            elif isinstance(event.body, g.ResolveSystemDep):
                settled[event.body.dep] = g.DepStatus(
                    dep=event.body.dep, state=event.body.state, message=event.body.message
                )
                await announce_settled()
        final = request.result.body
        if isinstance(final, g.ErrorRes):
            raise ProtocolError(final.code, f"sysdeps: {final.message}")
        assert isinstance(final, g.ResolveSystemDepsRes)
        for status in final.deps:  # the final word, should an event be missing
            settled.setdefault(status.dep, status)
        await announce_settled()


def service(client: Callable[[], Awaitable[Peer]]) -> Service:
    return AbstractDeps(client).service()
