from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.ovs.reconcile")

FAKE_PORTS = [
    {
        "bridge": "br-upl",
        "port": "ovnkern.2280",
        "name": "ovnkern.2280",
        "type": "",
        "mac": "",
        "options": {},
    },
    {
        "bridge": "br-upl",
        "port": "veth-ovn-1",
        "name": "veth-ovn-1",
        "type": "veth",
        "mac": "02:00:00:00:00:01",
        "options": {"peer": "veth-krn-1"},
    },
    {
        "bridge": "br-home",
        "port": "veth-ovn-2",
        "name": "veth-ovn-2",
        "type": "veth",
        "mac": "02:00:00:00:00:02",
        "options": {"peer": "veth-krn-2"},
    },
]

SCOPE = {"host": "test"}


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patchers = [
            mock.patch.object(mod, "list_bridge_ports", return_value=FAKE_PORTS),
            mock.patch.object(mod, "list_bridge_mappings", return_value={"br-home": "net-home"}),
            mock.patch.object(mod, "find_veth_peer", return_value=None),
        ]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
            patcher.start()
        self.nodes = mod.reconcile(SCOPE, None)

    def test_host_only_bridge_is_a_bridge_node_with_its_ports(self) -> None:
        node = self.nodes["bridge:br-upl"]
        self.assertEqual(node["kind"], "bridge")
        self.assertEqual(node["data"]["host"], "host:test")
        self.assertEqual(len(node["data"]["ports"]), 2)
        vlan = node["data"]["ports"][0]["iface"]
        self.assertEqual(vlan["kind"], "vlan")
        self.assertEqual(vlan["vlanParent"], {"kind": "physical", "name": "ovnkern"})
        self.assertEqual(vlan["vlanId"], 2280)
        veth = node["data"]["ports"][1]["iface"]
        self.assertEqual(veth["kind"], "veth")
        self.assertEqual(veth["peerName"], "veth-krn-1")

    def test_mapped_bridge_is_an_ovn_ls(self) -> None:
        node = self.nodes["ls:home"]
        self.assertEqual(node["kind"], "ovn.ls")
        self.assertEqual(node["key"], {"name": "home"})
        self.assertEqual(node["data"]["interfaces"][0]["host"], "test")

    def test_veth_pairs_emit_only_one_side(self) -> None:
        # veth-krn-1/2 are the peers; they must not become nodes of their own.
        self.assertNotIn("net.iface:veth-krn-1", self.nodes)
        self.assertNotIn("net.iface:veth-krn-2", self.nodes)

    def test_produces_nothing_for_a_real_namespace_pass(self) -> None:
        self.assertEqual(mod.reconcile(SCOPE, "ns-uplink-voda-avm"), {})


if __name__ == "__main__":
    unittest.main()
