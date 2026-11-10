"""Whose a RawIR node is (its `owner`).

Every node starts as `ovn-fabric`'s — ours: what a run fulfils, or else
cleans up. Discovery then asks the others what is theirs and marks it;
those nodes are never touched:

- kernel: what it makes by itself (routes that follow from an address or
  a link, or from a router advertisement; SLAAC and link-local addresses;
  the fallback tunnel devices of a netns; the root netns);
- docker: its networks' bridge interfaces, its iptables chains, jumps into
  them, the masquerade of its subnets (`docker network inspect`);
  containers we did not start (no spec label), and whatever is inside
  the namespace of a running container (the app's own tunnel, its routes);
- openvswitch: its datapath device and tunnel ports (`ovs-dpctl show`),
  the ids it sets itself;
- ovn-controller: the integration bridge with its ports, the patch ports.

Physical NICs are the host's too. Its other own interfaces are known from
its config only — `mark_host`,
called by the runtime (the topology's hostInterfaces, the IR's physical
parents) and by primate (the loopback).

An owner of a link owns what is on it — addresses (`linkRef`), routes
(`devRef`), its OVS interface and port; a bridge owns its ports and its
own interface. Not what is stacked on it: a VLAN over a host's NIC is a
link of its own.
"""

from __future__ import annotations

import ipaddress
import subprocess
from collections.abc import Callable, Iterable
from typing import Any

from ladops.docker import list_containers, list_networks
from ladops.ovs import list_datapath, list_external_ids

OVN_FABRIC = "ovn-fabric"
GLOBAL = "*global*"

Nodes = dict[str, dict[str, Any]]

# The label of a container we started (SPEC_LABEL, fabric/primitives/docker.py).
_SPEC_LABEL = "ovn-fabric.spec"

# Routes the kernel adds by itself, by their protocol.
_KERNEL_ROUTES = ("kernel", "ra", "redirect")

# Devices the kernel makes in every netns once a tunnel module is loaded.
_FALLBACK_DEVICES = (
    "sit0",
    "tunl0",
    "ip6tnl0",
    "ip_vti0",
    "ip6_vti0",
    "gre0",
    "gretap0",
    "erspan0",
    "ip6gre0",
)

# OVS's own Open_vSwitch external ids (what the chassis registration sets
# is ours: ovn-remote, ovn-encap-*, hostname).
_OVS_IDS = ("system-id", "rundir")


def ours(nodes: Nodes) -> Nodes:
    """The nodes that are ovn-fabric's."""
    return {node_id: node for node_id, node in nodes.items() if node["owner"] == OVN_FABRIC}


def _set(nodes: Nodes, ids: Iterable[str], owner: str) -> None:
    for node_id in ids:
        node = nodes.get(node_id)
        if node is not None and node["owner"] == OVN_FABRIC:
            node["owner"] = owner


def on_links(nodes: Nodes, links: set[str]) -> set[str]:
    """The links and what is on them: addresses, routes, their OVS
    interface and that interface's port."""
    names = {nodes[i]["ifname"] for i in links if i.startswith(f"link:{GLOBAL}|") and i in nodes}
    out = set(links)
    for node_id, node in nodes.items():
        kind = node["kind"]
        if kind == "addr" and node.get("linkRef") in links:
            out.add(node_id)
        elif kind == "route" and node.get("devRef") in links:
            out.add(node_id)
        elif kind == "ovs.iface" and node.get("name") in names:
            out.update((node_id, node.get("portRef", "")))
    return out


def _bridges(nodes: Nodes, names: Iterable[str]) -> set[str]:
    """OVS bridges with their ports, interfaces, and own link."""
    out: set[str] = set()
    for name in names:
        bridge = nodes.get(f"ovs.bridge:{name}")
        if bridge is None:
            continue
        out.add(bridge["id"])
        for port_id in bridge.get("portRefs", []):
            out.add(port_id)
            out.update(nodes.get(port_id, {}).get("ifaceRefs", []))
        out.update(on_links(nodes, {f"link:{GLOBAL}|{name}"}))
    return out


def mark(nodes: Nodes, owner: str, links: Iterable[str] = (), others: Iterable[str] = ()) -> None:
    """`owner` gets the links with what is on them, and `others` as they
    are — of what is still ovn-fabric's."""
    _set(nodes, on_links(nodes, {i for i in links if i in nodes}), owner)
    _set(nodes, others, owner)


