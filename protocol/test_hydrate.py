# protocol/test_hydrate.py — stdlib unittest, no pytest. Exercises
# hydrate_node()/hydrate_nodes() against raw dicts shaped exactly like
# real `generate-ir` output (src/ir.ts) — the actual JSON boundary this
# module sits on, not protocol/generated.py's own dataclass API.

from __future__ import annotations

import importlib
import unittest

from . import AppDhcpClient, AppDocker
from . import generated as pt

mod = importlib.import_module("protocol.hydrate")

RAW_INFRA_HOST = {
    "id": "host:central-1",
    "kind": "infra.host",
    "key": {"host": "central-1"},
    "data": {
        "connectAddress": "10.0.0.1",
        "ovnRole": "central",
        "encapIp": "10.0.0.1",
        "os": {"name": "ubuntu", "version": "26.04"},
    },
}

RAW_OVN_LS = {
    "id": "ls:home",
    "kind": "ovn.ls",
    "key": {"name": "home"},
    "data": {
        "interfaces": [
            {
                "host": "chassis-1",
                "iface": {
                    "kind": "vlan",
                    "vlanParent": "eth0",
                    "vlanId": 129,
                    "shortName": "br-home",
                },
            },
        ],
    },
}

RAW_OVN_LRP = {
    "id": "ovnrouter:router-home|lrp:left",
    "kind": "ovn.lrp",
    "key": {"ovnrouter": "router-home", "side": "left"},
    "data": {
        "l2Segment": "ls:home",
        "addresses": ["192.168.1.1/24"],
        "mac": "00:00:00:00:01:01",
        "gatewayChassis": "host:chassis-1",
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
        raw = {
            **RAW_INFRA_HOST,
            "data": {"connectAddress": "10.0.0.1", "os": {"name": "ubuntu", "version": "26.04"}},
        }
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
        self.assertEqual(node.data.interfaces[0].iface["shortName"], "br-home")


class HydrateOvnLrpTest(unittest.TestCase):
    def test_produces_the_typed_dataclass(self) -> None:
        node = mod.hydrate_node(RAW_OVN_LRP)
        self.assertIsInstance(node, pt.OvnLrpNode)
        self.assertEqual(node.key.ovnrouter, "router-home")
        self.assertEqual(node.key.side, pt.Side.left)
        self.assertEqual(node.data.mac, "00:00:00:00:01:01")
        self.assertEqual(node.data.gatewayChassis, "host:chassis-1")

    def test_missing_mac_raises_instead_of_reaching_the_deployer(self) -> None:
        # The whole point of hydrating at the boundary: a stale IR JSON
        # (predating src/ir.ts resolving mac itself) fails loudly HERE,
        # not deep inside ir_to_shell.py's script emission.
        raw = {**RAW_OVN_LRP, "data": {k: v for k, v in RAW_OVN_LRP["data"].items() if k != "mac"}}
        with self.assertRaises(KeyError):
            mod.hydrate_node(raw)


class HydrateKernelRouterTest(unittest.TestCase):
    RAW_KERNEL_ROUTER_RIGHT = {
        "id": "kernelrouter:kernel-0|side:right",
        "kind": "kernel.router",
        "key": {"name": "kernel-0", "side": "right"},
        "data": {
            "host": "host:chassis-1",
            "ipaddrs": ["192.168.132.93/24"],
            "routes": [{"dst": "0.0.0.0/0", "via": "192.168.132.1"}],
            "ifaces": [
                {
                    "host": "chassis-1",
                    "iface": {"kind": "vlan", "vlanParent": "eth0", "vlanId": 2280},
                },
            ],
        },
    }

    def test_right_side_ifaces_become_typed_interface_entries(self) -> None:
        node = mod.hydrate_node(self.RAW_KERNEL_ROUTER_RIGHT)
        self.assertIsInstance(node, pt.KernelRouterNode)
        self.assertEqual(node.key.name, "kernel-0")
        self.assertEqual(node.key.side, pt.Side.right)
        self.assertEqual(len(node.data.ifaces or []), 1)
        iface = node.data.ifaces[0]  # type: ignore[index]
        self.assertIsInstance(iface, pt.Interface)
        self.assertEqual(iface.host, "chassis-1")
        # iface itself stays a plain dict — InterfaceKind isn't modeled
        # in the cross-language protocol (see protocol.ts's own header).
        self.assertEqual(iface.iface["kind"], "vlan")
        self.assertEqual(iface.iface["vlanId"], 2280)

    def test_ifaces_default_to_none_when_absent(self) -> None:
        raw = {
            **self.RAW_KERNEL_ROUTER_RIGHT,
            "data": {
                k: v for k, v in self.RAW_KERNEL_ROUTER_RIGHT["data"].items() if k != "ifaces"
            },
        }
        node = mod.hydrate_node(raw)
        self.assertIsNone(node.data.ifaces)

    def test_security_group_ref_defaults_to_none_when_absent(self) -> None:
        node = mod.hydrate_node(self.RAW_KERNEL_ROUTER_RIGHT)
        self.assertIsNone(node.data.securityGroup)

    def test_security_group_ref_hydrates_when_present(self) -> None:
        raw = {
            **self.RAW_KERNEL_ROUTER_RIGHT,
            "data": {
                **self.RAW_KERNEL_ROUTER_RIGHT["data"],
                "securityGroup": "sg-kernel-0",
            },
        }
        node = mod.hydrate_node(raw)
        self.assertEqual(node.data.securityGroup, "sg-kernel-0")

    def test_apps_default_to_none_when_absent(self) -> None:
        node = mod.hydrate_node(self.RAW_KERNEL_ROUTER_RIGHT)
        self.assertIsNone(node.data.apps)

    def test_apps_hydrate_to_typed_app_entries(self) -> None:
        raw = {
            **self.RAW_KERNEL_ROUTER_RIGHT,
            "data": {
                **self.RAW_KERNEL_ROUTER_RIGHT["data"],
                "apps": [{"kind": "dhcp-client", "style": "dhclient"}],
            },
        }
        node = mod.hydrate_node(raw)
        self.assertEqual(len(node.data.apps or []), 1)
        app = node.data.apps[0]  # type: ignore[index]
        self.assertIsInstance(app, AppDhcpClient)
        self.assertEqual(app.kind, "dhcp-client")
        self.assertEqual(app.style, pt.Style.dhclient)

    def test_docker_app_hydrates_with_image_name_and_cmd(self) -> None:
        raw = {
            **self.RAW_KERNEL_ROUTER_RIGHT,
            "data": {
                **self.RAW_KERNEL_ROUTER_RIGHT["data"],
                "apps": [
                    {
                        "kind": "docker",
                        "image": "ubuntu",
                        "name": "kernel-0-test-docker",
                        "cmd": ["sleep", "86400"],
                        "ip": "10.200.0.2/24",
                        "routerIp": "10.200.0.1/24",
                        "vethName": "ve-12345678",
                    }
                ],
            },
        }
        node = mod.hydrate_node(raw)
        app = node.data.apps[0]  # type: ignore[index]
        self.assertIsInstance(app, AppDocker)
        self.assertEqual(app.kind, "docker")
        self.assertEqual(app.image, "ubuntu")
        self.assertEqual(app.name, "kernel-0-test-docker")
        self.assertEqual(app.cmd, ["sleep", "86400"])
        self.assertEqual(app.ip, "10.200.0.2/24")
        self.assertEqual(app.routerIp, "10.200.0.1/24")
        self.assertEqual(app.vethName, "ve-12345678")

    def test_wireguard_app_hydrates_with_config_and_masq(self) -> None:
        raw = {
            **self.RAW_KERNEL_ROUTER_RIGHT,
            "data": {
                **self.RAW_KERNEL_ROUTER_RIGHT["data"],
                "apps": [
                    {
                        "kind": "wireguard",
                        "ifaceName": "mullvad-de",
                        "config": {
                            "privateKey": "k",
                            "address": "10.64.56.207/32",
                            "peer": {
                                "publicKey": "p",
                                "allowedIps": "0.0.0.0/0",
                                "endpoint": "146.70.117.130:51820",
                            },
                        },
                        "masq": ["ipv4", "ipv6"],
                    }
                ],
            },
        }
        node = mod.hydrate_node(raw)
        app = node.data.apps[0]  # type: ignore[index]
        self.assertEqual(app.kind, "wireguard")
        self.assertEqual(app.ifaceName, "mullvad-de")
        self.assertEqual(app.config.privateKey, "k")
        self.assertEqual(app.config.peer.allowedIps, "0.0.0.0/0")
        self.assertEqual(app.masq, [pt.MasqEnum.ipv4, pt.MasqEnum.ipv6])


class HydrateSecurityGroupTest(unittest.TestCase):
    def test_produces_the_typed_dataclass_with_typed_rules(self) -> None:
        raw = {
            "id": "securitygroup:sg-kernel-0",
            "kind": "security.group",
            "key": {"name": "sg-kernel-0"},
            "data": {
                "rules": [
                    {"family": "ipv4", "kind": "masq"},
                    {"family": "ipv6", "kind": "masq"},
                ],
            },
        }
        node = mod.hydrate_node(raw)
        self.assertIsInstance(node, pt.SecurityGroupNode)
        self.assertEqual(node.key.name, "sg-kernel-0")
        self.assertEqual(
            [(r.family, r.kind) for r in node.data.rules],
            [(pt.Family.ipv4, "masq"), (pt.Family.ipv6, "masq")],
        )


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
