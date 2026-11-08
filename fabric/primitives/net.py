from __future__ import annotations

import dataclasses
import ipaddress
import shlex
from collections.abc import Sequence
from typing import ClassVar

from ladops import linux_net as linux_net_ops
from ladops import netns as netns_ops
from ladops import ovs as ovs_ops

from ..lifecycle import (
    Command,
    CommandPrimitive,
    Primitive,
    RawNodeTemplate,
)

GLOBAL_NETNS = "*global*"


def _ns_arg(netns: str | None) -> str:
    return GLOBAL_NETNS if netns is None else netns


def _prefix(netns: str | None, argv: Sequence[str]) -> tuple[str, ...]:
    return tuple(netns_ops.netns_exec_argv(list(argv), netns))


@dataclasses.dataclass(frozen=True)
class AddrPrimitive(CommandPrimitive):
    """One address on one link inside a netns (or the root namespace)."""

    host: str
    netns: str | None
    link: str
    addr: str

    @property
    def key(self) -> str:
        return f"addr:{_ns_arg(self.netns)}|{self.link}|{self.addr}"

    def as_primitive(self) -> Primitive:
        interface = ipaddress.ip_interface(self.addr)
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="addr",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "linkRef": f"link:{_ns_arg(self.netns)}|{self.link}",
                        "family": "ipv6" if interface.version == 6 else "ipv4",
                        "address": str(interface.ip),
                        "prefixlen": interface.network.prefixlen,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:addr {self.addr} ({self.link})",
                    _prefix(
                        self.netns,
                        linux_net_ops.add_addr_argv(self.addr, self.link),
                    ),
                ),
            ),
            delete_cmds=(
                Command(
                    f"{self.host}:addr {self.addr} (delete)",
                    _prefix(
                        self.netns,
                        ("ip", "addr", "del", self.addr, "dev", self.link),
                    ),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class RoutePrimitive(CommandPrimitive):
    """One static route in a netns."""

    host: str
    netns: str | None
    dst: str
    dev: str
    via: str | None = None
    metric: float | None = None
    table: int | None = None
    family: int | None = None

    @property
    def key(self) -> str:
        via = self.via or ""
        metric = "" if self.metric is None else f"{self.metric:g}"
        table = "main" if self.table is None else str(self.table)
        family = "" if self.family is None else f"ipv{self.family}"
        return (
            f"route:{_ns_arg(self.netns)}|{family or 'any'}|{table}|{self.dst}|"
            f"{self.dev}|{via}|{metric}"
        )

    def _argv(self, verb: str) -> tuple[str, ...]:
        if self.table is None and self.family is None:
            base = (
                linux_net_ops.add_route_argv(self.dst, self.dev, self.via, self.metric)
                if verb == "add"
                else linux_net_ops.delete_route_argv(self.dst, self.dev)
            )
            return _prefix(self.netns, base)
        argv = ["ip"]
        if self.family is not None:
            argv.append(f"-{self.family}")
        argv += ["route", verb, self.dst]
        if self.via is not None:
            argv += ["via", self.via]
        else:
            argv += ["dev", self.dev]
        if self.metric is not None:
            argv += ["metric", f"{self.metric:g}"]
        if self.table is not None:
            argv += ["table", str(self.table)]
        return _prefix(self.netns, argv)

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="route",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "prefix": self.dst,
                        "dev": self.dev,
                        "nexthop": self.via or "",
                        "metric": self.metric,
                        "table": self.table,
                        "family": self.family,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:route {self.dst} (create)",
                    self._argv("add"),
                ),
            ),
            delete_cmds=(
                Command(
                    f"{self.host}:route {self.dst} (delete)",
                    self._argv("del"),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class LinkUpPrimitive(CommandPrimitive):
    """Waits for a device an app makes itself, and brings it up. With the
    host's `ip`, in the netns: the app's image need not have it. Goes with
    the app, so DELETE does nothing."""

    host: str
    netns: str
    dev: str
    seconds: int = 60

    @property
    def key(self) -> str:
        return f"link.up:{self.netns}|{self.dev}"

    def as_primitive(self) -> Primitive:
        dev = shlex.quote(self.dev)
        wait = (
            f"for i in $(seq 1 {self.seconds}); do"
            f" ip link set dev {dev} up 2>/dev/null && exit 0; sleep 1;"
            f" done; echo {dev}: no such device after {self.seconds}s >&2; exit 1"
        )
        return Primitive(
            key=self.key,
            nodes=(),
            create_cmds=(
                Command(
                    f"{self.host}:wait for {self.dev} in {self.netns}",
                    _prefix(self.netns, ("sh", "-c", wait)),
                    timeout=self.seconds + 30,
                ),
            ),
            delete_cmds=(),
        )


@dataclasses.dataclass(frozen=True)
class SysctlPrimitive(CommandPrimitive):
    """One netns-local sysctl value."""

    host: str
    netns: str
    key: str
    value: str

    @property
    def ref(self) -> str:
        return f"sysctl:{self.netns}|{self.key}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="sysctl",
                    fields={
                        "netnsRef": f"netns:{self.netns}",
                        "key": self.key,
                        "value": self.value,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:sysctl {self.key}",
                    _prefix(
                        self.netns,
                        ("sysctl", "-w", f"{self.key}={self.value}"),
                    ),
                ),
            ),
            delete_cmds=(),
        )