def mark_host(nodes: Nodes, interfaces: Iterable[str], parents: Iterable[str] = ()) -> None:
    """The host's own: `interfaces` in every netns (the loopback, the
    management NIC), `parents` in the root netns (a VLAN's NIC)."""
    names = set(interfaces)
    links = {
        node_id
        for node_id, node in nodes.items()
        if node["kind"] == "link" and node.get("ifname") in names
    }
    links |= {f"link:{GLOBAL}|{name}" for name in parents}
    mark(nodes, "host", links)


# -- discovery: one function per owner ----------------------------------------


def _kernel(nodes: Nodes) -> None:
    link_local = ipaddress.ip_network("fe80::/10")

    def is_link_local(value: Any) -> bool:
        try:
            return ipaddress.ip_interface(str(value)).ip in link_local
        except ValueError:
            return False

    mark(
        nodes,
        "kernel",
        others=[
            node_id
            for node_id, node in nodes.items()
            if (node["kind"] == "addr" and is_link_local(node.get("address")))
            or (node["kind"] == "route" and is_link_local(node.get("prefix")))
            or (node["kind"] == "addr" and node.get("dynamic"))
            or (node["kind"] == "route" and node.get("protocol") in _KERNEL_ROUTES)
            or node_id == f"netns:{GLOBAL}"
        ],
        links=[
            node_id
            for node_id, node in nodes.items()
            if node["kind"] == "link" and node.get("ifname") in _FALLBACK_DEVICES
        ],
    )


def _docker(nodes: Nodes) -> None:
    networks = list_networks()
    subnets = [ipaddress.ip_network(s) for n in networks for s in n["subnets"]]
    containers = _containers()
    # A running container's namespace, by both names it is collected under.
    inside = {f"netns:{prefix}{c['name']}" for c in containers for prefix in ("", "docker:")}

    def docker_rule(node: dict[str, Any]) -> bool:
        rule = node.get("rule") or {}
        if str(node.get("chain", "")).startswith("DOCKER"):
            return True
        if str(rule.get("target") or "").startswith("DOCKER"):
            return True
        source = (rule.get("fields") or {}).get("src")
        if rule.get("action") == "MASQUERADE" and source:
            try:
                return any(ipaddress.ip_network(source) == net for net in subnets)
            except ValueError:
                return False
        return False

    mark(
        nodes,
        "docker",
        links=[f"link:{GLOBAL}|{n['bridge']}" for n in networks],
        others=[
            node_id
            for node_id, node in nodes.items()
            if (
                node["kind"] == "iptables.rule"
                and node.get("netnsRef") == f"netns:{GLOBAL}"
                and docker_rule(node)
            )
            or node_id in inside
            # What is inside its netns — not the container itself, whose own
            # netnsRef is that netns (it is ours when we started it).
            or (node["kind"] != "docker.container" and node.get("netnsRef") in inside)
        ]
        + [
            f"docker.container:{c['name']}"
            for c in containers
            if _SPEC_LABEL not in c.get("labels", {})
        ],
    )


def _containers() -> list[dict[str, Any]]:
    try:
        return list_containers()
    except (OSError, subprocess.CalledProcessError):
        return []  # no docker


def _openvswitch(nodes: Nodes) -> None:
    datapath = list_datapath()
    devices = [*datapath["datapaths"], *datapath["tunnels"]]
    mark(
        nodes,
        "openvswitch",
        links=[f"link:{GLOBAL}|{name}" for name in devices],
        others=[f"ovs.external-id:{key}" for key in _OVS_IDS],
    )


def _ovn_controller(nodes: Nodes) -> None:
    bridge = list_external_ids().get("ovn-bridge", "br-int")
    patches = [
        node_id
        for node_id, node in nodes.items()
        if node["kind"] == "ovs.iface" and node.get("type") == "patch"
    ]
    owned = _bridges(nodes, [bridge])
    owned |= {*patches, *(nodes[i].get("portRef", "") for i in patches)}
    mark(nodes, "ovn-controller", others=owned)


def _physical(nodes: Nodes) -> None:
    """A physical NIC: nothing we could have made, used by the IR or not."""
    mark(
        nodes,
        "host",
        links=[
            node_id
            for node_id, node in nodes.items()
            if node["kind"] == "link" and node.get("linkKind") == "physical"
        ],
    )


# Asked in this order: the first owner found keeps a node.
DISCOVER: dict[str, Callable[[Nodes], None]] = {
    "kernel": _kernel,
    "host": _physical,
    "ovn-controller": _ovn_controller,
    "openvswitch": _openvswitch,
    "docker": _docker,
}


def discover(nodes: Nodes) -> None:
    """Every node ovn-fabric's, then each owner's own marked."""
    for node in nodes.values():
        node["owner"] = OVN_FABRIC
    for find in DISCOVER.values():
        find(nodes)
