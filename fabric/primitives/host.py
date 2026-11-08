from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from ..lifecycle import (
    Command,
    CommandPrimitive,
    LifecycleController,
    Primitive,
    RawNodeTemplate,
)

if TYPE_CHECKING:
    from .docker import DockerImage, DockerPrimitive, DockerPull

from .fs import ChmodPrimitive, DirectoryPrimitive, RemovePathPrimitive
from .net import (
    AddrPrimitive,
    DefaultRule,
    DhclientPrimitive,
    FilterRule,
    IpRulePrimitive,
    LinkMovePrimitive,
    MasqRule,
    NetnsLinkPrimitive,
    RoutePrimitive,
    StatefulAccept,
    SysctlFilePrimitive,
    SysctlPrimitive,
    WireguardPrimitive,
)
from .ovn import (
    OVNNbCtlAcl,
    OVNNbCtlConnection,
    OVNNbCtlLr,
    OVNNbCtlLrp,
    OVNNbCtlLs,
    OVNNbCtlLsp,
    OVNNbCtlNat,
    OVNNbCtlRoute,
    OVNSbCtlConnection,
)
from .ovs import (
    OVSAfxdpPort,
    OVSBridge,
    OVSExternalId,
    OVSIpfix,
    OVSMapping,
    OVSPort,
)

GLOBAL_NETNS = "*global*"


def _ns_arg(netns: str | None) -> str:
    return GLOBAL_NETNS if netns is None else netns


def _prefix(netns: str | None, argv: Sequence[str]) -> tuple[str, ...]:
    if netns is None:
        return tuple(argv)
    return ("ip", "netns", "exec", netns, *argv)


