"""A RawIR node's natural key (ADR 0010): what a single resource is, by its
values — where its `id` is who it is, by a name.

Only single resources have one: a thing the kernel refuses to have twice
with the same value.

    physical NIC   nic:<permanent MAC>
    VLAN           vlan:<parent's natural key>|<VLAN ID>
    veth           veth:<MAC>
    WireGuard      wg:<hash of its private key>
    ZeroTier       zt:<node address>   (its container's label)
    OVN route      ovnroute:<router>|<policy>|<prefix>
    kernel route   route:<netns>|<table>|<prefix>|<metric>
    address        addr:<collision domain>|<address>   (ADR 0011 §3a)

An address is a single resource within its collision domain: OVN does not
refuse two router ports with one address on a switch (VM 302, 2026-11-11),
so the key is what makes the second one a clash. Never the address alone —
tenant prefixes may overlap. A node may hold several addresses, so it has
several address keys (`address_keys`), beside its one natural key.

A route is single per prefix in its router or netns (OVN: "duplicate
prefix"; the kernel: "File exists", whatever the next hop): its key is
scoped by that container's name — the conflict is only ever inside it.

A natural key never contains a name (a NIC name udev may change, a
generated or shortened one): only hardware identity, protocol values and a
parent's natural key. It is computed, never stored: `natural_key(node,
live)` for a live node and for a planned one alike, the live graph giving
a parent's key.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

Node = Mapping[str, Any]
Graph = Mapping[str, Node]


def _fields(node: Node) -> Mapping[str, Any]:
    """A node's fields: flat in a graph, under `collection` as collected."""
    collection = node.get("collection")
    return collection if isinstance(collection, Mapping) else node


def _nic(node: Node, live: Graph) -> str | None:
    mac = _fields(node).get("permMac") or _fields(node).get("mac")
    return f"nic:{mac}" if mac else None


def _vlan(node: Node, live: Graph) -> str | None:
    fields = _fields(node)
    parent = live.get(fields.get("linkRef") or "")
    vlan_id = fields.get("vlanId")
    if parent is None or vlan_id is None:
        return None
    parent_key = natural_key(parent, live)
    return f"vlan:{parent_key}|{vlan_id}" if parent_key else None


def _veth(node: Node, live: Graph) -> str | None:
    mac = _fields(node).get("mac")
    return f"veth:{mac}" if mac else None


def _wireguard(node: Node, live: Graph) -> str | None:
    key = _fields(node).get("wgKeyHash")
    return f"wg:{key}" if key else None


def _container(node: Node, live: Graph) -> str | None:
    """What a container declares itself to be (a ZeroTier instance)."""
    label = _fields(node).get("naturalKeyLabel")
    return str(label) if label else None


def _ovn_route(node: Node, live: Graph) -> str | None:
    fields = _fields(node)
    policy = fields.get("policy") or "dst-ip"
    return f"ovnroute:{fields.get('routerRef')}|{policy}|{fields.get('prefix')}"


def _ovn_nat(node: Node, live: Graph) -> str | None:
    """What OVN holds unique in a router: an SNAT per logical network, any
    other NAT per external address — a changed other side is the same NAT
    (`lr-nat-add` refuses a second)."""
    fields = _fields(node)
    nat_type = fields.get("type")
    unique = fields.get("logicalIp") if nat_type == "snat" else fields.get("externalIp")
    if nat_type is None or unique is None:
        return None
    return f"ovnnat:{fields.get('routerRef')}|{nat_type}|{unique}"


def _route(node: Node, live: Graph) -> str | None:
    fields = _fields(node)
    table = fields.get("table") or "main"
    # No metric: the kernel's default (0 for IPv4, 1024 for IPv6).
    metric = fields.get("metric")
    if metric is None:
        metric = 1024 if fields.get("family") == "ipv6" else 0
    return f"route:{fields.get('netnsRef')}|{table}|{fields.get('prefix')}|{metric}"


# By link kind: the single resources among links.
LINK_KEYS: dict[str, Callable[[Node, Graph], str | None]] = {
    "physical": _nic,
    "vlan": _vlan,
    "veth": _veth,
    "wireguard": _wireguard,
}

# By node kind; links by their link kind. Every other node has none.
KEYS: dict[str, Callable[[Node, Graph], str | None]] = {
    "link": lambda node, live: (
        LINK_KEYS[kind](node, live)
        if (kind := str(_fields(node).get("linkKind"))) in LINK_KEYS
        else None
    ),
    "docker.container": _container,
    "ovn.nat": _ovn_nat,
    "ovn.route": _ovn_route,
    "route": _route,
}


def natural_key(node: Node, live: Graph) -> str | None:
    """The node's natural key; None when it is no single resource, or a
    part of its key (a parent, a VLAN ID) is not known."""
    key = KEYS.get(str(node.get("kind")))
    return key(node, live) if key is not None else None


def natural_keys(live: Graph) -> dict[str, str]:
    """Every live node's natural key, by node id."""
    out: dict[str, str] = {}
    for node_id, node in live.items():
        key = natural_key(node, live)
        if key is not None:
            out[node_id] = key
    return out


def _router_port_switch(node: Node, live: Graph) -> str | None:
    """The switch an OVN router port is on: the one of its peer port
    (`options:router-port`), found in `live` (which may hold planned nodes)."""
    name = _fields(node).get("name")
    for other in live.values():
        fields = _fields(other)
        if (
            other.get("kind") == "ovn.lsp"
            and (fields.get("options") or {}).get("router-port") == name
        ):
            ref = fields.get("switchRef")
            return str(ref) if ref else None
    return None


def _router_port_addresses(node: Node, live: Graph) -> list[str]:
    switch = _router_port_switch(node, live)
    if switch is None:
        return []
    return [
        f"addr:{switch}|{str(network).split('/')[0]}"
        for network in _fields(node).get("networks") or []
    ]


def _nic_address(node: Node, live: Graph) -> list[str]:
    """A kernel address on a workload NIC: the NIC is named after its switch
    (src/ir.ts shortIfaceName — the segment's name when it fits IFNAMSIZ),
    so a switch of that name is its collision domain. A hashed (long) name,
    a link-local or a loopback address has no key: its domain is unknown,
    or it is the kernel's own."""
    fields = _fields(node)
    address = str(fields.get("address") or "")
    nic = str(fields.get("linkRef") or "").rpartition("|")[2]
    if not address or not nic or address.startswith(("fe80:", "127.")) or address == "::1":
        return []
    switch = f"ovn.ls:{nic}"
    return [f"addr:{switch}|{address}"] if switch in live else []


# By node kind: the addresses a node holds, each a single resource.
ADDRESS_KEYS: dict[str, Callable[[Node, Graph], list[str]]] = {
    "ovn.lrp": _router_port_addresses,
    "addr": _nic_address,
}


def address_keys(node: Node, live: Graph) -> list[str]:
    """`addr:<collision domain>|<address>` for every address `node` holds."""
    keys = ADDRESS_KEYS.get(str(node.get("kind")))
    return keys(node, live) if keys is not None else []


def all_keys(node: Node, live: Graph) -> list[str]:
    """Every single resource `node` is or holds: its natural key and its
    address keys."""
    key = natural_key(node, live)
    return ([key] if key is not None else []) + address_keys(node, live)