@dataclasses.dataclass(frozen=True)
class WireguardPrimitive(CommandPrimitive):
    """A WireGuard tunnel inside a netns (`wg-quick up/down`)."""

    host: str
    netns: str
    iface: str
    conf_path: str
    # The config it is brought up with (written to conf_path by its unit):
    # what its decider compares the file on the host with.
    conf: str = ""

    @property
    def ref(self) -> str:
        return f"wireguard:{self.netns}|{self.iface}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="wireguard",
                    fields={
                        "netnsRef": f"netns:{self.netns}",
                        "iface": self.iface,
                        "confPath": self.conf_path,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:wireguard {self.iface} (up)",
                    _prefix(
                        self.netns,
                        ("wg-quick", "up", self.conf_path),
                    ),
                ),
            ),
            delete_cmds=(
                Command(
                    f"{self.host}:wireguard {self.iface} (down)",
                    _prefix(
                        self.netns,
                        ("wg-quick", "down", self.conf_path),
                    ),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class SysctlFilePrimitive(CommandPrimitive):
    """Write one /proc/sys file inside a netns. Needed for iface names
    containing dots, which `sysctl` cannot address."""

    # Set on a link when the link comes: it has no RawIR of its own, and the
    # link may have moved on (lent to a container), so it follows its unit.
    follows_unit: ClassVar[bool] = True

    host: str
    netns: str
    path: str
    value: str

    @property
    def ref(self) -> str:
        return f"sysctl.file:{self.netns}|{self.path}"

    def as_primitive(self) -> Primitive:
        script = f"echo {shlex.quote(self.value)} > {shlex.quote(self.path)}"
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="sysctl",
                    fields={
                        "netnsRef": f"netns:{self.netns}",
                        "path": self.path,
                        "value": self.value,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:sysctl-file {self.path}",
                    _prefix(self.netns, ("sh", "-c", script)),
                ),
            ),
            delete_cmds=(),
        )


@dataclasses.dataclass(frozen=True)
class DhclientPrimitive(CommandPrimitive):
    """A dhclient lease on `dev` inside a netns, with its own lease file.
    CREATE runs dhclient as a foreground daemon (the job's child); DELETE
    releases the lease explicitly — SIGTERM through `ip netns exec` is
    not a reliable dhclient shutdown."""

    host: str
    netns: str
    dev: str
    lease: str

    @property
    def ref(self) -> str:
        return f"dhclient:{self.netns}|{self.dev}"

    def as_primitive(self) -> Primitive:
        dhclient = ("/usr/sbin/dhclient", "-lf", self.lease)
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="dhclient",
                    fields={
                        "netnsRef": f"netns:{self.netns}",
                        "linkRef": f"link:{self.netns}|{self.dev}",
                        "lease": self.lease,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:dhclient {self.dev}",
                    _prefix(self.netns, (*dhclient, "-d", self.dev)),
                ),
            ),
            delete_cmds=(
                Command(
                    f"{self.host}:dhclient {self.dev} (release)",
                    _prefix(self.netns, (*dhclient, "-r", self.dev)),
                ),
            ),
        )


# A security group's vocabulary (the IR's) -> iptables'.
_CHAIN = {"input": "INPUT", "output": "OUTPUT", "forward": "FORWARD"}
_TARGET = {"allow": "ACCEPT", "deny": "DROP", "reject": "REJECT"}
_BINARY = {"ipv4": "iptables", "ipv6": "ip6tables"}

# One iptables line: (binary, table, chain, args).
Line = tuple[str, str, str, tuple[str, ...]]