@dataclasses.dataclass(frozen=True)
class SystemdEnabled(CommandPrimitive):
    """A systemd unit started at boot: enabled, not started now. Its file is
    written by its own step."""

    host: str
    unit: str
    # It leaves nothing in RawIR, by nature.
    no_rawir: ClassVar[bool] = True

    @property
    def key(self) -> str:
        return f"systemd:{self.unit}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(),
            create_cmds=(
                Command(f"{self.unit} (reload)", ("systemctl", "daemon-reload")),
                Command(f"{self.unit} (enable)", ("systemctl", "enable", "--quiet", self.unit)),
            ),
            delete_cmds=(
                Command(
                    f"{self.unit} (disable)",
                    ("systemctl", "disable", "--quiet", self.unit),
                    optional=True,
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class HostPrimitive(CommandPrimitive):
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
            create_cmds=(),
            delete_cmds=(),
        )

    def netns(self, name: str | None, *, owned: bool = True) -> "NetnsPrimitive":
        return NetnsPrimitive(host=self.name, name=name, owned=owned)

    def container_netns(self, name: str) -> "NetnsPrimitive":
        """A netns owned by Docker (or another supervisor): journal it,
        but never create/delete it with `ip netns`."""
        return self.netns(name, owned=False)

    def rootNs(self) -> "NetnsPrimitive":
        """The root namespace (`host.netns(None)`), named so generated
        code binds it once and reads `root.add_port(...)`."""
        return self.netns(None)

    @property
    def kernel(self) -> "NetnsPrimitive":
        return self.rootNs()

    # -- containers -----------------------------------------------------

    def docker(
        self,
        name: str,
        image: str,
        *,
        command: Sequence[str] = (),
        privileged: bool = False,
        network: str = "none",
        environment: Sequence[tuple[str, str]] = (),
        volumes: Sequence[tuple[str, str]] = (),
        run_flags: Sequence[str] = (),
        remove_existing: bool = True,
        files: str = "",
    ) -> "DockerPrimitive":
        from .docker import DockerPrimitive

        return DockerPrimitive(
            host=self.name,
            name=name,
            image=image,
            command=tuple(command),
            privileged=privileged,
            network=network,
            environment=tuple(environment),
            volumes=tuple(volumes),
            run_flags=tuple(run_flags),
            remove_existing=remove_existing,
            files=files,
        )

    def docker_image(
        self, image: str, dockerfile: str, context: str, spec: str = ""
    ) -> "DockerImage":
        from .docker import DockerImage

        return DockerImage(self.name, image, dockerfile, context, spec)

    def docker_pull(self, image: str) -> "DockerPull":
        from .docker import DockerPull

        return DockerPull(self.name, image)

    def systemd_enabled(self, unit: str) -> SystemdEnabled:
        return SystemdEnabled(self.name, unit)

    # -- OVS / host bindings --------------------------------------------

    def make_dir(self, path: str) -> DirectoryPrimitive:
        return DirectoryPrimitive(self.name, path)

    def state_dir(self, path: str) -> DirectoryPrimitive:
        """A directory this deployer owns with its content (see
        DirectoryPrimitive)."""
        return DirectoryPrimitive(self.name, path, owned=True)

    def chmod(self, path: str, mode: str) -> ChmodPrimitive:
        return ChmodPrimitive(self.name, path, mode)

    def remove_file(self, path: str) -> RemovePathPrimitive:
        return RemovePathPrimitive(self.name, path)

    def remove_tree(self, path: str) -> RemovePathPrimitive:
        return RemovePathPrimitive(self.name, path, recursive=True)

    def ovs_bridge(
        self,
        name: str,
        *,
        datapath: str | None = None,
        fail_mode: str = "standalone",
    ) -> OVSBridge:
        return OVSBridge(name, datapath=datapath, fail_mode=fail_mode)

    def ovs_port(
        self,
        bridge: str,
        port: str,
        *,
        tag: int | None = None,
        trunks: Sequence[int] = (),
    ) -> OVSPort:
        return OVSPort(bridge, port, tag=tag, trunks=tuple(trunks))

    def ovs_afxdp_port(
        self,
        bridge: str,
        port: str,
        mode: str,
        n_rxq: int,
        *,
        trunks: Sequence[int] = (),
    ) -> OVSAfxdpPort:
        return OVSAfxdpPort(bridge, port, mode, n_rxq, trunks=tuple(trunks))

    def ovs_mapping(self, mappings: Sequence[str]) -> OVSMapping:
        return OVSMapping(tuple(mappings))

    def ovs_external_id(self, key: str, value: str) -> OVSExternalId:
        return OVSExternalId(key, value)

    def ovs_ipfix(self, bridge: str, script: str) -> OVSIpfix:
        return OVSIpfix(bridge, script)

    def ovn_nb_connection(self) -> OVNNbCtlConnection:
        return OVNNbCtlConnection()

    def ovn_sb_connection(self) -> OVNSbCtlConnection:
        return OVNSbCtlConnection()

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
class NetnsPrimitive(CommandPrimitive):
    host: str
    name: str | None
    owned: bool = True

    @property
    def ref(self) -> str:
        return f"netns:{_ns_arg(self.name)}"

    def as_primitive(self) -> Primitive:
        if self.name is None or not self.owned:
            create: tuple[Command, ...] = ()
            delete: tuple[Command, ...] = ()
        else:
            create = (
                Command(
                    f"{self.host}:netns {self.name} (create)",
                    ("ip", "netns", "add", self.name),
                ),
                Command(
                    f"{self.host}:netns {self.name} (lo up)",
                    ("ip", "netns", "exec", self.name, "ip", "link", "set", "lo", "up"),
                ),
                Command(
                    f"{self.host}:netns {self.name} (ipv4 forwarding)",
                    (
                        "ip",
                        "netns",
                        "exec",
                        self.name,
                        "sysctl",
                        "-w",
                        "net.ipv4.ip_forward=1",
                    ),
                ),
                Command(
                    f"{self.host}:netns {self.name} (ipv6 forwarding)",
                    (
                        "ip",
                        "netns",
                        "exec",
                        self.name,
                        "sysctl",
                        "-w",
                        "net.ipv6.conf.all.forwarding=1",
                    ),
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
                        "scope": (
                            "global"
                            if self.name is None
                            else ("container" if not self.owned else "isolated")
                        ),
                        "name": _ns_arg(self.name),
                        "linkRefs": [],
                        "routeRefs": [],
                        "ruleRefs": [],
                    },
                ),
            ),
            create_cmds=create,
            delete_cmds=delete,
        )

    def add_if(
        self,
        name: str,
        *,
        kind: str = "dummy",
        peer: str | None = None,
        mac: str | None = None,
        up: bool = False,
    ) -> "LinkPrimitive":
        return LinkPrimitive(
            host=self.host,
            netns=self.name,
            name=name,
            kind=kind,
            peer=peer,
            mac=mac,
            up=up,
        )

    def add_vlan(self, parent: str, name: str, vlan_id: int) -> "VlanPrimitive":
        return VlanPrimitive(
            host=self.host,
            netns=self.name,
            parent=parent,
            name=name,
            vlan_id=vlan_id,
        )

    # -- kernel-router children -----------------------------------------

    def add_link(
        self,
        name: str,
        kind: str,
        *,
        rename_to: str | None = None,
        parent: str | None = None,
        vlan_id: int | None = None,
        mac: str | None = None,
        root_leg: str | None = None,
        bridge: str | None = None,
    ) -> NetnsLinkPrimitive:
        if self.name is None:
            raise ValueError("a kernel link needs a named netns")
        return NetnsLinkPrimitive(
            host=self.host,
            netns=self.name,
            name=name,
            kind=kind,
            rename_to=rename_to,
            parent=parent,
            vlan_id=vlan_id,
            mac=mac,
            root_leg=root_leg,
            bridge=bridge,
        )

    def add_addr(self, link: str, addr: str) -> AddrPrimitive:
        return AddrPrimitive(host=self.host, netns=self.name, link=link, addr=addr)

    def add_route(
        self,
        dst: str,
        dev: str,
        via: str | None = None,
        metric: float | None = None,
        table: int | None = None,
        family: int | None = None,
    ) -> RoutePrimitive:
        return RoutePrimitive(
            host=self.host,
            netns=self.name,
            dst=dst,
            dev=dev,
            via=via,
            # A metric is a whole number to the kernel; the IR's JSON may say 400.0.
            metric=int(metric) if metric is not None else None,
            table=table,
            family=family,
        )

    def set_sysctl_file(self, path: str, value: str) -> SysctlFilePrimitive:
        if self.name is None:
            raise ValueError("a sysctl file needs a named netns")
        return SysctlFilePrimitive(self.host, self.name, path, value)

    def dhclient(self, dev: str, lease: str) -> DhclientPrimitive:
        if self.name is None:
            raise ValueError("dhclient needs a named netns")
        return DhclientPrimitive(self.host, self.name, dev, lease)

    def wireguard(self, iface: str, conf_path: str, conf: str = "") -> WireguardPrimitive:
        if self.name is None:
            raise ValueError("wireguard needs a named netns")
        return WireguardPrimitive(self.host, self.name, iface, conf_path, conf)

    def add_ip_rule(self, source: str, table: int, family: int = 4) -> IpRulePrimitive:
        return IpRulePrimitive(
            host=self.host, netns=self.name, source=source, table=table, family=family
        )

    def move_link(self, name: str, to: NetnsPrimitive) -> LinkMovePrimitive:
        """Lend this namespace's link `name` to `to` (moved back on delete)."""
        if self.name is None or to.name is None:
            raise ValueError("moving a link needs two named namespaces")
        return LinkMovePrimitive(host=self.host, netns=self.name, name=name, to=to.name)

    def set_sysctl(self, key: str, value: str) -> SysctlPrimitive:
        if self.name is None:
            raise ValueError("a sysctl needs a named netns")
        return SysctlPrimitive(host=self.host, netns=self.name, key=key, value=value)

    def _rule_netns(self) -> str:
        if self.name is None:
            raise ValueError("an iptables rule needs a named netns")
        return self.name

    def add_filter(
        self,
        family: str,
        direction: str,
        action: str,
        *,
        sources: Sequence[str] = (),
        exclude_sources: Sequence[str] = (),
        destinations: Sequence[str] = (),
        protocol: str | None = None,
        ports: Sequence[tuple[int, int]] = (),
    ) -> FilterRule:
        """A security group's filter rule (the IR's: direction, action, …)."""
        return FilterRule(
            self.host,
            self._rule_netns(),
            family,
            direction,
            action,
            sources=tuple(sources),
            exclude_sources=tuple(exclude_sources),
            destinations=tuple(destinations),
            protocol=protocol,
            ports=tuple(ports),
        )

    def add_masq(self, family: str, out_dev: str) -> MasqRule:
        """A security group's masq rule, out through `out_dev`."""
        return MasqRule(self.host, self._rule_netns(), family, out_dev)

    def add_default(self, direction: str, action: str) -> DefaultRule:
        """A security group's default for `direction`, both families."""
        return DefaultRule(self.host, self._rule_netns(), direction, action)

    def add_stateful_accept(self, family: str, direction: str) -> StatefulAccept:
        """The replies of accepted connections, in `direction`."""
        return StatefulAccept(self.host, self._rule_netns(), family, direction)


