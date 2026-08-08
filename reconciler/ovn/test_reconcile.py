# reconciler/ovn/test_reconcile.py — stdlib unittest, no pytest, same
# reasoning as every other reconciler's test file in this package.
#
# Fixtures are synthetic but shape-accurate, modeled directly on real
# `ovn-nbctl -f json list Logical_Router`/`Logical_Router_Port` output
# captured from the router (docs/adr/0002-intermediate-representation.md
# doesn't include this reconciler's design yet, but the real headings/
# row shape were confirmed live): router-home legitimately owns more
# than one LRP, the exact case that broke the ADR's literal
# `router:<scope>|lrp` key (no per-port identity).

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.ovn.reconcile")

FAKE_ROUTERS = [
    {
        "_uuid": "r-home",
        "name": "router-home",
        "ports": ["lrp-home-uuid", "lrp-home-bb-uuid"],
    },
    {
        "_uuid": "r-usa",
        "name": "router-usa",
        "ports": ["lrp-usa-uuid"],
    },
]

FAKE_PORTS = [
    {
        "_uuid": "lrp-home-uuid",
        "name": "lrp-home",
        "mac": "00:00:c0:a8:80:01",
        "networks": ["192.168.128.1/24", "fd00:192:168:128::1/64"],
        "gateway_chassis": ["effd37ab-685f-4c33-8c67-43017f4c7c52"],
    },
    {
        "_uuid": "lrp-home-bb-uuid",
        "name": "lrp-home-bb",
        "mac": "00:00:0a:50:08:01",
        "networks": ["10.80.8.1/16"],
        "gateway_chassis": ["effd37ab-685f-4c33-8c67-43017f4c7c52"],
    },
    {
        "_uuid": "lrp-usa-uuid",
        "name": "lrp-usa",
        "mac": "00:00:c0:a8:83:01",
        "networks": ["192.168.131.1/24"],
        "gateway_chassis": [],
    },
    {
        "_uuid": "lrp-orphan-uuid",
        "name": "lrp-orphan",
        "mac": "00:00:00:00:00:00",
        "networks": [],
        "gateway_chassis": [],
    },
]


def _fake_list_table(argv, table, netns=None):
    assert argv == ["ovn-nbctl"]
    if table == "Logical_Router":
        return FAKE_ROUTERS
    if table == "Logical_Router_Port":
        return FAKE_PORTS
    raise AssertionError(f"unexpected table: {table}")


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "list_table", side_effect=_fake_list_table)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile("host:test", None)

    def test_a_router_with_multiple_lrps_gets_one_node_per_port_not_one_collapsed_node(self) -> None:
        # this is the exact case that broke the ADR's literal
        # `router:<scope>|lrp` key (no per-port identity) — router-home
        # really does own two distinct LRPs.
        a = self.nodes["router:router-home|lrp:lrp-home"]
        b = self.nodes["router:router-home|lrp:lrp-home-bb"]
        self.assertNotEqual(a["key"], b["key"])
        self.assertEqual(a["kind"], "ovn.lrp")
        self.assertEqual(b["kind"], "ovn.lrp")

    def test_scope_is_the_owning_router_not_the_host(self) -> None:
        node = self.nodes["router:router-usa|lrp:lrp-usa"]
        self.assertEqual(node["scope"], "router:router-usa")

    def test_networks_and_mac_and_gateway_chassis_carried_through(self) -> None:
        node = self.nodes["router:router-home|lrp:lrp-home"]
        self.assertEqual(node["data"]["mac"], "00:00:c0:a8:80:01")
        self.assertEqual(node["data"]["networks"], ["192.168.128.1/24", "fd00:192:168:128::1/64"])
        self.assertEqual(node["data"]["gatewayChassis"], ["effd37ab-685f-4c33-8c67-43017f4c7c52"])

    def test_a_port_not_attached_to_any_router_is_skipped_not_crashed_on(self) -> None:
        self.assertEqual(len(self.nodes), 3)
        self.assertFalse(any("orphan" in key for key in self.nodes))

    def test_produces_nothing_for_a_real_namespace_pass(self) -> None:
        with mock.patch.object(mod, "list_table", side_effect=_fake_list_table):
            self.assertEqual(mod.reconcile("host:test", "ns-uplink-voda-avm"), {})


if __name__ == "__main__":
    unittest.main()