@dataclasses.dataclass(frozen=True)
class FirewallRule(CommandPrimitive):
    """Base of the security-group rules inside a netns. Each one is a rule
    of the IR's security group (`security.group`) — with its field names
    and values (direction input/output/forward, action allow/deny/reject,
    lists of sources, ports …) — and becomes the iptables/ip6tables lines
    that implement it: create appends them (`-A`), delete removes them
    (`-D`, in reverse). The translation into iptables' vocabulary lives
    here, once. Not a primitive of its own."""

    host: str
    netns: str

    # The NetnsPrimitive method that builds it (the old generator renders
    # a rule back into that call).
    builder: ClassVar[str] = ""

    @property
    def key(self) -> str:
        raise NotImplementedError

    def lines(self) -> list[Line]:
        raise NotImplementedError

    def _argv(self, verb: str, line: Line) -> tuple[str, ...]:
        binary, table, chain, args = line
        return _prefix(self.netns, [binary, "-t", table, verb, chain, *args])

    def as_primitive(self) -> Primitive:
        lines = self.lines()
        return Primitive(
            key=self.key,
            nodes=tuple(
                RawNodeTemplate(
                    id=f"iptables.rule:{self.netns}|{binary}|{table}|{chain}|{' '.join(args)}",
                    kind="iptables.rule",
                    fields={
                        "netnsRef": f"netns:{self.netns}",
                        "binary": binary,
                        "table": table,
                        "chain": chain,
                        "args": list(args),
                    },
                )
                for binary, table, chain, args in lines
            ),
            create_cmds=tuple(
                Command(f"{self.host}:{line[0]} {line[2]}", self._argv("-A", line))
                for line in lines
            ),
            delete_cmds=tuple(
                Command(f"{self.host}:{line[0]} {line[2]} (delete)", self._argv("-D", line))
                for line in reversed(lines)
            ),
        )


@dataclasses.dataclass(frozen=True)
class FilterRule(FirewallRule):
    """A security group's filter rule: `action` for traffic in `direction`
    that matches — from any of `sources` (but none of `exclude_sources`),
    to any of `destinations`, with `protocol`, to any of `ports`
    ((from, to), tcp/udp only); an empty list matches anything. One line
    per source × destination × port."""

    family: str  # "ipv4" | "ipv6"
    direction: str  # "input" | "output" | "forward"
    action: str  # "allow" | "deny" | "reject"
    sources: tuple[str, ...] = ()
    exclude_sources: tuple[str, ...] = ()
    destinations: tuple[str, ...] = ()
    protocol: str | None = None
    ports: tuple[tuple[int, int], ...] = ()
    builder: ClassVar[str] = "add_filter"

    def __post_init__(self) -> None:
        if self.ports and self.protocol not in ("tcp", "udp"):
            raise ValueError(f"filter rule in {self.netns}: ports require tcp/udp")

    @property
    def key(self) -> str:
        return (
            f"firewall.filter:{self.netns}|{self.family}|{self.direction}|{self.action}|"
            f"{','.join(self.sources)}|{','.join(self.exclude_sources)}|"
            f"{','.join(self.destinations)}|{self.protocol or ''}|"
            f"{','.join(f'{a}-{b}' for a, b in self.ports)}"
        )

    def lines(self) -> list[Line]:
        out: list[Line] = []
        for source in self.sources or (None,):
            for destination in self.destinations or (None,):
                for port in self.ports or (None,):
                    args: list[str] = []
                    if source is not None:
                        args += ["-s", source]
                    for excluded in self.exclude_sources:
                        args += ["!", "-s", excluded]
                    if destination is not None:
                        args += ["-d", destination]
                    if self.protocol is not None:
                        args += ["-p", self.protocol]
                    if port is not None:
                        first, last = port
                        args += ["--dport", str(first) if first == last else f"{first}:{last}"]
                    args += ["-j", _TARGET[self.action]]
                    out.append(
                        (_BINARY[self.family], "filter", _CHAIN[self.direction], tuple(args))
                    )
        return out


@dataclasses.dataclass(frozen=True)
class MasqRule(FirewallRule):
    """A security group's masq rule, applied where it is used: masquerade
    what leaves through `out_dev`."""

    family: str
    out_dev: str
    builder: ClassVar[str] = "add_masq"

    @property
    def key(self) -> str:
        return f"firewall.masq:{self.netns}|{self.family}|{self.out_dev}"

    def lines(self) -> list[Line]:
        args = ("-o", self.out_dev, "-j", "MASQUERADE")
        return [(_BINARY[self.family], "nat", "POSTROUTING", args)]


