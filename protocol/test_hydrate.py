# protocol/test_hydrate.py — stdlib unittest, no pytest. Exercises
# hydrate_node()/hydrate_nodes() against raw dicts shaped exactly like
# real `generate-ir` output (src/ir.ts) — the actual JSON boundary this
# module sits on, not protocol/generated.py's own dataclass API.

from __future__ import annotations

import importlib
import unittest

from . import generated as pt

mod = importlib.import_module("protocol.hydrate")

RAW_INFRA_HOST = {
    "id": "host:central-1",
    "kind": "infra.host",
    "key": {"host": "central-1"},
    "data": {"connectAddress": "10.0.0.1", "ovnRole": "central", "encapIp": "10.0.0.1"},
}

RAW_OVN_LS = {
    "id": "ls:home",
    "kind": "ovn.ls",
    "key": {"name": "home"},
    "data": {
        "interfaces": [
            {"host": "chassis-1", "iface": {"kind": "vlan", "vlanParent": "eth0", "vlanId": 129}},
        ],
    },
}

RAW_OVN_LRP = {
    "id": "router:router-home|lrp:left",
    "kind": "ovn.lrp",
    "key": {"router": "router-home", "side": "left"},
    "data": {
        "l2Segment": "home",
        "addresses": ["192.168.1.1/24"],
        "mac": "00:00:00:00:01:01",
        "gatewayChassis": "chassis-1",
    },
}


class HydrateInfraHostTest(unittest.TestCase):
    def test_produces_the_typed_dataclass(self) -> None:
        node = mod.hydrate_node(RAW_INFRA_HOST)
        self.assertIsInstance(node, pt.InfraHostNode)
        self.assertEqual(node.key.host, "central-1")
        self.assertEqual(node.data.connectAddress, "10.0.0.1")
        self.assertEqual(node.data.ovnRole, pt.OvnRole.central)
        self.assertEqual(node.data.encapIp, "10.0.0.1")

    def test_optional_fields_default_to_none_when_absent(self) -> None:
        raw = {**RAW_INFRA_HOST, "data": {"connectAddress": "10.0.0.1"}}
        node = mod.hydrate_node(raw)
        self.assertIsNone(node.data.ovnRole)
        self.assertIsNone(node.data.encapIp)


class HydrateOvnLsTest(unittest.TestCase):
    def test_produces_the_typed_dataclass_with_nested_interfaces(self) -> None:
        node = mod.hydrate_node(RAW_OVN_LS)
        self.assertIsInstance(node, pt.OvnLsNode)
        self.assertEqual(node.key.name, "home")
        self.assertEqual(len(node.data.interfaces), 1)
        self.assertIsInstance(node.data.interfaces[0], pt.Interface)
        self.assertEqual(node.data.interfaces[0].host, "chassis-1")
        # iface itself stays a plain dict — InterfaceKind isn't modeled
        # in the cross-language protocol (see protocol.ts's own header).
        self.assertEqual(node.data.interfaces[0].iface["kind"], "vlan")


class HydrateOvnLrpTest(unittest.TestCase):
    def test_produces_the_typed_dataclass(self) -> None:
        node = mod.hydrate_node(RAW_OVN_LRP)
        self.assertIsInstance(node, pt.OvnLrpNode)
        self.assertEqual(node.key.router, "router-home")
        self.assertEqual(node.key.side, pt.Side.left)
        self.assertEqual(node.data.mac, "00:00:00:00:01:01")
        self.assertEqual(node.data.gatewayChassis, "chassis-1")

    def test_missing_mac_raises_instead_of_reaching_the_deployer(self) -> None:
        # The whole point of hydrating at the boundary: a stale IR JSON
        # (predating src/ir.ts resolving mac itself) fails loudly HERE,
        # not deep inside ir_to_shell.py's script emission.
        raw = {**RAW_OVN_LRP, "data": {k: v for k, v in RAW_OVN_LRP["data"].items() if k != "mac"}}
        with self.assertRaises(KeyError):
            mod.hydrate_node(raw)


class HydrateUnknownKindTest(unittest.TestCase):
    def test_an_unrecognized_kind_raises_a_clear_error(self) -> None:
        raw = {"id": "x:1", "kind": "net.segment", "key": {}, "data": {}}
        with self.assertRaises(ValueError) as ctx:
            mod.hydrate_node(raw)
        self.assertIn("net.segment", str(ctx.exception))


class HydrateNodesTest(unittest.TestCase):
    def test_hydrates_a_mixed_list_in_order(self) -> None:
        nodes = mod.hydrate_nodes([RAW_INFRA_HOST, RAW_OVN_LS, RAW_OVN_LRP])
        self.assertEqual([type(n) for n in nodes], [pt.InfraHostNode, pt.OvnLsNode, pt.OvnLrpNode])


if __name__ == "__main__":
    unittest.main()
