"""The sample calls — per primitive class, the only hand-written input of
the library (ADR 0008 § 3).

A sample constructs the primitive class directly from its dataclass
fields, so the learned function's parameters are exactly those fields.
Most fields get a **marker** whose type follows from the field's name
(MARKER_KIND: a name, a CIDR, a network, a plain IP, a MAC, a number). What cannot
be marked is given per sample:

- `fixed`: enum-like fields (a link's `kind`, a NAT's `type`) with the
  one value this sample learns; the generated function refuses any other
  value instead of guessing;
- `setup`: what must exist before the primitive (its netns, its link,
  its router), built from the same markers and removed afterwards;
- `shape`: for a tuple field (a router port's `networks`), how many
  elements this sample learns — each gets its own marker (`networks_0`,
  …), and the generated function refuses another length;
- `pairs`: for a (key, value) tuple field (a port's `options`), the keys
  this sample learns — each value gets its own marker (`options_0`, …),
  and the generated function refuses other keys;
- `each`: for a tuple of formatted strings of any length (the bridge
  mappings, `network:bridge`), the format of one element — each of its
  parts gets a marker (`network`, `bridge`); one element is learned, the
  generated function applies it to every element;
- `kinds`: a marker kind per field or element where MARKER_KIND's does
  not fit (`networks_1`: an IPv6 CIDR);
- `variant`: a name for one more shape of a class (a route with a
  gateway); the generated library tries a class's shapes in order;
- `maps`: a (key, value) tuple field RawIR holds as it is, whatever its
  keys — field -> (the node's field, the sample's value); the generated
  function passes it on (a router port's RA configs);
- `free`: fields that leave no trace in RawIR (a container's command,
  its environment) — the sample runs with the given value, the
  generated function ignores the field; learn checks it left no trace;
- `keep`: for a primitive that sets one of the host's global settings
  (the bridge mappings, where a database listens) — saves the host's
  value, clears it for the sample, and returns what restores it after.

Two runs use different markers, so what a field becomes in RawIR can be
told apart from what the system fills in.
"""

from __future__ import annotations

import dataclasses
import re
import secrets
from collections.abc import Callable, Mapping
from typing import Any

from ip_address import IPAddress

