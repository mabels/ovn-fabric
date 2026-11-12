"""The leftovers of a run: what the first look at the host found to be ours
(`owner` ovn-fabric) and no step of the plan fulfilled. They are removed at
the end of a run, as one unit of work, in reverse — as a delete rewinds.

Which nodes those are is decided by crossing out (fabric/desired.py); if
this removes what should stay, the crossing out is wrong — there is no
second check here.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any

from ladops import ovn as ovn_ops
from ladops import ovs as ovs_ops

from . import generated as g

Node = Mapping[str, Any]
GLOBAL = "*global*"


def _in_netns(node: Node, argv: list[str]) -> list[str]:
    netns = str(node["netnsRef"]).removeprefix("netns:")
    return argv if netns == GLOBAL else ["ip", "netns", "exec", netns, *argv]


def _after(ref: str, kind: str) -> str:
    """The name in a reference: "ovn.lr:router-a" -> "router-a"."""
    return ref.removeprefix(f"{kind}:")


def _dev(ref: str) -> str:
    """The link's name in its reference: "link:ns|eth0" -> "eth0"."""
    return ref.rsplit("|", 1)[-1]


def _iptables(node: Node) -> list[list[str]]:
    if node.get("nftHandle") is None:
        return []
    family = "ip" if node["family"] == "ipv4" else "ip6"
    table, chain = str(node["table"]), str(node["chain"])
    handle = str(node["nftHandle"])
    return [_in_netns(node, ["nft", "delete", "rule", family, table, chain, "handle", handle])]


def _route(node: Node) -> list[list[str]]:
    dev = _dev(str(node["devRef"]))
    family = ["-6"] if node["family"] == "ipv6" else ["-4"]
    argv = ["ip", *family, "route", "del", str(node["prefix"]), "dev", dev]
    if node.get("via") and node["via"] != dev:
        argv += ["via", str(node["via"])]
    return [_in_netns(node, [*argv, "table", str(node["table"])])]


def _addr(node: Node) -> list[list[str]]:
    dev = _dev(str(node["linkRef"]))
    return [
        _in_netns(node, ["ip", "addr", "del", f"{node['address']}/{node['prefixlen']}", "dev", dev])
    ]


def _link(node: Node) -> list[list[str]]:
    return [_in_netns(node, ["ip", "link", "delete", str(node["ifname"])])]


def _netns(node: Node) -> list[list[str]]:
    return [["ip", "netns", "delete", str(node["name"])]]


def _container(node: Node) -> list[list[str]]:
    return [["/usr/bin/docker", "rm", "-f", str(node["name"])]]


def _acl(node: Node) -> list[list[str]]:
    switch = _after(str(node["switchRef"]), "ovn.ls")
    return [
        ovn_ops.acl_del_argv(
            switch, str(node["direction"]), int(node["priority"]), str(node["match"])
        )
    ]


def _nat(node: Node) -> list[list[str]]:
    # An snat is named by its logical address, the others by their external
    # one (as OVNNbCtlNat deletes it).
    named_by = node["logicalIp"] if str(node["type"]) == "snat" else node["externalIp"]
    router = _after(str(node["routerRef"]), "ovn.lr")
    return [ovn_ops.lr_nat_del_argv(router, str(node["type"]), str(named_by))]


def _ovn_route(node: Node) -> list[list[str]]:
    router = _after(str(node["routerRef"]), "ovn.lr")
    return [ovn_ops.lr_route_del_argv(router, str(node["prefix"]), str(node["nexthop"]))]


def _connection(node: Node) -> list[list[str]]:
    delete = (
        ovn_ops.nb_del_connection_argv if node["db"] == "nb" else ovn_ops.sb_del_connection_argv
    )
    return [delete()]


def _ovs_port(node: Node) -> list[list[str]]:
    return [ovs_ops.del_port_argv(_after(str(node["bridgeRef"]), "ovs.bridge"), str(node["name"]))]


def _nothing(node: Node) -> list[list[str]]:
    return []  # it goes with another node (an OVS interface with its port)


