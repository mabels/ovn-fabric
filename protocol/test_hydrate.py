# protocol/test_hydrate.py — stdlib unittest, no pytest. Exercises
# hydrate_node()/hydrate_nodes() against raw dicts shaped exactly like
# real `generate-ir` output (src/ir.ts) — the actual JSON boundary this
# module sits on, not protocol/generated.py's own dataclass API.
#
# hydrate.py is GENERIC (it walks generated.py's type hints), so these
# tests are what pin its rules: the per-kind classes below check that
# real-shaped nodes come out typed, GenericWalkTest checks the walker's
# own behaviour (strictness, unions, the keyword alias).

from __future__ import annotations

import importlib
import unittest
from typing import Any

from . import AppDhcpClient, AppDocker, AppWireguard, AppZerotier
from . import generated as pt

mod = importlib.import_module("protocol.hydrate")

RAW_INFRA_HOST: dict[str, Any] = {
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

RAW_OVN_LS: dict[str, Any] = {
    "id": "ls:home",
    "kind": "ovn.ls",
    "key": {"name": "home"},
    "data": {
        "interfaces": [
            {
                "host": "chassis-1",
                "iface": {
                    "kind": "vlan",
                    "vlanParent": {"kind": "physical", "name": "eth0"},
                    "vlanId": 129,
                    "bridge": "br-home",
                },
            },
        ],
    },
}

RAW_OVN_LRP: dict[str, Any] = {
    "id": "ovnrouter:router-home|lrp:lrp-home",
    "kind": "ovn.lrp",
    "key": {"ovnrouter": "router-home", "name": "lrp-home"},
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
        self.assertIsNone(node.data.ipfix)

    def test_ipfix_hydrates_to_the_typed_dataclass(self) -> None:
        # Flow export (2026-10-10): WHERE to export, host-level. Only
        # `targets` is required; the tunables stay None when omitted.
        raw = {
            **RAW_INFRA_HOST,
            "data": {
                **RAW_INFRA_HOST["data"],
                "ipfix": {"targets": "192.168.129.88:4739", "sampling": 512},
            },
        }
        node = mod.hydrate_node(raw)
        self.assertIsInstance(node.data.ipfix, pt.IpfixData)
        self.assertEqual(node.data.ipfix.targets, "192.168.129.88:4739")
        self.assertEqual(node.data.ipfix.sampling, 512)
        self.assertIsNone(node.data.ipfix.cacheActiveTimeout)
        self.assertIsNone(node.data.ipfix.cacheMaxFlows)


class HydrateOvnLsTest(unittest.TestCase):
    def test_produces_the_typed_dataclass_with_nested_interfaces(self) -> None:
        node = mod.hydrate_node(RAW_OVN_LS)
        self.assertIsInstance(node, pt.OvnLsNode)
        self.assertEqual(node.key.name, "home")
        self.assertEqual(len(node.data.interfaces), 1)
        self.assertIsInstance(node.data.interfaces[0], pt.OvnLsInterface)
        self.assertEqual(node.data.interfaces[0].host, "chassis-1")
        # The iface is typed too: the `kind` picks the OvnLsIface variant.
        iface = node.data.interfaces[0].iface
        self.assertIsInstance(iface, pt.OvnLsIfaceVlan)
        self.assertEqual(iface.vlanId, 129)
        self.assertEqual(iface.bridge, "br-home")
        self.assertIsNone(iface.flow)

    def test_an_iface_missing_its_bridge_name_raises(self) -> None:
        # ovn.ls bindings REQUIRE `bridge` (the OVS bridge) — the
        # deployer would otherwise name a bridge "None".
        raw_iface = {
            "kind": "vlan",
            "vlanParent": {"kind": "physical", "name": "eth0"},
            "vlanId": 129,
        }
        raw = {
            **RAW_OVN_LS,
            "data": {"interfaces": [{"host": "chassis-1", "iface": raw_iface}]},
        }
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(raw)
        self.assertIn("bridge", str(caught.exception))

    def test_an_unknown_iface_kind_raises(self) -> None:
        raw = {
            **RAW_OVN_LS,
            "data": {
                "interfaces": [
                    {"host": "chassis-1", "iface": {"kind": "bond", "bridge": "br-home"}}
                ]
            },
        }
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(raw)
        self.assertIn("kind='bond'", str(caught.exception))


class HydrateOvnLrTest(unittest.TestCase):
    def test_produces_the_typed_dataclass(self) -> None:
        node = mod.hydrate_node(
            {
                "id": "ovnrouter:router-home",
                "kind": "ovn.lr",
                "key": {"name": "router-home"},
                "data": {"options": {"mac_binding_age_threshold": 300}},
            }
        )
        self.assertIsInstance(node, pt.OvnLrNode)
        self.assertEqual(node.key.name, "router-home")
        self.assertEqual(node.data.options, pt.OvnLrOptions(mac_binding_age_threshold=300))

    def test_an_option_the_protocol_does_not_declare_raises(self) -> None:
        # `options` is a CLOSED shape: a typo'd or unknown OVN option can't
        # ride the IR through to `ovn-nbctl set`.
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(
                {
                    "id": "ovnrouter:router-home",
                    "kind": "ovn.lr",
                    "key": {"name": "router-home"},
                    "data": {"options": {"mac_binding_age_threshold": 300, "mac_binding_age": 5}},
                }
            )
        self.assertIn("mac_binding_age", str(caught.exception))

    def test_a_stringified_option_value_raises(self) -> None:
        with self.assertRaises(mod.IRShapeError):
            mod.hydrate_node(
                {
                    "id": "ovnrouter:router-home",
                    "kind": "ovn.lr",
                    "key": {"name": "router-home"},
                    "data": {"options": {"mac_binding_age_threshold": "300"}},
                }
            )


class HydrateOvnLrpTest(unittest.TestCase):
    def test_produces_the_typed_dataclass(self) -> None:
        node = mod.hydrate_node(RAW_OVN_LRP)
        self.assertIsInstance(node, pt.OvnLrpNode)
        self.assertEqual(node.key.ovnrouter, "router-home")
        self.assertEqual(node.key.name, "lrp-home")
        self.assertEqual(node.data.mac, "00:00:00:00:01:01")
        self.assertEqual(node.data.gatewayChassis, "host:chassis-1")

    def test_service_ref_mac_hydrates_and_defaults_to_none(self) -> None:
        # A workload NIC's MAC is resolved by the generator (src/ir.ts);
        # an IR written before that simply has none.
        raw = {
            **RAW_OVN_LRP,
            "data": {
                **RAW_OVN_LRP["data"],
                "serviceRefs": [
                    {
                        "service": "kernel.app.container:dns",
                        "name": "home",
                        "mac": "00:00:c0:a8:01:35",
                        "ipaddrs": ["192.168.1.53/24"],
                    },
                    {"service": "kernel.app.container:ntp", "name": "home"},
                ],
            },
        }
        refs = mod.hydrate_node(raw).data.serviceRefs
        self.assertEqual(refs[0].mac, "00:00:c0:a8:01:35")
        self.assertIsNone(refs[1].mac)

    def test_missing_mac_raises_instead_of_reaching_the_deployer(self) -> None:
        # The whole point of hydrating at the boundary: a stale IR JSON
        # (predating src/ir.ts resolving mac itself) fails loudly HERE,
        # not deep inside ir_common.py's script emission.
        raw = {**RAW_OVN_LRP, "data": {k: v for k, v in RAW_OVN_LRP["data"].items() if k != "mac"}}
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(raw)
        # The message names the node and the path of the missing field.
        self.assertIn("ovnrouter:router-home|lrp:lrp-home.data.mac", str(caught.exception))


class HydrateKernelRouterTest(unittest.TestCase):
    RAW_KERNEL_ROUTER_RIGHT: dict[str, Any] = {
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
                    "iface": {
                        "kind": "vlan",
                        "vlanParent": {"kind": "physical", "name": "eth0"},
                        "vlanId": 2280,
                    },
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
        iface = node.data.ifaces[0]
        # Whatever class the generated annotation names for this field —
        # the walker builds from the type hints, it picks no class itself.
        self.assertIsInstance(iface, pt.KernelRouterInterface)
        self.assertEqual(iface.host, "chassis-1")
        # The iface is typed too: a kernel-side vlan carries no bridge name.
        self.assertIsInstance(iface.iface, pt.KernelIfaceVlan)
        self.assertEqual(iface.iface.vlanId, 2280)

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
        self.assertIsNone(node.data.securityGroups)

    def test_security_group_ref_hydrates_when_present(self) -> None:
        raw = {
            **self.RAW_KERNEL_ROUTER_RIGHT,
            "data": {
                **self.RAW_KERNEL_ROUTER_RIGHT["data"],
                "securityGroups": [{"render": "kernel", "name": "sg-kernel-0"}],
            },
        }
        node = mod.hydrate_node(raw)
        self.assertEqual(node.data.securityGroups[0].name, "sg-kernel-0")

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
        app = node.data.apps[0]
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
        app = node.data.apps[0]
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
                        "securityGroups": [{"render": "kernel", "name": "masq-router"}],
                    }
                ],
            },
        }
        node = mod.hydrate_node(raw)
        app = node.data.apps[0]
        self.assertEqual(app.kind, "wireguard")
        self.assertEqual(app.ifaceName, "mullvad-de")
        self.assertEqual(app.config.privateKey, "k")
        self.assertEqual(app.config.peer.allowedIps, "0.0.0.0/0")
        self.assertEqual(app.securityGroups[0].name, "masq-router")


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


