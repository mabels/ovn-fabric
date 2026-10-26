from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import Any, cast

from ladops import ovn as ovn_ops

from .lifecycle import (
    Command,
    LifecycleController,
    Primitive,
    RawNodeTemplate,
)


def _first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else ""


def _uuid_probe(table: str, command: Command) -> Any:
    async def probe(lc: LifecycleController) -> Mapping[str, Any] | None:
        value = _first_line(await lc.capture_cmd(command.label, command.argv, False))
        if not value:
            return None
        return {"provider": "ovn", "table": table, "uuid": value}

    return probe


def _find_probe(table: str, filters: Sequence[tuple[str, object]]) -> Any:
    argv = tuple(
        [
            "ovn-nbctl",
            "--bare",
            "--columns=_uuid",
            "find",
            table,
            *[f"{key}={value}" for key, value in filters],
        ]
    )
    command = Command(f"{table} (uuid)", argv)

    async def probe(lc: LifecycleController) -> Mapping[str, Any] | None:
        value = _first_line(await lc.capture_cmd(command.label, command.argv, False))
        if not value:
            return None
        return {"provider": "ovn", "table": table, "uuid": value}

    return probe


@dataclasses.dataclass(frozen=True)
class OVNNbCtlLs:
    """`ovn-nbctl ls-add` + the raw ovn.ls node."""

    name: str

    @property
    def key(self) -> str:
        return f"ovn.ls:{self.name}"

    def as_primitive(self) -> Primitive:
        exists = Command(
            f"ls:{self.name} (exists)",
            ("ovn-nbctl", "get", "Logical_Switch", self.name, "_uuid"),
        )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.ls",
                    fields={
                        "name": self.name,
                        "portRefs": [],
                        "externalIds": {},
                    },
                ),
            ),
            create=(Command(f"ls:{self.name} (create)", tuple(ovn_ops.ls_add_argv(self.name))),),
            delete=(Command(f"ls:{self.name} (delete)", tuple(ovn_ops.ls_del_argv(self.name))),),
            exists=exists,
            probe=_uuid_probe("Logical_Switch", exists),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlLsp:
    """`ovn-nbctl lsp-add` plus its type/addresses/options setters."""

    switch: str
    name: str
    type: str | None = None
    addresses: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"ovn.lsp:{self.switch}|{self.name}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(
                f"lsp:{self.switch}/{self.name} (create)",
                ("ovn-nbctl", "lsp-add", self.switch, self.name),
            )
        ]
        if self.type:
            create.append(
                Command(
                    f"lsp:{self.name} (type)",
                    ("ovn-nbctl", "lsp-set-type", self.name, self.type),
                )
            )
        if self.addresses:
            create.append(
                Command(
                    f"lsp:{self.name} (addresses)",
                    (
                        "ovn-nbctl",
                        "lsp-set-addresses",
                        self.name,
                        ",".join(self.addresses),
                    ),
                )
            )
        for key, value in sorted(self.options):
            create.append(
                Command(
                    f"lsp:{self.name} (option {key})",
                    ("ovn-nbctl", "lsp-set-options", self.name, f"{key}={value}"),
                )
            )
        exists = Command(
            f"lsp:{self.name} (exists)",
            ("ovn-nbctl", "get", "Logical_Switch_Port", self.name, "_uuid"),
        )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.lsp",
                    fields={
                        "switchRef": f"ovn.ls:{self.switch}",
                        "name": self.name,
                        "type": self.type or "",
                        "addresses": list(self.addresses),
                        "options": dict(self.options),
                        "externalIds": {},
                    },
                ),
            ),
            create=tuple(create),
            delete=(
                Command(
                    f"lsp:{self.name} (delete)",
                    tuple(ovn_ops.lsp_del_argv(self.name)),
                ),
            ),
            exists=exists,
            probe=_uuid_probe("Logical_Switch_Port", exists),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlLr:
    """`ovn-nbctl lr-add`, its options, and its external_ids."""

    name: str
    options: tuple[tuple[str, str], ...] = ()
    external_ids: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"ovn.lr:{self.name}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(f"lr:{self.name} (create)", tuple(ovn_ops.lr_add_argv(self.name)))
        ]
        for key, value in sorted(self.options):
            create.append(
                Command(
                    f"lr:{self.name} (option {key})",
                    tuple(ovn_ops.lr_set_option_argv(self.name, key, value)),
                )
            )
        for key, value in sorted(self.external_ids):
            create.append(
                Command(
                    f"lr:{self.name} (external id {key})",
                    tuple(ovn_ops.set_lr_external_id_argv(self.name, key, value)),
                )
            )
        exists = Command(
            f"lr:{self.name} (exists)",
            ("ovn-nbctl", "get", "Logical_Router", self.name, "_uuid"),
        )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.lr",
                    fields={
                        "name": self.name,
                        "options": dict(self.options),
                        "portRefs": [],
                        "routeRefs": [],
                        "natRefs": [],
                        "aclRefs": [],
                        "externalIds": dict(self.external_ids),
                    },
                ),
            ),
            create=tuple(create),
            delete=(Command(f"lr:{self.name} (delete)", tuple(ovn_ops.lr_del_argv(self.name))),),
            exists=exists,
            probe=_uuid_probe("Logical_Router", exists),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)

    # -- children scoped to this router ---------------------------------

    def add_port(
        self,
        name: str,
        mac: str,
        networks: Sequence[str] = (),
        *,
        gateway_chassis: Any = None,
        ipv6_ra_configs: Sequence[tuple[str, str]] = (),
    ) -> "OVNNbCtlLrp":
        return OVNNbCtlLrp(
            router=self.name,
            name=name,
            mac=mac,
            networks=tuple(networks),
            gateway_chassis=gateway_chassis,
            ipv6_ra_configs=tuple(ipv6_ra_configs),
        )

    def add_route(self, prefix: str, nexthop: str) -> "OVNNbCtlRoute":
        return OVNNbCtlRoute(self.name, prefix, nexthop)

    def add_nat(
        self, nat_type: str, external_ip: str, logical_ip: str, *, reset: bool = False
    ) -> "OVNNbCtlNat":
        return OVNNbCtlNat(
            self.name,
            nat_type,
            external_ip,
            logical_ip,
            reset=reset,
        )

    def add_acl(
        self,
        switch: str,
        direction: str,
        priority: int,
        match: str,
        action: str,
    ) -> "OVNNbCtlAcl":
        return OVNNbCtlAcl(switch, direction, priority, match, action)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlLrp:
    """`ovn-nbctl lrp-add`, gateway pinning, and ipv6_ra_configs."""

    router: str
    name: str
    mac: str
    networks: tuple[str, ...] = ()
    gateway_chassis: Any = None
    ipv6_ra_configs: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"ovn.lrp:{self.router}|{self.name}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(
                f"lrp:{self.name} (create)",
                tuple(ovn_ops.lrp_add_argv(self.router, self.name, self.mac, list(self.networks))),
            )
        ]
        if self.gateway_chassis is not None:
            create.append(
                Command(
                    f"lrp:{self.name} (gateway chassis)",
                    cast(
                        tuple[str, ...],
                        tuple(
                            ovn_ops.lrp_set_gateway_chassis_argv(self.name, self.gateway_chassis)
                        ),
                    ),
                )
            )
            create.append(
                Command(
                    f"lrp:{self.name} (redirect chassis)",
                    tuple(ovn_ops.lrp_set_redirect_chassis_argv(self.name)),
                )
            )
        for key, value in sorted(self.ipv6_ra_configs):
            create.append(
                Command(
                    f"lrp:{self.name} (ipv6_ra {key})",
                    tuple(ovn_ops.lrp_set_ipv6_ra_config_argv(self.name, key, value)),
                )
            )
        exists = Command(
            f"lrp:{self.name} (exists)",
            ("ovn-nbctl", "get", "Logical_Router_Port", self.name, "_uuid"),
        )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.lrp",
                    fields={
                        "routerRef": f"ovn.lr:{self.router}",
                        "name": self.name,
                        "mac": self.mac,
                        "networks": list(self.networks),
                        "gatewayChassis": (
                            [self.gateway_chassis] if self.gateway_chassis is not None else []
                        ),
                        "ipv6RaConfigs": dict(self.ipv6_ra_configs),
                        "externalIds": {},
                    },
                ),
            ),
            create=tuple(create),
            delete=(Command(f"lrp:{self.name} (delete)", tuple(ovn_ops.lrp_del_argv(self.name))),),
            exists=exists,
            probe=_uuid_probe("Logical_Router_Port", exists),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)

    def bind_switch(self, switch: str, lsp: str) -> "OVNNbCtlLsp":
        """The router-type LSP that binds this port to `switch`."""
        return OVNNbCtlLsp(
            switch=switch,
            name=lsp,
            type="router",
            addresses=("router",),
            options=(("router-port", self.name),),
        )


