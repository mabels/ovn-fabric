# reconciler/iptables/reconcile.py — shapes ladops.iptables's real rule
# facts into ipv4.fwrule/ipv6.fwrule IR nodes.
#
# Thin on purpose: every "how do I actually talk to nft" concern —
# running `nft -j list ruleset`, parsing its JSON — lives in
# ladops/iptables.py, not here. This module's only job is IR shaping:
# the canonical, self-typed `id` construction (matches docs/adr/0002-
# intermediate-representation.md, "Firewall / security-group
# subschema") from ladops's plain rule facts, plus a structured `key`
# (host/netns/table/chain/match-fields as real attributes, not a string
# to parse — see reconciler/linux_net/reconcile.py's header for why).
# `key` never includes "kind" (already the node's own top-level field);
# `id`'s own canonical-JSON local part still does, unchanged — that's a
# separate, pre-existing mechanism for flat-string uniqueness, not
# something "move kind out" touches.
#
# `order` is a plain zero-padded sequential index (rule position within
# table+chain, as ladops.iptables.list_rules() returns it, which
# reflects real application order) — not real fractional-indexing
# (base-62 midpoint generation, ADR's "Ordering" section). That
# algorithm exists to support stable insertion between two arbitrary
# existing keys; a reconciler only ever reports *observed* order, a
# strictly denser problem than what fractional-indexing solves, and
# pulling in `fractional-indexing`'s Python equivalent for that would be
# a real dependency this project hasn't decided it needs yet. Still a
# valid member of the "lexically comparable string" order space the ADR
# specifies.
#
# Namespace-scoped, unlike ovn/ovs: confirmed on the real router — the
# global namespace has zero rules, each ns-uplink-* has its own,
# independent MASQUERADE rules. reconcile(scope, netns) follows the
# same shape as reconciler/linux_net/reconcile.py for this reason.

from __future__ import annotations

import json

from ladops.iptables import list_rules
from ladops.netns import netns_scope, scope_id

_FAMILY_TO_KIND = {"ip": "ipv4.fwrule", "ip6": "ipv6.fwrule"}


def _canonical_key(shape: dict[str, str]) -> str:
    return json.dumps(shape, sort_keys=True, separators=(",", ":"))


def reconcile(scope: dict, netns: str | None = None) -> dict[str, dict]:
    ns_scope = netns_scope(scope, netns)
    ns_scope_id = scope_id(ns_scope)
    nodes: dict[str, dict] = {}
    for rule in list_rules(netns):
        # ladops.iptables.list_rules() already only returns ip/ip6
        # families, so this mapping is exhaustive for anything reaching
        # here — a KeyError would mean that contract broke, not a real
        # family to defensively skip.
        kind = _FAMILY_TO_KIND[rule["family"]]
        match_fields = {"table": rule["table"], "chain": rule["chain"], **rule["fields"]}
        local = _canonical_key({"kind": kind, **match_fields})
        id_ = f"{ns_scope_id}|{local}"
        nodes[id_] = {
            "id": id_,
            "kind": kind,
            "key": {**ns_scope, **match_fields},
            # match_fields are already in `key` (identity), repeated
            # here too so a diff/human doesn't have to cross-reference
            # key just to see what table/chain/src/... a rule matched.
            "data": {**match_fields, "action": rule["action"], "order": rule["order"]},
        }
    return nodes
