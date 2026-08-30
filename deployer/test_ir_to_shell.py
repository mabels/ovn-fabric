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

import contextlib
import dataclasses
import importlib
import io
import os
import shlex
import tempfile
import unittest
from unittest import mock

from protocol import AppDhcpClient, AppDocker, AppWireguard, AppZerotier
from protocol import generated as pt

mod = importlib.import_module("deployer.ir_to_shell")

NODES: list[pt.Model] = [
    pt.InfraHostNode(
        id="host:central-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="central-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.1",
            ovnRole=pt.OvnRole.central,
            encapIp="10.0.0.1",
            os=pt.Os(name="ubuntu", version="26.04"),
        ),
    ),
    pt.InfraHostNode(
        id="host:chassis-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="chassis-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.2",
            ovnRole=pt.OvnRole.chassis,
            encapIp="10.0.0.2",
            os=pt.Os(name="ubuntu", version="26.04"),
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
            ifaces=[
                pt.Interface(
                    host="chassis-1",
                    iface={"kind": "vlan", "vlanParent": "eth0", "vlanId": 2280},
                ),
            ],
        ),
    ),
]


def _replace_home_ls(nodes: list[pt.Model], **changes: object) -> list[pt.Model]:
    return [
        (dataclasses.replace(n, **changes) if n.kind == "ovn.ls" and n.key.name == "home" else n)
        for n in nodes
    ]


# The one per-router up/down script body (the literal shell file the host
# create pass writes via `cat > /usr/local/sbin/ovn-kernel-<router>.sh <<
# 'OVN'`). The host create script embeds BOTH branches, so its content is
# the single source of truth for what `systemctl start` (up) and
# `systemctl stop` (down) will do. Ends at the first line exactly `OVN`;
# the router script's own nested app heredocs use the distinct `OVNKERNEL`
# delimiter, so they never trip this scan.
def _router_script(host_script: str, router: str = "kernel-0") -> str:
    lines = host_script.splitlines()
    marker = f"cat > /usr/local/sbin/ovn-kernel-{router}.sh << 'OVN'"
    start = next(i for i, l in enumerate(lines) if l == marker)
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "OVN")
    return "\n".join(lines[start + 1 : end])


# Split a router script body into its `up)` and `down)` branch bodies.
def _router_branches(router_script: str) -> tuple[str, str]:
    lines = router_script.splitlines()
    up_start = lines.index("up)")
    up_end = lines.index(";;", up_start)
    down_start = lines.index("down)", up_end)
    down_end = lines.index(";;", down_start)
    return "\n".join(lines[up_start + 1 : up_end]), "\n".join(lines[down_start + 1 : down_end])


