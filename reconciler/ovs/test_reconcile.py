# reconciler/ovs/test_reconcile.py — stdlib unittest, no pytest, same
# reasoning as every other reconciler's test file in this package.
#
# Fixture is synthetic but shape-accurate, modeled on real `ovs-vsctl -f
# json list Interface` rows captured from the router (patch ports and a
# real internal bridge port were both present in the real capture).

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.ovs.reconcile")

FAKE_INTERFACES = [
    {
        "name": "patch-lsp-uplink-voda-modem-transfer-localnet-to-br-int",
        "type": "patch",
        "admin_state": "up",
        "link_state": "up",
        "ofport": 1,
    },
    {
        "name": "br-bd-4",
        "type": "internal",
        "admin_state": "down",
        "link_state": "down",
        "ofport": 65534,
    },
]


def _fake_list_table(argv, table, netns=None):
    assert argv == ["ovs-vsctl"]
    assert table == "Interface"
    return FAKE_INTERFACES


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "list_table", side_effect=_fake_list_table)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile("host:test", None)

    def test_no_facts_lost(self) -> None:
        self.assertEqual(len(self.nodes), len(FAKE_INTERFACES))

    def test_key_and_kind(self) -> None:
        node = self.nodes["host:test|ovsiface:br-bd-4"]
        self.assertEqual(node["kind"], "ovs.iface")
        self.assertEqual(node["scope"], "host:test")

    def test_type_and_state_carried_through(self) -> None:
        node = self.nodes["host:test|ovsiface:patch-lsp-uplink-voda-modem-transfer-localnet-to-br-int"]
        self.assertEqual(node["data"]["type"], "patch")
        self.assertEqual(node["data"]["adminState"], "up")
        self.assertEqual(node["data"]["linkState"], "up")
        self.assertEqual(node["data"]["ofport"], 1)

    def test_produces_nothing_for_a_real_namespace_pass(self) -> None:
        with mock.patch.object(mod, "list_table", side_effect=_fake_list_table):
            self.assertEqual(mod.reconcile("host:test", "ns-uplink-voda-avm"), {})


if __name__ == "__main__":
    unittest.main()
