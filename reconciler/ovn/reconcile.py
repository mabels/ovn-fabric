# reconciler/ovn/reconcile.py — shapes ladops.ovn's real LRP facts into
# ovn.lrp IR nodes.
#
# Thin on purpose: every "how do I actually talk to ovn-nbctl" concern —
# running `-f json list <table>`, decoding OVSDB atoms, joining a port
# back to its owning router — lives in ladops/ovn.py, not here. This
# module's only job is IR shaping.
#
# OVN's northbound DB is one cluster-wide store, not namespace-scoped
# like addr/route/iptables are — reconcile(scope, netns) still takes the
# same (scope, netns) shape every reconciler does, so cli.py's
# orchestration loop can call every kind uniformly, but this only
# produces nodes on the netns=None (global) pass, same pattern as
# reconciler/host/reconcile.py.
#
# `id` extends the ADR's node-kind table (`ovnrouter:<scope>|lrp`) with
# the port's own name as the local identity (`ovnrouter:<scope>|lrp:
# <port-name>`) — the table's literal key omits it, but a router
# legitimately owns more than one LRP (confirmed on the real router:
# router-home alone has 4 — lrp-home, lrp-home-bb, and two
# backbone-extra ports), so the bare form collides every LRP under the
# same router onto one key.
#
# `key` is scoped by the owning OVN logical router (`{"ovnrouter":
# name}`, named precisely — not the bare "router" — once KernelRouter
# (types.ts) made "router" ambiguous between an OVN router and a real
# Linux netns; matches src/protocol.ts's OvnLrpKey/RouteKey, confirmed
# live 2026-08-12), not by `scope`/host — an ovn.lrp's real container is
# the router it belongs to, not the physical host (this deployment only
# has one physical host, but OVN's own NB DB isn't host-partitioned the
# way addr/route/iptables state is). `scope` is accepted only for
# calling-convention uniformity with every other reconciler; unused
# here, same as before this envelope redesign.
#
# Still deferred, matching the ADR's own "Consequences" section: only
# each router's LRPs are reconciled here, not logical switches/ports —
# those aren't in the node-kind table yet.

from __future__ import annotations

from ladops.ovn import list_lrps


def reconcile(scope: dict, netns: str | None = None) -> dict[str, dict]:
    if netns is not None:
        return {}

    nodes: dict[str, dict] = {}
    for lrp in list_lrps():
        id_ = f"ovnrouter:{lrp['router']}|lrp:{lrp['name']}"
        nodes[id_] = {
            "id": id_,
            "kind": "ovn.lrp",
            "key": {"ovnrouter": lrp["router"], "name": lrp["name"]},
            "data": {
                "name": lrp["name"],
                "mac": lrp["mac"],
                "networks": lrp["networks"],
                "gatewayChassis": lrp["gatewayChassis"],
            },
        }
    return nodes