# The walker's own rules — independent of any one node kind.
class GenericWalkTest(unittest.TestCase):
    RAW_KERNEL_ROUTER: dict[str, Any] = {
        "id": "kernelrouter:kernel-0|side:right",
        "kind": "kernel.router",
        "key": {"name": "kernel-0", "side": "right"},
        "data": {"host": "host:chassis-1"},
    }

    def _with_apps(self, apps: list[dict[str, Any]]) -> dict[str, Any]:
        return {**self.RAW_KERNEL_ROUTER, "data": {"host": "host:chassis-1", "apps": apps}}

    def test_every_node_kind_of_the_generated_model_is_dispatchable(self) -> None:
        # The kind -> class table is read off generated.Model itself, so
        # a kind added to src/protocol.ts needs no edit here or in
        # hydrate.py.
        import typing

        classes = mod._node_classes()
        self.assertEqual(set(classes.values()), set(typing.get_args(pt.Model)))
        self.assertIn("ovn.lr", classes)
        self.assertIn("kernel.app.container", classes)

    def test_every_annotation_reachable_from_the_model_is_understood(self) -> None:
        # Walk the generated types themselves (no data): if
        # datamodel-codegen ever emits a shape the walker has no rule
        # for, this fails at build time instead of on the first IR that
        # happens to carry that field.
        import dataclasses
        import enum
        import types
        import typing

        seen: set[object] = set()

        def visit(tp: object) -> None:
            if tp in seen:
                return
            seen.add(tp)
            origin = typing.get_origin(tp)
            if origin in (typing.Union, types.UnionType, list, dict):
                for arg in typing.get_args(tp):
                    visit(arg)
            elif origin is typing.Literal or tp is typing.Any or tp is type(None):
                return
            elif dataclasses.is_dataclass(tp):
                for hint in typing.get_type_hints(tp).values():
                    visit(hint)
            elif isinstance(tp, type) and issubclass(tp, enum.Enum):
                return
            else:
                self.assertIn(tp, (str, bool, int, float), f"unsupported annotation {tp!r}")

        visit(pt.Model)
        self.assertIn(pt.DockerBuild, seen)

    def test_an_undeclared_field_raises_instead_of_being_dropped(self) -> None:
        # The failure the hand-written mapping could not have: data the
        # protocol does not declare is an error, not silently lost.
        raw = {**RAW_OVN_LRP, "data": {**RAW_OVN_LRP["data"], "notInTheProtocol": 1}}
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(raw)
        self.assertIn("notInTheProtocol", str(caught.exception))
        self.assertIn("OvnLrpData", str(caught.exception))

    def test_a_wrongly_typed_value_raises_with_its_path(self) -> None:
        raw = {**RAW_OVN_LRP, "data": {**RAW_OVN_LRP["data"], "addresses": "192.168.1.1/24"}}
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(raw)
        self.assertIn(".data.addresses: expected a list", str(caught.exception))

    def test_an_enum_value_outside_the_schema_raises(self) -> None:
        raw = {**self.RAW_KERNEL_ROUTER, "key": {"name": "kernel-0", "side": "middle"}}
        with self.assertRaises(mod.IRShapeError):
            mod.hydrate_node(raw)

    def test_an_explicit_null_for_an_optional_field_is_none(self) -> None:
        raw = {**RAW_OVN_LRP, "data": {**RAW_OVN_LRP["data"], "gatewayChassis": None}}
        self.assertIsNone(mod.hydrate_node(raw).data.gatewayChassis)

    def test_a_union_of_dataclasses_is_told_apart_by_its_literal_kind(self) -> None:
        node = mod.hydrate_node(
            self._with_apps(
                [
                    {"kind": "dhcp-client", "style": "dhcpcd"},
                    {
                        "kind": "zerotier",
                        "networkId": "02cfbec15c2319ff",
                        "instanceDir": "/var/lib/zt",
                        "securityGroups": [{"render": "kernel", "name": "masq-zt"}],
                        "routes": [{"dst": "192.168.0.0/16"}],
                    },
                    {
                        "kind": "wireguard",
                        "ifaceName": "wg0",
                        "securityGroups": [],
                        "config": {
                            "privateKey": "k",
                            "address": "10.64.0.2/32",
                            "peer": {
                                "publicKey": "p",
                                "endpoint": "e:1",
                                "allowedIps": "0.0.0.0/0",
                            },
                        },
                    },
                ]
            )
        )
        dhcp, zerotier, wireguard = node.data.apps
        self.assertIsInstance(dhcp, AppDhcpClient)
        self.assertIsInstance(zerotier, AppZerotier)
        self.assertIsInstance(wireguard, AppWireguard)
        # A field the hand-written mapping used to drop.
        self.assertEqual(zerotier.routes, [pt.KernelRouterRoute(dst="192.168.0.0/16")])
        self.assertEqual(zerotier.securityGroups[0].name, "masq-zt")

    def test_an_unknown_union_member_raises(self) -> None:
        with self.assertRaises(mod.IRShapeError) as caught:
            mod.hydrate_node(self._with_apps([{"kind": "openvpn"}]))
        self.assertIn("kind='openvpn'", str(caught.exception))

    def test_a_python_keyword_json_key_maps_to_its_suffixed_field(self) -> None:
        # "from" is a Python keyword — datamodel-codegen names the field
        # `from_`. Also a field the hand-written mapping dropped (a docker
        # APP's `build`), so the image was never auto-built.
        node = mod.hydrate_node(
            self._with_apps(
                [
                    {
                        "kind": "docker",
                        "image": "ovn-fabric-dhcpcd",
                        "name": "dhcpcd",
                        "build": {"from": "alpine:latest", "packages": ["dhcpcd"]},
                    }
                ]
            )
        )
        (app,) = node.data.apps
        self.assertIsInstance(app, AppDocker)
        self.assertEqual(app.build, pt.DockerBuild(from_="alpine:latest", packages=["dhcpcd"]))

    def test_a_union_of_primitives_keeps_the_value_as_it_came(self) -> None:
        # File.content is `str | list[str]`.
        raw = {
            "id": "kernel.app.container:dns",
            "kind": "kernel.app.container",
            "key": {"name": "dns"},
            "data": {
                "host": "host:chassis-1",
                "image": "ovn-fabric-dns",
                "files": [
                    {"path": "/etc/a", "content": "one string"},
                    {"path": "/etc/b", "content": ["line 1", "line 2"]},
                ],
            },
        }
        files = mod.hydrate_node(raw).data.files
        self.assertEqual(files[0].content, "one string")
        self.assertEqual(files[1].content, ["line 1", "line 2"])


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
