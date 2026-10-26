from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from .lifecycle import (
    Command,
    LifecycleController,
    Primitive,
    RawNodeTemplate,
)
from .primitives_ovn import (
    OVNNbCtlAcl,
    OVNNbCtlLr,
    OVNNbCtlLrp,
    OVNNbCtlLs,
    OVNNbCtlLsp,
    OVNNbCtlNat,
    OVNNbCtlRoute,
)

GLOBAL_NETNS = "*global*"


def _ns_arg(netns: str | None) -> str:
    return GLOBAL_NETNS if netns is None else netns


def _prefix(netns: str | None, argv: Sequence[str]) -> tuple[str, ...]:
    if netns is None:
        return tuple(argv)
    return ("ip", "netns", "exec", netns, *argv)


@dataclasses.dataclass(frozen=True)
class HostPrimitive:
    name: str

    @property
    def ref(self) -> str:
        return f"host:{self.name}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="host",
                    fields={"hostname": self.name},
                ),
            ),
            create=(),
            delete=(),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)

    def netns(self, name: str | None) -> "NetnsPrimitive":
        return NetnsPrimitive(host=self.name, name=name)

    # -- OVN northbound objects -----------------------------------------
    # The cluster pass runs on a central chassis; these factory methods
    # keep the generated body to `lc.run(host.ovn_nbctl_*(...))`.

    def ovn_nbctl_ls(self, name: str) -> OVNNbCtlLs:
        return OVNNbCtlLs(name)

    def ovn_nbctl_lsp(
        self,
        switch: str,
        name: str,
        *,
        type: str | None = None,
        addresses: Sequence[str] = (),
        options: Sequence[tuple[str, str]] = (),
    ) -> OVNNbCtlLsp:
        return OVNNbCtlLsp(
            switch=switch,
            name=name,
            type=type,
            addresses=tuple(addresses),
            options=tuple(options),
        )

    def ovn_nbctl_lr(
        self,
        name: str,
        *,
        options: Sequence[tuple[str, str]] = (),
        external_ids: Sequence[tuple[str, str]] = (),
    ) -> OVNNbCtlLr:
        return OVNNbCtlLr(
            name=name,
            options=tuple(options),
            external_ids=tuple(external_ids),
        )

    def ovn_nbctl_lrp(
        self,
        router: str,
        name: str,
        mac: str,
        networks: Sequence[str] = (),
        *,
        gateway_chassis: Any = None,
        ipv6_ra_configs: Sequence[tuple[str, str]] = (),
    ) -> OVNNbCtlLrp:
        return OVNNbCtlLrp(
            router=router,
            name=name,
            mac=mac,
            networks=tuple(networks),
            gateway_chassis=gateway_chassis,
            ipv6_ra_configs=tuple(ipv6_ra_configs),
        )

    def ovn_nbctl_route(self, router: str, prefix: str, nexthop: str) -> OVNNbCtlRoute:
        return OVNNbCtlRoute(router, prefix, nexthop)

    def ovn_nbctl_nat(
        self,
        router: str,
        nat_type: str,
        external_ip: str,
        logical_ip: str,
        *,
        reset: bool = False,
    ) -> OVNNbCtlNat:
        return OVNNbCtlNat(
            router=router,
            type=nat_type,
            external_ip=external_ip,
            logical_ip=logical_ip,
            reset=reset,
        )

    def ovn_nbctl_acl(
        self,
        switch: str,
        direction: str,
        priority: int,
        match: str,
        action: str,
    ) -> OVNNbCtlAcl:
        return OVNNbCtlAcl(switch, direction, priority, match, action)


@dataclasses.dataclass(frozen=True)
class NetnsPrimitive:
    host: str
    name: str | None

    @property
    def ref(self) -> str:
        return f"netns:{_ns_arg(self.name)}"

    def as_primitive(self) -> Primitive:
        if self.name is None:
            create: tuple[Command, ...] = ()
            delete: tuple[Command, ...] = ()
        else:
            create = (
                Command(
                    f"{self.host}:netns {self.name} (create)",
                    ("ip", "netns", "add", self.name),
                ),
            )
            delete = (
                Command(
                    f"{self.host}:netns {self.name} (delete)",
                    ("ip", "netns", "delete", self.name),
                ),
            )
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="netns",
                    fields={
                        "scope": "global" if self.name is None else "isolated",
                        "name": _ns_arg(self.name),
                        "linkRefs": [],
                        "routeRefs": [],
                        "ruleRefs": [],
                    },
                ),
            ),
            create=create,
            delete=delete,
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)

    def add_if(
        self,
        name: str,
        *,
        kind: str = "dummy",
        peer: str | None = None,
        mac: str | None = None,
    ) -> "LinkPrimitive":
        return LinkPrimitive(
            host=self.host,
            netns=self.name,
            name=name,
            kind=kind,
            peer=peer,
            mac=mac,
        )


async def _link_probe(lc: LifecycleController) -> dict[str, Any] | None:
    return None


@dataclasses.dataclass(frozen=True)
class LinkPrimitive:
    host: str
    netns: str | None
    name: str
    kind: str = "dummy"
    peer: str | None = None
    mac: str | None = None

    @property
    def ref(self) -> str:
        return f"link:{_ns_arg(self.netns)}|{self.name}"

    def as_primitive(self) -> Primitive:
        create_argv: tuple[str, ...]
        nodes: tuple[RawNodeTemplate, ...]
        if self.kind == "veth":
            if self.peer is None:
                raise ValueError("veth addIf needs peer=")
            create_argv = (
                "ip",
                "link",
                "add",
                self.name,
                "type",
                "veth",
                "peer",
                "name",
                self.peer,
            )
            peer_ref = f"link:{_ns_arg(self.netns)}|{self.peer}"
            nodes = (
                RawNodeTemplate(
                    id=self.ref,
                    kind="link",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "ifname": self.name,
                        "ifindex": 0,
                        "linkKind": "veth",
                        "peerRefs": [peer_ref],
                        "adminUp": False,
                    },
                ),
                RawNodeTemplate(
                    id=peer_ref,
                    kind="link",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "ifname": self.peer,
                        "ifindex": 0,
                        "linkKind": "veth",
                        "peerRefs": [self.ref],
                        "adminUp": False,
                    },
                ),
            )
        else:
            create_argv = ("ip", "link", "add", self.name, "type", self.kind)
            nodes = (
                RawNodeTemplate(
                    id=self.ref,
                    kind="link",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "ifname": self.name,
                        "ifindex": 0,
                        "linkKind": "dummy" if self.kind == "dummy" else "other",
                        "adminUp": False,
                        **({"mac": self.mac} if self.mac else {}),
                    },
                ),
            )
        create = [Command(f"{self.host}:if {self.name} (create)", _prefix(self.netns, create_argv))]
        if self.mac:
            create.append(
                Command(
                    f"{self.host}:if {self.name} (mac)",
                    _prefix(self.netns, ("ip", "link", "set", self.name, "address", self.mac)),
                )
            )
        return Primitive(
            key=self.ref,
            nodes=nodes,
            create=tuple(create),
            delete=(
                Command(
                    f"{self.host}:if {self.name} (delete)",
                    _prefix(self.netns, ("ip", "link", "delete", self.name)),
                ),
            ),
            exists=Command(
                f"{self.host}:if {self.name} (exists)",
                _prefix(self.netns, ("ip", "link", "show", self.name)),
            ),
            probe=_link_probe,
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)
