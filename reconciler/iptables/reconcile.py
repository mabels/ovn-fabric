# reconciler/iptables/reconcile.py — `nft -j list ruleset` -> ipv4.fwrule/
# ipv6.fwrule IR nodes.
#
# `nft -j list ruleset`, not `iptables-save`/`ip6tables-save` text: the
# real router's rules are all `-A`-style additions, but its own
# `iptables-save` output header ("v1.8.11 (nf_tables)") confirms the
# kernel backend is actually nftables — and nftables has a real,
# documented JSON schema (nft(8), JSON OUTPUT section), covering both
# ip/ip6 families in one call, unlike text `iptables-save`/`ip6tables-
# save` which need two separate invocations and have no formal grammar
# at all beyond "whatever xtables prints."
#
# Matches docs/adr/0002-intermediate-representation.md, "Firewall /
# security-group subschema": identity is the canonical, self-typed
# match-selector tuple (JSON.stringify-with-sorted-keys over
# {kind, table, chain, proto, src, dst, sport, dport, iif, oif}), not a
# name and not the rule text. `iif`/`oif` extend the ADR's documented
# FwRuleKey shape (table/chain/proto/src/dst/sport/dport only) — every
# real MASQUERADE rule captured from the router matches on `-o
# <realIface>` as an essential part of what makes it a distinct rule
# (the same source CIDR gets separate rules per uplink), so omitting it
# would collide genuinely different rules onto the same key.
#
# A dimension with no real match on the rule is left out of the key/data
# entirely, not filled with a "*" sentinel — every FwRuleKey field is
# optional, not a required string. table/chain are the only fields every
# rule always carries (nft always attaches a rule to one).
#
# `order` is a plain zero-padded sequential index (rule position within
# table+chain, as returned by `nft -j list ruleset`, which reflects real
# application order) — not real fractional-indexing (base-62 midpoint
# generation, ADR's "Ordering" section). That algorithm exists to support
# stable insertion between two arbitrary existing keys; a reconciler only
# ever reports *observed* order, a strictly denser problem than what
# fractional-indexing solves, and pulling in `fractional-indexing`'s
# Python equivalent for that would be a real dependency this project
# hasn't decided it needs yet. Still a valid member of the "lexically
# comparable string" order space the ADR specifies.
#
# Namespace-scoped, unlike ovn/ovs: confirmed on the real router — the
# global namespace has zero rules, each ns-uplink-* has its own,
# independent MASQUERADE rules. reconcile(scope, netns) follows the
# same shape as reconciler/linux_net/reconcile.py for this reason.

from __future__ import annotations

import json
from typing import Any

from ..netns import netns_scope
from ..netns import run as run_in_netns

_FAMILY_TO_KIND = {"ip": "ipv4.fwrule", "ip6": "ipv6.fwrule"}

_PAYLOAD_FIELD_TO_KEY = {"saddr": "src", "daddr": "dst", "sport": "sport", "dport": "dport"}
_META_KEY_TO_KEY = {"oifname": "oif", "iifname": "iif"}

# native nftables verdicts, for rules authored directly in nft rather
# than translated from an `iptables -j <TARGET>` call — none of the real
# rules captured use this form (every real rule is xt-wrapped
# MASQUERADE), but nft's own JSON schema documents both shapes.
_NATIVE_VERDICTS = {
    "accept",
    "drop",
    "reject",
    "masquerade",
    "snat",
    "dnat",
    "redirect",
    "continue",
    "return",
    "jump",
    "goto",
}


def _canonical_key(shape: dict[str, str]) -> str:
    return json.dumps(shape, sort_keys=True, separators=(",", ":"))


def _format_right(right: Any) -> str:
    if isinstance(right, dict) and "prefix" in right:
        p = right["prefix"]
        return f"{p['addr']}/{p['len']}"
    return str(right)


def _parse_rule_expr(expr: list[dict]) -> tuple[dict[str, str], str]:
    # A dimension with no real match present is omitted, not filled with
    # a "*" sentinel — "*" would be one more magic string a real value
    # could (in principle) collide with, and every unset field would
    # otherwise still cost a key/data slot for a constraint that isn't
    # there. Omission is the more direct way to say "no constraint."
    fields: dict[str, str] = {}
    action = "*"
    for item in expr:
        match = item.get("match")
        if match is not None:
            left = match.get("left", {})
            payload = left.get("payload")
            meta = left.get("meta")
            if isinstance(payload, dict):
                mapped = _PAYLOAD_FIELD_TO_KEY.get(payload.get("field"))
                if mapped:
                    fields[mapped] = _format_right(match["right"])
                    # a sport/dport match's own "protocol" says which L4
                    # header the port field was read from (tcp/udp) —
                    # the only source of `proto` nft's JSON gives us,
                    # since these rules never match protocol directly.
                    if mapped in ("sport", "dport") and payload.get("protocol") in ("tcp", "udp"):
                        fields["proto"] = payload["protocol"]
            elif isinstance(meta, dict):
                mapped = _META_KEY_TO_KEY.get(meta.get("key"))
                if mapped:
                    fields[mapped] = _format_right(match["right"])
            continue
        xt = item.get("xt")
        if xt is not None and xt.get("type") == "target":
            action = xt["name"]
            continue
        verdict_keys = _NATIVE_VERDICTS & item.keys()
        if verdict_keys:
            action = next(iter(verdict_keys)).upper()
    return fields, action


def _parse_nft_ruleset(doc: dict) -> list[dict]:
    order_counters: dict[tuple[str, str, str], int] = {}
    rules = []
    for item in doc.get("nftables", []):
        rule = item.get("rule")
        if rule is None:
            continue
        family = rule["family"]
        table = rule["table"]
        chain = rule["chain"]
        fields, action = _parse_rule_expr(rule.get("expr", []))
        counter_key = (family, table, chain)
        idx = order_counters.get(counter_key, 0)
        order_counters[counter_key] = idx + 1
        rules.append(
            {
                "family": family,
                "table": table,
                "chain": chain,
                "fields": fields,
                "action": action,
                "order": f"{idx:03d}",
            }
        )
    return rules


def reconcile(scope: str, netns: str | None = None) -> dict[str, dict]:
    ns_scope = netns_scope(scope, netns)
    out = run_in_netns(["nft", "-j", "list", "ruleset"], netns).stdout
    doc = json.loads(out)

    nodes: dict[str, dict] = {}
    for rule in _parse_nft_ruleset(doc):
        kind = _FAMILY_TO_KIND.get(rule["family"])
        if kind is None:
            continue  # only ip/ip6 in scope — this project has no inet/bridge/netdev tables
        match_fields = {"table": rule["table"], "chain": rule["chain"], **rule["fields"]}
        local = _canonical_key({"kind": kind, **match_fields})
        key = f"{ns_scope}|{local}"
        nodes[key] = {
            "key": key,
            "kind": kind,
            "scope": ns_scope,
            # match_fields are already in the key (identity), repeated
            # here too so a diff/human doesn't have to re-parse the
            # canonical-key JSON string just to see what table/chain/
            # src/... a rule actually matched on.
            "data": {**match_fields, "action": rule["action"], "order": rule["order"]},
        }
    return nodes
