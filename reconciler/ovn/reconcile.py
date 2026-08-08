# reconciler/ovn/reconcile.py — ovn-nbctl-based reconciler: Logical_Router
# + Logical_Router_Port -> ovn.lrp IR nodes.
#
# OVN's northbound DB is one cluster-wide store, not namespace-scoped
# like addr/route/iptables are — reconcile(scope, netns) still takes the
# same (scope, netns) shape every reconciler does, so cli.py's
# orchestration loop can call every kind uniformly, but this only
# produces nodes on the netns=None (global) pass, same pattern as
# reconciler/host/reconcile.py.
#
# `ovn-nbctl -f json list <table>`, not `ovn-nbctl show` — `show`
# ignores --format entirely (confirmed against the real router: still
# prints its normal tree-text output with -f json passed), while `list`
# returns real self-describing structured data, decoded via the shared
# reconciler/ovsdb.py (the same OVSDB wire format reconciler/ovs uses).
#
# Key extends the ADR's node-kind table (`router:<scope>|lrp`) with the
# port's own name as the local identity (`router:<scope>|lrp:<port-
# name>`) — the table's literal key omits it, but a router legitimately
# owns more than one LRP (confirmed on the real router: router-home
# alone has 4 — lrp-home, lrp-home-bb, and two backbone-extra ports), so
# the bare form collides every LRP under the same router onto one key.
#
# Still deferred, matching the ADR's own "Consequences" section: only
# each router's LRPs are reconciled here, not logical switches/ports —
# those aren't in the node-kind table yet.

from __future__ import annotations

from ..ovsdb import list_table

_NBCTL = ["ovn-nbctl"]


def reconcile(scope: str, netns: str | None = None) -> dict[str, dict]:
    if netns is not None:
        return {}

    router_name_by_port_uuid: dict[str, str] = {}
    for router in list_table(_NBCTL, "Logical_Router"):
        for port_uuid in router["ports"]:
            router_name_by_port_uuid[port_uuid] = router["name"]

    nodes: dict[str, dict] = {}
    for port in list_table(_NBCTL, "Logical_Router_Port"):
        router_name = router_name_by_port_uuid.get(port["_uuid"])
        if router_name is None:
            continue  # an LRP not (yet) attached to any router — nothing to scope it under
        port_scope = f"router:{router_name}"
        key = f"{port_scope}|lrp:{port['name']}"
        nodes[key] = {
            "key": key,
            "kind": "ovn.lrp",
            "scope": port_scope,
            "data": {
                "name": port["name"],
                "mac": port["mac"],
                "networks": port["networks"],
                "gatewayChassis": port["gateway_chassis"],
            },
        }
    return nodes
