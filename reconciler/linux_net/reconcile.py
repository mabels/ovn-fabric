# reconciler/linux_net/reconcile.py — shapes ladops.linux_net's real
# link/addr/route facts into IR nodes, for whichever namespace it's
# called for.
#
# Every node has both an `id` (today's flat "host:X|netns:Y|kind:value"
# string — still what reconcile_all/_serialize dedup and sort by) and a
# `key` (a plain dict of the same identity's real attributes — host,
# netns, and whatever the kind-specific local identity is). `key` never
# includes "kind" — that's already the node's own top-level field, so
# repeating it inside key would just be the same duplication problem
# `id` already has (a fact appearing twice, once opaque-string-encoded,
# once structured) recurring one level down. The point of `key` existing
# at all is that nothing reading a node should ever have to parse `id`
# as a string to recover host/netns/value — real router output doing
# exactly that (a raw addr node with only a string key) is what this
# shape replaced.
#
# Thin on purpose: every "how do I actually talk to `ip`" concern —
# running commands, parsing JSON, building add/remove argv — lives in
# ladops/linux_net.py, not here. This module's only job is IR shaping:
# computing each node's id/key/kind from ladops's plain facts. The
# deployer will import ladops.linux_net directly for its write side
# (add_addr/delete_addr/add_route/delete_route); it has no reason to go
# through this reconciler module at all, since IR-node shaping is a
# reconciler-only concern.
#
# reconcile(scope, netns) takes the namespace as an explicit argument —
# it does not enumerate `ip netns list` itself. That enumeration lives
# in exactly one place (ladops/netns.py's list_netns(), driven by
# reconciler/cli.py's orchestration loop), because namespace context is
# not a linux_net-specific concept: iptables rules vary by namespace the
# same way addr/route/link do. Baking the netns sweep into this one
# reconciler would mean reimplementing the same loop again elsewhere.
#
# net.iface (ip link show) is reconciled here alongside addr/route, not
# left out: addr/route only surface interfaces that happen to have an
# address or a route referencing them, so a bare veth end with nothing
# assigned yet would otherwise be invisible. Since a namespace holds
# nothing but `lo` until something is moved into it, "which net.iface
# nodes exist under this namespace's scope" *is* the fact of which
# devices were added to it from the global namespace.
#
# net.netns is the namespace itself, one node per namespace (including
# the global one), data.interfaces a roster of the same ifnames already
# reconciled as individual net.iface nodes above — not a second real
# query. `ip netns` itself has no subcommand that reports a given
# namespace's interface membership (confirmed: `ip netns list` only
# lists names/ids); the roster is derived from the same list_links(netns)
# call net.iface already makes, not asked for separately. Each entry
# carries peerInOtherNetns too (see ladops/linux_net.py's list_links())
# — the real, verifiable signal for "this device has a link to another
# namespace," not just "isn't lo".
#
# Plain dicts, not the generated dataclasses (reconciler/ir_types/) —
# that module is ephemeral, produced fresh at build time from ArkType's
# exported JSON Schema (see docs/adr/0002-intermediate-representation.md,
# "Reconciler/deployer runtime"), never hand-written or checked in.

from __future__ import annotations

from ladops.linux_net import list_addrs, list_links, list_routes
from ladops.netns import netns_scope, scope_id


def _addr_kind(family: str) -> str:
    return "ipv6.addr" if family == "ipv6" else "ipv4.addr"


def _route_kind(family: str) -> str:
    return "ipv6.route" if family == "ipv6" else "ipv4.route"


def reconcile(scope: dict, netns: str | None = None) -> dict[str, dict]:
    """Reconcile one namespace's real interfaces/addresses/routes into
    IR nodes — `netns=None` means the root/global namespace, scoped
    "*global*"; a real name means that `ip netns`, reached via `ip netns
    exec`. `id` is keyed the same way as every other reconciler (see the
    ADR's node-kind table) — {scope}|netns:{name}|{kind}:{identity}."""
    ns_scope = netns_scope(scope, netns)
    ns_scope_id = scope_id(ns_scope)
    nodes: dict[str, dict] = {}

    links = list_links(netns)
    for link in links:
        id_ = f"{ns_scope_id}|link:{link['ifname']}"
        nodes[id_] = {
            "id": id_,
            "kind": "net.iface",
            "key": {**ns_scope, "ifname": link["ifname"]},
            "data": link,
        }

    # The namespace itself, id == its own scope (same pattern as
    # infra.host, whose id is likewise exactly its scope) — no further
    # local identity needed since the namespace *is* the scope.
    nodes[ns_scope_id] = {
        "id": ns_scope_id,
        "kind": "net.netns",
        "key": {**ns_scope},
        "data": {
            "interfaces": sorted(
                (
                    {"ifname": link["ifname"], "peerInOtherNetns": link["peerInOtherNetns"]}
                    for link in links
                ),
                key=lambda entry: entry["ifname"],
            )
        },
    }

    for addr in list_addrs(netns):
        id_ = f"{ns_scope_id}|addr:{addr['addr']}"
        nodes[id_] = {
            "id": id_,
            "kind": _addr_kind(addr["family"]),
            "key": {**ns_scope, "addr": addr["addr"]},
            "data": {"addr": addr["addr"], "interface": addr["interface"], "role": "interface"},
        }

    for family in ("-4", "-6"):
        for route in list_routes(netns, family):
            # `id` includes both `kind` and `dev`, not just `prefix`:
            # - `dev`: the same destination (fe80::/64 being the common
            #   case, confirmed on a real router: one per interface, ~9
            #   on a modest box) legitimately exists as a DIFFERENT
            #   route on each interface simultaneously.
            # - `kind`: "default" is family-ambiguous as a bare string
            #   and can appear on the SAME device in both the v4 and v6
            #   route lists — confirmed on the same real router.
            kind = _route_kind(route["family"])
            id_ = f"{ns_scope_id}|route:{kind}:{route['dev']}:{route['prefix']}"
            nodes[id_] = {
                "id": id_,
                "kind": kind,
                "key": {**ns_scope, "dev": route["dev"], "prefix": route["prefix"]},
                "data": {
                    "prefix": route["prefix"],
                    "dev": route["dev"],
                    "nexthopRef": route["nexthop"],
                },
            }

    return nodes
