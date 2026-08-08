# reconciler/ovs/reconcile.py — ovs-vsctl-based reconciler: Interface ->
# ovs.iface IR nodes.
#
# Not netns-scoped, same reasoning as reconciler/ovn/reconcile.py — OVS's
# own database is host-global, so this only produces nodes on the
# netns=None pass.
#
# `ovs.iface` wasn't in docs/adr/0002-intermediate-representation.md's
# node-kind table before this — OVS wasn't designed in that ADR pass at
# all. Mirrors reconciler/linux_net's own net.iface shape (name + link/
# admin state, "get all data, let the deployer decide what matters")
# rather than inventing a differently-shaped kind for what's
# conceptually the same fact at the OVS layer.

from __future__ import annotations

from ..ovsdb import list_table

_VSCTL = ["ovs-vsctl"]


def reconcile(scope: str, netns: str | None = None) -> dict[str, dict]:
    if netns is not None:
        return {}

    nodes: dict[str, dict] = {}
    for iface in list_table(_VSCTL, "Interface"):
        name = iface["name"]
        key = f"{scope}|ovsiface:{name}"
        nodes[key] = {
            "key": key,
            "kind": "ovs.iface",
            "scope": scope,
            "data": {
                "name": name,
                "type": iface["type"],
                "adminState": iface["admin_state"],
                "linkState": iface["link_state"],
                "ofport": iface["ofport"],
            },
        }
    return nodes
