# deployer/test_ir_to_shell.py — stdlib unittest, no pytest, matching
# ladops's own test style. Fixtures are protocol.generated's typed
# dataclasses directly (InfraHostNode/OvnLsNode/OvnLrpNode) — the same
# shape a real run gets from protocol/hydrate.py, not raw dicts — so
# these tests exercise ir_to_shell.py exactly as deployer/cli.py
# actually calls it.
#
# Two hosts (one central, one chassis), two logical switches (one with
# a real VLAN interface bound on the chassis host, one with none), one
# router with a gateway-chassis pin on its left port and none on its
# right — small enough to hand-verify every generated line against, but
# exercising every branch build_scripts() has (create vs delete,
# central vs chassis host script, pinned vs unpinned LRP, bound vs
# unbound logical switch).
#
# Never runs anything for real — build_scripts() only ever returns
# text; nothing here mocks subprocess because nothing here calls it.

from __future__ import annotations

import dataclasses
import importlib
import unittest

from protocol import generated as pt

mod = importlib.import_module("deployer.ir_to_shell")

NODES: list[pt.Model] = [
    pt.InfraHostNode(
        id="host:central-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="central-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.1", ovnRole=pt.OvnRole.central, encapIp="10.0.0.1"
        ),
    ),
    pt.InfraHostNode(
        id="host:chassis-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="chassis-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.2", ovnRole=pt.OvnRole.chassis, encapIp="10.0.0.2"
        ),
    ),
    pt.OvnLsNode(
        id="ls:home",
        kind="ovn.ls",
        key=pt.OvnLsKey(name="home"),
        data=pt.OvnLsData(
            interfaces=[
                pt.Interface(
                    host="chassis-1",
                    iface={
                        "kind": "vlan",
                        "vlanParent": "eth0",
                        "vlanId": 129,
                        "shortName": "br-home",
                    },
                ),
            ],
        ),
    ),
    pt.OvnLsNode(
        id="ls:backbone",
        kind="ovn.ls",
        key=pt.OvnLsKey(name="backbone"),
        data=pt.OvnLsData(interfaces=[]),
    ),
    pt.OvnLrpNode(
        id="ovnrouter:router-home|lrp:left",
        kind="ovn.lrp",
        key=pt.OvnLrpKey(ovnrouter="router-home", side=pt.Side.left),
        data=pt.OvnLrpData(
            l2Segment="ls:home",
            addresses=["192.168.1.1/24"],
            mac="00:00:00:00:01:01",
            gatewayChassis="host:chassis-1",
        ),
    ),
    pt.OvnLrpNode(
        id="ovnrouter:router-home|lrp:right",
        kind="ovn.lrp",
        key=pt.OvnLrpKey(ovnrouter="router-home", side=pt.Side.right),
        data=pt.OvnLrpData(
            l2Segment="ls:backbone",
            addresses=["172.22.0.1/16"],
            mac="00:00:00:00:01:02",
            gatewayChassis=None,
        ),
    ),
    pt.KernelRouterNode(
        id="kernelrouter:kernel-0",
        kind="kernel.router",
        key=pt.KernelRouterKey(name="kernel-0", side=None),
        data=pt.KernelRouterData(host="host:chassis-1"),
    ),
    pt.KernelRouterNode(
        id="kernelrouter:kernel-0|side:left",
        kind="kernel.router",
        key=pt.KernelRouterKey(name="kernel-0", side=pt.Side.left),
        data=pt.KernelRouterData(host="host:chassis-1", ipaddrs=["10.99.0.2/28"]),
    ),
    pt.KernelRouterNode(
        id="kernelrouter:kernel-0|side:right",
        kind="kernel.router",
        key=pt.KernelRouterKey(name="kernel-0", side=pt.Side.right),
        data=pt.KernelRouterData(
            host="host:chassis-1",
            ipaddrs=["192.168.132.93/24"],
            routes=[pt.Route(dst="0.0.0.0/0", via="192.168.132.1")],
        ),
    ),
]


def _replace_home_ls(nodes: list[pt.Model], **changes: object) -> list[pt.Model]:
    return [
        (
            dataclasses.replace(n, **changes)
            if n.kind == "ovn.ls" and n.key.name == "home"
            else n
        )
        for n in nodes
    ]


class ClusterScriptCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, self.hosts = mod.build_scripts(NODES, "create")

    def test_no_may_exist_anywhere(self) -> None:
        self.assertNotIn("--may-exist", self.cluster)

    def test_ls_add_for_every_switch(self) -> None:
        self.assertIn("ovn-nbctl ls-add home", self.cluster)
        self.assertIn("ovn-nbctl ls-add backbone", self.cluster)

    def test_lr_add_exactly_once_for_a_two_sided_router(self) -> None:
        # Regression: the removed TypeScript generator called lr-add
        # once per endpoint (left AND right), which only worked because
        # --may-exist silently no-op'd the second call. Without it, a
        # second unconditional lr-add on the same router would error.
        self.assertEqual(self.cluster.count("ovn-nbctl lr-add router-home"), 1)

    def test_gateway_chassis_pin_only_on_the_pinned_side(self) -> None:
        self.assertIn(
            "ovn-nbctl lrp-set-gateway-chassis lrp-router-home-left chassis-1 100", self.cluster
        )
        self.assertNotIn("lrp-set-gateway-chassis lrp-router-home-right", self.cluster)

    def test_lrp_add_carries_mac_and_addresses(self) -> None:
        self.assertIn(
            "ovn-nbctl lrp-add router-home lrp-router-home-left 00:00:00:00:01:01 192.168.1.1/24",
            self.cluster,
        )

    def test_switch_side_lsp_bound_to_the_right_lrp(self) -> None:
        self.assertIn("ovn-nbctl lsp-add home lsp-router-home-left", self.cluster)
        self.assertIn(
            "ovn-nbctl lsp-set-options lsp-router-home-left router-port=lrp-router-home-left",
            self.cluster,
        )

    def test_one_host_script_per_infra_host_node(self) -> None:
        self.assertEqual(set(self.hosts), {"central-1", "chassis-1"})


class ClusterScriptDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, _ = mod.build_scripts(NODES, "delete")

    def test_lr_del_and_ls_del_use_if_exists(self) -> None:
        self.assertIn("ovn-nbctl --if-exists lr-del router-home", self.cluster)
        self.assertIn("ovn-nbctl --if-exists ls-del home", self.cluster)
        self.assertIn("ovn-nbctl --if-exists ls-del backbone", self.cluster)

    def test_no_lrp_add_or_lsp_add_in_delete_mode(self) -> None:
        self.assertNotIn("lrp-add", self.cluster)
        self.assertNotIn("lsp-add", self.cluster)


class HostScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.hosts = mod.build_scripts(NODES, "create")

    def test_central_exposes_nb_and_sb_connections(self) -> None:
        script = self.hosts["central-1"]
        self.assertIn("ovn-nbctl set-connection ptcp:6641:0.0.0.0", script)
        self.assertIn("ovn-sbctl set-connection ptcp:6642:0.0.0.0", script)
        self.assertIn("external-ids:ovn-remote=unix:/var/run/ovn/ovnsb_db.sock", script)

    def test_chassis_points_ovn_remote_at_the_central_encap_ip(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("external-ids:ovn-remote=tcp:10.0.0.1:6642", script)
        self.assertNotIn("set-connection", script)  # central-only

    def test_gateway_eligible_host_gets_cms_options(self) -> None:
        # chassis-1 is the gatewayChassis on lrp-router-home-left.
        self.assertIn("external-ids:ovn-cms-options=enable-chassis-as-gw", self.hosts["chassis-1"])

    def test_non_gateway_eligible_host_has_no_cms_options(self) -> None:
        self.assertNotIn("ovn-cms-options", self.hosts["central-1"])

    def test_both_hosts_get_encap_ip_and_type(self) -> None:
        for script in self.hosts.values():
            self.assertIn("external-ids:ovn-encap-type=geneve", script)
            self.assertIn("external-ids:ovn-encap-ip=", script)


class HostScriptDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.hosts = mod.build_scripts(NODES, "delete")

    def test_central_drops_its_connections(self) -> None:
        script = self.hosts["central-1"]
        self.assertIn("ovn-nbctl del-connection", script)
        self.assertIn("ovn-sbctl del-connection", script)

    def test_every_host_removes_its_external_ids(self) -> None:
        for script in self.hosts.values():
            self.assertIn("ovs-vsctl remove open_vswitch . external-ids ovn-remote", script)


class IfaceBindingCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, self.hosts = mod.build_scripts(NODES, "create")

    def test_localnet_lsp_emitted_only_for_the_bound_domain(self) -> None:
        self.assertIn("ovn-nbctl lsp-add home lsp-home-localnet", self.cluster)
        self.assertIn("ovn-nbctl lsp-set-type lsp-home-localnet localnet", self.cluster)
        self.assertIn("ovn-nbctl lsp-set-addresses lsp-home-localnet unknown", self.cluster)
        self.assertIn(
            "ovn-nbctl lsp-set-options lsp-home-localnet network_name=net-home", self.cluster
        )
        self.assertNotIn("lsp-backbone-localnet", self.cluster)  # backbone has no real interfaces

    def test_vlan_created_and_brought_up_on_the_owning_host(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip link add link eth0 name eth0.129 type vlan id 129", script)
        self.assertIn("ip link set eth0.129 up", script)

    def test_bridge_created_and_port_attached_on_the_owning_host(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ovs-vsctl add-br br-home", script)
        self.assertIn("ovs-vsctl set bridge br-home fail-mode=standalone", script)
        self.assertIn("ovs-vsctl add-port br-home eth0.129", script)

    def test_bridge_mapping_set_on_the_owning_host(self) -> None:
        self.assertIn("external-ids:ovn-bridge-mappings=net-home:br-home", self.hosts["chassis-1"])

    def test_uses_the_ir_supplied_short_iface_name_verbatim(self) -> None:
        # The IFNAMSIZ-safe fallback for an over-length domain name (was
        # "br-voda-modem-v2", 16 chars, really failing on a live
        # container with ofproto "Invalid argument") is now computed by
        # src/ir.ts's own shortIfaceName() — see src/ir_test.ts for that
        # regression. This module only ever consumes the already-
        # resolved value, verbatim, never re-derives it.
        nodes = _replace_home_ls(
            NODES,
            data=pt.OvnLsData(
                interfaces=[
                    pt.Interface(
                        host="chassis-1",
                        iface={
                            "kind": "vlan",
                            "vlanParent": "eth0",
                            "vlanId": 129,
                            "shortName": "br-8d8c0a55",
                        },
                    ),
                ],
            ),
        )
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ovs-vsctl add-br br-8d8c0a55", script)
        self.assertIn(
            "external-ids:ovn-bridge-mappings=net-home:br-8d8c0a55",
            script,
        )

    def test_no_binding_commands_on_a_host_with_no_interfaces(self) -> None:
        script = self.hosts["central-1"]
        self.assertNotIn("add-br", script)
        self.assertNotIn("ovn-bridge-mappings", script)

    def test_no_may_exist_in_binding_commands(self) -> None:
        self.assertNotIn("--may-exist", self.hosts["chassis-1"])


class IfaceBindingDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, self.hosts = mod.build_scripts(NODES, "delete")

    def test_no_localnet_lsp_add_in_delete_mode(self) -> None:
        # ls-del already cascades the localnet LSP away with the switch.
        self.assertNotIn("lsp-add", self.cluster)

    def test_del_br_uses_if_exists_and_cascades(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ovs-vsctl --if-exists del-br br-home", script)
        self.assertNotIn("del-port", script)  # del-br cascades its own ports

    def test_vlan_link_deleted(self) -> None:
        self.assertIn("ip link delete eth0.129", self.hosts["chassis-1"])

    def test_bridge_mapping_removed(self) -> None:
        self.assertIn(
            "ovs-vsctl remove open_vswitch . external-ids ovn-bridge-mappings",
            self.hosts["chassis-1"],
        )


class KernelRouterCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.hosts = mod.build_scripts(NODES, "create")

    def test_netns_created_once_on_the_owning_host(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertEqual(script.count("ip netns add ns-kernel-0"), 1)

    def test_no_kernel_router_content_on_a_different_host(self) -> None:
        script = self.hosts["central-1"]
        self.assertNotIn("ns-kernel-0", script)
        self.assertNotIn("dummy-", script)

    def test_dummy_device_created_outside_the_netns_then_moved_in(self) -> None:
        # `ip link add ... type dummy` must run in the global namespace
        # (a device is only visible to `ip link set <dev> netns <ns>`
        # from wherever it currently lives), not via `ip netns exec`.
        script = self.hosts["chassis-1"]
        self.assertIn("ip link add dummy-left type dummy", script)
        self.assertNotIn("ip netns exec ns-kernel-0 ip link add dummy-left", script)
        self.assertIn("ip link add dummy-right type dummy", script)
        self.assertNotIn("ip netns exec ns-kernel-0 ip link add dummy-right", script)

    def test_dummy_device_moved_into_the_netns_after_it_exists(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip link set dummy-left netns ns-kernel-0", script)
        self.assertIn("ip link set dummy-right netns ns-kernel-0", script)
        self.assertGreater(
            script.index("ip link add dummy-left type dummy"),
            script.index("ip netns add ns-kernel-0"),
        )
        self.assertGreater(
            script.index("ip link set dummy-left netns ns-kernel-0"),
            script.index("ip link add dummy-left type dummy"),
        )

    def test_addresses_assigned_on_the_matching_sides_own_dummy_device(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip netns exec ns-kernel-0 ip addr add 10.99.0.2/28 dev dummy-left", script)
        self.assertIn(
            "ip netns exec ns-kernel-0 ip addr add 192.168.132.93/24 dev dummy-right", script
        )

    def test_both_dummy_devices_brought_up(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip netns exec ns-kernel-0 ip link set dummy-left up", script)
        self.assertIn("ip netns exec ns-kernel-0 ip link set dummy-right up", script)

    def test_up_runs_before_address_assignment(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertLess(
            script.index("ip netns exec ns-kernel-0 ip link set dummy-left up"),
            script.index("ip netns exec ns-kernel-0 ip addr add 10.99.0.2/28 dev dummy-left"),
        )

    def test_route_with_a_real_nexthop_applied_on_its_own_side(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn(
            "ip netns exec ns-kernel-0 ip route add 0.0.0.0/0 dev dummy-right via 192.168.132.1",
            script,
        )

    def test_side_with_no_declared_routes_gets_no_route_command(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertNotIn("dev dummy-left via", script)
        # Exactly the one route declared on the right side, none invented
        # for the left side (which has no `routes` at all in the fixture).
        self.assertEqual(script.count("ip route add"), 1)


class KernelRouterDeleteTest(unittest.TestCase):
    def test_delete_only_removes_the_netns_not_the_devices_inside_it(self) -> None:
        _, hosts = mod.build_scripts(NODES, "delete")
        script = hosts["chassis-1"]
        self.assertIn("ip netns delete ns-kernel-0", script)
        self.assertNotIn("dummy-", script)
        self.assertNotIn("ip addr", script)
        self.assertNotIn("ip route", script)


class UnsupportedIfaceKindTest(unittest.TestCase):
    def test_unsupported_kind_is_skipped_with_a_comment_not_crashed_on(self) -> None:
        nodes = _replace_home_ls(
            NODES,
            data=pt.OvnLsData(
                interfaces=[pt.Interface(host="chassis-1", iface={"kind": "dummy"})],
            ),
        )
        cluster, hosts = mod.build_scripts(nodes, "create")
        self.assertIn("unsupported interface kind", hosts["chassis-1"])
        self.assertNotIn("add-br", hosts["chassis-1"])
        self.assertNotIn("lsp-home-localnet", cluster)


class NoCentralChassisTest(unittest.TestCase):
    def test_a_chassis_with_no_declared_central_raises(self) -> None:
        nodes = [
            n
            for n in NODES
            if not (n.kind == "infra.host" and n.data.ovnRole == pt.OvnRole.central)
        ]
        with self.assertRaises(ValueError):
            mod.build_scripts(nodes, "create")


class InvalidActionTest(unittest.TestCase):
    def test_rejects_an_unknown_action(self) -> None:
        with self.assertRaises(ValueError):
            mod.build_scripts(NODES, "destroy")


if __name__ == "__main__":
    unittest.main()