# The command lines a host script RUNS directly, excluding every file it
# writes (heredocs: `cat > path << 'DELIM'` through the closing `DELIM`)
# and the `#`/`set ` envelope — the Python front-end's run_cmd() argv
# must match exactly these (file writes are write_file(), not commands).
def _shell_command_lines(script: str) -> list[list[str]]:
    commands: list[list[str]] = []
    in_heredoc: str | None = None
    for line in script.splitlines():
        if in_heredoc is not None:
            if line == in_heredoc:
                in_heredoc = None
            continue
        stripped = line.strip()
        if stripped.startswith("cat > "):
            in_heredoc = stripped.split("'")[1]
            continue
        if line and not line.startswith("#") and not line.startswith("set "):
            commands.append(shlex.split(line))
    return commands


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

    def test_veth_root_leg_brought_up_before_joining_the_bridge(self) -> None:
        # A transit domain's ovn.ls carries the veth pair; the kernel
        # router block brings the netns-side leg up, but the ROOT leg
        # stays DOWN until told otherwise (confirmed live, 2026-08-19) —
        # so the binding block brings it up before it becomes a bridge
        # port.
        nodes = NODES + [
            pt.OvnLsNode(
                id="ls:transit-router-wan",
                kind="ovn.ls",
                key=pt.OvnLsKey(name="transit-router-wan"),
                data=pt.OvnLsData(
                    interfaces=[
                        pt.Interface(
                            host="chassis-1",
                            iface={
                                "kind": "veth",
                                "ifaceName": "veth-krn-9adb1d",
                                "peerName": "veth-ovn-9adb1d",
                                "shortName": "br-1f5b95ad",
                            },
                        ),
                    ],
                ),
            ),
        ]
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ip link set veth-ovn-9adb1d up", script)
        self.assertLess(
            script.index("ip link set veth-ovn-9adb1d up"),
            script.index("ovs-vsctl add-port br-1f5b95ad veth-ovn-9adb1d"),
        )

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

    def test_router_script_chmodded_executable_before_enable(self) -> None:
        # The router script is written via heredoc (non-executable), but
        # the unit's ExecStart runs it directly — without `chmod +x`
        # systemd aborts with 203/EXEC "Permission denied" (hit live
        # 2026-08-30).
        script = self.hosts["chassis-1"]
        self.assertIn("chmod +x /usr/local/sbin/ovn-kernel-kernel-0.sh", script)
        self.assertLess(
            script.index("chmod +x /usr/local/sbin/ovn-kernel-kernel-0.sh"),
            script.index("systemctl enable --now ovn-kernel-kernel-0.service"),
        )

    def test_ipv4_and_ipv6_forwarding_enabled_in_the_netns(self) -> None:
        # A kernel router IS a router — the netns's own forwarding knobs
        # are what make Linux actually forward between its two sides
        # (2026-08-19).
        script = self.hosts["chassis-1"]
        self.assertIn("ip netns exec ns-kernel-0 sysctl -w net.ipv4.ip_forward=1", script)
        self.assertIn(
            "ip netns exec ns-kernel-0 sysctl -w net.ipv6.conf.all.forwarding=1",
            script,
        )
        self.assertLess(
            script.index("ip netns exec ns-kernel-0 sysctl -w net.ipv4.ip_forward=1"),
            script.index("ip link add dummy-left type dummy"),
        )

    def test_no_kernel_router_content_on_a_different_host(self) -> None:
        script = self.hosts["central-1"]
        self.assertNotIn("ns-kernel-0", script)
        self.assertNotIn("dummy-", script)

    def test_devices_created_outside_the_netns_then_moved_in(self) -> None:
        # The real VLAN (`right`) and the dummy stand-in (`left`) are
        # both created in the global namespace (a device is only visible
        # to `ip link set <dev> netns <ns>` from wherever it currently
        # lives), not via `ip netns exec`.
        script = self.hosts["chassis-1"]
        self.assertIn("ip link add link eth0 name eth0.2280 type vlan id 2280", script)
        self.assertNotIn("ip netns exec ns-kernel-0 ip link add link eth0", script)
        self.assertIn("ip link add dummy-left type dummy", script)
        self.assertNotIn("ip netns exec ns-kernel-0 ip link add dummy-left", script)
        self.assertNotIn("ip link add dummy-right type dummy", script)

    def test_devices_moved_into_the_netns_after_it_exists(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip link set eth0.2280 netns ns-kernel-0", script)
        self.assertIn("ip link set dummy-left netns ns-kernel-0", script)
        self.assertGreater(
            script.index("ip link add link eth0 name eth0.2280 type vlan id 2280"),
            script.index("ip netns add ns-kernel-0"),
        )
        self.assertGreater(
            script.index("ip link set eth0.2280 netns ns-kernel-0"),
            script.index("ip link add link eth0 name eth0.2280 type vlan id 2280"),
        )
        self.assertGreater(
            script.index("ip link set dummy-left netns ns-kernel-0"),
            script.index("ip link add dummy-left type dummy"),
        )

    def test_addresses_assigned_on_the_matching_sides_own_device(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip netns exec ns-kernel-0 ip addr add 10.99.0.2/28 dev dummy-left", script)
        self.assertIn(
            "ip netns exec ns-kernel-0 ip addr add 192.168.132.93/24 dev eth0.2280", script
        )

    def test_both_devices_brought_up(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ip netns exec ns-kernel-0 ip link set dummy-left up", script)
        self.assertIn("ip netns exec ns-kernel-0 ip link set eth0.2280 up", script)

    def test_up_runs_before_address_assignment(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertLess(
            script.index("ip netns exec ns-kernel-0 ip link set dummy-left up"),
            script.index("ip netns exec ns-kernel-0 ip addr add 10.99.0.2/28 dev dummy-left"),
        )

    def test_route_with_a_real_nexthop_applied_on_its_own_side(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn(
            "ip netns exec ns-kernel-0 ip route add 0.0.0.0/0 dev eth0.2280 via 192.168.132.1",
            script,
        )

    def test_side_with_no_declared_routes_gets_no_route_command(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertNotIn("dev dummy-left via", script)
        # Exactly the one route declared on the right side, none invented
        # for the left side (which has no `routes` at all in the fixture).
        self.assertEqual(script.count("ip route add"), 1)

    def test_veth_created_for_a_side_with_a_veth_iface(self) -> None:
        # The transit leg (`left`) is a veth pair: created in the root
        # namespace (a device is only visible to `ip link set <dev>
        # netns <ns>` from wherever it currently lives), then the netns-
        # side leg is moved in and the addresses bind to it.
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=pt.KernelRouterData(
                        host="host:chassis-1",
                        ipaddrs=["10.99.0.2/28"],
                        ifaces=[
                            pt.Interface(
                                host="chassis-1",
                                iface={
                                    "kind": "veth",
                                    "ifaceName": "veth-krn-0",
                                    "peerName": "veth-ovn-0",
                                    "shortName": "0",
                                },
                            ),
                        ],
                    ),
                )
                if n.kind == "kernel.router" and n.key.side == pt.Side.left
                else n
            )
            for n in NODES
        ]
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ip link add veth-ovn-0 type veth peer name veth-krn-0", script)
        self.assertNotIn("ip netns exec ns-kernel-0 ip link add veth-ovn-0", script)
        self.assertIn("ip link set veth-krn-0 netns ns-kernel-0", script)
        self.assertIn("ip netns exec ns-kernel-0 ip addr add 10.99.0.2/28 dev veth-krn-0", script)
        self.assertNotIn("ip link add dummy-left type dummy", script)

    def test_masq_services_emit_security_group_rules_on_the_wan_iface(self) -> None:
        # A kernelRouterEndpoint's kernel.* masq services (define.ts)
        # implicitly generate an implementation-abstract `security.group`
        # node, attached to the kernel router's right (WAN) side via
        # data.securityGroup. The deployer applies those rules INSIDE the
        # netns on that side's real interface — a plain `-A` add, same as
        # every other create emitter (no `-C || -A` shell conditional:
        # the generated Python deployer executes lines via shlex.split()
        # as argv, so `2>/dev/null`/`||` would reach iptables as literal
        # arguments — confirmed live 2026-08-23).
        nodes = [
            pt.SecurityGroupNode(
                id="securitygroup:sg-kernel-0",
                kind="security.group",
                key=pt.SecurityGroupKey(name="sg-kernel-0"),
                data=pt.SecurityGroupData(
                    rules=[
                        pt.Rule(family=pt.Family.ipv4, kind="masq"),
                        pt.Rule(family=pt.Family.ipv6, kind="masq"),
                    ]
                ),
            ),
            *[
                (
                    dataclasses.replace(
                        n,
                        data=dataclasses.replace(n.data, securityGroup="sg-kernel-0"),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ],
        ]
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn(
            "ip netns exec ns-kernel-0 iptables -t nat -A POSTROUTING -o eth0.2280 -j MASQUERADE",
            script,
        )
        self.assertIn(
            "ip netns exec ns-kernel-0 ip6tables -t nat -A POSTROUTING -o eth0.2280 -j MASQUERADE",
            script,
        )

    def test_no_security_group_attached_emits_no_nat_commands(self) -> None:
        # The base fixture has no security.group attached — a side must
        # opt in via data.securityGroup, nothing is emitted by default.
        _, hosts = mod.build_scripts(NODES, "create")
        script = hosts["chassis-1"]
        self.assertNotIn("MASQUERADE", script)
        self.assertNotIn("iptables", script)

    def test_kernel_app_dhcp_client_deploys_systemd_unit_on_create_and_delete(self) -> None:
        # `kernel.app.dhcp-client` resolves to a KernelApp descriptor on
        # the right side (define.ts). The deployer turns it into a real
        # systemd unit (Restart=always) supervising the client inside the
        # netns — installed + started on create; stopped/disabled/removed
        # on delete, BEFORE `ip netns delete`, since an app daemon holds
        # its netns alive past deletion (2026-08-23).

        def with_app(style: str) -> list[pt.Model]:
            return [
                (
                    dataclasses.replace(
                        n,
                        data=dataclasses.replace(
                            n.data,
                            apps=[
                                AppDhcpClient(
                                    kind="dhcp-client",
                                    style=pt.Style(style),
                                )
                            ],
                        ),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ]

        for style, exec_client, exec_stop in [
            ("dhclient", "/usr/sbin/dhclient -lf /var/lib/dhcp/dhclient.kernel-0.leases -d eth0.2280", "/usr/sbin/dhclient -lf /var/lib/dhcp/dhclient.kernel-0.leases -r eth0.2280"),
            ("dhcpcd", "/usr/sbin/dhcpcd -B eth0.2280", "/usr/sbin/dhcpcd -k eth0.2280"),
        ]:
            with self.subTest(style=style):
                _, create_hosts = mod.build_scripts(with_app(style), "create")
                create_script = create_hosts["chassis-1"]
                # The one router service is installed + started.
                self.assertIn(
                    "cat > /etc/systemd/system/ovn-kernel-kernel-0.service << 'OVN'",
                    create_script,
                )
                self.assertIn(
                    "systemctl enable --now ovn-kernel-kernel-0.service",
                    create_script,
                )
                # The client lives in the router's up/down script (no
                # per-app unit). The up backgroundS the foreground daemon
                # so `enable --now` returns once the netns/veths exist.
                router = _router_script(create_script)
                up, down = _router_branches(router)
                self.assertIn(
                    f"ip netns exec ns-kernel-0 {exec_client} &",
                    up,
                )
                # ExecStop (the down branch) releases the lease explicitly —
                # SIGTERM through `ip netns exec` is not reliable dhclient
                # shutdown.
                self.assertIn(
                    f"ip netns exec ns-kernel-0 {exec_stop}",
                    down,
                )
                self.assertNotIn("ovn-kernel-kernel-0-dhcp-client", create_script)
                _, delete_hosts = mod.build_scripts(with_app(style), "delete")
                delete_script = delete_hosts["chassis-1"]
                self.assertIn(
                    "systemctl disable --now ovn-kernel-kernel-0.service",
                    delete_script,
                )

    def test_os_dependencies_are_installed_at_the_start_of_create(self) -> None:
        # The IR's ABSTRACT dependencies (ovn/ovs/ip/iptables/dhclient/
        # zerotier...) are installed at the very START of the create pass,
        # mapped to the host OS's package form (Ubuntu/Debian apt +
        # ovn-host for a chassis role); zerotier is its curl installer.
        # No deinstall ever (2026-08-23).
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(
                        n.data,
                        dependencies=[
                            "ovn",
                            "ovs",
                            "ip",
                            "iptables",
                            "dhclient",
                            "zerotier",
                        ],
                    ),
                )
                if n.kind == "infra.host" and n.key.host == "chassis-1"
                else n
            )
            for n in NODES
        ]
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("apt-get update", script)
        self.assertIn("apt-get install -y ovn-host", script)
        self.assertIn("apt-get install -y openvswitch-switch", script)
        self.assertIn("apt-get install -y iproute2", script)
        self.assertIn("apt-get install -y iptables", script)
        self.assertIn("apt-get install -y isc-dhcp-client", script)
        # zerotier's curl installer runs via sh -c so the pipe is argv-safe.
        self.assertIn("sh -c 'curl -s https://install.zerotier.com | bash'", script)
        # dependencies come before any OVS/OVN work.
        self.assertLess(script.index("apt-get update"), script.index("ovs-vsctl"))
        # delete never uninstalls.
        _, delete_hosts = mod.build_scripts(nodes, "delete")
        delete_script = delete_hosts["chassis-1"]
        self.assertNotIn("apt-get install", delete_script)

    def test_no_apps_emits_no_dhcp_client_commands(self) -> None:
        # The base fixture has no apps — nothing is started by default.
        _, hosts = mod.build_scripts(NODES, "create")
        script = hosts["chassis-1"]
        self.assertNotIn("dhclient", script)
        self.assertNotIn("dhcpcd", script)

    def test_upstream_peer_leg_binds_to_the_backdoor_bridge(self) -> None:
        # A tunnel router's upstream leg (right side veth): the pair is
        # created, the netns leg moves into the tunnel netns with the
        # default via the backdoor peer — and the ROOT-side peer is NOT
        # addressed in root (it binds to the backdoor bridge via the
        # backdoor ovn.ls's localnet instead; the peer's address lives on
        # the internal upstream-peer router's backdoor LRP, 2026-08-23).
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=pt.KernelRouterData(
                        host="host:chassis-1",
                        ipaddrs=["10.12.82.14/28"],
                        routes=[
                            pt.Route(dst="0.0.0.0/0", via="10.12.82.1"),
                        ],
                        ifaces=[
                            pt.Interface(
                                host="chassis-1",
                                iface={
                                    "kind": "veth",
                                    "ifaceName": "veth-up-kernel-0",
                                    "peerName": "veth-up-kernel-0-peer",
                                    "shortName": "0",
                                },
                            ),
                        ],
                        upstreamPeerAddrs=["10.12.82.1/28"],
                    ),
                )
                if n.kind == "kernel.router" and n.key.side == pt.Side.right
                else n
            )
            for n in NODES
        ]
        _, hosts = mod.build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ip link add veth-ovn-0 type veth peer name veth-krn-0", script)
        self.assertIn("ip link set veth-krn-0 netns ns-kernel-0", script)
        self.assertIn(
            "ip netns exec ns-kernel-0 ip route add 0.0.0.0/0 dev veth-krn-0 via 10.12.82.1",
            script,
        )
        # The peer is NOT brought up/addressed in root — the backdoor
        # bridge binding (from the backdoor ovn.ls) owns it.
        self.assertNotIn("ip addr add 10.12.82.1/28 dev veth-ovn-0", script)

    def test_kernel_app_docker_injects_a_veth_into_the_router_netns(self) -> None:
        # `kernel.app.docker` resolves to a KernelApp descriptor (name
        # prefixed with the router at resolve time — define.ts) and the
        # deployer supervises a container whose ONE interface is a veth
        # injected INTO the router netns (CNI/Multus-style, 2026-08-23):
        # the unit runs a wire script (`docker run --network none`, then
        # moves the pair into the container netns and the router netns,
        # with `docker rm -f` the hard way on delete).

        def with_docker() -> list[pt.Model]:
            return [
                (
                    dataclasses.replace(
                        n,
                        data=dataclasses.replace(
                            n.data,
                            apps=[
                                AppDocker(
                                    kind="docker",
                                    image="ubuntu",
                                    name="kernel-0-test-docker",
                                    cmd=["sleep", "86400"],
                                    ip="10.200.0.2/24",
                                    routerIp="10.200.0.1/24",
                                    vethName="ve-12345678",
                                )
                            ],
                        ),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ]

        wire = "/usr/local/sbin/ovn-kernel-kernel-0-docker-wire.sh"
        _, create_hosts = mod.build_scripts(with_docker(), "create")
        create_script = create_hosts["chassis-1"]
        # The container's wire script lives inside the router's up script
        # (a nested OVNKERNEL heredoc), and the router up runs it in the
        # background (it ends in `docker wait`). No per-app docker unit.
        router = _router_script(create_script)
        up, down = _router_branches(router)
        self.assertIn(f"cat > {wire} << 'OVNKERNEL'", up)
        self.assertIn(
            'docker run -d --network none --name "$container" "$image" sleep 86400',
            up,
        )
        self.assertIn(
            'ip link add "$veth" type veth peer name "$peer"',
            up,
        )
        self.assertIn(
            'ip netns exec "$netns" ip addr add "$router_ip" dev "$veth"',
            up,
        )
        self.assertIn(
            'ip netns exec "$container" ip addr add "$container_ip" dev eth0',
            up,
        )
        self.assertIn(
            'ip netns exec "$container" ip route add default via "$gateway" dev eth0',
            up,
        )
        self.assertIn('exec /usr/bin/docker wait "$container"', up)
        self.assertIn(f"/bin/sh {wire} &", up)
        # The hard stop (docker rm -f via the daemon) is the router down.
        self.assertIn("/usr/bin/docker rm -f kernel-0-test-docker", down)
        self.assertIn(f"rm -f {wire}", down)
        self.assertIn("rm -f /var/run/netns/kernel-0-test-docker", down)
        self.assertNotIn("ovn-kernel-kernel-0-docker.service", create_script)
        # The down branch stops the container before the netns is deleted.
        self.assertLess(
            down.index("/usr/bin/docker rm -f kernel-0-test-docker"),
            down.index("ip netns delete ns-kernel-0"),
        )
        _, delete_hosts = mod.build_scripts(with_docker(), "delete")
        delete_script = delete_hosts["chassis-1"]
        self.assertIn(
            "systemctl disable --now ovn-kernel-kernel-0.service",
            delete_script,
        )

    def test_kernel_app_wireguard_emits_conf_wgquick_and_masq_out_the_tunnel(self) -> None:
        # `kernel.app.wireguard` (the tunnelRouterEndpoint's middle leg):
        # write /etc/wireguard/<iface>.conf verbatim, `wg-quick up` inside
        # the netns, and MASQUERADE the declared families out the TUNNEL
        # iface (`-o mullvad-de`) — not the upstream veth. Delete brings
        # it down and removes the rules (2026-08-23).

        def with_wireguard() -> list[pt.Model]:
            return [
                (
                    dataclasses.replace(
                        n,
                        data=dataclasses.replace(
                            n.data,
                            apps=[
                                AppWireguard(
                                    kind="wireguard",
                                    ifaceName="mullvad-de",
                                    config=pt.Config(
                                        privateKey="k",
                                        address="10.64.56.207/32",
                                        peer=pt.Peer(
                                            publicKey="p",
                                            allowedIps="0.0.0.0/0",
                                            endpoint="146.70.117.130:51820",
                                        ),
                                    ),
                                    masq=[pt.MasqEnum.ipv4, pt.MasqEnum.ipv6],
                                )
                            ],
                        ),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ]

        _, create_hosts = mod.build_scripts(with_wireguard(), "create")
        create_script = create_hosts["chassis-1"]
        router = _router_script(create_script)
        up, down = _router_branches(router)
        self.assertIn("cat > /etc/wireguard/mullvad-de.conf << 'OVNKERNEL'", up)
        self.assertIn("PrivateKey = k", up)
        self.assertIn("AllowedIPs = 0.0.0.0/0", up)
        self.assertIn(
            "ip netns exec ns-kernel-0 wg-quick up /etc/wireguard/mullvad-de.conf",
            up,
        )
        self.assertIn(
            "ip netns exec ns-kernel-0 iptables -t nat -A POSTROUTING -o mullvad-de -j MASQUERADE",
            up,
        )
        self.assertIn(
            "ip netns exec ns-kernel-0 ip6tables -t nat -A POSTROUTING -o mullvad-de -j MASQUERADE",
            up,
        )
        # wg-quick is in-kernel (returns immediately) — it stays in the
        # foreground, NOT backgrounded.
        self.assertNotIn(
            "ip netns exec ns-kernel-0 wg-quick up /etc/wireguard/mullvad-de.conf &",
            up,
        )
        self.assertIn(
            "ip netns exec ns-kernel-0 wg-quick down /etc/wireguard/mullvad-de.conf",
            down,
        )
        self.assertIn(
            "ip netns exec ns-kernel-0 iptables -t nat -D POSTROUTING -o mullvad-de -j MASQUERADE",
            down,
        )
        _, delete_hosts = mod.build_scripts(with_wireguard(), "delete")
        delete_script = delete_hosts["chassis-1"]
        self.assertIn(
            "systemctl disable --now ovn-kernel-kernel-0.service",
            delete_script,
        )

    def test_kernel_app_zerotier_applies_tunnel_egress_routes_over_the_runtime_iface(
        self,
    ) -> None:
        # The zerotier tunnel's via-less declared routes (the ztnet mesh
        # supernet 192.168.0.0/16) ride on the app and are applied as
        # `ip route add <dst> dev "$var"` once ZeroTier names the real
        # interface — NOT on the upstream veth (2026-08-30).
        def with_zerotier() -> list[pt.Model]:
            return [
                (
                    dataclasses.replace(
                        n,
                        data=dataclasses.replace(
                            n.data,
                            apps=[
                                AppZerotier(
                                    kind="zerotier",
                                    networkId="02cfbec15c2319ff",
                                    instanceDir="/var/lib/zerotier-one-uplink-zerotier",
                                    masq=[pt.MasqEnum.ipv4],
                                    routes=[pt.Route(dst="192.168.0.0/16")],
                                )
                            ],
                        ),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ]
        _, create_hosts = mod.build_scripts(with_zerotier(), "create")
        router = _router_script(create_hosts["chassis-1"])
        up, _ = _router_branches(router)
        wire = "/usr/local/sbin/ovn-kernel-kernel-0-zerotier-wire.sh"
        self.assertIn(f"cat > {wire} << 'OVNKERNEL'", up)
        # The supernet is pushed over the runtime-named tunnel interface.
        self.assertIn(
            'ip netns exec "$netns" ip route add 192.168.0.0/16 dev "$var"',
            up,
        )
        self.assertIn(
            'ip netns exec "$netns" ip link set "$var" up',
            up,
        )


class KernelRouterDeleteTest(unittest.TestCase):
    def test_delete_only_removes_the_netns_not_the_devices_inside_it(self) -> None:
        # The host delete pass stops the router service (which runs its
        # `down`: stop apps, then `ip netns delete`, then remove the root
        # veth leg). The devices INSIDE the netns are never individually
        # addressed — the netns teardown destroys them.
        _, hosts = mod.build_scripts(NODES, "delete")
        script = hosts["chassis-1"]
        self.assertIn(
            "systemctl disable --now ovn-kernel-kernel-0.service",
            script,
        )
        self.assertNotIn("ip netns delete", script)
        self.assertNotIn("dummy-", script)
        self.assertNotIn("ip addr", script)
        self.assertNotIn("ip route", script)
        _, create_hosts = mod.build_scripts(NODES, "create")
        _, down = _router_branches(_router_script(create_hosts["chassis-1"]))
        self.assertIn("ip netns delete ns-kernel-0", down)

    def test_netns_removed_before_the_root_interface_bindings(self) -> None:
        # The netns owns the kernel router's devices (moved-in vlan/veth
        # legs), so it is torn down BEFORE the root-namespace binding
        # deletes run — otherwise `ip link delete` would target a device
        # still living inside the netns (2026-08-18). The netns teardown
        # now happens inside the router service's `down` (run by
        # `systemctl disable --now`), so that must precede the bindings.
        _, hosts = mod.build_scripts(NODES, "delete")
        script = hosts["chassis-1"]
        self.assertGreater(
            script.index("ip link delete eth0.129"),
            script.index("systemctl disable --now ovn-kernel-kernel-0.service"),
        )

    def test_veth_root_leg_removed_in_delete(self) -> None:
        # create adds the transit veth's ROOT-side leg in the global
        # namespace; the router `down` removes it explicitly, after the
        # netns (which destroyed the netns-side leg) is gone (2026-08-19).
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=pt.KernelRouterData(
                        host="host:chassis-1",
                        ipaddrs=["10.99.0.2/28"],
                        ifaces=[
                            pt.Interface(
                                host="chassis-1",
                                iface={
                                    "kind": "veth",
                                    "ifaceName": "veth-krn-0",
                                    "peerName": "veth-ovn-0",
                                    "shortName": "0",
                                },
                            ),
                        ],
                    ),
                )
                if n.kind == "kernel.router" and n.key.side == pt.Side.left
                else n
            )
            for n in NODES
        ]
        _, create_hosts = mod.build_scripts(nodes, "create")
        _, down = _router_branches(_router_script(create_hosts["chassis-1"]))
        self.assertIn("ip link delete veth-ovn-0", down)
        self.assertGreater(
            down.index("ip link delete veth-ovn-0"),
            down.index("ip netns delete ns-kernel-0"),
        )

    def test_moved_in_vlan_removed_from_inside_the_netns_before_teardown(self) -> None:
        # The moved-in WAN vlan must be `ip link del`'d from INSIDE the
        # netns BEFORE `ip netns delete` — netns teardown alone leaks a
        # stale 8021q registration on the parent, so recreating the same
        # vlan afterwards fails with "VLAN device already exists" (hit
        # live 2026-08-19 on an LXC).
        _, create_hosts = mod.build_scripts(NODES, "create")
        _, down = _router_branches(_router_script(create_hosts["chassis-1"]))
        self.assertIn("ip netns exec ns-kernel-0 ip link delete eth0.2280", down)
        self.assertLess(
            down.index("ip netns exec ns-kernel-0 ip link delete eth0.2280"),
            down.index("ip netns delete ns-kernel-0"),
        )


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


class GeneratePythonDeployerTest(unittest.TestCase):
    def _exec_runtime(self) -> dict:
        ns: dict = {}
        exec(mod.generate_python_deployer(NODES), ns)
        return ns

    def test_generated_file_is_valid_python(self) -> None:
        compile(mod.generate_python_deployer(NODES), "<generated>", "exec")

    def test_generated_file_compiles_with_a_docker_veth_app(self) -> None:
        # The docker app embeds a wire script that ENDS in a `"` (the
        # `exec docker wait "$container"` line) — a regression guard for
        # the triple-quote boundary in the Python emitter's write_file
        # (2026-08-23): the generated file must still be valid Python.
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(
                        n.data,
                        apps=[
                            AppDocker(
                                kind="docker",
                                image="ubuntu",
                                name="kernel-0-test-docker",
                                cmd=["sleep", "86400"],
                                ip="10.200.0.2/24",
                                routerIp="10.200.0.1/24",
                                vethName="ve-12345678",
                            )
                        ],
                    ),
                )
                if n.kind == "kernel.router" and n.key.side == pt.Side.right
                else n
            )
            for n in NODES
        ]
        compile(mod.generate_python_deployer(nodes), "<generated>", "exec")

    def test_generated_host_function_issues_the_same_commands_as_the_shell(self) -> None:
        # The Emitter refactor's core guarantee (2026-08-23): the Python
        # front-end and the shell front-end render the SAME emitter
        # operations, so running a generated host function must issue
        # exactly the argv the shell generator prints as commands.
        ns = self._exec_runtime()
        _, create_hosts = mod.build_scripts(NODES, "create")
        # File writes (heredocs for the router script + unit) are
        # write_file() in Python, not commands — skip them from the shell
        # side so only command argv are compared (2026-08-30).
        shell_commands = _shell_command_lines(create_hosts["chassis-1"])
        argv_seen: list[list[str]] = []
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            # File writes (the router script + unit) are write_file(), not
            # commands — no-op them so the run_cmd argv stays comparable.
            ns["write_file"] = mock.Mock()
            ns["_host_chassis_1_create"](False, True)
            argv_seen = [call.args[0] for call in run.call_args_list]
        self.assertEqual(shell_commands, argv_seen)

    def test_runtime_exposes_the_cli_selectors(self) -> None:
        src = mod.generate_python_deployer(NODES)
        for marker in (
            "--action",
            "--cluster",
            "--host",
            "--verbose",
            "def run_cmd",
            "def write_file",
            "argparse.ArgumentParser",
            'choices=["create", "delete"]',
        ):
            self.assertIn(marker, src)

    def test_run_cmd_warns_for_delete_and_aborts_for_create(self) -> None:
        ns = self._exec_runtime()
        stderr = io.StringIO()
        # delete: a failing command warns and the pass continues.
        with contextlib.redirect_stderr(stderr):
            ns["run_cmd"]("host", "false", False, False)
        self.assertIn("warning: failed in host", stderr.getvalue())
        # create: the same command aborts the pass.
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stderr(stderr):
                ns["run_cmd"]("host", "false", False, True)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("error: failed in host", stderr.getvalue())

    def test_run_cmd_verbose_echoes_every_command(self) -> None:
        ns = self._exec_runtime()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            ns["run_cmd"]("host", "true", True, False)
        self.assertIn("host$ true", stdout.getvalue())

    def test_run_cmd_missing_binary_warns_not_crashes(self) -> None:
        ns = self._exec_runtime()
        stderr = io.StringIO()
        with mock.patch(
            "subprocess.run",
            side_effect=FileNotFoundError(2, "No such file or directory", "ovn-nbctl"),
        ):
            with contextlib.redirect_stderr(stderr):
                ns["run_cmd"]("host", "ovn-nbctl ls-add home", False, False)
        self.assertIn("warning: cannot run in host", stderr.getvalue())

    def test_write_file_writes_content_with_a_trailing_newline(self) -> None:
        # The append() emitter operation at runtime must produce the same
        # file the shell front-end's heredoc would — content plus a
        # single trailing newline (2026-08-23).
        ns = self._exec_runtime()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "unit.service")
            ns["write_file"]("host", path, "[Unit]\nDescription=x", False, False)
            with open(path, encoding="utf-8") as out:
                self.assertEqual(out.read(), "[Unit]\nDescription=x\n")


if __name__ == "__main__":
    unittest.main()