# Per kind, what removes a node.
DELETERS: dict[str, Callable[[Node], list[list[str]]]] = {
    "iptables.rule": _iptables,
    "route": _route,
    "addr": _addr,
    "docker.container": _container,
    "ovn.acl": _acl,
    "ovn.nat": _nat,
    "ovn.route": _ovn_route,
    "ovn.lsp": lambda node: [ovn_ops.lsp_del_argv(str(node["name"]))],
    "ovn.lrp": lambda node: [ovn_ops.lrp_del_argv(str(node["name"]))],
    "ovn.ls": lambda node: [ovn_ops.ls_del_argv(str(node["name"]))],
    "ovn.lr": lambda node: [ovn_ops.lr_del_argv(str(node["name"]))],
    "ovn.connection": _connection,
    "ovs.port": _ovs_port,
    "ovs.iface": _nothing,
    "ovs.bridge": lambda node: [ovs_ops.del_br_argv(str(node["name"]))],
    "link": _link,
    "ovs.external-id": lambda node: [ovs_ops.remove_external_id_argv(str(node["key"]))],
    "netns": _netns,
}

# The order a create builds them in (fabric/plan.py) — leftovers are removed
# in reverse, as a delete rewinds its plan.
CREATE_ORDER = (
    "netns",
    "ovs.external-id",
    "ovn.connection",
    "ovs.bridge",
    "link",
    "ovs.port",
    "ovs.iface",
    "ovn.ls",
    "ovn.lr",
    "ovn.lrp",
    "ovn.lsp",
    "ovn.route",
    "ovn.nat",
    "ovn.acl",
    "addr",
    "route",
    "iptables.rule",
    "docker.container",
)

_ORDER = {kind: index for index, kind in enumerate(CREATE_ORDER)}


# Not removed for being unfulfilled: a container decides itself and has no
# learned footprint, so "no step fulfils it" says nothing about it. Only
# `wanted_containers` tells (see leftovers), or `delete`, or a release before
# claim (the same natural key under another name, ADR 0010).
NEVER_CLEANED = ("docker.container",)


def leftovers(
    nodes: Mapping[str, Node],
    fulfilled: set[str],
    wanted_containers: Collection[str] | None = None,
) -> list[Node]:
    """The nodes not fulfilled, in removal_order. A container of ours is a
    leftover only when `wanted_containers` (the `docker.container:<name>`
    ids the plan's steps start) is given and does not name it — its router
    or workload left the IR. Without it, containers are never leftovers."""
    return removal_order(
        node
        for node_id, node in nodes.items()
        if node_id not in fulfilled
        and (
            node["kind"] not in NEVER_CLEANED
            or (wanted_containers is not None and node_id not in wanted_containers)
        )
    )


def wanted_containers(steps: Iterable[object]) -> set[str]:
    """The containers the plan's steps start, as `docker.container:<name>`."""
    return {
        f"docker.container:{getattr(step, 'name')}"
        for step in steps
        if type(step).__name__ == "DockerPrimitive"
    }


def removal_order(nodes: Iterable[Node]) -> list[Node]:
    """The order nodes are removed in: the reverse of a create, by kind;
    within a kind the reverse of how they were found. A bridge's own link
    goes with the bridge."""
    left = list(nodes)
    bridges = {f"link:{GLOBAL}|{node['name']}" for node in left if node["kind"] == "ovs.bridge"}
    created = [node for node in left if node["id"] not in bridges]
    created.sort(key=lambda node: _ORDER[str(node["kind"])])  # stable: found order
    return created[::-1]


def unit(nodes: list[Node]) -> g.Unit:
    """The unit that removes `nodes`: run as a delete, best effort, its
    steps rewound — so they are listed last-removed first."""
    steps: list[g.UnitStepSpec] = [
        g.PrimitiveStep(
            kind="Primitive",
            key=str(node["id"]),
            nodes=[],
            create=[],
            delete=[
                g.StepCommand(label=f"{node['id']} (remove)", argv=list(argv), optional=True)
                for argv in DELETERS.get(str(node["kind"]), _nothing)(node)
            ],
        )
        for node in reversed(nodes)
    ]
    return g.Unit(name="cleanup: leftovers", kind="cleanup", steps=steps)
