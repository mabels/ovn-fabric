# reconciler/iptables/test_reconcile.py — stdlib unittest, no pytest.
#
# This module is a thin IR-shaping layer over ladops.iptables now —
# these tests mock ladops.iptables.list_rules directly (the
# reconciler/ladops boundary) rather than `nft`'s JSON output, which
# ladops/test_iptables.py already covers on its own.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.iptables.reconcile")

SCOPE = {"host": "test"}

FAKE_RULES = [
    {
        "family": "ip",
        "table": "nat",
        "chain": "POSTROUTING",
        "fields": {"src": "10.99.0.64/28", "oif": "ens18.1280"},
        "action": "MASQUERADE",
        "order": "000",
    },
    {
        "family": "ip",
        "table": "nat",
        "chain": "POSTROUTING",
        "fields": {"src": "10.99.0.96/28", "oif": "ens18.1280"},
        "action": "MASQUERADE",
        "order": "001",
    },
    {
        "family": "ip6",
        "table": "nat",
        "chain": "POSTROUTING",
        "fields": {"src": "fd00:192:168:128::/64", "oif": "ens18.1280"},
        "action": "MASQUERADE",
        "order": "000",
    },
    {
        "family": "ip",
        "table": "filter",
        "chain": "INPUT",
        "fields": {"dport": "22", "proto": "tcp"},
        "action": "ACCEPT",
        "order": "000",
    },
]


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "list_rules", return_value=FAKE_RULES)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile(SCOPE, None)

    def test_no_facts_lost(self) -> None:
        self.assertEqual(len(self.nodes), 4)

    def test_oif_and_src_distinguish_otherwise_identical_looking_rules(self) -> None:
        keys = {(n["key"]["host"], n["key"]["netns"]) for n in self.nodes.values()}
        self.assertEqual(keys, {("test", "*global*")})
        matches = [n for n in self.nodes.values() if n["data"]["action"] == "MASQUERADE" and n["kind"] == "ipv4.fwrule"]
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(matches[0]["id"], matches[1]["id"])
        self.assertNotEqual(matches[0]["key"], matches[1]["key"])

    def test_ipv4_and_ipv6_rules_get_different_kinds_from_nft_family(self) -> None:
        kinds = {n["kind"] for n in self.nodes.values()}
        self.assertEqual(kinds, {"ipv4.fwrule", "ipv6.fwrule"})

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

    def test_key_carries_scope_and_match_fields_as_real_attributes_not_a_string(self) -> None:
        node = next(n for n in self.nodes.values() if n["data"].get("src") == "10.99.0.64/28")
        self.assertEqual(
            node["key"],
            {
                "host": "test",
                "netns": "*global*",
                "table": "nat",
                "chain": "POSTROUTING",
                "src": "10.99.0.64/28",
                "oif": "ens18.1280",
            },
        )
        self.assertNotIn("kind", node["key"])

    def test_unmatched_dimensions_are_omitted_not_filled_with_a_wildcard_sentinel(self) -> None:
        node = next(n for n in self.nodes.values() if n["data"].get("src") == "10.99.0.64/28")
        for dimension in ("proto", "dst", "sport", "dport", "iif"):
            self.assertNotIn(dimension, node["data"])
            self.assertNotIn(dimension, node["key"])

    def test_dport_and_proto_carried_through_into_the_key(self) -> None:
        node = next(n for n in self.nodes.values() if n["kind"] == "ipv4.fwrule" and n["data"]["action"] == "ACCEPT")
        self.assertEqual(node["key"]["dport"], "22")
        self.assertEqual(node["key"]["proto"], "tcp")

    def test_produces_correctly_scoped_nodes_for_a_real_namespace(self) -> None:
        with mock.patch.object(mod, "list_rules", return_value=FAKE_RULES):
            nodes = mod.reconcile(SCOPE, "ns-uplink-voda-avm")
        netns_values = {n["key"]["netns"] for n in nodes.values()}
        self.assertEqual(netns_values, {"ns-uplink-voda-avm"})


if __name__ == "__main__":
    unittest.main()