@dataclasses.dataclass(frozen=True)
class DefaultRule(FirewallRule):
    """A security group's default for one direction: `action` for what no
    rule decided — the last line of the chain, for both families."""

    direction: str
    action: str
    builder: ClassVar[str] = "add_default"

    @property
    def key(self) -> str:
        return f"firewall.default:{self.netns}|{self.direction}|{self.action}"

    def lines(self) -> list[Line]:
        return [
            (binary, "filter", _CHAIN[self.direction], ("-j", _TARGET[self.action]))
            for binary in ("iptables", "ip6tables")
        ]


@dataclasses.dataclass(frozen=True)
class StatefulAccept(FirewallRule):
    """What makes a direction's filter rules stateful: accept what belongs
    to a connection already accepted (the replies). No rule of the IR —
    derived for every family and direction that has filter rules."""

    family: str
    direction: str
    builder: ClassVar[str] = "add_stateful_accept"

    @property
    def key(self) -> str:
        return f"firewall.stateful:{self.netns}|{self.family}|{self.direction}"

    def lines(self) -> list[Line]:
        args = ("-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
        return [(_BINARY[self.family], "filter", _CHAIN[self.direction], args)]


@dataclasses.dataclass(frozen=True)
class IpRulePrimitive(CommandPrimitive):
    """A policy-routing rule inside a netns (`ip rule add from ... lookup`)."""

    host: str
    netns: str | None
    source: str
    table: int
    family: int = 4

    @property
    def key(self) -> str:
        return f"ip.rule:{_ns_arg(self.netns)}|{self.family}|{self.source}|{self.table}"

    def _argv(self, verb: str) -> tuple[str, ...]:
        return _prefix(
            self.netns,
            [
                "ip",
                f"-{self.family}",
                "rule",
                verb,
                "from",
                self.source,
                "lookup",
                str(self.table),
            ],
        )

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ip.rule",
                    fields={
                        "netnsRef": f"netns:{_ns_arg(self.netns)}",
                        "family": self.family,
                        "source": self.source,
                        "table": self.table,
                    },
                ),
            ),
            create_cmds=(Command(f"{self.host}:ip rule {self.source}", self._argv("add")),),
            delete_cmds=(
                Command(f"{self.host}:ip rule {self.source} (delete)", self._argv("del")),
            ),
        )


