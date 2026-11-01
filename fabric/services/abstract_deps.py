"""Abstract dependencies: "ovn", "docker", … made ready through their
system dependencies.

    AbstractDepsReq { deps: [{name, systemDeps: [...]}], timeout? }
      → (internal ResolveSystemDepsReq for all their system deps at once;
         its logs relayed)
      → AbstractDep { name, state, deps }   one per abstract dep, as it settles
      → AbstractDepsReady { names }         once every one is ready
      → AbstractDepsRes { deps: [AbstractDepStatus] }

What an abstract dependency means (its system dependencies) is this
service's table (SYSTEM_DEPS): the packages per package manager — the
column of the one the request names (the host's config chose it). A
request entry may name them itself instead. A name the table does not
know, or has no packages for on this manager, without its own system
dependencies, fails. Other services ask by abstract name —
docker prepare asks for `docker`. All system dependencies go to sysdeps in one request, so they
share its queue and one apt run; an abstract dependency settles as soon as
all of its own have, so an early one is announced without waiting for the
rest. Its state is the worst of its system dependencies' (failed, then
timeout, then ready); one with none is ready at once.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

from runtime import command
from runtime import generated as rg
from runtime.command import Report
from runtime.message import BodySpec, ProtocolError, register_bodies
from runtime.peer import Call, Peer
from runtime.service import Service

from .. import generated as g

BODIES = {
    "AbstractDepsReq": BodySpec(g.AbstractDepsReq, "request"),
    "AbstractDep": BodySpec(g.AbstractDep, "event"),
    "AbstractDepsReady": BodySpec(g.AbstractDepsReady, "event"),
    "AbstractDepsRes": BodySpec(g.AbstractDepsRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

log = logging.getLogger("fabric.abstract_deps")

# What an abstract dependency means, per package manager (apt: the names in
# use on Ubuntu; dnf: checked against Fedora 44's repositories). `ovn` is
# ovn-controller (every OVN host runs one); the central additionally needs
# `ovn-central` (the NB/SB DBs and northd). A manager missing from an entry:
# not packaged there.
SYSTEM_DEPS: dict[str, dict[str, list[str]]] = {
    "ovn": {"apt": ["ovn-host"], "dnf": ["ovn-host"]},
    "ovn-central": {"apt": ["ovn-central"], "dnf": ["ovn-central"]},
    "ovs": {"apt": ["openvswitch-switch"], "dnf": ["openvswitch"]},
    "ip": {"apt": ["iproute2"], "dnf": ["iproute"]},
    "iptables": {"apt": ["iptables"], "dnf": ["iptables-nft"]},
    "dhclient": {"apt": ["isc-dhcp-client"], "dnf": ["dhcp-client"]},
    "dhcpcd": {"apt": ["dhcpcd"], "dnf": ["dhcpcd"]},
    "wireguard": {"apt": ["wireguard-tools"], "dnf": ["wireguard-tools"]},
    "docker": {"apt": ["docker.io"], "dnf": ["moby-engine"]},
    "arp": {"apt": ["net-tools"], "dnf": ["net-tools"]},
    "ping": {"apt": ["iputils-ping"], "dnf": ["iputils"]},
    "traceroute": {"apt": ["traceroute"], "dnf": ["traceroute"]},
    "python": {"apt": ["python-is-python3"], "dnf": ["python-unversioned-command"]},
}

# The systemd units an abstract dependency needs running, per package
# manager. Debian/Ubuntu start a package's services when it is installed;
# Fedora installs them disabled and stopped (preset `disabled`), so there
# they are enabled and started here. A dependency is ready only once its
# units run.
SERVICES: dict[str, dict[str, list[str]]] = {
    "ovs": {"dnf": ["openvswitch"]},
    "ovn": {"dnf": ["ovn-controller"]},
    "ovn-central": {"dnf": ["ovn-northd"]},  # starts the NB/SB databases too
    "docker": {"dnf": ["docker"]},
}


class ServiceManager(Protocol):
    async def start(self, units: list[str], report: Report) -> tuple[bool, str]:
        """The units running, now and at boot; (ok, output)."""


class Systemd:
    """Starting units — `systemctl enable --now`: running now and at boot."""

    async def start(self, units: list[str], report: Report) -> tuple[bool, str]:
        return await command.run(["systemctl", "enable", "--now", *units], log=log, report=report)


_WORST = [g.SystemDepState.failed, g.SystemDepState.timeout, g.SystemDepState.ready]


def _state(statuses: list[g.DepStatus]) -> g.SystemDepState:
    states = {status.state for status in statuses}
    return next((state for state in _WORST if state in states), g.SystemDepState.ready)


class AbstractDeps:
    def __init__(
        self,
        client: Callable[[], Awaitable[Peer]],
        table: Mapping[str, Mapping[str, list[str]]] = SYSTEM_DEPS,
        services: Mapping[str, Mapping[str, list[str]]] = SERVICES,
        systemd: ServiceManager | None = None,
    ) -> None:
        self._client = client  # the runtime's internal connection, to ask sysdeps
        self._table = table
        self._services = services
        self._systemd: ServiceManager = systemd if systemd is not None else Systemd()

    def service(self) -> Service:
        return Service(
            name="abstract_deps", bodies=BODIES, handlers={"AbstractDepsReq": self.resolve}
        )

    async def resolve(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.AbstractDepsReq)
        names = [spec.name for spec in req.deps]
        if len(set(names)) != len(names):
            raise ProtocolError("bad-request", "an abstract dependency is listed twice")
        manager = req.packageManager  # the tables' column
        settled: dict[str, g.DepStatus] = {}
        announced: dict[str, g.AbstractDepStatus] = {}
        wanted: dict[str, list[str]] = {}
        for spec in req.deps:
            deps = spec.systemDeps
            if deps is None:
                by_manager = self._table.get(spec.name)
                deps = None if by_manager is None else by_manager.get(manager.value)
            if deps is None:
                announced[spec.name] = g.AbstractDepStatus(
                    name=spec.name, state=g.SystemDepState.failed, deps=[]
                )
                why = (
                    "unknown abstract dependency"
                    if spec.name not in self._table
                    else f"not packaged for {manager.value}"
                )
                await call.log("warn", f"{spec.name}: {why}")
                await call.emit(
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
                if _state(statuses) is g.SystemDepState.ready:
                    statuses += await self._start_services(call, manager, name)
                status = announced[name] = g.AbstractDepStatus(
                    name=name, state=_state(statuses), deps=statuses
                )
                await call.emit(
                    g.AbstractDep(
                        kind="AbstractDep", name=name, state=status.state, deps=status.deps
                    )
                )

        await announce_settled()  # the ones without system dependencies
        system = list(dict.fromkeys(dep for deps in wanted.values() for dep in deps))
        if system:
            await self._system(call, manager, system, req.timeout, settled, announce_settled)

        res = [announced[name] for name in names]
        if all(status.state is g.SystemDepState.ready for status in res):
            await call.emit(g.AbstractDepsReady(kind="AbstractDepsReady", names=names))
        return g.AbstractDepsRes(kind="AbstractDepsRes", deps=res)

    async def _start_services(
        self, call: Call, manager: g.PackageManager, name: str
    ) -> list[g.DepStatus]:
        """Its units running, as one status each (`service:<unit>`)."""
        units = self._services.get(name, {}).get(manager.value, [])
        if not units:
            return []
        ok, out = await self._systemd.start(units, call.report)
        state = g.SystemDepState.ready if ok else g.SystemDepState.failed
        message = "running" if ok else "\n".join(out.strip().splitlines()[-3:])
        return [g.DepStatus(dep=f"service:{unit}", state=state, message=message) for unit in units]

    async def _system(
        self,
        call: Call,
        manager: g.PackageManager,
        deps: list[str],
        timeout: float | None,
        settled: dict[str, g.DepStatus],
        announce_settled: Callable[[], Awaitable[None]],
    ) -> None:
        client = await self._client()
        request = await client.request(
            g.ResolveSystemDepsReq(
                kind="ResolveSystemDepsReq", packageManager=manager, deps=deps, timeout=timeout
            ),
            parent=call.req_id,
        )
        async for event in request:
            if isinstance(event.body, rg.LogMsg):  # sysdeps' progress, as our own
                await call.emit(event.body)
            elif isinstance(event.body, g.ResolveSystemDep):
                settled[event.body.dep] = g.DepStatus(
                    dep=event.body.dep, state=event.body.state, message=event.body.message
                )
                await announce_settled()
        final = request.result.body
        if isinstance(final, rg.ErrorRes):
            raise ProtocolError(final.code, f"sysdeps: {final.message}")
        assert isinstance(final, g.ResolveSystemDepsRes)
        for status in final.deps:  # the final word, should an event be missing
            settled.setdefault(status.dep, status)
        await announce_settled()


def service(client: Callable[[], Awaitable[Peer]]) -> Service:
    return AbstractDeps(client).service()
