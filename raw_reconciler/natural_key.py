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