async def _link_probe(lc: LifecycleController) -> dict[str, Any] | None:
    return None


@dataclasses.dataclass(frozen=True)
class LinkPrimitive(CommandPrimitive):
    host: str
    netns: str | None
    name: str
    kind: str = "dummy"
    peer: str | None = None
    mac: str | None = None
    up: bool = False

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
                        "adminUp": self.up,
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
                        "adminUp": self.up,
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
                        "adminUp": self.up,
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
        if self.up:
            create.append(
                Command(
                    f"{self.host}:if {self.name} (up)",
                    _prefix(self.netns, ("ip", "link", "set", self.name, "up")),
                )
            )
            if self.kind == "veth" and self.peer is not None:
                create.append(
                    Command(
                        f"{self.host}:if {self.peer} (up)",
                        _prefix(
                            self.netns,
                            ("ip", "link", "set", self.peer, "up"),
                        ),
                    )
                )
        return Primitive(
            key=self.ref,
            nodes=nodes,
            create_cmds=tuple(create),
            delete_cmds=(
                Command(
                    f"{self.host}:if {self.name} (delete)",
                    _prefix(self.netns, ("ip", "link", "delete", self.name)),
                ),
            ),
            probe=_link_probe,
        )


@dataclasses.dataclass(frozen=True)
class VlanPrimitive(CommandPrimitive):
    """A kernel VLAN sub-interface in the host's netns (or a named one)."""

    host: str
    netns: str | None
    parent: str
    name: str
    vlan_id: int

    @property
    def ref(self) -> str:
        return f"link:{_ns_arg(self.netns)}|{self.name}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="link",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "ifname": self.name,
                        "ifindex": 0,
                        "linkKind": "vlan",
                        "adminUp": True,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:vlan {self.name} (create)",
                    _prefix(
                        self.netns,
                        (
                            "ip",
                            "link",
                            "add",
                            "link",
                            self.parent,
                            "name",
                            self.name,
                            "type",
                            "vlan",
                            "id",
                            str(self.vlan_id),
                        ),
                    ),
                ),
                Command(
                    f"{self.host}:vlan {self.name} (up)",
                    _prefix(self.netns, ("ip", "link", "set", self.name, "up")),
                ),
            ),
            delete_cmds=(
                Command(
                    f"{self.host}:vlan {self.name} (delete)",
                    _prefix(self.netns, ("ip", "link", "delete", self.name)),
                ),
            ),
        )
