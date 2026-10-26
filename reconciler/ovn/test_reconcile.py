# reconciler/ovn/test_reconcile.py — stdlib unittest, no pytest.
#
# This module is a thin IR-shaping layer over ladops.ovn now — these
# tests mock ladops.ovn.list_lrps directly (the reconciler/ladops
# boundary) rather than `ovn-nbctl`'s JSON output, which
# ladops/test_ovn.py already covers on its own.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.ovn.reconcile")

FAKE_LRPS = [
    {
        "router": "router-home",
        "name": "lrp-home",
        "mac": "00:00:c0:a8:80:01",
        "networks": ["192.168.128.1/24", "fd00:192:168:128::1/64"],
        "gatewayChassis": ["effd37ab-685f-4c33-8c67-43017f4c7c52"],
        "ipv6RaConfigs": {},
        "external_ids": {},
    },
    {
        "router": "router-home",
        "name": "lrp-home-bb",
        "mac": "00:00:0a:50:08:01",
        "networks": ["10.80.8.1/16"],
        "gatewayChassis": ["effd37ab-685f-4c33-8c67-43017f4c7c52"],
        "ipv6RaConfigs": {},
        "external_ids": {},
    },
    {
        "router": "router-usa",
        "name": "lrp-usa",
        "mac": "00:00:c0:a8:83:01",
        "networks": ["192.168.131.1/24"],
        "gatewayChassis": [],
        "ipv6RaConfigs": {},
        "external_ids": {},
    },
]

FAKE_LSPS = [
    {
        "switch": "home",
        "name": "lsp-lrp-home",
        "type": "router",
        "addresses": [],
        "options": {"router-port": "lrp-home"},
        "external_ids": {},
    },
    {
        "switch": "home",
        "name": "lsp-lrp-home-bb",
        "type": "router",
        "addresses": [],
        "options": {"router-port": "lrp-home-bb"},
        "external_ids": {},
    },
    {
        "switch": "usa",
        "name": "lsp-lrp-usa",
        "type": "router",
        "addresses": [],
        "options": {"router-port": "lrp-usa"},
        "external_ids": {},
    },
]


SCOPE = {"host": "test"}


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patchers = [
            mock.patch.object(mod, "list_lrps", return_value=FAKE_LRPS),
            mock.patch.object(mod, "list_lsps", return_value=FAKE_LSPS),
            mock.patch.object(mod, "list_logical_switches", return_value=[]),
            mock.patch.object(mod, "list_logical_routers", return_value=[]),
            mock.patch.object(mod, "list_static_routes", return_value=[]),
        ]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
            patcher.start()
        self.nodes = mod.reconcile(SCOPE, None)

    def test_a_router_with_multiple_lrps_gets_one_node_per_port_not_one_collapsed_node(
        self,
    ) -> None:
        # this is the exact case that broke the ADR's literal
        # `router:<scope>|lrp` key (no per-port identity) — router-home
        # really does own two distinct LRPs.
        a = self.nodes["ovnrouter:router-home|lrp:lrp-home"]
        b = self.nodes["ovnrouter:router-home|lrp:lrp-home-bb"]
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["key"], b["key"])
        self.assertEqual(a["kind"], "ovn.lrp")
        self.assertEqual(b["kind"], "ovn.lrp")

    def test_key_is_the_owning_router_not_the_host(self) -> None:
        # confirms scope (host) is NOT threaded into ovn.lrp's key at
        # all — an ovn.lrp's real container is the OVN logical router it
        # belongs to, unrelated to the passed-in host scope.
        node = self.nodes["ovnrouter:router-usa|lrp:lrp-usa"]
        self.assertEqual(node["key"], {"ovnrouter": "router-usa", "name": "lrp-usa"})

    def test_networks_and_mac_and_gateway_chassis_carried_through(self) -> None:
        node = self.nodes["ovnrouter:router-home|lrp:lrp-home"]
        self.assertEqual(node["data"]["mac"], "00:00:c0:a8:80:01")
        self.assertEqual(
            node["data"]["addresses"],
            ["192.168.128.1/24", "fd00:192:168:128::1/64"],
        )
        self.assertEqual(node["data"]["l2Segment"], "ls:home")
        self.assertNotIn("gatewayChassis", node["data"])

    def test_produces_nothing_for_a_real_namespace_pass(self) -> None:
        with mock.patch.object(mod, "list_lrps", return_value=FAKE_LRPS):
            self.assertEqual(mod.reconcile(SCOPE, "ns-uplink-voda-avm"), {})


if __name__ == "__main__":
    unittest.main()
