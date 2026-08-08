# reconciler/ovs/test_reconcile.py — stdlib unittest, no pytest.
#
# This module is a thin IR-shaping layer over ladops.ovs now — these
# tests mock ladops.ovs.list_interfaces directly (the reconciler/ladops
# boundary) rather than `ovs-vsctl`'s JSON output, which
# ladops/test_ovs.py already covers on its own.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.ovs.reconcile")

FAKE_INTERFACES = [
    {
        "name": "patch-lsp-uplink-voda-modem-transfer-localnet-to-br-int",
        "type": "patch",
        "adminState": "up",
        "linkState": "up",
        "ofport": 1,
    },
    {
        "name": "br-bd-4",
        "type": "internal",
        "adminState": "down",
        "linkState": "down",
        "ofport": 65534,
    },
]


SCOPE = {"host": "test"}


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "list_interfaces", return_value=FAKE_INTERFACES)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile(SCOPE, None)

    def test_no_facts_lost(self) -> None:
        self.assertEqual(len(self.nodes), len(FAKE_INTERFACES))

    def test_id_kind_and_key(self) -> None:
        node = self.nodes["host:test|ovsiface:br-bd-4"]
        self.assertEqual(node["id"], "host:test|ovsiface:br-bd-4")
        self.assertEqual(node["kind"], "ovs.iface")
        self.assertEqual(node["key"], {"host": "test", "name": "br-bd-4"})

    def test_type_and_state_carried_through(self) -> None:
        node = self.nodes["host:test|ovsiface:patch-lsp-uplink-voda-modem-transfer-localnet-to-br-int"]
        self.assertEqual(node["data"]["type"], "patch")
        self.assertEqual(node["data"]["adminState"], "up")
        self.assertEqual(node["data"]["linkState"], "up")
        self.assertEqual(node["data"]["ofport"], 1)

    def test_produces_nothing_for_a_real_namespace_pass(self) -> None:
        with mock.patch.object(mod, "list_interfaces", return_value=FAKE_INTERFACES):
            self.assertEqual(mod.reconcile(SCOPE, "ns-uplink-voda-avm"), {})


if __name__ == "__main__":
    unittest.main()
