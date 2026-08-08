# ladops/iptables.py — `nft -j list ruleset` -> plain rule facts.
# Read-only for now: no add_*/remove_*/replace_* here. nft rule handles
# aren't stable identity (reassigned on reload), so there's no per-row
# write operation to build against the reconciler's own canonical key —
# see docs/adr/0002-intermediate-representation.md, "Firewall /
# security-group subschema". Whatever the real write primitive turns
# out to be (whole table+chain replace, most likely) is a decision for
# the deployer to make once it's actually being built, not something to
# speculatively design into this module first.
#
# `nft -j list ruleset`, not `iptables-save`/`ip6tables-save` text: the
# real router's rules are all `-A`-style additions, but its own
# `iptables-save` output header ("v1.8.11 (nf_tables)") confirms the
# kernel backend is actually nftables — and nftables has a real,
# documented JSON schema (nft(8), JSON OUTPUT section), covering both
# ip/ip6 families in one call, unlike text `iptables-save`/`ip6tables-
# save` which need two separate invocations and have no formal grammar
# at all beyond "whatever xtables prints."

from __future__ import annotations

import json
from typing import Any

from .netns import run as run_in_netns

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


def list_rules(netns: str | None) -> list[dict]:
    """Every real nat/filter/mangle rule in ip/ip6 families, in real
    application order (order is per table+chain, zero-padded — see
    reconciler/iptables/reconcile.py for why this isn't real
    fractional-indexing)."""
    out = run_in_netns(["nft", "-j", "list", "ruleset"], netns).stdout
    doc = json.loads(out)

    order_counters: dict[tuple[str, str, str], int] = {}
    rules = []
    for item in doc.get("nftables", []):
        rule = item.get("rule")
        if rule is None:
            continue
        family = rule["family"]
        if family not in ("ip", "ip6"):
            continue  # this project has no inet/bridge/netdev tables
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
