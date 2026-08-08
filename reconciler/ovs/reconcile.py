# reconciler/ovs/reconcile.py — shapes ladops.ovs's real interface facts
# into ovs.iface IR nodes.
#
# Thin on purpose: every "how do I actually talk to ovs-vsctl" concern
# lives in ladops/ovs.py, not here.
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

from ladops.netns import scope_id
from ladops.ovs import list_interfaces


def reconcile(scope: dict, netns: str | None = None) -> dict[str, dict]:
    if netns is not None:
        return {}

    nodes: dict[str, dict] = {}
    for iface in list_interfaces():
        id_ = f"{scope_id(scope)}|ovsiface:{iface['name']}"
        nodes[id_] = {
            "id": id_,
            "kind": "ovs.iface",
            "key": {**scope, "name": iface["name"]},
            "data": iface,
        }
    return nodes