@dataclasses.dataclass(frozen=True)
class NetnsLinkPrimitive(CommandPrimitive):
    """A link created in the root namespace and moved into `netns`.

    `veth` creates the pair (`root_leg` in the root namespace), optionally
    attaches the root leg to an OVS bridge, and moves the peer into the
    netns. `vlan` creates the sub-interface on `parent`; `dummy` creates
    a dummy link; `physical` only moves an already-existing device.
    """

    host: str
    netns: str
    name: str
    kind: str
    rename_to: str | None = None
    parent: str | None = None
    vlan_id: int | None = None
    mac: str | None = None
    root_leg: str | None = None
    bridge: str | None = None

    @property
    def final_name(self) -> str:
        return self.rename_to or self.name

    @property
    def ref(self) -> str:
        return f"link:{self.netns}|{self.final_name}"

    def _root_ref(self) -> str:
        return f"link:{GLOBAL_NETNS}|{self.root_leg}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = []
        nodes: list[RawNodeTemplate] = []
        if self.kind == "vlan":
            if self.parent is None or self.vlan_id is None:
                raise ValueError("vlan link needs parent= and vlan_id=")
            create.append(
                Command(
                    f"{self.host}:vlan {self.name} (create)",
                    tuple(linux_net_ops.add_vlan_argv(self.parent, self.name, self.vlan_id)),
                )
            )
        elif self.kind == "veth":
            if self.root_leg is None:
                raise ValueError("veth link needs root_leg=")
            create.append(
                Command(
                    f"{self.host}:veth {self.name} (create)",
                    tuple(linux_net_ops.add_veth_argv(self.root_leg, self.name, self.mac)),
                )
            )
            create.append(
                Command(
                    f"{self.host}:veth {self.root_leg} (up)",
                    tuple(linux_net_ops.set_link_up_argv(self.root_leg)),
                )
            )
            if self.bridge is not None:
                create.append(
                    Command(
                        f"{self.host}:veth {self.root_leg} (port)",
                        tuple(ovs_ops.add_port_argv(self.bridge, self.root_leg)),
                    )
                )
                nodes.append(
                    RawNodeTemplate(
                        id=f"ovs.port:{self.root_leg}",
                        kind="ovs.port",
                        fields={
                            "bridgeRef": f"ovs.bridge:{self.bridge}",
                            "name": self.root_leg,
                            "trunks": [],
                            "ifaceRefs": [],
                            "externalIds": {},
                        },
                    )
                )
            nodes.append(
                RawNodeTemplate(
                    id=self._root_ref(),
                    kind="link",
                    fields={
                        "netnsRef": f"netns:{GLOBAL_NETNS}",
                        "ifname": self.root_leg,
                        "ifindex": 0,
                        "linkKind": "veth",
                        "peerRefs": [self.ref],
                        "adminUp": True,
                    },
                )
            )
        elif self.kind == "dummy":
            create.append(
                Command(
                    f"{self.host}:dummy {self.name} (create)",
                    tuple(linux_net_ops.add_dummy_argv(self.name)),
                )
            )
        elif self.kind != "physical":
            raise ValueError(f"unsupported link kind {self.kind!r}")

        if self.kind != "physical":
            create.append(
                Command(
                    f"{self.host}:link {self.name} (move)",
                    tuple(linux_net_ops.add_if_to_netns_argv(self.name, self.netns)),
                )
            )
        if self.rename_to is not None:
            create.append(
                Command(
                    f"{self.host}:link {self.name} (rename)",
                    _prefix(
                        self.netns,
                        ("ip", "link", "set", self.name, "name", self.rename_to),
                    ),
                )
            )
        create.append(
            Command(
                f"{self.host}:link {self.final_name} (up)",
                _prefix(self.netns, ("ip", "link", "set", self.final_name, "up")),
            )
        )
        nodes.append(
            RawNodeTemplate(
                id=self.ref,
                kind="link",
                fields={
                    "netnsRef": f"netns:{self.netns}",
                    "ifname": self.final_name,
                    "ifindex": 0,
                    "linkKind": self.kind,
                    "peerRefs": [self._root_ref()] if self.kind == "veth" else [],
                    "adminUp": True,
                    **({"mac": self.mac} if self.mac else {}),
                },
            )
        )
        delete: list[Command] = []
        if self.kind == "veth" and self.bridge is not None and self.root_leg is not None:
            delete.append(
                Command(
                    f"{self.host}:veth {self.root_leg} (port delete)",
                    tuple(ovs_ops.del_port_argv(self.bridge, self.root_leg)),
                )
            )
        delete.append(
            Command(
                (
                    f"{self.host}:veth {self.root_leg} (delete)"
                    if self.kind == "veth"
                    else f"{self.host}:link {self.name} (delete)"
                ),
                (
                    tuple(linux_net_ops.delete_link_argv(self.root_leg or self.name))
                    if self.kind == "veth"
                    else _prefix(
                        self.netns,
                        linux_net_ops.delete_link_argv(self.name),
                    )
                ),
            )
        )
        return Primitive(
            key=self.ref,
            nodes=tuple(nodes),
            create_cmds=tuple(create),
            delete_cmds=tuple(delete),
        )


@dataclasses.dataclass(frozen=True)
class LinkMovePrimitive(CommandPrimitive):
    """An existing link lent by its namespace (`netns`, its owner's) to
    another one (`to`): CREATE moves it there and sets it up; DELETE moves
    it back and up again, as its owner had it. Neither creates nor deletes
    the link."""

    host: str
    netns: str
    name: str
    to: str

    @property
    def ref(self) -> str:
        return f"link:{self.to}|{self.name}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.ref,
            nodes=(
                RawNodeTemplate(
                    id=self.ref,
                    kind="link",
                    fields={
                        "netnsRef": f"netns:{self.to}",
                        "ifname": self.name,
                        "ifindex": 0,
                        "linkKind": "other",
                        "peerRefs": [],
                        "adminUp": True,
                    },
                ),
            ),
            create_cmds=(
                Command(
                    f"{self.host}:link {self.name} -> {self.to}",
                    _prefix(self.netns, ("ip", "link", "set", self.name, "netns", self.to)),
                ),
                Command(
                    f"{self.host}:link {self.name} (up)",
                    _prefix(self.to, ("ip", "link", "set", self.name, "up")),
                ),
            ),
            delete_cmds=(
                Command(
                    f"{self.host}:link {self.name} -> {self.netns}",
                    _prefix(self.to, ("ip", "link", "set", self.name, "netns", self.netns)),
                    optional=True,
                ),
                Command(
                    f"{self.host}:link {self.name} (up again)",
                    _prefix(self.netns, ("ip", "link", "set", self.name, "up")),
                    optional=True,
                ),
            ),
        )
