# reconciler/iptables/test_reconcile.py — stdlib unittest, no pytest,
# same reasoning as every other reconciler's test file in this package.
#
# The two-rule fixture is real shape, not invented — modeled line for
# line on `ip netns exec ns-uplink-voda-avm nft -j list ruleset`
# captured from the router: two MASQUERADE rules in the same table+
# chain, same action, differing only in `src`, both matching `-o
# ens18.1280` — exactly the case that needs `oif` in the key (without
# it these would collide) and needs `order` to distinguish otherwise-
# identical-looking entries. The TCP dport rule has no real counterpart
# captured (every real rule is a bare MASQUERADE on src+oif) — it's
# synthetic, built to nft's documented JSON shape, to exercise the
# proto-from-sport/dport-match inference path at all.

from __future__ import annotations

import importlib
import json
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.iptables.reconcile")

FAKE_RULESET = {
    "nftables": [
        {"metainfo": {"version": "1.1.6", "json_schema_version": 1}},
        {"table": {"family": "ip", "name": "nat", "handle": 2}},
        {
            "chain": {
                "family": "ip",
                "table": "nat",
                "name": "POSTROUTING",
                "handle": 1,
                "type": "nat",
                "hook": "postrouting",
                "prio": 100,
                "policy": "accept",
            }
        },
        {
            "rule": {
                "family": "ip",
                "table": "nat",
                "chain": "POSTROUTING",
                "handle": 4,
                "expr": [
                    {
                        "match": {
                            "op": "==",
                            "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                            "right": {"prefix": {"addr": "10.99.0.64", "len": 28}},
                        }
                    },
                    {
                        "match": {
                            "op": "==",
                            "left": {"meta": {"key": "oifname"}},
                            "right": "ens18.1280",
                        }
                    },
                    {"counter": {"packets": 1555, "bytes": 275684}},
                    {"xt": {"type": "target", "name": "MASQUERADE"}},
                ],
            }
        },
        {
            "rule": {
                "family": "ip",
                "table": "nat",
                "chain": "POSTROUTING",
                "handle": 5,
                "expr": [
                    {
                        "match": {
                            "op": "==",
                            "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                            "right": {"prefix": {"addr": "10.99.0.96", "len": 28}},
                        }
                    },
                    {
                        "match": {
                            "op": "==",
                            "left": {"meta": {"key": "oifname"}},
                            "right": "ens18.1280",
                        }
                    },
                    {"counter": {"packets": 1081, "bytes": 189248}},
                    {"xt": {"type": "target", "name": "MASQUERADE"}},
                ],
            }
        },
        {"table": {"family": "ip6", "name": "nat", "handle": 3}},
        {
            "chain": {
                "family": "ip6",
                "table": "nat",
                "name": "POSTROUTING",
                "handle": 1,
                "type": "nat",
                "hook": "postrouting",
                "prio": 100,
                "policy": "accept",
            }
        },
        {
            "rule": {
                "family": "ip6",
                "table": "nat",
                "chain": "POSTROUTING",
                "handle": 2,
                "expr": [
                    {
                        "match": {
                            "op": "==",
                            "left": {"payload": {"protocol": "ip6", "field": "saddr"}},
                            "right": {"prefix": {"addr": "fd00:192:168:128::", "len": 64}},
                        }
                    },
                    {
                        "match": {
                            "op": "==",
                            "left": {"meta": {"key": "oifname"}},
                            "right": "ens18.1280",
                        }
                    },
                    {"counter": {"packets": 831726, "bytes": 0}},
                    {"xt": {"type": "target", "name": "MASQUERADE"}},
                ],
            }
        },
        # synthetic — no real rule like this was captured, exercises
        # dport-implies-proto inference (nft's own documented shape)
        {
            "rule": {
                "family": "ip",
                "table": "filter",
                "chain": "INPUT",
                "handle": 99,
                "expr": [
                    {
                        "match": {
                            "op": "==",
                            "left": {"payload": {"protocol": "tcp", "field": "dport"}},
                            "right": 22,
                        }
                    },
                    {"accept": None},
                ],
            }
        },
    ]
}


def _fake_run(argv, netns):
    assert argv == ["nft", "-j", "list", "ruleset"]
    return mock.Mock(stdout=json.dumps(FAKE_RULESET))


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "run_in_netns", side_effect=_fake_run)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile("host:test", None)

    def test_no_facts_lost(self) -> None:
        self.assertEqual(len(self.nodes), 4)

    def test_oif_distinguishes_otherwise_identical_looking_rules(self) -> None:
        # both real MASQUERADE rules share table/chain/action/oif and
        # differ only in src — confirms oif is in the key (or these two
        # sources would already collide) and that src itself is too.
        srcs = {n["scope"] for n in self.nodes.values()}
        self.assertEqual(srcs, {"host:test|netns:*global*"})
        matches = [n for n in self.nodes.values() if n["data"]["action"] == "MASQUERADE" and n["kind"] == "ipv4.fwrule"]
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(matches[0]["key"], matches[1]["key"])

    def test_ipv4_and_ipv6_rules_get_different_kinds_from_nft_family(self) -> None:
        kinds = {n["kind"] for n in self.nodes.values()}
        self.assertEqual(kinds, {"ipv4.fwrule", "ipv6.fwrule"})

    def test_order_reflects_position_within_table_and_chain(self) -> None:
        orders = sorted(
            n["data"]["order"] for n in self.nodes.values() if n["kind"] == "ipv4.fwrule" and n["data"]["action"] == "MASQUERADE"
        )
        self.assertEqual(orders, ["000", "001"])

    def test_prefix_match_formats_as_addr_slash_len(self) -> None:
        found = [k for k in self.nodes if '"src":"10.99.0.64/28"' in k]
        self.assertEqual(len(found), 1)

    def test_match_fields_are_duplicated_into_data_for_diffing_without_reparsing_the_key(self) -> None:
        node = next(n for n in self.nodes.values() if n["data"].get("src") == "10.99.0.64/28")
        self.assertEqual(
            node["data"],
            {
                "table": "nat",
                "chain": "POSTROUTING",
                "src": "10.99.0.64/28",
                "oif": "ens18.1280",
                "action": "MASQUERADE",
                "order": "000",
            },
        )

    def test_unmatched_dimensions_are_omitted_not_filled_with_a_wildcard_sentinel(self) -> None:
        node = next(n for n in self.nodes.values() if n["data"].get("src") == "10.99.0.64/28")
        for dimension in ("proto", "dst", "sport", "dport", "iif"):
            self.assertNotIn(dimension, node["data"])
            self.assertNotIn(f'"{dimension}"', node["key"])

    def test_dport_match_infers_proto_and_native_verdict_is_captured(self) -> None:
        node = next(n for n in self.nodes.values() if n["kind"] == "ipv4.fwrule" and n["data"]["action"] == "ACCEPT")
        self.assertIn('"dport":"22"', node["key"])
        self.assertIn('"proto":"tcp"', node["key"])

    def test_produces_correctly_scoped_keys_for_a_real_namespace(self) -> None:
        with mock.patch.object(mod, "run_in_netns", side_effect=_fake_run):
            nodes = mod.reconcile("host:test", "ns-uplink-voda-avm")
        scopes = {n["scope"] for n in nodes.values()}
        self.assertEqual(scopes, {"host:test|netns:ns-uplink-voda-avm"})


if __name__ == "__main__":
    unittest.main()
