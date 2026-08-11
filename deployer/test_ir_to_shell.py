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
        data=pt.InfraHostData(connectAddress="10.0.0.1", ovnRole=pt.OvnRole.central, encapIp="10.0.0.1"),
    ),
    pt.InfraHostNode(
        id="host:chassis-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="chassis-1"),
        data=pt.InfraHostData(connectAddress="10.0.0.2", ovnRole=pt.OvnRole.chassis, encapIp="10.0.0.2"),
    ),
    pt.OvnLsNode(
        id="ls:home",
        kind="ovn.ls",
        key=pt.OvnLsKey(name="home"),
        data=pt.OvnLsData(
            interfaces=[
                pt.Interface(host="chassis-1", iface={"kind": "vlan", "vlanParent": "eth0", "vlanId": 129}),
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
        id="router:router-home|lrp:left",
        kind="ovn.lrp",
        key=pt.OvnLrpKey(router="router-home", side=pt.Side.left),
        data=pt.OvnLrpData(
            l2Segment="home",
            addresses=["192.168.1.1/24"],
            mac="00:00:00:00:01:01",
            gatewayChassis="chassis-1",
        ),
    ),
    pt.OvnLrpNode(
        id="router:router-home|lrp:right",
        kind="ovn.lrp",
        key=pt.OvnLrpKey(router="router-home", side=pt.Side.right),
        data=pt.OvnLrpData(
            l2Segment="backbone",
            addresses=["172.22.0.1/16"],
            mac="00:00:00:00:01:02",
            gatewayChassis=None,
        ),
    ),
]


def _replace_home_ls(nodes: list[pt.Model], **changes: object) -> list[pt.Model]:
    return [
        dataclasses.replace(n, **changes) if isinstance(n, pt.OvnLsNode) and n.key.name == "home" else n
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
        self.assertIn("ovn-nbctl lrp-set-gateway-chassis lrp-router-home-left chassis-1 100", self.cluster)
        self.assertNotIn("lrp-set-gateway-chassis lrp-router-home-right", self.cluster)

    def test_lrp_add_carries_mac_and_addresses(self) -> None:
        self.assertIn(
            "ovn-nbctl lrp-add router-home lrp-router-home-left 00:00:00:00:01:01 192.168.1.1/24",
            self.cluster,
        )

    def test_switch_side_lsp_bound_to_the_right_lrp(self) -> None:
        self.assertIn("ovn-nbctl lsp-add home lsp-router-home-left", self.cluster)
        self.assertIn("ovn-nbctl lsp-set-options lsp-router-home-left router-port=lrp-router-home-left", self.cluster)

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
        self.assertIn("ovn-nbctl lsp-set-options lsp-home-localnet network_name=net-home", self.cluster)
        self.assertNotIn("lsp-backbone-localnet", self.cluster)  # backbone has no real interfaces

    def test_vlan_created_and_brought_up_on_the_owning_host(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip link add link eth0 name eth0.129 type vlan id 129", script)
        self.assertIn("ip link set eth0.129 up", script)

    def test_bridge_created_and_port_attached_on_the_owning_host(self) -> None:
        # "home" is short enough that br-<domain> fits IFNAMSIZ as-is —
        # readable, not hashed (see _bridge_name's own doc comment).
        script = self.hosts["chassis-1"]
        self.assertIn("ovs-vsctl add-br br-home", script)
        self.assertIn("ovs-vsctl set bridge br-home fail-mode=standalone", script)
        self.assertIn("ovs-vsctl add-port br-home eth0.129", script)

    def test_bridge_mapping_set_on_the_owning_host(self) -> None:
        self.assertIn("external-ids:ovn-bridge-mappings=net-home:br-home", self.hosts["chassis-1"])

    def test_long_domain_name_falls_back_to_a_short_deterministic_hash(self) -> None:
        # Regression: "br-voda-modem-v2" (16 chars) really failed on a
        # live container with ofproto "Invalid argument" — IFNAMSIZ is
        # 15 usable characters. A domain name long enough to blow that
        # budget must still produce a short, valid bridge name — via
        # FNV-1a (deterministic: the same long name always yields the
        # same bridge, so a second create/delete pass still targets the
        # same real object), not a running counter.
        long_name = "voda-modem-v2-extremely-long-domain-name"
        nodes = _replace_home_ls(NODES, id=f"ls:{long_name}", key=pt.OvnLsKey(name=long_name))
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertNotIn(f"br-{long_name}", script)

        bridge_names = [
            line.split()[-1] for line in script.splitlines() if line.startswith("ovs-vsctl add-br ")
        ]
        self.assertEqual(len(bridge_names), 1)
        (bridge_name,) = bridge_names
        self.assertLessEqual(len(bridge_name), 15)

        # Same input, same output — regenerating the script must target
        # the same real bridge, not a fresh random/incrementing one.
        _, hosts_again = mod.build_scripts(nodes, "create")
        self.assertEqual(hosts["chassis-1"], hosts_again["chassis-1"])

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
        self.assertIn("ovs-vsctl remove open_vswitch . external-ids ovn-bridge-mappings", self.hosts["chassis-1"])


class UnsupportedIfaceKindTest(unittest.TestCase):
    def test_unsupported_kind_is_skipped_with_a_comment_not_crashed_on(self) -> None:
        nodes = _replace_home_ls(
            NODES,
            data=pt.OvnLsData(interfaces=[pt.Interface(host="chassis-1", iface={"kind": "dummy"})]),
        )
        cluster, hosts = mod.build_scripts(nodes, "create")
        self.assertIn("unsupported interface kind", hosts["chassis-1"])
        self.assertNotIn("add-br", hosts["chassis-1"])
        self.assertNotIn("lsp-home-localnet", cluster)


class NoCentralChassisTest(unittest.TestCase):
    def test_a_chassis_with_no_declared_central_raises(self) -> None:
        nodes = [n for n in NODES if not (isinstance(n, pt.InfraHostNode) and n.data.ovnRole == pt.OvnRole.central)]
        with self.assertRaises(ValueError):
            mod.build_scripts(nodes, "create")


class InvalidActionTest(unittest.TestCase):
    def test_rejects_an_unknown_action(self) -> None:
        with self.assertRaises(ValueError):
            mod.build_scripts(NODES, "destroy")


if __name__ == "__main__":
    unittest.main()