from fabric.lifecycle import Primitive
from fabric.primitives.docker import DockerPrimitive, DockerPull
from fabric.primitives.host import HostPrimitive, LinkPrimitive, NetnsPrimitive, VlanPrimitive
from fabric.primitives.net import (
    AddrPrimitive,
    IpRulePrimitive,
    LinkMovePrimitive,
    MasqRule,
    NetnsLinkPrimitive,
    RoutePrimitive,
    SysctlPrimitive,
)
from fabric.primitives.ovn import (
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
from fabric.primitives.ovs import OVSBridge, OVSExternalId, OVSMapping, OVSPort
from ladops.ovn import list_connections
from ladops.ovs import list_external_ids

from . import system

# A field's name -> the kind of marker it takes (for a tuple field: each
# element's).
MARKER_KIND: dict[str, str] = {
    **dict.fromkeys(
        (
            "name",
            "netns",
            "link",
            "dev",
            "parent",
            "bridge",
            "port",
            "switch",
            "router",
            "iface",
            "container",
            "to",
            "peer",
            "rename_to",
            "root_leg",
            "key_name",
            "value",
            "out_dev",
        ),
        "name",
    ),
    "gateway_chassis": "name",
    **dict.fromkeys(("addr", "source", "logical_ip", "networks"), "cidr"),
    **dict.fromkeys(("dst", "prefix"), "net"),
    **dict.fromkeys(("via", "nexthop", "external_ip"), "ip"),
    "mac": "mac",
    **dict.fromkeys(("vlan_id", "table", "priority", "tag", "n_rxq", "metric"), "int"),
}

# Fields that are no part of the object (only the commands' labels).
CONTEXT = ("host",)

Setup = Callable[[HostPrimitive, Mapping[str, Any]], list[Any]]
# Saves a global setting; returns what restores it.
Keep = Callable[[], Callable[[], None]]


@dataclasses.dataclass(frozen=True)
class Sample:
    primitive: type
    fixed: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    setup: Setup = lambda host, markers: []
    shape: Mapping[str, int] = dataclasses.field(default_factory=dict)
    pairs: Mapping[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)
    each: Mapping[str, str] = dataclasses.field(default_factory=dict)
    kinds: Mapping[str, str] = dataclasses.field(default_factory=dict)
    variant: str = ""
    free: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    maps: Mapping[str, tuple[str, Any]] = dataclasses.field(default_factory=dict)
    keep: Keep | None = None

    @property
    def name(self) -> str:
        """The generated function's name: the class name in snake case,
        without the `Primitive` suffix (AddrPrimitive -> addr), and the
        variant (route_via)."""
        base = self.primitive.__name__.removesuffix("Primitive")
        name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", base).lower()
        return f"{name}_{self.variant}" if self.variant else name

    def marked(self) -> list[str]:
        """The fields that get markers."""
        return [
            field.name
            for field in dataclasses.fields(self.primitive)
            if field.name not in CONTEXT
            and field.name not in self.fixed
            and field.name not in self.free
            and field.name not in self.maps
        ]

    def element_names(self, field: str) -> list[str]:
        """A field's marker names: itself, or one per tuple element."""
        if field in self.shape:
            return [f"{field}_{i}" for i in range(self.shape[field])]
        if field in self.pairs:
            return [f"{field}_{i}" for i in range(len(self.pairs[field]))]
        if field in self.each:
            return each_parts(self.each[field])
        return [field]


def each_parts(element: str) -> list[str]:
    """The parts of an `each` element format: "{network}:{bridge}" ->
    ["network", "bridge"]."""
    return re.findall(r"\{([a-z_0-9]+)\}", element)


def _marker(kind: str, index: int, run: int) -> Any:
    if kind == "name":
        return f"pm{'ab'[run]}{index}{secrets.token_hex(3)}"
    if kind == "cidr":
        return f"10.25{1 + run}.{17 + index}.{33 + run * 32}/{27 - run}"
    if kind == "net":  # a network: no host bits (a route's destination)
        return f"10.24{5 + run}.{17 + index}.{128 * run}/{26 - run}"
    if kind == "ip":
        return f"10.25{3 + run}.{17 + index}.{41 + run * 32}"
    if kind == "cidr6":
        return f"fd5{run}:{17 + index:x}::{0x21 + run * 0x20:x}/{64 - run * 8}"
    if kind == "net6":  # an IPv6 network: no host bits
        return f"fd6{run}:{17 + index:x}::/{64 - run * 8}"
    if kind == "ip6":
        return f"fd7{run}:{17 + index:x}::{0x29 + run * 0x20:x}"
    if kind == "image":  # a real one: it is pulled
        return ("busybox:latest", "alpine:latest")[run]
    if kind == "mac":
        return f"02:5e:0{run}:{index:02x}:{secrets.token_hex(1)}:{secrets.token_hex(1)}"
    if kind == "int":
        return 3001 + index * 10 + run
    raise ValueError(f"unknown marker kind {kind!r}")


def marker_kinds(sample: Sample) -> dict[str, str]:
    """Every marker's name and kind, in order."""
    out: dict[str, str] = {}
    for field in sample.marked():
        for name in sample.element_names(field):
            kind = sample.kinds.get(name) or sample.kinds.get(field)
            named = field in sample.pairs or field in sample.each
            kind = kind or ("name" if named else MARKER_KIND.get(field))
            if kind is None:
                raise ValueError(
                    f"{sample.primitive.__name__}.{field}: no marker kind — "
                    "give it a fixed value in the sample, or a kind in MARKER_KIND"
                )
            out[name] = kind
    return out


def markers(sample: Sample, run: int) -> dict[str, Any]:
    """Distinct marker values for one run (0 or 1) of a sample."""
    return {
        name: _marker(kind, index, run)
        for index, (name, kind) in enumerate(marker_kinds(sample).items())
    }


def build(sample: Sample, host: str, values: Mapping[str, Any]) -> tuple[list[Any], Any]:
    """(setup operations, the primitive) for one run."""
    fields: dict[str, Any] = (
        {"host": host} if "host" in {f.name for f in dataclasses.fields(sample.primitive)} else {}
    )
    for field in sample.marked():
        names = sample.element_names(field)
        if field in sample.shape:
            fields[field] = tuple(values[n] for n in names)
        elif field in sample.each:
            fields[field] = (sample.each[field].format(**{n: values[n] for n in names}),)
        elif field in sample.pairs:
            keys = sample.pairs[field]
            fields[field] = tuple((key, values[n]) for key, n in zip(keys, names, strict=True))
        else:
            fields[field] = values[field]
    mapped = {field: value for field, (_node_field, value) in sample.maps.items()}
    primitive = sample.primitive(**fields, **sample.fixed, **sample.free, **mapped)
    return sample.setup(HostPrimitive(host), values), primitive


def _netns(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [host.netns(m["netns"])]


def _netns_and_link(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    netns = host.netns(m["netns"])
    return [netns, netns.add_if(m["link"], kind="dummy", up=True)]


def _netns_and_dev(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    netns = host.netns(m["netns"])
    return [netns, netns.add_if(m["dev"], kind="dummy", up=True)]


def _netns_dev_and_gateway(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    """The netns, its link, and an address on it the gateway `via` is in."""
    netns = host.netns(m["netns"])
    via = IPAddress.parse(m["via"])
    if via is None:
        raise ValueError(f"not an address: {m['via']!r}")
    prefix = 24 if via.is_ipv4() else 64
    gateway = via.change_prefix(prefix)
    first = gateway.network().first()
    own = (first if first.to_s() != gateway.to_s() else first.inc()).to_s()
    return [
        netns,
        netns.add_if(m["dev"], kind="dummy", up=True),
        netns.add_addr(m["dev"], f"{own}/{prefix}"),
    ]


def _bridge_with_port(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    """A bridge the collector reports (it lists bridges with a port)."""
    port = f"{m['bridge']}p"
    return [
        host.ovs_bridge(m["bridge"]),
        host.rootNs().add_if(port, kind="dummy", up=True),
        host.ovs_port(m["bridge"], port),
    ]


def _keep_mappings() -> Callable[[], None]:
    saved = list_external_ids().get("ovn-bridge-mappings")
    if saved:  # cleared: the sample must see what setting it does
        system.run(
            ["ovs-vsctl", "remove", "open_vswitch", ".", "external-ids", "ovn-bridge-mappings"]
        )

    def restore() -> None:
        if saved:
            system.run(
                [
                    "ovs-vsctl",
                    "set",
                    "open_vswitch",
                    ".",
                    f"external-ids:ovn-bridge-mappings={saved}",
                ]
            )

    return restore


def _keep_connection(db: str) -> Keep:
    def keep() -> Callable[[], None]:
        saved = list_connections(db)
        if saved:  # cleared: the sample must see what setting it does
            system.run([f"ovn-{db}ctl", "del-connection"])

        def restore() -> None:
            if saved:
                system.run([f"ovn-{db}ctl", "set-connection", *saved])

        return restore

    return keep


def _two_netns_and_link(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    netns = host.netns(m["netns"])
    return [netns, host.netns(m["to"]), netns.add_if(m["name"], kind="dummy", up=True)]


@dataclasses.dataclass(frozen=True)
class _Pulled:
    """An image the sample needs there: pulled, and left there afterwards
    (a workload of the host may use it — DockerPull's delete removes it)."""

    pull: DockerPull

    def as_primitive(self) -> Primitive:
        return dataclasses.replace(self.pull.as_primitive(), delete_cmds=())


def _image(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [_Pulled(host.docker_pull(m["image"]))]


def _netns_and_bridge(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [host.netns(m["netns"]), host.ovs_bridge(m["bridge"])]


def _root_parent(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [host.rootNs().add_if(m["parent"], kind="dummy", up=True)]


def _netns_and_root_parent(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    """A netns, and the VLAN's parent in the root one (a wan's NIC)."""
    return [host.netns(m["netns"]), host.rootNs().add_if(m["parent"], kind="dummy", up=True)]


def _bridge_and_link(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [host.ovs_bridge(m["bridge"]), host.rootNs().add_if(m["port"], kind="dummy", up=True)]


def _router(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [host.ovn_nbctl_lr(m["router"])]


def _switch(host: HostPrimitive, m: Mapping[str, Any]) -> list[Any]:
    return [host.ovn_nbctl_ls(m["switch"])]


NO_PAIRS: tuple[tuple[str, str], ...] = ()
RA_CONFIGS = {"ipv6_ra_configs": ("ipv6RaConfigs", (("address_mode", "slaac"),))}

SAMPLES = [
    Sample(NetnsPrimitive, fixed={"owned": True}),
    Sample(AddrPrimitive, setup=_netns_and_link),
    Sample(AddrPrimitive, kinds={"addr": "cidr6"}, setup=_netns_and_link, variant="v6"),
    Sample(
        LinkPrimitive,
        fixed={"netns": None, "kind": "dummy", "peer": None, "mac": None, "up": True},
    ),
    Sample(VlanPrimitive, fixed={"netns": None}, setup=_root_parent),
    Sample(
        NetnsLinkPrimitive,
        fixed={
            "kind": "dummy",
            "rename_to": None,
            "parent": None,
            "vlan_id": None,
            "mac": None,
            "root_leg": None,
            "bridge": None,
        },
        setup=_netns,
    ),
    Sample(
        RoutePrimitive,
        fixed={"via": None, "metric": None, "table": None, "family": None},
        setup=_netns_and_dev,
    ),
    Sample(
        RoutePrimitive,
        fixed={"metric": None, "table": None, "family": None},
        setup=_netns_dev_and_gateway,
        variant="via",
    ),
    Sample(
        RoutePrimitive,
        fixed={"via": None, "metric": None, "table": None, "family": None},
        kinds={"dst": "net6"},
        setup=_netns_and_dev,
        variant="v6",
    ),
    Sample(
        RoutePrimitive,
        fixed={"metric": None, "table": None, "family": None},
        kinds={"dst": "net6", "via": "ip6"},
        setup=_netns_dev_and_gateway,
        variant="via_v6",
    ),
    # A workload NIC's routes: with a metric, in its own table (and again
    # in main for the default), per family.
    *(
        Sample(
            RoutePrimitive,
            fixed={"family": family, **({} if table else {"table": None})},
            kinds=kinds,
            setup=_netns_dev_and_gateway,
            variant=f"metric{'_table' if table else ''}{'_v6' if family == 6 else ''}",
        )
        for family, kinds in ((4, {}), (6, {"dst": "net6", "via": "ip6"}))
        for table in (True, False)
    ),
    # A veth: one leg in the netns, the other one in the root netns and
    # in an OVS bridge (a kernel router side, a workload NIC).
    Sample(
        NetnsLinkPrimitive,
        fixed={"kind": "veth", "rename_to": None, "parent": None, "vlan_id": None},
        setup=_netns_and_bridge,
        variant="veth",
    ),
    # A VLAN made on its parent in the root netns and moved into a netns (a
    # wan's interface, ADR 0009).
    Sample(
        NetnsLinkPrimitive,
        fixed={"kind": "vlan", "rename_to": None, "mac": None, "root_leg": None, "bridge": None},
        setup=_netns_and_root_parent,
        variant="vlan",
    ),
    # … renamed in the netns (a workload NIC: eth1 in the container).
    Sample(
        NetnsLinkPrimitive,
        fixed={"kind": "veth", "parent": None, "vlan_id": None},
        setup=_netns_and_bridge,
        variant="veth_renamed",
    ),
    Sample(IpRulePrimitive, fixed={"family": 4}, setup=_netns),
    Sample(LinkMovePrimitive, setup=_two_netns_and_link),
    *(
        Sample(MasqRule, fixed={"family": family}, setup=_netns, variant=family)
        for family in ("ipv4", "ipv6")
    ),
    Sample(SysctlPrimitive, fixed={"key": "net.ipv4.ip_forward", "value": "1"}, setup=_netns),
    # A container that stays up; what it runs and with which settings is no
    # part of RawIR.
    Sample(
        DockerPrimitive,
        setup=_image,
        # natural_key a marker: RawIR shows it as the container's
        # naturalKeyLabel (ADR 0010) — learned, so a container with one (a
        # ZeroTier instance) is not "different" from its footprint.
        kinds={"image": "image", "natural_key": "name"},
        free={
            "command": ("sleep", "3600"),
            "privileged": False,
            "network": "none",
            "environment": (),
            "volumes": (),
            "run_flags": (),
            "remove_existing": True,
            "files": "",
        },
    ),
    Sample(OVSBridge, fixed={"datapath": None, "fail_mode": "standalone"}),
    Sample(OVSPort, fixed={"tag": None, "trunks": ()}, setup=_bridge_and_link),
    # The mappings are ONE global setting: the host's is kept and restored.
    Sample(
        OVSMapping,
        each={"mappings": "{network}:{bridge}"},
        setup=_bridge_with_port,
        keep=_keep_mappings,
    ),
    Sample(OVSExternalId),
    # Where the NB/SB databases listen: ONE setting per database — the
    # host's is kept and restored.
    Sample(OVNNbCtlConnection, fixed={"key": "ovn.nb.connection"}, keep=_keep_connection("nb")),
    Sample(OVNSbCtlConnection, fixed={"key": "ovn.sb.connection"}, keep=_keep_connection("sb")),
    Sample(OVNNbCtlLs),
    Sample(OVNNbCtlLr, fixed={"options": NO_PAIRS, "external_ids": NO_PAIRS}),
    Sample(
        OVNNbCtlLr,
        fixed={"external_ids": NO_PAIRS},
        pairs={"options": ("mac_binding_age_threshold",)},
        variant="aged",
    ),
    Sample(
        OVNNbCtlLr,
        pairs={
            "options": ("mac_binding_age_threshold",),
            "external_ids": ("ovn-fabric.securityGroups",),
        },
        variant="aged_groups",
    ),
    Sample(
        OVNNbCtlLrp,
        fixed={"gateway_chassis": None},
        maps=RA_CONFIGS,
        shape={"networks": 1},
        setup=_router,
    ),
    Sample(
        OVNNbCtlLrp,
        maps=RA_CONFIGS,
        shape={"networks": 2},
        kinds={"networks_1": "cidr6"},
        setup=_router,
        variant="gateway",
    ),
    # A port without addresses (an L2 attachment), with or without a chassis.
    Sample(
        OVNNbCtlLrp,
        maps=RA_CONFIGS,
        shape={"networks": 0},
        setup=_router,
        variant="no_networks_gateway",
    ),
    Sample(
        OVNNbCtlLrp,
        fixed={"gateway_chassis": None},
        maps=RA_CONFIGS,
        shape={"networks": 0},
        setup=_router,
        variant="no_networks",
    ),
    Sample(
        OVNNbCtlLrp,
        fixed={"gateway_chassis": None},
        maps=RA_CONFIGS,
        shape={"networks": 2},
        kinds={"networks_1": "cidr6"},
        setup=_router,
        variant="dual",
    ),
    Sample(
        OVNNbCtlLsp,
        fixed={"type": None, "addresses": ("unknown",), "options": NO_PAIRS},
        setup=_switch,
    ),
    Sample(
        OVNNbCtlLsp,
        fixed={"type": "localnet", "addresses": ("unknown",)},
        pairs={"options": ("network_name",)},
        setup=_switch,
        variant="localnet",
    ),
    Sample(
        OVNNbCtlLsp,
        fixed={"type": "router", "addresses": ("router",)},
        pairs={"options": ("router-port",)},
        setup=_switch,
        variant="router",
    ),
    Sample(OVNNbCtlRoute, setup=_router),
    Sample(OVNNbCtlRoute, kinds={"prefix": "net6", "nexthop": "ip6"}, setup=_router, variant="v6"),
    Sample(
        OVNNbCtlNat,
        fixed={"type": "snat", "reset": False},
        free={"gateway_port": None},
        setup=_router,
    ),
    Sample(
        OVNNbCtlNat,
        fixed={"type": "snat", "reset": False},
        free={"gateway_port": None},
        kinds={"external_ip": "ip6", "logical_ip": "cidr6"},
        setup=_router,
        variant="v6",
    ),
    Sample(
        OVNNbCtlAcl,
        fixed={"direction": "to-lport", "match": "ip4", "action": "allow"},
        setup=_switch,
    ),
    # An ACL's match is free text, passed through: learned as a marker.
    # Direction and action are OVN's own words: one shape per pair.
    *(
        Sample(
            OVNNbCtlAcl,
            fixed={"direction": direction, "action": action},
            kinds={"match": "name"},
            setup=_switch,
            variant=f"{direction.removesuffix('-lport')}_{action.replace('-', '_')}",
        )
        for direction in ("to-lport", "from-lport")
        for action in ("allow", "allow-related", "drop", "reject")
    ),
]
