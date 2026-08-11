# ladops/test_iptables.py — stdlib unittest, no pytest.
#
# The two-rule fixture is real shape, not invented — modeled line for
# line on `ip netns exec ns-uplink-voda-avm nft -j list ruleset`
# captured from the router: two MASQUERADE rules in the same table+
# chain, same action, differing only in `src`, both matching `-o
# ens18.1280`. The TCP dport rule has no real counterpart captured
# (every real rule is a bare MASQUERADE on src+oif) — it's synthetic,
# built to nft's documented JSON shape, to exercise the proto-from-
# sport/dport-match inference path at all.

from __future__ import annotations

import importlib
import json
import unittest
from unittest import mock

mod = importlib.import_module("ladops.iptables")

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
        # a non-ip/ip6 family table, confirms it's skipped rather than
        # crashing or producing a bogus fwrule kind
        {"table": {"family": "bridge", "name": "filter", "handle": 9}},
        {
            "rule": {
                "family": "bridge",
                "table": "filter",
                "chain": "FORWARD",
                "handle": 50,
                "expr": [{"accept": None}],
            }
        },
    ]
}


def _fake_run(argv, netns):
    assert argv == ["nft", "-j", "list", "ruleset"]
    return mock.Mock(stdout=json.dumps(FAKE_RULESET))


class ListRulesTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "run_in_netns", side_effect=_fake_run)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.rules = mod.list_rules(None)

    def test_only_ip_and_ip6_families_are_returned(self) -> None:
        self.assertEqual(len(self.rules), 4)
        self.assertTrue(all(r["family"] in ("ip", "ip6") for r in self.rules))

    def test_oif_and_src_are_captured_as_separate_fields(self) -> None:
        matches = [r for r in self.rules if r["action"] == "MASQUERADE" and r["family"] == "ip"]
        self.assertEqual(len(matches), 2)
        srcs = {r["fields"]["src"] for r in matches}
        self.assertEqual(srcs, {"10.99.0.64/28", "10.99.0.96/28"})
        self.assertTrue(all(r["fields"]["oif"] == "ens18.1280" for r in matches))

    def test_unmatched_dimensions_are_omitted_from_fields(self) -> None:
        rule = next(r for r in self.rules if r["fields"].get("src") == "10.99.0.64/28")
        for dimension in ("proto", "dst", "sport", "dport", "iif"):
            self.assertNotIn(dimension, rule["fields"])

    def test_order_is_zero_padded_and_per_table_chain(self) -> None:
        orders = sorted(
            r["order"] for r in self.rules if r["action"] == "MASQUERADE" and r["family"] == "ip"
        )
        self.assertEqual(orders, ["000", "001"])

    def test_dport_match_infers_proto_and_native_verdict_is_captured(self) -> None:
        rule = next(r for r in self.rules if r["action"] == "ACCEPT")
        self.assertEqual(rule["fields"]["dport"], "22")
        self.assertEqual(rule["fields"]["proto"], "tcp")

    def test_netns_is_passed_through_to_run(self) -> None:
        with mock.patch.object(mod, "run_in_netns", side_effect=_fake_run) as run:
            mod.list_rules("ns-uplink-voda-avm")
        run.assert_called_once_with(["nft", "-j", "list", "ruleset"], "ns-uplink-voda-avm")


if __name__ == "__main__":
    unittest.main()