@dataclasses.dataclass(frozen=True)
class OVNNbCtlRoute:
    """A static route on a logical router."""

    router: str
    prefix: str
    nexthop: str

    @property
    def key(self) -> str:
        return f"ovn.route:{self.router}|{self.prefix}|{self.nexthop}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.route",
                    fields={
                        "routerRef": f"ovn.lr:{self.router}",
                        "prefix": self.prefix,
                        "nexthop": self.nexthop,
                        "externalIds": {},
                    },
                ),
            ),
            create=(
                Command(
                    f"route:{self.prefix} (create)",
                    tuple(ovn_ops.lr_route_add_argv(self.router, self.prefix, self.nexthop)),
                ),
            ),
            # lr-del cascades the router's own static routes.
            delete=(),
            probe=_find_probe(
                "Logical_Router_Static_Route",
                (("ip_prefix", self.prefix), ("nexthop", self.nexthop)),
            ),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlNat:
    """One SNAT/DNAT mapping. `reset` replaces the whole external IP
    first, matching the create-only emitter (lr-nat-add has no
    --may-exist)."""

    router: str
    type: str
    external_ip: str
    logical_ip: str
    reset: bool = False

    @property
    def key(self) -> str:
        return f"ovn.nat:{self.router}|{self.type}|{self.external_ip}|{self.logical_ip}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = []
        if self.reset:
            create.append(
                Command(
                    f"nat:{self.router}/{self.external_ip} (reset)",
                    tuple(ovn_ops.lr_nat_del_argv(self.router, self.type, self.external_ip)),
                )
            )
        create.append(
            Command(
                f"nat:{self.router}/{self.external_ip} (create)",
                tuple(
                    ovn_ops.lr_nat_add_argv(
                        self.router, self.type, self.external_ip, self.logical_ip
                    )
                ),
            )
        )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.nat",
                    fields={
                        "routerRef": f"ovn.lr:{self.router}",
                        "type": self.type,
                        "externalIp": self.external_ip,
                        "logicalIp": self.logical_ip,
                        "externalMac": None,
                        "logicalPort": None,
                        "externalIds": {},
                    },
                ),
            ),
            create=tuple(create),
            delete=(
                Command(
                    f"nat:{self.router}/{self.external_ip} (delete)",
                    tuple(ovn_ops.lr_nat_del_argv(self.router, self.type, self.external_ip)),
                ),
            ),
            probe=_find_probe(
                "NAT",
                (
                    ("type", self.type),
                    ("external_ip", self.external_ip),
                    ("logical_ip", self.logical_ip),
                ),
            ),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlAcl:
    """One ACL on a logical switch."""

    switch: str
    direction: str
    priority: int
    match: str
    action: str

    @property
    def key(self) -> str:
        return f"ovn.acl:{self.switch}|{self.direction}|{self.priority}|{self.match}|{self.action}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.acl",
                    fields={
                        "switchRef": f"ovn.ls:{self.switch}",
                        "direction": self.direction,
                        "priority": self.priority,
                        "match": self.match,
                        "action": self.action,
                        "externalIds": {},
                    },
                ),
            ),
            create=(
                Command(
                    f"acl:{self.switch}/{self.direction} (create)",
                    tuple(
                        ovn_ops.acl_add_argv(
                            self.switch,
                            self.direction,
                            self.priority,
                            self.match,
                            self.action,
                        )
                    ),
                ),
            ),
            delete=(
                Command(
                    f"acl:{self.switch}/{self.direction} (delete)",
                    tuple(
                        ovn_ops.acl_del_argv(self.switch, self.direction, self.priority, self.match)
                    ),
                ),
            ),
            probe=_find_probe(
                "ACL",
                (
                    ("direction", self.direction),
                    ("priority", self.priority),
                    ("match", self.match),
                    ("action", self.action),
                ),
            ),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)
