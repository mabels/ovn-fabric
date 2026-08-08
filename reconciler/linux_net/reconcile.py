# reconciler/linux_net/reconcile.py — `ip -j link`/`ip -j addr`/`ip -j
# route` -> IR, for whichever namespace it's called for.
#
# reconcile(scope, netns) takes the namespace as an explicit argument —
# it does not enumerate `ip netns list` itself. That enumeration lives
# in exactly one place (reconciler/netns.py's list_netns(), driven by
# reconciler/cli.py's orchestration loop), because namespace context is
# not a linux_net-specific concept: iptables rules, and potentially ovs
# ports, vary by namespace the same way addr/route/link do. Baking the
# netns sweep into this one reconciler would mean reimplementing the
# same loop again in every other namespace-aware reconciler.
#
# net.iface (ip link show) is reconciled here alongside addr/route, not
# left out: addr/route only surface interfaces that happen to have an
# address or a route referencing them, so a bare veth end with nothing
# assigned yet would otherwise be invisible. Since a namespace holds
# nothing but `lo` until something is moved into it, "which net.iface
# nodes exist under this namespace's scope" *is* the fact of which
# devices were added to it from the global namespace.
#
# CLI-output parsing, not any internal/native API (netlink, etc.) —
# iptables has no clean structured API at all, so the ovs/ovn reconcilers
# will need CLI-output parsing regardless; this stays consistent with
# that rather than being netlink-native just because it could be.
#
# Plain dicts, not the generated dataclasses (reconciler/ir_types/) —
# that module is ephemeral, produced fresh at build time from ArkType's
# exported JSON Schema (see docs/adr/0002-intermediate-representation.md,
# "Reconciler/deployer runtime"), never hand-written or checked in. This
# reconciler works against the same plain JSON-shaped data either way;
# wiring it to import the generated types is a follow-up, not a
# prerequisite for it to function.

from __future__ import annotations

import json
from typing import Any

from ..netns import netns_scope
from ..netns import run as run_in_netns


def _run_ip_json(args: list[str], netns: str | None) -> Any:
    out = run_in_netns(["ip", "-j", *args], netns).stdout
    return json.loads(out)


def _addr_kind(value: str) -> str:
    # Safe here specifically because an address VALUE always contains a
    # real dotted-quad or colon-separated literal — unlike a route
    # prefix, it's never the bare, family-ambiguous string "default".
    return "ipv6.addr" if ":" in value else "ipv4.addr"


def _reconcile_links(ns_scope: str, nodes: dict[str, dict], netns: str | None) -> None:
    for entry in _run_ip_json(["link", "show"], netns):
        ifname = entry["ifname"]
        key = f"{ns_scope}|link:{ifname}"
        nodes[key] = {
            "key": key,
            "kind": "net.iface",
            "scope": ns_scope,
            "data": {
                "ifname": ifname,
                "linkType": entry.get("link_type", "unknown"),
                "operstate": entry.get("operstate", "UNKNOWN"),
            },
        }


def _reconcile_addrs(ns_scope: str, nodes: dict[str, dict], netns: str | None) -> None:
    # Deliberately unfiltered — every address including fe80:: link-local
    # ones is captured. The reconciler's job is a complete, faithful
    # record of what's real; deciding what's relevant (autoconfigured vs.
    # topology-managed) is the deployer's job, not this layer's.
    for entry in _run_ip_json(["addr", "show"], netns):
        ifname = entry["ifname"]
        for a in entry.get("addr_info", []):
            value = f"{a['local']}/{a['prefixlen']}"
            key = f"{ns_scope}|addr:{value}"
            nodes[key] = {
                "key": key,
                "kind": _addr_kind(value),
                "scope": ns_scope,
                "data": {"value": value, "owner": ifname, "role": "interface"},
            }


def _reconcile_routes(ns_scope: str, nodes: dict[str, dict], family: str, netns: str | None) -> None:
    # Family comes from which command produced the entry, not from
    # sniffing the value for ":" — the destination is literally the bare
    # string "default" for BOTH families (confirmed against a real
    # router's `ip route show`/`ip -6 route show`), so string-sniffing
    # silently mislabels every v6 default route as v4.
    #
    # The key includes both `kind` and `dev`, not just `prefix`:
    # - `dev`: the same destination (fe80::/64 being the common case,
    #   confirmed on a real router: one per interface, ~9 on a modest
    #   box) legitimately exists as a DIFFERENT route on each interface
    #   simultaneously. Keying by prefix alone collapsed 9 real, distinct
    #   facts into 1.
    # - `kind`: "default" is family-ambiguous as a bare string (no colon
    #   either way) and can appear on the SAME device in both the v4 and
    #   v6 route lists — confirmed on the same real router. Without kind
    #   in the key, those two still collide even with `dev` added.
    kind = "ipv6.route" if family == "-6" else "ipv4.route"
    args = ["-6", "route", "show"] if family == "-6" else ["route", "show"]
    for r in _run_ip_json(args, netns):
        prefix = r["dst"]
        dev = r.get("dev", "unknown")
        nexthop_ref = r.get("gateway") or dev
        key = f"{ns_scope}|route:{kind}:{dev}:{prefix}"
        nodes[key] = {
            "key": key,
            "kind": kind,
            "scope": ns_scope,
            "data": {"prefix": prefix, "dev": dev, "nexthopRef": nexthop_ref},
        }


def reconcile(scope: str, netns: str | None = None) -> dict[str, dict]:
    """Reconcile one namespace's real interfaces/addresses/routes into
    IR nodes — `netns=None` means the root/global namespace, scoped
    "*global*"; a real name means that `ip netns`, reached via `ip netns
    exec`. Keyed the same way as every other reconciler (see the ADR's
    node-kind table) — {scope}|netns:{name}|{kind}:{identity}."""
    ns_scope = netns_scope(scope, netns)
    nodes: dict[str, dict] = {}
    _reconcile_links(ns_scope, nodes, netns)
    _reconcile_addrs(ns_scope, nodes, netns)
    _reconcile_routes(ns_scope, nodes, "-4", netns)
    _reconcile_routes(ns_scope, nodes, "-6", netns)
    return nodes
