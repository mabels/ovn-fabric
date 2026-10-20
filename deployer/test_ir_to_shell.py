# deployer/test_ir_to_shell.py — stdlib unittest, no pytest, matching
# ladops's own test style. Fixtures are protocol.generated's typed
# dataclasses directly (InfraHostNode/OvnLsNode/OvnLrpNode) — the same
# shape a real run gets from protocol/hydrate.py, not raw dicts.
#
# What is under test is deployer/ir_common.py's emitters: WHICH
# operations a set of IR nodes turns into, and in what order. They are
# read through build_scripts() below — a test-only TRACE of the emitted
# operations as text, one cluster trace plus one per host (the file keeps
# its name from the shell-script generator this trace format comes from;
# that generator is gone, the Python deployer is the only front-end).
# GeneratePythonDeployerTest covers the real front-end, including that it
# issues exactly the commands the trace shows.
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
# text; only the generated-deployer tests mock subprocess.

from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import json
import os
import pathlib
import shlex
import sys
import tempfile
import unittest
from collections.abc import Collection, Sequence
from typing import Any
from unittest import mock

from protocol import AppDhcpClient, AppDocker, AppWireguard, AppZerotier
from protocol import generated as pt
from protocol.hydrate import hydrate_as

from . import install_queue
from .ir_common import (
    Action,
    Emitter,
    Ref,
    _emit_cluster_body,
    _emit_host_script,
    _shell_token,
)

mod = importlib.import_module("deployer.ir_to_python")


class _TraceEmitter(Emitter):
    """The emitted operations as readable text, one per line: a command
    as its shlex-joined argv (` &` when backgrounded, ` || true` when
    optional), a capture as `VAR=$(argv)`, a file write as a
    `cat > path << 'OVN'` block — a queued file too, with `<queued-at>`
    where the deployer puts the time it runs at. A test fixture, not a
    deployable script — it carries no envelope and nothing ever runs it."""

    def sh(
        self, argv: Sequence[str | Ref], background: bool = False, optional: bool = False
    ) -> None:
        line = " ".join(_shell_token(token) for token in argv) + (" &" if background else "")
        self.lines.append(line + (" || true" if optional else ""))

    def capture(self, var: str, argv: list[str]) -> None:
        self.lines.append(f"{var}=$({shlex.join(argv)})")

    def append(self, path: str, content: str) -> None:
        self.lines.append(f"cat > {path} << 'OVN'")
        self.lines.append(content)
        self.lines.append("OVN")

    def enqueue(self, directory: str, name: str, content: str) -> None:
        self.append(f"{directory}/<queued-at>-{name}", content)

    def install_self(self, path: str) -> None:
        # No shell equivalent — a comment keeps it out of
        # _shell_command_lines (the generated Python's install_self() call
        # likewise is not a run_cmd).
        self.lines.append(f"# install-self {path}")

    def enable_unit(self, unit: str, optional: bool = True) -> None:
        # The trace is the deploy pass (`no_start` false), so render the
        # branch the generated Python takes there.
        self.sh(["systemctl", "enable", "--now", unit], optional=optional)


def build_scripts(nodes: list[pt.Model], action: Action) -> tuple[str, dict[str, str]]:
    """(the cluster pass, {host name: that host's pass}) for `action`, each
    as a trace of the operations the emitters produce."""
    cluster = _TraceEmitter(action)
    _emit_cluster_body(nodes, cluster)
    hosts: dict[str, str] = {}
    for node in nodes:
        if node.kind != "infra.host":
            continue
        host = _TraceEmitter(action)
        _emit_host_script(node, nodes, host)
        hosts[node.key.host] = "\n".join(host.lines)
    return "\n".join(cluster.lines), hosts


# Interface fixtures are written the way the IR carries them (a raw
# `iface` object) and hydrated through the SAME strict walk a real run
# uses, so a fixture that isn't a valid protocol shape fails here instead
# of exercising the deployer with data the generator can never emit.
def _ls_interface(host: str, iface: dict[str, object]) -> pt.OvnLsInterface:
    return hydrate_as(pt.OvnLsInterface, {"host": host, "iface": iface})


def _kr_interface(host: str, iface: dict[str, object]) -> pt.KernelRouterInterface:
    return hydrate_as(pt.KernelRouterInterface, {"host": host, "iface": iface})


NODES: list[pt.Model] = [
    pt.InfraHostNode(
        id="host:central-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="central-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.1",
            ovnRole=pt.OvnRole.central,
            encapIp="10.0.0.1",
            os=pt.HostOs(name="ubuntu", version="26.04"),
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
            os=pt.HostOs(name="ubuntu", version="26.04"),
        ),
    ),
    pt.OvnLsNode(
        id="ls:home",
        kind="ovn.ls",
        key=pt.OvnLsKey(name="home"),
        data=pt.OvnLsData(
            interfaces=[
                _ls_interface(
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
    pt.OvnLrNode(
        id="ovnrouter:router-home",
        kind="ovn.lr",
        key=pt.OvnLrKey(name="router-home"),
        data=pt.OvnLrData(options=pt.OvnLrOptions(mac_binding_age_threshold=300)),
    ),
    pt.OvnLrpNode(
        id="ovnrouter:router-home|lrp:lrp-router-home-left",
        kind="ovn.lrp",
        key=pt.OvnLrpKey(ovnrouter="router-home", name="lrp-router-home-left"),
        data=pt.OvnLrpData(
            l2Segment="ls:home",
            addresses=["192.168.1.1/24"],
            mac="00:00:00:00:01:01",
            gatewayChassis="host:chassis-1",
        ),
    ),
    pt.OvnLrpNode(
        id="ovnrouter:router-home|lrp:lrp-router-home-right",
        kind="ovn.lrp",
        key=pt.OvnLrpKey(ovnrouter="router-home", name="lrp-router-home-right"),
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
            routes=[pt.KernelRouterRoute(dst="0.0.0.0/0", via="192.168.132.1")],
            ifaces=[
                _kr_interface(
                    host="chassis-1",
                    iface={"kind": "vlan", "vlanParent": "eth0", "vlanId": 2280},
                ),
            ],
        ),
    ),
]


def _replace_home_ls(nodes: list[pt.Model], **changes: Any) -> list[pt.Model]:
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
    start = next(i for i, line in enumerate(lines) if line == marker)
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


# The body of the `cat > path << 'OVN'` block a trace writes `path` with.
def _written_file(script: str, path: str) -> str:
    lines = script.splitlines()
    start = lines.index(f"cat > {path} << 'OVN'") + 1
    return "\n".join(lines[start : lines.index("OVN", start)])


_QUEUED = "cat > /var/spool/ovn-fabric/install/<queued-at>-"


# The install queue a host trace fills, in the order it is queued:
# (the file's name after its timestamp, the job it holds).
def _queued_jobs(script: str) -> list[tuple[str, dict[str, Any]]]:
    queued: list[tuple[str, dict[str, Any]]] = []
    lines = script.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(_QUEUED):
            name = line[len(_QUEUED) :].removesuffix(" << 'OVN'")
            body = "\n".join(lines[index + 1 : lines.index("OVN", index)])
            queued.append((name, json.loads(body)))
    return queued


def _queued_packages(script: str) -> list[dict[str, Any]]:
    return [job for _, job in _queued_jobs(script) if job["kind"] == "package"]


# The command lines of a host trace, excluding every file it writes
# (`cat > path << 'DELIM'` through the closing `DELIM`) and comments — the
# generated deployer's run_cmd() argv must match exactly these (file
# writes are write_file(), not commands).
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
            argv = shlex.split(line)
            # `|| true` is the SHELL backend's rendering of an OPTIONAL
            # command (Emitter.sh(optional=True), 2026-09-16); the Python
            # backend expresses the same thing through run_cmd's abort
            # flag, so drop it before comparing argv.
            if argv[-2:] == ["||", "true"]:
                argv = argv[:-2]
            commands.append(argv)
    return commands


class ClusterScriptCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, self.hosts = build_scripts(NODES, "create")

    def test_every_cluster_add_is_idempotent(self) -> None:
        # The create pass must be re-runnable (README's idempotency
        # guarantee): every add carries --may-exist so a second run
        # doesn't abort on objects that already exist (2026-09-19).
        self.assertIn("ovn-nbctl --may-exist ls-add home", self.cluster)
        self.assertIn("ovn-nbctl --may-exist lr-add router-home", self.cluster)
        self.assertIn("ovn-nbctl --may-exist lrp-add router-home", self.cluster)
        self.assertIn("ovn-nbctl --may-exist lsp-add", self.cluster)

    def test_ls_add_for_every_switch(self) -> None:
        self.assertIn("ovn-nbctl --may-exist ls-add home", self.cluster)
        self.assertIn("ovn-nbctl --may-exist ls-add backbone", self.cluster)

    def test_lr_add_exactly_once_for_a_two_sided_router(self) -> None:
        # Regression: the removed TypeScript generator called lr-add
        # once per endpoint (left AND right), which only worked because
        # --may-exist silently no-op'd the second call. The deployer
        # emits it exactly once for the router.
        self.assertEqual(self.cluster.count("ovn-nbctl --may-exist lr-add router-home"), 1)

    def test_router_options_come_from_the_ovn_lr_node(self) -> None:
        # The router's own ovn.lr node carries its FINAL Logical_Router
        # options (src/ir.ts's routerToIR); the deployer applies each pair
        # right after the router exists. mac_binding_age_threshold is the
        # one that matters today: OVN keeps a learned MAC_Binding forever
        # by default, and a neighbour that came back with another MAC
        # stayed black-holed (confirmed live 2026-10-11).
        add = "ovn-nbctl --may-exist lr-add router-home"
        age = "ovn-nbctl set logical_router router-home options:mac_binding_age_threshold=300"
        self.assertIn(age, self.cluster)
        self.assertLess(self.cluster.index(add), self.cluster.index(age))

    def test_router_option_value_is_whatever_the_ir_carries(self) -> None:
        nodes = [
            (
                dataclasses.replace(
                    n, data=pt.OvnLrData(options=pt.OvnLrOptions(mac_binding_age_threshold=0))
                )
                if n.kind == "ovn.lr"
                else n
            )
            for n in NODES
        ]
        cluster, _ = build_scripts(nodes, "create")
        self.assertIn("options:mac_binding_age_threshold=0", cluster)
        self.assertNotIn("options:mac_binding_age_threshold=300", cluster)

    def test_router_without_an_ovn_lr_node_gets_no_options(self) -> None:
        # An IR written before the ovn.lr node existed still deploys — the
        # deployer has no default of its own.
        nodes: list[pt.Model] = [n for n in NODES if n.kind != "ovn.lr"]
        cluster, _ = build_scripts(nodes, "create")
        self.assertIn("ovn-nbctl --may-exist lr-add router-home", cluster)
        self.assertNotIn("set logical_router router-home", cluster)

    def test_gateway_chassis_pin_only_on_the_pinned_side(self) -> None:
        # The pin names the chassis's RUNTIME system-id, resolved by an
        # `ovn-sbctl` lookup on the hostname the host pass registered —
        # NOT the IR host name, which `lrp-set-gateway-chassis` can never
        # match (confirmed live 2026-09-19). The lookup runs once, before
        # the first pin, and a `test -n` guards the empty result.
        self.assertIn(
            "OVN_CHASSIS_chassis_1=$(ovn-sbctl --bare --columns=name find Chassis "
            "hostname=chassis-1)",
            self.cluster,
        )
        self.assertIn('test -n "$OVN_CHASSIS_chassis_1"', self.cluster)
        self.assertIn(
            'ovn-nbctl lrp-set-gateway-chassis lrp-router-home-left "$OVN_CHASSIS_chassis_1" 100',
            self.cluster,
        )
        self.assertNotIn("lrp-set-gateway-chassis lrp-router-home-right", self.cluster)
        # the lookup precedes the pin it feeds
        self.assertLess(
            self.cluster.index("find Chassis hostname=chassis-1"),
            self.cluster.index("lrp-set-gateway-chassis lrp-router-home-left"),
        )

    def test_host_pass_registers_its_topology_hostname(self) -> None:
        # The cluster pass's chassis lookup keys on this external_id, so
        # the host pass must set it to the IR host name (not OVS's auto
        # FQDN).
        _, create_hosts = build_scripts(NODES, "create")
        self.assertIn(
            "ovs-vsctl set open_vswitch . external-ids:hostname=chassis-1",
            create_hosts["chassis-1"],
        )
        self.assertIn(
            "ovs-vsctl set open_vswitch . external-ids:hostname=central-1",
            create_hosts["central-1"],
        )
        _, delete_hosts = build_scripts(NODES, "delete")
        self.assertIn(
            "ovs-vsctl remove open_vswitch . external-ids hostname",
            delete_hosts["chassis-1"],
        )

    def test_lrp_add_carries_mac_and_addresses(self) -> None:
        self.assertIn(
            "ovn-nbctl --may-exist lrp-add router-home lrp-router-home-left "
            "00:00:00:00:01:01 192.168.1.1/24",
            self.cluster,
        )

    def test_switch_side_lsp_bound_to_the_right_lrp(self) -> None:
        self.assertIn("ovn-nbctl --may-exist lsp-add home lsp-router-home-left", self.cluster)
        self.assertIn(
            "ovn-nbctl lsp-set-options lsp-router-home-left router-port=lrp-router-home-left",
            self.cluster,
        )

    def test_one_host_script_per_infra_host_node(self) -> None:
        self.assertEqual(set(self.hosts), {"central-1", "chassis-1"})


class ClusterScriptDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, _ = build_scripts(NODES, "delete")

    def test_lr_del_and_ls_del_use_if_exists(self) -> None:
        self.assertIn("ovn-nbctl --if-exists lr-del router-home", self.cluster)
        self.assertIn("ovn-nbctl --if-exists ls-del home", self.cluster)
        self.assertIn("ovn-nbctl --if-exists ls-del backbone", self.cluster)

    def test_no_lrp_add_or_lsp_add_in_delete_mode(self) -> None:
        self.assertNotIn("lrp-add", self.cluster)
        self.assertNotIn("lsp-add", self.cluster)


class HostScriptTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.hosts = build_scripts(NODES, "create")

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
        _, self.hosts = build_scripts(NODES, "delete")

    def test_central_drops_its_connections(self) -> None:
        script = self.hosts["central-1"]
        self.assertIn("ovn-nbctl del-connection", script)
        self.assertIn("ovn-sbctl del-connection", script)

    def test_every_host_removes_its_external_ids(self) -> None:
        for script in self.hosts.values():
            self.assertIn("ovs-vsctl remove open_vswitch . external-ids ovn-remote", script)


class IfaceBindingCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, self.hosts = build_scripts(NODES, "create")

    def test_localnet_lsp_emitted_only_for_the_bound_domain(self) -> None:
        self.assertIn("ovn-nbctl --may-exist lsp-add home lsp-home-localnet", self.cluster)
        self.assertIn("ovn-nbctl lsp-set-type lsp-home-localnet localnet", self.cluster)
        self.assertIn("ovn-nbctl lsp-set-addresses lsp-home-localnet unknown", self.cluster)
        self.assertIn(
            "ovn-nbctl lsp-set-options lsp-home-localnet network_name=net-home", self.cluster
        )
        self.assertNotIn("lsp-backbone-localnet", self.cluster)  # backbone has no real interfaces

    def test_vlan_created_and_brought_up_on_the_owning_host(self) -> None:
        script = self.hosts["chassis-1"]
        # The create is guarded so a re-run tolerates the VLAN already
        # existing (`ip link add` has no --may-exist).
        self.assertIn("ip link show eth0.129", script)
        self.assertIn("ip link add link eth0 name eth0.129 type vlan id 129", script)
        self.assertIn("ip link set eth0.129 up", script)

    def test_bridge_created_and_port_attached_on_the_owning_host(self) -> None:
        script = self.hosts["chassis-1"]
        self.assertIn("ovs-vsctl --may-exist add-br br-home", script)
        self.assertIn("ovs-vsctl set bridge br-home fail-mode=standalone", script)
        self.assertIn("ovs-vsctl --may-exist add-port br-home eth0.129", script)

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
                        _ls_interface(
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
        _, hosts = build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ip link set veth-ovn-9adb1d up", script)
        # The LIVE create block must up the root leg before adding it as
        # a port. (The boot bindings block above it also adds the port,
        # before any veth exists — the router unit creates and ups it —
        # so start the search at the live block's `up`.)
        up = script.index("ip link set veth-ovn-9adb1d up")
        port = script.index("ovs-vsctl --may-exist add-port br-1f5b95ad veth-ovn-9adb1d", up)
        self.assertLess(up, port)

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
                    _ls_interface(
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
        _, hosts = build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ovs-vsctl --may-exist add-br br-8d8c0a55", script)
        self.assertIn(
            "external-ids:ovn-bridge-mappings=net-home:br-8d8c0a55",
            script,
        )

    def test_no_binding_commands_on_a_host_with_no_interfaces(self) -> None:
        script = self.hosts["central-1"]
        self.assertNotIn("add-br", script)
        self.assertNotIn("ovn-bridge-mappings", script)

    def test_binding_adds_are_idempotent(self) -> None:
        # Re-running the create pass must not abort on an existing
        # bridge/port/VLAN (2026-09-19).
        script = self.hosts["chassis-1"]
        self.assertIn("ovs-vsctl --may-exist add-br", script)
        self.assertIn("ovs-vsctl --may-exist add-port", script)
        self.assertIn("ip link show eth0.129", script)


class IfaceBindingDeleteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cluster, self.hosts = build_scripts(NODES, "delete")

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


class BootBindingsTest(unittest.TestCase):
    """The per-host ovn-fabric-bindings.service: OVS keeps its bridge/port
    rows across a reboot, the netdevices they reference do not (confirmed
    live, 2026-10-20 — every segment bridge read "could not open network
    device eth0.<vlan>"). This unit re-runs the idempotent create bindings
    at boot, after OVS and before the fabric's own units."""

    SCRIPT = "/usr/local/sbin/ovn-fabric-bindings.sh"
    UNIT = "/etc/systemd/system/ovn-fabric-bindings.service"

    def setUp(self) -> None:
        _, self.hosts = build_scripts(NODES, "create")

    def test_recreates_the_vlan_and_its_bridge_at_boot(self) -> None:
        body = _written_file(self.hosts["chassis-1"], self.SCRIPT)
        self.assertIn("ip link add link eth0 name eth0.129 type vlan id 129", body)
        self.assertIn("ip link set eth0.129 up", body)
        self.assertIn("ovs-vsctl --may-exist add-br br-home", body)
        self.assertIn("ovs-vsctl --may-exist add-port br-home eth0.129", body)

    def test_ordered_after_ovs_and_enabled(self) -> None:
        host = self.hosts["chassis-1"]
        unit = _written_file(host, self.UNIT)
        self.assertIn("After=network-pre.target ovsdb-server.service ovs-vswitchd.service", unit)
        self.assertIn("systemctl enable ovn-fabric-bindings.service", host)

    def test_fabric_units_are_ordered_after_the_bindings(self) -> None:
        router = _written_file(
            self.hosts["chassis-1"], "/etc/systemd/system/ovn-kernel-kernel-0.service"
        )
        self.assertIn("After=network-pre.target ovn-fabric-bindings.service", router)

    def test_a_host_without_bindings_gets_no_unit(self) -> None:
        # The restore unit mentions it in After=, but no bindings files
        # are written on a host that has nothing to bind.
        self.assertNotIn("/usr/local/sbin/ovn-fabric-bindings.sh", self.hosts["central-1"])
        self.assertNotIn("/etc/systemd/system/ovn-fabric-bindings.service", self.hosts["central-1"])

    def test_delete_removes_the_unit_before_the_bridges(self) -> None:
        _, hosts = build_scripts(NODES, "delete")
        script = hosts["chassis-1"]
        self.assertIn("systemctl disable --now ovn-fabric-bindings.service", script)
        self.assertLess(
            script.index("systemctl disable --now ovn-fabric-bindings.service"),
            script.index("ovs-vsctl --if-exists del-br br-home"),
        )


class BootRestoreTest(unittest.TestCase):
    """ovn-fabric-restore.service: the generated program is installed on
    the host and its host pass re-runs at boot (--skip-deps --no-start).
    The fabric units are ordered After= it, and `--no-start` is what
    keeps that from deadlocking: the restore pass enables them, systemd
    starts them once it has finished."""

    SCRIPT = "/usr/local/sbin/ovn-fabric-deployer.py"
    UNIT = "/etc/systemd/system/ovn-fabric-restore.service"

    def setUp(self) -> None:
        _, self.hosts = build_scripts(NODES, "create")

    def test_installs_the_running_program_and_enables_the_unit(self) -> None:
        host = self.hosts["chassis-1"]
        self.assertIn(f"# install-self {self.SCRIPT}", host)
        unit = _written_file(host, self.UNIT)
        self.assertIn(
            f"ExecStart=/usr/bin/python3 {self.SCRIPT} --host chassis-1 --skip-deps --no-start",
            unit,
        )
        self.assertIn(f"ExecCondition=/usr/bin/test -x {self.SCRIPT}", unit)
        self.assertIn("systemctl enable ovn-fabric-restore.service", host)

    def test_runs_after_ovs_and_the_bindings(self) -> None:
        unit = _written_file(self.hosts["chassis-1"], self.UNIT)
        self.assertIn(
            "After=network-pre.target ovsdb-server.service ovs-vswitchd.service"
            " ovn-fabric-bindings.service",
            unit,
        )

    def test_router_units_wait_for_the_restore(self) -> None:
        router = _written_file(
            self.hosts["chassis-1"],
            "/etc/systemd/system/ovn-kernel-kernel-0.service",
        )
        self.assertIn(
            "After=network-pre.target ovn-fabric-bindings.service" " ovn-fabric-restore.service",
            router,
        )

    def test_delete_removes_the_restore_unit_before_the_fabric(self) -> None:
        _, hosts = build_scripts(NODES, "delete")
        script = hosts["chassis-1"]
        self.assertIn("systemctl disable --now ovn-fabric-restore.service", script)
        self.assertLess(
            script.index("systemctl disable --now ovn-fabric-restore.service"),
            script.index("systemctl disable --now ovn-kernel-kernel-0.service"),
        )


class KernelRouterCreateTest(unittest.TestCase):
    def setUp(self) -> None:
        _, self.hosts = build_scripts(NODES, "create")

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
                            _kr_interface(
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
        _, hosts = build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        self.assertIn("ip link add veth-ovn-0 type veth peer name veth-krn-0", script)
        # No `mac` in the iface blob (an older IR) -> no `address`.
        self.assertNotIn("peer name veth-krn-0 address", script)
        self.assertNotIn("ip netns exec ns-kernel-0 ip link add veth-ovn-0", script)
        self.assertIn("ip link set veth-krn-0 netns ns-kernel-0", script)
        self.assertIn("ip netns exec ns-kernel-0 ip addr add 10.99.0.2/28 dev veth-krn-0", script)
        self.assertNotIn("ip link add dummy-left type dummy", script)
        # The ROOT leg is born DOWN; the boot unit must up it itself, or
        # the netns leg stays LOWERLAYERDOWN and its routes linkdown
        # after a reboot (confirmed live, 2026-10-20).
        self.assertIn("ip link set veth-ovn-0 up", script)
        self.assertLess(
            script.index("ip link add veth-ovn-0 type veth peer name veth-krn-0"),
            script.index("ip link set veth-ovn-0 up"),
        )

    def test_veth_netns_leg_gets_the_generator_resolved_mac(self) -> None:
        # The generator resolves the netns-side leg's MAC from the side's
        # own address (src/ir.ts) so it is identical on every recreation
        # — an OVN router never ages a learned MAC_Binding, so a random
        # MAC after a down/up black-holed the leg (confirmed live
        # 2026-10-11). The deployer only applies it: `address` after
        # `peer name` lands on the PEER (netns) leg.
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=pt.KernelRouterData(
                        host="host:chassis-1",
                        ipaddrs=["10.99.0.2/28"],
                        ifaces=[
                            _kr_interface(
                                host="chassis-1",
                                iface={
                                    "kind": "veth",
                                    "ifaceName": "veth-krn-0",
                                    "peerName": "veth-ovn-0",
                                    "shortName": "0",
                                    "mac": "00:00:0a:63:00:02",
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
        _, hosts = build_scripts(nodes, "create")
        self.assertIn(
            "ip link add veth-ovn-0 type veth peer name veth-krn-0 address 00:00:0a:63:00:02",
            hosts["chassis-1"],
        )

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
                        pt.SecurityGroupRule(family=pt.Family.ipv4, kind="masq"),
                        pt.SecurityGroupRule(family=pt.Family.ipv6, kind="masq"),
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
        _, hosts = build_scripts(nodes, "create")
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
        _, hosts = build_scripts(NODES, "create")
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

        def with_app() -> list[pt.Model]:
            return [
                (
                    dataclasses.replace(
                        n,
                        data=dataclasses.replace(
                            n.data,
                            apps=[
                                AppDhcpClient(
                                    kind="dhcp-client",
                                    style=pt.Style("dhclient"),
                                )
                            ],
                        ),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ]

        # dhcpcd is NOT a dhcp-client app anymore — it resolves to a generic
        # docker app that OWNS the router's interfaces, covered by the docker
        # tests.
        _, create_hosts = build_scripts(with_app(), "create")
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
        router = _router_script(create_script)
        up, down = _router_branches(router)
        # dhclient runs in the router netns, backgrounded; the down releases
        # the lease explicitly.
        self.assertIn(
            "ip netns exec ns-kernel-0 /usr/sbin/dhclient -lf "
            "/var/lib/dhcp/dhclient.kernel-0.leases -d eth0.2280 &",
            up,
        )
        self.assertIn(
            "ip netns exec ns-kernel-0 /usr/sbin/dhclient -lf "
            "/var/lib/dhcp/dhclient.kernel-0.leases -r eth0.2280",
            down,
        )
        self.assertNotIn("ovn-kernel-kernel-0-dhcp-client", create_script)
        _, delete_hosts = build_scripts(with_app(), "delete")
        delete_script = delete_hosts["chassis-1"]
        self.assertIn(
            "systemctl disable --now ovn-kernel-kernel-0.service",
            delete_script,
        )

    def test_dhcpcd_docker_applies_transit_veth_addressing_in_container(self) -> None:
        # dhcpcd is a GENERIC docker app that OWNS the router's interfaces
        # (signaled by omitting veth addressing): the container owns BOTH the
        # transit veth leg and the real iface, and the transit
        # leg's OWN static addresses (left side) are applied in the container
        # too — an interface's addresses don't survive a netns move
        # (2026-08-31).
        left = pt.KernelRouterData(
            host="host:chassis-1",
            ipaddrs=["10.12.80.14/28"],
            ifaces=[
                _kr_interface(
                    host="chassis-1",
                    iface={
                        "kind": "veth",
                        "ifaceName": "veth-krn-9adb1d",
                        "peerName": "veth-ovn-9adb1d",
                        "shortName": "9adb1d",
                    },
                )
            ],
        )

        def with_dhcpcd() -> list[pt.Model]:
            return [
                (
                    dataclasses.replace(n, data=left)
                    if n.kind == "kernel.router" and n.key.side == pt.Side.left
                    else (
                        dataclasses.replace(
                            n,
                            data=dataclasses.replace(
                                n.data,
                                apps=[
                                    # The IR signals "owns the interfaces" by
                                    # OMITTING ip/routerIp (no veth addressing).
                                    AppDocker(
                                        kind="docker",
                                        image="ovn-fabric-kernel-0",
                                        name="kernel-0-dhcpcd",
                                        cmd=["/sbin/dhcpcd"],
                                        build=pt.DockerBuild(
                                            from_="alpine:latest", packages=["dhcpcd"]
                                        ),
                                    )
                                ],
                            ),
                        )
                        if n.kind == "kernel.router" and n.key.side == pt.Side.right
                        else n
                    )
                )
                for n in NODES
            ]

        _, create_hosts = build_scripts(with_dhcpcd(), "create")
        router = _router_script(create_hosts["chassis-1"])
        up, _ = _router_branches(router)
        # The transit veth is moved into the container AND gets its own
        # static address there (same _apply_side_network path as the router
        # netns).
        self.assertIn(
            'ip netns exec "$netns" ip link set "$transit_leg" netns "$container"',
            up,
        )
        self.assertIn(
            "ip netns exec kernel-0-dhcpcd ip addr add 10.12.80.14/28 dev veth-krn-9adb1d",
            up,
        )
        # The image build uses a mktemp -d build context, cleaned up after —
        # nothing is scattered on the host (2026-08-31).
        self.assertIn("d=$(mktemp -d)", up)
        self.assertIn('rm -rf "$d"', up)
        self.assertNotIn("/usr/local/lib", up)

    def test_dependencies_are_queued_at_the_start_of_create(self) -> None:
        # The IR's ABSTRACT dependencies (ovn/ovs/ip/iptables/dhclient/
        # zerotier...) are mapped to the host OS's package form
        # (Ubuntu/Debian apt + ovn-host for a chassis role; zerotier is its
        # curl installer) — but NOT installed inline anymore: deploying a
        # router can cut the host's internet, so the pass QUEUES one job
        # file per dependency for the queue's consumer (2026-10-11).
        # No deinstall ever (2026-08-23).
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(
                        n.data,
                        dependencies=[
                            "arp",
                            "ping",
                            "traceroute",
                            "curl",
                            "ovn",
                            "ovs",
                            "ip",
                            "iptables",
                            "dhclient",
                            "zerotier",
                            "jq",
                        ],
                    ),
                )
                if n.kind == "infra.host" and n.key.host == "chassis-1"
                else n
            )
            for n in NODES
        ]
        _, hosts = build_scripts(nodes, "create")
        script = hosts["chassis-1"]
        packages = _queued_packages(script)
        installs = {job["name"]: " ".join(job["install"]) for job in packages}
        apt = "env DEBIAN_FRONTEND=noninteractive apt-get install -y "
        self.assertEqual(
            installs,
            {
                # base debugging tools come along on every host (arp/ifconfig
                # from net-tools, ping, traceroute, curl).
                "arp": apt + "net-tools",
                "ping": apt + "iputils-ping",
                "traceroute": apt + "traceroute",
                "curl": apt + "curl",
                "ovn": apt + "ovn-host",
                "ovs": apt + "openvswitch-switch",
                "ip": apt + "iproute2",
                "iptables": apt + "iptables",
                "dhclient": apt + "isc-dhcp-client",
                # zerotier's curl installer runs via sh -c so the pipe is
                # argv-safe.
                "zerotier": "sh -c curl -s https://install.zerotier.com | bash",
                # zerotier's wire script parses `zerotier-cli -j` with jq.
                "jq": apt + "jq",
            },
        )
        # One file per dependency, queued in the IR's order (curl before
        # the zerotier installer that needs it).
        self.assertEqual(
            [name for name, _ in _queued_jobs(script)][:4],
            [
                "package-arp.json",
                "package-ping.json",
                "package-traceroute.json",
                "package-curl.json",
            ],
        )
        by_name = {job["name"]: job for job in packages}
        # Done-ness is decided per dpkg package, with no network. WHEN a
        # job may be tried is not the generator's business: the consumer
        # decides whether the host is online.
        self.assertEqual(
            by_name["ovs"],
            {
                "kind": "package",
                "name": "ovs",
                "packages": ["openvswitch-switch"],
                "install": (apt + "openvswitch-switch").split(),
                "apt": True,
            },
        )
        self.assertEqual(by_name["zerotier"]["packages"], ["zerotier-one"])
        self.assertFalse(by_name["zerotier"]["apt"])
        # Nothing touches the network inline: the queue is filled and its
        # consumer asked once, before any OVS/OVN work.
        commands = [" ".join(argv) for argv in _shell_command_lines(script)]
        self.assertFalse(any("apt-get" in c or "curl" in c for c in commands))
        self.assertLess(
            commands.index("/usr/local/sbin/ovn-fabric-install --once"),
            next(i for i, c in enumerate(commands) if c.startswith("ovs-vsctl")),
        )
        # delete never uninstalls — and queues nothing.
        _, delete_hosts = build_scripts(nodes, "delete")
        self.assertNotIn("apt-get", delete_hosts["chassis-1"])
        self.assertEqual(_queued_jobs(delete_hosts["chassis-1"]), [])

    def test_every_queued_job_is_one_the_consumer_accepts(self) -> None:
        # The generator and the consumer share the job types
        # (install_queue.Job); this is the same contract at runtime: what
        # is written into a queue file passes the consumer's own check.
        nodes = [
            (
                dataclasses.replace(
                    n, data=dataclasses.replace(n.data, dependencies=["ovs", "zerotier", "docker"])
                )
                if n.kind == "infra.host"
                else n
            )
            for n in NODES
        ]
        _, hosts = build_scripts(nodes, "create")
        queued = _queued_jobs(hosts["chassis-1"])
        self.assertEqual(
            [job["kind"] for _, job in queued], ["package", "package", "package", "unit"]
        )
        for _, job in queued:
            self.assertEqual(install_queue.parse_job(job), job)

    def test_the_queue_consumer_is_installed_run_once_and_left_running(self) -> None:
        _, hosts = build_scripts(NODES, "create")
        script = hosts["chassis-1"]
        commands = [" ".join(argv) for argv in _shell_command_lines(script)]
        # First: the consumer and its unit, an empty queue for THIS
        # deploy, one go in the foreground (which may fail or find the host
        # offline — never fatal), then the service, which polls the queue
        # from then on.
        self.assertEqual(
            commands[:7],
            [
                "chmod +x /usr/local/sbin/ovn-fabric-install",
                "systemctl daemon-reload",
                "rm -rf /var/spool/ovn-fabric/install",
                "mkdir -p /var/spool/ovn-fabric/install",
                "/usr/local/sbin/ovn-fabric-install --once",
                "systemctl enable ovn-fabric-install.service",
                # restart: a consumer an earlier deploy left running is
                # replaced by the one just written.
                "systemctl restart ovn-fabric-install.service",
            ],
        )
        # A create stops nothing — on a fresh host there is no such unit
        # to stop.
        self.assertNotIn("systemctl stop", script)
        # ...and the consumer's own steps may not stop the deploy.
        for optional in (
            "/usr/local/sbin/ovn-fabric-install --once",
            "systemctl enable ovn-fabric-install.service",
            "systemctl restart ovn-fabric-install.service",
        ):
            self.assertIn(f"{optional} || true", script)
        # What is installed IS deployer/install_queue.py, not a copy of
        # its logic: the consumer on the host is the tested file.
        source = pathlib.Path(install_queue.__file__).read_text(encoding="utf-8")
        self.assertEqual(
            _written_file(script, "/usr/local/sbin/ovn-fabric-install"), source.rstrip("\n")
        )
        unit = _written_file(script, "/etc/systemd/system/ovn-fabric-install.service")
        self.assertIn("ExecStart=/usr/local/sbin/ovn-fabric-install\n", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        # It waits for the internet itself — it must not wait for systemd
        # to think the network is up.
        self.assertNotIn("network-online", unit)

    def test_units_are_queued_last(self) -> None:
        # The fabric's units are queued once they exist — the END of the
        # pass, nothing after it — so they sort behind the packages and
        # images queued when the pass began. Nothing is started for them:
        # the consumer polls.
        _, hosts = build_scripts(NODES, "create")
        script = hosts["chassis-1"]
        self.assertEqual(
            _queued_jobs(script),
            [
                (
                    "unit-ovn-kernel-kernel-0.service.json",
                    {"kind": "unit", "unit": "ovn-kernel-kernel-0.service"},
                )
            ],
        )
        lines = script.splitlines()
        queued_at = lines.index(f"{_QUEUED}unit-ovn-kernel-kernel-0.service.json << 'OVN'")
        self.assertGreater(
            queued_at, lines.index("systemctl enable --now ovn-kernel-kernel-0.service || true")
        )
        self.assertEqual(_shell_command_lines("\n".join(lines[queued_at:])), [])

    def test_the_images_of_a_hosts_units_are_queued_before_the_units(self) -> None:
        # The consumer restarts the fabric's OWN units that are not up,
        # and provides their docker images first — so a restart needs no
        # network (a unit that is the uplink is not torn down to fail
        # again).
        workload = pt.KernelAppContainerNode(
            id="kernel.app.container:dns",
            kind="kernel.app.container",
            key=pt.KernelServiceKey(name="dns"),
            data=pt.KernelAppContainerData(host="host:chassis-1", image="coredns/coredns:latest"),
        )
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(
                        n.data,
                        apps=[
                            AppDocker(
                                kind="docker",
                                image="ovn-fabric-kernel-0",
                                name="kernel-0-dhcpcd",
                                cmd=["/sbin/dhcpcd"],
                                build=pt.DockerBuild(from_="alpine:latest", packages=["dhcpcd"]),
                            ),
                            AppDocker(kind="docker", image="ubuntu", name="kernel-0-tool"),
                        ],
                    ),
                )
                if n.kind == "kernel.router" and n.key.side == pt.Side.right
                else n
            )
            for n in NODES
        ] + [workload]
        _, hosts = build_scripts(nodes, "create")
        self.assertEqual(
            _queued_jobs(hosts["chassis-1"]),
            [
                (
                    # A registry path is not a file name.
                    "image-coredns_coredns_latest.json",
                    {
                        "kind": "image",
                        "image": "coredns/coredns:latest",
                        # No build declared: the image is pulled.
                        "dockerfile": None,
                    },
                ),
                (
                    "image-ovn-fabric-kernel-0.json",
                    {
                        "kind": "image",
                        "image": "ovn-fabric-kernel-0",
                        "dockerfile": "FROM alpine:latest\nRUN apk add --no-cache dhcpcd",
                    },
                ),
                (
                    "image-ubuntu.json",
                    {"kind": "image", "image": "ubuntu", "dockerfile": None},
                ),
                (
                    "unit-ovn-kernel-kernel-0.service.json",
                    {"kind": "unit", "unit": "ovn-kernel-kernel-0.service"},
                ),
                (
                    "unit-ovn-kernel-service-dns.service.json",
                    {"kind": "unit", "unit": "ovn-kernel-service-dns.service"},
                ),
            ],
        )
        # The images are in the queue before the consumer's foreground go.
        lines = hosts["chassis-1"].splitlines()
        self.assertLess(
            lines.index(f"{_QUEUED}image-ubuntu.json << 'OVN'"),
            lines.index("/usr/local/sbin/ovn-fabric-install --once || true"),
        )

    def test_delete_removes_the_install_queue_before_anything_else(self) -> None:
        # Its consumer must not restart a unit the delete pass is tearing
        # down.
        _, hosts = build_scripts(NODES, "delete")
        commands = [" ".join(argv) for argv in _shell_command_lines(hosts["chassis-1"])]
        self.assertEqual(
            commands[:5],
            [
                "systemctl disable --now ovn-fabric-install.service",
                "rm -f /usr/local/sbin/ovn-fabric-install",
                "rm -f /etc/systemd/system/ovn-fabric-install.service",
                "rm -rf /var/spool/ovn-fabric/install",
                "systemctl daemon-reload",
            ],
        )

    def test_a_host_with_nothing_to_install_or_restart_gets_no_queue(self) -> None:
        # central-1 in the base fixture declares no dependencies and owns
        # no kernel router or workload.
        for action in ("create", "delete"):
            _, hosts = build_scripts(NODES, action)
            self.assertNotIn("ovn-fabric-install", hosts["central-1"])

    def test_base_deps_from_the_generator_map_to_packages(self) -> None:
        # The base tooling is a GENERATOR concern: src/ir.ts's
        # hostDependencies always emits the abstract capabilities
        # (arp/ping/traceroute/curl); the deployer only maps them to the
        # distro packages (2026-09-19). A host the IR lists no deps for
        # has no package queued.
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(
                        n.data,
                        dependencies=["arp", "ping", "traceroute", "curl"],
                    ),
                )
                if n.kind == "infra.host" and n.key.host == "central-1"
                else n
            )
            for n in NODES
        ]
        _, hosts = build_scripts(nodes, "create")
        self.assertEqual(
            [job["packages"] for job in _queued_packages(hosts["central-1"])],
            [["net-tools"], ["iputils-ping"], ["traceroute"], ["curl"]],
        )
        self.assertEqual(_queued_packages(hosts["chassis-1"]), [])

    def test_central_host_installs_both_ovn_central_and_ovn_host(self) -> None:
        # A central chassis is ALSO a chassis: it runs OVS and must run
        # ovn-controller (ovn-host), while additionally hosting the shared
        # NB/SB DBs + northd (ovn-central). Installing only ovn-central
        # left the central with no controller, so no chassis ever joined
        # and the fabric carried no traffic (confirmed live 2026-09-19).
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(n.data, dependencies=["ovn"]),
                )
                if n.kind == "infra.host"
                else n
            )
            for n in NODES
        ]
        _, hosts = build_scripts(nodes, "create")
        (central,) = _queued_packages(hosts["central-1"])
        # `ovn` is done only when BOTH packages are installed.
        self.assertEqual(central["packages"], ["ovn-central", "ovn-host"])
        self.assertEqual(central["install"][-2:], ["ovn-central", "ovn-host"])
        (chassis,) = _queued_packages(hosts["chassis-1"])
        self.assertEqual(chassis["packages"], ["ovn-host"])

    def test_an_unknown_os_or_dependency_is_noted_not_guessed(self) -> None:
        def with_host(**changes: Any) -> str:
            nodes = [
                (
                    dataclasses.replace(n, data=dataclasses.replace(n.data, **changes))
                    if n.kind == "infra.host" and n.key.host == "chassis-1"
                    else n
                )
                for n in NODES
            ]
            return build_scripts(nodes, "create")[1]["chassis-1"]

        unknown_dep = with_host(dependencies=["ovs", "quantum-vpn"])
        self.assertIn('# unknown dependency "quantum-vpn" — skipped', unknown_dep)
        self.assertEqual([job["name"] for job in _queued_packages(unknown_dep)], ["ovs"])
        unknown_os = with_host(dependencies=["ovs"], os=pt.HostOs(name="plan9", version="4"))
        self.assertIn("# unknown OS 'plan9' — cannot install dependencies, skipping", unknown_os)
        self.assertEqual(_queued_packages(unknown_os), [])

    def test_no_apps_emits_no_dhcp_client_commands(self) -> None:
        # The base fixture has no apps — nothing is started by default.
        _, hosts = build_scripts(NODES, "create")
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
                            pt.KernelRouterRoute(dst="0.0.0.0/0", via="10.12.82.1"),
                        ],
                        ifaces=[
                            _kr_interface(
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
        _, hosts = build_scripts(nodes, "create")
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
        _, create_hosts = build_scripts(with_docker(), "create")
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
        _, delete_hosts = build_scripts(with_docker(), "delete")
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
                                    config=pt.WireguardAppConfig(
                                        privateKey="k",
                                        address="10.64.56.207/32",
                                        peer=pt.WireguardPeerConfig(
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

        _, create_hosts = build_scripts(with_wireguard(), "create")
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
        _, delete_hosts = build_scripts(with_wireguard(), "delete")
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
                                    routes=[pt.KernelRouterRoute(dst="192.168.0.0/16")],
                                )
                            ],
                        ),
                    )
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ]

        _, create_hosts = build_scripts(with_zerotier(), "create")
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
        _, hosts = build_scripts(NODES, "delete")
        script = hosts["chassis-1"]
        self.assertIn(
            "systemctl disable --now ovn-kernel-kernel-0.service",
            script,
        )
        self.assertNotIn("ip netns delete", script)
        self.assertNotIn("dummy-", script)
        self.assertNotIn("ip addr", script)
        self.assertNotIn("ip route", script)
        _, create_hosts = build_scripts(NODES, "create")
        _, down = _router_branches(_router_script(create_hosts["chassis-1"]))
        self.assertIn("ip netns delete ns-kernel-0", down)

    def test_netns_removed_before_the_root_interface_bindings(self) -> None:
        # The netns owns the kernel router's devices (moved-in vlan/veth
        # legs), so it is torn down BEFORE the root-namespace binding
        # deletes run — otherwise `ip link delete` would target a device
        # still living inside the netns (2026-08-18). The netns teardown
        # now happens inside the router service's `down` (run by
        # `systemctl disable --now`), so that must precede the bindings.
        _, hosts = build_scripts(NODES, "delete")
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
                            _kr_interface(
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
        _, create_hosts = build_scripts(nodes, "create")
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
        _, create_hosts = build_scripts(NODES, "create")
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
                interfaces=[
                    _ls_interface(host="chassis-1", iface={"kind": "dummy", "shortName": "br-home"})
                ],
            ),
        )
        cluster, hosts = build_scripts(nodes, "create")
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
            build_scripts(nodes, "create")


class GeneratePythonDeployerTest(unittest.TestCase):
    def _exec_runtime(self) -> dict[str, Any]:
        ns: dict[str, Any] = {}
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

    def test_generated_host_function_issues_the_commands_the_trace_shows(self) -> None:
        # What ties every trace-based test in this file to the real
        # deployer: the generated Python renders the SAME emitter
        # operations build_scripts() traces, so running a generated host
        # function must issue exactly the argv the trace lists as commands.
        ns = self._exec_runtime()
        _, create_hosts = build_scripts(NODES, "create")
        # File writes (the router script + unit) are write_file() in the
        # generated Python, not commands — skip them from the trace so
        # only command argv are compared (2026-08-30).
        shell_commands = _shell_command_lines(create_hosts["chassis-1"])
        argv_seen: list[list[str]] = []
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            # File writes (the router script + unit) are write_file(), not
            # commands, and so are the install queue's jobs (queue_file()) —
            # no-op them so the run_cmd argv stays comparable.
            ns["write_file"] = mock.Mock()
            ns["queue_file"] = mock.Mock()
            ns["install_self"] = mock.Mock()
            ns["_host_chassis_1_create"](False, True, False, False)
            argv_seen = [call.args[0] for call in run.call_args_list]
        self.assertEqual(shell_commands, argv_seen)

    def test_no_start_enables_without_starting_the_fabric_units(self) -> None:
        # ovn-fabric-restore.service runs the host pass with --no-start:
        # the units are ordered After= the restore, so starting one from
        # inside the pass would wait forever on the pass itself.
        ns = self._exec_runtime()
        ns["write_file"] = mock.Mock()
        ns["queue_file"] = mock.Mock()
        ns["install_self"] = mock.Mock()
        with mock.patch("subprocess.run") as run:
            run.return_value.returncode = 0
            ns["_host_chassis_1_create"](False, True, True, True)
            argv = [call.args[0] for call in run.call_args_list]
        self.assertIn(["systemctl", "enable", "ovn-kernel-kernel-0.service"], argv)
        self.assertNotIn(["systemctl", "enable", "--now", "ovn-kernel-kernel-0.service"], argv)

    def test_runtime_exposes_the_cli_selectors(self) -> None:
        src = mod.generate_python_deployer(NODES)
        for marker in (
            "--action",
            "--cluster",
            "--host",
            "--verbose",
            "--skip-deps",
            "--no-start",
            "--ovn-purge",
            "def run_cmd",
            "def write_file",
            "def queue_file",
            "argparse.ArgumentParser",
            'choices=["create", "delete"]',
        ):
            self.assertIn(marker, src)

    def test_skip_deps_omits_only_the_install_queue(self) -> None:
        # --skip-deps (2026-10-10): the generated deployer runs against a
        # host whose packages are already present, so everything about
        # the install queue is wrapped in `if not skip_deps:` — dropping
        # just that, not the rest of the create pass.
        nodes = [
            (
                dataclasses.replace(
                    n,
                    data=dataclasses.replace(
                        n.data,
                        dependencies=["ovn", "ovs", "ip", "iptables"],
                    ),
                )
                if n.kind == "infra.host" and n.key.host == "chassis-1"
                else n
            )
            for n in NODES
        ]
        ns: dict[str, Any] = {}
        exec(mod.generate_python_deployer(nodes), ns)

        def _argv(skip_deps: bool) -> list[str]:
            seen: list[list[str]] = []
            with mock.patch("subprocess.run") as run:
                run.return_value.returncode = 0
                ns["write_file"] = mock.Mock()
                ns["queue_file"] = mock.Mock()
                ns["install_self"] = mock.Mock()
                ns["_host_chassis_1_create"](False, True, skip_deps, False)
                seen = [call.args[0] for call in run.call_args_list]
            return [" ".join(argv) for argv in seen]

        with_deps = _argv(False)
        self.assertIn("/usr/local/sbin/ovn-fabric-install --once", with_deps)
        # Each job is queued by the runtime (which names the file after
        # the time it runs at), in order: packages, then — once they
        # exist, after the last command — the units.
        queued = ns["queue_file"].call_args_list
        self.assertEqual({call.args[1] for call in queued}, {"/var/spool/ovn-fabric/install"})
        self.assertEqual(
            [call.args[2] for call in queued],
            [
                "package-ovn.json",
                "package-ovs.json",
                "package-ip.json",
                "package-iptables.json",
                "unit-ovn-kernel-kernel-0.service.json",
            ],
        )
        self.assertEqual(json.loads(queued[1].args[3])["packages"], ["openvswitch-switch"])
        # The rest of the create pass still runs (OVS/OVN setup).
        self.assertTrue(any("ovs-vsctl" in line for line in with_deps))

        # --skip-deps leaves the host's dependencies alone ALTOGETHER:
        # nothing is queued, no consumer is installed, asked or started.
        skipped = _argv(True)
        self.assertFalse(any("ovn-fabric-install" in line for line in skipped))
        self.assertTrue(any("ovs-vsctl" in line for line in skipped))
        ns["queue_file"].assert_not_called()
        ns["write_file"].assert_called()
        self.assertFalse(
            any("ovn-fabric-install" in call.args[1] for call in ns["write_file"].call_args_list)
        )

    def test_default_create_runs_hosts_before_cluster(self) -> None:
        # The dependencies-vs-commands ordering bug (2026-09-19): in the
        # all-in-one run, create must bring up every host FIRST (each host
        # pass installs its OS dependencies and, on the central, exposes
        # the NB/SB DBs) and run the cluster-wide ovn-nbctl pass LAST —
        # otherwise `ovn-nbctl ls-add ...` runs on a host that doesn't
        # have ovn installed yet. Delete reverses: cluster first.
        ns = self._exec_runtime()
        order: list[str] = []
        ns["cluster_create"] = lambda v, a: order.append("cluster:create")
        ns["cluster_delete"] = lambda v, a: order.append("cluster:delete")
        for name, fns in ns["HOSTS"].items():
            fns["create"] = (lambda n: lambda v, a, s, t: order.append(f"host:{n}:create"))(name)
            fns["delete"] = (lambda n: lambda v, a, s, t: order.append(f"host:{n}:delete"))(name)

        with mock.patch.object(sys, "argv", ["deployer", "--action", "create"]):
            ns["main"]()
        self.assertEqual(order[-1], "cluster:create")
        self.assertEqual(
            sorted(order[:-1]),
            sorted(f"host:{n}:create" for n in ns["HOSTS"]),
        )

        order.clear()
        with mock.patch.object(sys, "argv", ["deployer", "--action", "delete"]):
            ns["main"]()
        self.assertEqual(order[0], "cluster:delete")
        self.assertEqual(
            sorted(order[1:]),
            sorted(f"host:{n}:delete" for n in ns["HOSTS"]),
        )

    def test_cluster_selector_runs_only_the_cluster_pass(self) -> None:
        ns = self._exec_runtime()
        order: list[str] = []
        ns["cluster_create"] = lambda v, a: order.append("cluster:create")
        for name, fns in ns["HOSTS"].items():
            fns["create"] = (lambda n: lambda v, a, s, t: order.append(f"host:{n}:create"))(name)
        with mock.patch.object(sys, "argv", ["deployer", "--cluster"]):
            ns["main"]()
        self.assertEqual(order, ["cluster:create"])

    # ── --ovn-purge ──────────────────────────────────────────────────
    def _purge(self, *cli: str, system_id: str = '"sys-1"') -> tuple[list[str], mock.Mock, int]:
        """Run the generated deployer with `--ovn-purge` + `cli` against a
        faked subprocess; returns (commands in order, write_file mock,
        exit code). The system-id lookup answers `system_id`."""
        ns = self._exec_runtime()
        ns["write_file"] = mock.Mock()
        # A purge must never fall through into a deploy pass.
        ns["cluster_create"] = ns["cluster_delete"] = mock.Mock(side_effect=AssertionError)
        for fns in ns["HOSTS"].values():
            fns["create"] = fns["delete"] = mock.Mock(side_effect=AssertionError)

        def fake_run(argv: list[str], **_kwargs: Any) -> mock.Mock:
            is_lookup = argv[:2] == ["ovs-vsctl", "--if-exists"]
            return mock.Mock(returncode=0, stdout=f"{system_id}\n" if is_lookup else "")

        with mock.patch("subprocess.run", side_effect=fake_run) as run:
            with mock.patch.object(sys, "argv", ["deployer", "--ovn-purge", *cli]):
                code = ns["main"]()
            commands = [" ".join(call.args[0]) for call in run.call_args_list]
        return commands, ns["write_file"], code

    def test_ovn_purge_on_the_central_wipes_ovs_and_the_nb_sb_databases(self) -> None:
        commands, write_file, code = self._purge("--host", "central-1")
        self.assertEqual(code, 0)
        self.assertEqual(
            commands,
            [
                "ovs-vsctl --if-exists get open_vswitch . external_ids:system-id",
                "systemctl stop ovn-controller",
                "systemctl stop ovn-northd ovn-ovsdb-server-nb ovn-ovsdb-server-sb",
                "systemctl stop ovs-vswitchd ovsdb-server",
                "rm -f /var/lib/openvswitch/conf.db",
                "rm -f /var/lib/ovn/ovnnb_db.db /var/lib/ovn/ovnsb_db.db"
                " /var/lib/ovn/.ovnnb_db.db.~lock~ /var/lib/ovn/.ovnsb_db.db.~lock~",
                "systemctl start ovsdb-server ovs-vswitchd",
                # The chassis keeps its identity: system-id IS Chassis.name
                # in the SB database.
                "ovs-vsctl set open_vswitch . external_ids:system-id=sys-1",
                "systemctl start ovn-ovsdb-server-nb ovn-ovsdb-server-sb ovn-northd",
                "systemctl start ovn-controller",
            ],
        )
        write_file.assert_called_once_with(
            "host:central-1 (purge)", "/etc/openvswitch/system-id.conf", "sys-1", False, True
        )

    def test_ovn_purge_on_a_chassis_leaves_the_nb_sb_databases_alone(self) -> None:
        commands, _, _ = self._purge("--host", "chassis-1")
        self.assertIn("rm -f /var/lib/openvswitch/conf.db", commands)
        self.assertFalse(any("/var/lib/ovn/" in c or "ovn-northd" in c for c in commands))

    def test_ovn_purge_with_cluster_wipes_only_the_nb_sb_databases(self) -> None:
        commands, write_file, _ = self._purge("--cluster")
        self.assertEqual(
            commands,
            [
                "systemctl stop ovn-northd ovn-ovsdb-server-nb ovn-ovsdb-server-sb",
                "rm -f /var/lib/ovn/ovnnb_db.db /var/lib/ovn/ovnsb_db.db"
                " /var/lib/ovn/.ovnnb_db.db.~lock~ /var/lib/ovn/.ovnsb_db.db.~lock~",
                "systemctl start ovn-ovsdb-server-nb ovn-ovsdb-server-sb ovn-northd",
            ],
        )
        write_file.assert_not_called()

    def test_ovn_purge_without_a_selector_covers_every_host(self) -> None:
        commands, _, _ = self._purge()
        self.assertEqual(commands.count("rm -f /var/lib/openvswitch/conf.db"), 2)
        # ...and NB/SB once, for the one host the IR makes the central.
        self.assertEqual(sum(1 for c in commands if c.startswith("rm -f /var/lib/ovn/")), 1)

    def test_ovn_purge_without_a_system_id_restores_none(self) -> None:
        commands, write_file, _ = self._purge("--host", "chassis-1", system_id="")
        self.assertFalse(any("external_ids:system-id=" in c for c in commands))
        write_file.assert_not_called()
        self.assertEqual(commands[-1], "systemctl start ovn-controller")

    def test_ovn_purge_skips_a_host_outside_the_ovn_cluster(self) -> None:
        nodes = [
            (
                dataclasses.replace(n, data=dataclasses.replace(n.data, ovnRole=None))
                if n.kind == "infra.host" and n.key.host == "chassis-1"
                else n
            )
            for n in NODES
        ]
        ns: dict[str, Any] = {}
        exec(mod.generate_python_deployer(nodes), ns)
        stdout = io.StringIO()
        with mock.patch("subprocess.run") as run:
            with mock.patch.object(sys, "argv", ["deployer", "--ovn-purge", "--host", "chassis-1"]):
                with contextlib.redirect_stdout(stdout):
                    self.assertEqual(ns["main"](), 0)
        run.assert_not_called()
        self.assertIn("nothing to purge", stdout.getvalue())

    def test_ovn_purge_refuses_to_be_combined_with_an_action(self) -> None:
        # It replaces the action — an --action next to it is an error, not
        # something silently ignored or silently run as well.
        ns = self._exec_runtime()
        stderr = io.StringIO()
        with mock.patch("subprocess.run") as run:
            with mock.patch.object(sys, "argv", ["deployer", "--ovn-purge", "--action", "create"]):
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(stderr):
                    ns["main"]()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("--ovn-purge replaces the action", stderr.getvalue())
        run.assert_not_called()

    def test_ovn_purge_unknown_host_is_an_error(self) -> None:
        ns = self._exec_runtime()
        stderr = io.StringIO()
        with mock.patch("subprocess.run") as run:
            with mock.patch.object(sys, "argv", ["deployer", "--ovn-purge", "--host", "nope"]):
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(ns["main"](), 2)
        run.assert_not_called()

    def test_ovn_purge_tolerates_a_daemon_that_is_not_running(self) -> None:
        # Stops only warn (a half-installed host must still be purgeable);
        # a database that cannot be removed aborts.
        ns = self._exec_runtime()
        ns["write_file"] = mock.Mock()

        def fake_run(argv: list[str], **_kwargs: Any) -> mock.Mock:
            failing = argv[:2] == ["systemctl", "stop"]
            return mock.Mock(returncode=5 if failing else 0, stdout="")

        stderr = io.StringIO()
        with mock.patch("subprocess.run", side_effect=fake_run) as run:
            with contextlib.redirect_stderr(stderr):
                ns["ovn_purge"]("host:x (purge)", False, ovs=True, central=False)
            commands = [" ".join(call.args[0]) for call in run.call_args_list]
        self.assertIn("warning: failed in host:x (purge): systemctl stop", stderr.getvalue())
        self.assertEqual(commands[-1], "systemctl start ovn-controller")

        def rm_fails(argv: list[str], **_kwargs: Any) -> mock.Mock:
            return mock.Mock(returncode=1 if argv[0] == "rm" else 0, stdout="")

        with mock.patch("subprocess.run", side_effect=rm_fails):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
                ns["ovn_purge"]("host:x (purge)", False, ovs=True, central=False)

    def test_default_action_is_still_create(self) -> None:
        # --action lost its argparse default so --ovn-purge can tell
        # whether one was given; with neither flag the run is a create.
        ns = self._exec_runtime()
        order: list[str] = []
        ns["cluster_create"] = lambda v, a: order.append(f"cluster:create:abort={a}")
        for fns in ns["HOSTS"].values():
            fns["create"] = lambda v, a, s, t: order.append("host:create")
        with mock.patch.object(sys, "argv", ["deployer"]):
            self.assertEqual(ns["main"](), 0)
        self.assertEqual(order[-1], "cluster:create:abort=True")

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
        # The append() emitter operation at runtime: the file holds the
        # content plus a single trailing newline (2026-08-23).
        ns = self._exec_runtime()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "unit.service")
            ns["write_file"]("host", path, "[Unit]\nDescription=x", False, False)
            with open(path, encoding="utf-8") as out:
                self.assertEqual(out.read(), "[Unit]\nDescription=x\n")

    def test_queue_file_names_a_file_after_the_time_it_is_queued(self) -> None:
        # The enqueue() emitter operation at runtime: the file's name
        # starts with a UTC timestamp, so a queue's files sort by name
        # into the order they were queued in — here against the alphabet.
        ns = self._exec_runtime()
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("unit-z.json", "package-m.json", "image-a.json"):
                ns["queue_file"]("host", tmp, name, f'{{"name": "{name}"}}', False, True)
            names = sorted(os.listdir(tmp))
            self.assertEqual(
                [name.split("-", 1)[1] for name in names],
                ["unit-z.json", "package-m.json", "image-a.json"],
            )
            for name in names:
                self.assertRegex(name, r"^\d{8}T\d{6}\.\d{6}Z-[a-z]+-[a-z]\.json$")
            # Whole files under their final name only — nothing half-written
            # is left for the consumer to trip over.
            with open(os.path.join(tmp, names[0]), encoding="utf-8") as out:
                self.assertEqual(out.read(), '{"name": "unit-z.json"}\n')

    def test_queue_stamps_never_repeat_or_go_back(self) -> None:
        # Two files queued within the same microsecond — or across a clock
        # that steps back — must still sort in the order they were queued.
        ns = self._exec_runtime()
        at = 1_791_748_501_000_123_000  # ns
        with mock.patch.object(ns["time"], "time_ns", side_effect=[at, at, at - 5_000_000, at]):
            stamps = [ns["queue_stamp"]() for _ in range(4)]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), 4)
        self.assertEqual(stamps[0], "20261011T195501.000123Z")
        self.assertEqual(stamps[3], "20261011T195501.000126Z")

    def test_queue_file_follows_the_runtimes_error_policy(self) -> None:
        ns = self._exec_runtime()
        missing = os.path.join(tempfile.gettempdir(), "ovn-fabric-no-such-queue")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ns["queue_file"]("host", missing, "unit-x.json", "{}", False, False)
        self.assertIn("warning: cannot queue in host", stderr.getvalue())
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(stderr):
            ns["queue_file"]("host", missing, "unit-x.json", "{}", False, True)
        self.assertEqual(ctx.exception.code, 127)


if __name__ == "__main__":
    unittest.main()


# A kernel.service node (a workload) renders ONE container, with one veth
# per REFERENCING ovn.lrp (each lrp carries a serviceRef — its NIC).
class KernelServiceDeployTest(unittest.TestCase):
    def _ls(self, name: str, short: str, vlan: int) -> pt.OvnLsNode:
        return pt.OvnLsNode(
            id=f"ls:{name}",
            kind="ovn.ls",
            key=pt.OvnLsKey(name=name),
            data=pt.OvnLsData(
                interfaces=[
                    _ls_interface(
                        host="chassis-1",
                        iface={
                            "kind": "vlan",
                            "vlanParent": "eth0",
                            "vlanId": vlan,
                            "shortName": short,
                        },
                    )
                ]
            ),
        )

    def _lrp(self, router: str, side: str, seg: str, ref: pt.ServiceRef) -> pt.OvnLrpNode:
        # Endpoint identity is its own `name` (no more positional side);
        # keep the historical lrp-<router>-<side> shape so the emitted
        # lrp/lsp names (and the assertions on them) stay unchanged.
        name = f"lrp-{router}-{side}"
        return pt.OvnLrpNode(
            id=f"ovnrouter:{router}|lrp:{name}",
            kind="ovn.lrp",
            key=pt.OvnLrpKey(ovnrouter=router, name=name),
            data=pt.OvnLrpData(
                l2Segment=f"ls:{seg}",
                addresses=[],
                mac="02:00:00:00:00:01",
                serviceRefs=[ref],
            ),
        )

    def test_kernel_service_two_nics_create_and_delete(self) -> None:
        seg = self._ls("container-seg", "br-cont", 1130)
        cp = self._ls("control-plane", "br-cp", 1143)
        svc = pt.KernelAppContainerNode(
            id="kernel.app.container:dns",
            kind="kernel.app.container",
            key=pt.KernelServiceKey(name="dns"),
            data=pt.KernelAppContainerData(
                host="host:chassis-1",
                image="ovn-fabric-dns",
                cmd=["/usr/sbin/dnsmasq", "--no-daemon"],
            ),
        )
        lrp_seg = self._lrp(
            "router-x",
            "left",
            "container-seg",
            pt.ServiceRef(
                service="kernel.app.container:dns",
                name="container-seg",
                ipaddrs=["192.168.50.53/24"],
            ),
        )
        lrp_cp = self._lrp(
            "router-cp",
            "left",
            "control-plane",
            pt.ServiceRef(
                service="kernel.app.container:dns",
                name="control-plane",
                ipaddrs=["10.43.0.5/24"],
                routes=[pt.KernelRouterRoute(dst="0.0.0.0/0", via="10.43.0.1", metric=200)],
            ),
        )
        nodes = list(NODES) + [seg, cp, svc, lrp_seg, lrp_cp]
        _, create_hosts = build_scripts(nodes, "create")
        create = create_hosts["chassis-1"]
        self.assertIn("cat > /usr/local/sbin/ovn-kernel-service-dns.sh << 'OVN'", create)
        self.assertIn('image="ovn-fabric-dns"', create)
        # The container (and its netns) is ovn-fabric-prefixed.
        self.assertIn('container="ovn-fabric-dns"', create)
        # In-container NICs are named after their SEGMENT (IFNAMSIZ-safe).
        self.assertIn(
            'ip netns exec "$container" ip addr add 192.168.50.53/24 dev "container-seg"',
            create,
        )
        self.assertIn(
            'ip netns exec "$container" ip addr add 10.43.0.5/24 dev "control-plane"',
            create,
        )
        # Per-NIC source-based policy routing: the route goes into its own
        # table, selected by a rule matching the NIC's own source prefix.
        # A DEFAULT is additionally placed in `main` (with a metric) so
        # locally-originated traffic can pick a source at all (2026-09-19).
        self.assertRegex(
            create,
            r'ip netns exec "\$container" ip -4 route add 0\.0\.0\.0/0'
            r" via 10\.43\.0\.1 metric \d+ table 100\d",
        )
        self.assertRegex(
            create,
            r'ip netns exec "\$container" ip -4 route add 0\.0\.0\.0/0'
            r" via 10\.43\.0\.1 metric \d+\n",
        )
        self.assertRegex(
            create,
            r'ip netns exec "\$container" ip -4 rule add from 10\.43\.0\.0/24 lookup 100\d',
        )
        self.assertIn('ovs-vsctl add-port "br-cont" "sc-', create)
        self.assertIn('ovs-vsctl add-port "br-cp" "sc-', create)
        # Create installs + starts a systemd unit (like the kernel router).
        self.assertIn(
            "cat > /etc/systemd/system/ovn-kernel-service-dns.service << 'OVN'",
            create,
        )
        self.assertIn("systemctl enable --now ovn-kernel-service-dns.service", create)
        # The `down` branch (inside the script) removes the container + ports.
        self.assertIn('/usr/bin/docker rm -f "ovn-fabric-dns"', create)
        self.assertIn('del-port "br-cont" "sc-', create)
        self.assertIn('del-port "br-cp" "sc-', create)
        _, delete_hosts = build_scripts(nodes, "delete")
        delete = delete_hosts["chassis-1"]
        self.assertIn("systemctl disable --now ovn-kernel-service-dns.service", delete)

    def test_kernel_service_nic_gets_the_generator_resolved_mac(self) -> None:
        # The container is recreated on every `up`; a NIC with a random
        # MAC would leave the segment's OVN router holding a MAC_Binding
        # for the PREVIOUS NIC. ServiceRef.mac (resolved by the generator)
        # pins the container-side leg; a ref without one (an older IR)
        # keeps the plain `ip link add`.
        seg = self._ls("container-seg", "br-cont", 1130)
        cp = self._ls("control-plane", "br-cp", 1143)
        svc = pt.KernelAppContainerNode(
            id="kernel.app.container:dns",
            kind="kernel.app.container",
            key=pt.KernelServiceKey(name="dns"),
            data=pt.KernelAppContainerData(host="host:chassis-1", image="ovn-fabric-dns"),
        )
        lrp_seg = self._lrp(
            "router-x",
            "left",
            "container-seg",
            pt.ServiceRef(
                service="kernel.app.container:dns",
                name="container-seg",
                mac="00:00:c0:a8:32:35",
                ipaddrs=["192.168.50.53/24"],
            ),
        )
        lrp_cp = self._lrp(
            "router-cp",
            "left",
            "control-plane",
            pt.ServiceRef(
                service="kernel.app.container:dns",
                name="control-plane",
                ipaddrs=["10.43.0.5/24"],
            ),
        )
        nodes = list(NODES) + [seg, cp, svc, lrp_seg, lrp_cp]
        _, create_hosts = build_scripts(nodes, "create")
        create = create_hosts["chassis-1"]
        links = [line for line in create.splitlines() if line.startswith('ip link add "sc-')]
        self.assertEqual(len(links), 2)
        pinned = [line for line in links if " address " in line]
        self.assertEqual(len(pinned), 1)
        self.assertRegex(
            pinned[0],
            r'^ip link add "(sc-[^"]+)" type veth peer name "\1c" address 00:00:c0:a8:32:35$',
        )

    def test_service_resolver_renders_docker_dns_flags(self) -> None:
        # A resolved ServiceRef.resolver becomes `docker run --dns …` (Docker
        # writes /etc/resolv.conf; nothing is bind-mounted over it).
        seg = self._ls("container-seg", "br-cont", 1130)
        svc = pt.KernelAppContainerNode(
            id="kernel.app.container:dns",
            kind="kernel.app.container",
            key=pt.KernelServiceKey(name="dns"),
            data=pt.KernelAppContainerData(
                host="host:chassis-1",
                image="ovn-fabric-dns",
                cmd=["/usr/sbin/dnsmasq", "--no-daemon"],
            ),
        )
        lrp = self._lrp(
            "router-x",
            "left",
            "container-seg",
            pt.ServiceRef(
                service="kernel.app.container:dns",
                name="container-seg",
                ipaddrs=["192.168.50.53/24"],
                resolver=pt.Resolver(nameservers=["10.43.0.10", "1.1.1.1"]),
            ),
        )
        nodes = list(NODES) + [seg, svc, lrp]
        _, create_hosts = build_scripts(nodes, "create")
        create = create_hosts["chassis-1"]
        self.assertIn("--dns 10.43.0.10 --dns 1.1.1.1", create)

    def test_kernel_service_ipv6_routes_use_the_v6_family(self) -> None:
        cp = self._ls("control-plane", "br-cp", 1143)
        svc = pt.KernelAppContainerNode(
            id="kernel.app.container:dns6",
            kind="kernel.app.container",
            key=pt.KernelServiceKey(name="dns6"),
            data=pt.KernelAppContainerData(
                host="host:chassis-1",
                image="ovn-fabric-dns",
                cmd=["/usr/sbin/dnsmasq", "--no-daemon"],
            ),
        )
        lrp = self._lrp(
            "router-cp",
            "left",
            "control-plane",
            pt.ServiceRef(
                service="kernel.app.container:dns6",
                name="control-plane",
                ipaddrs=["10.43.0.5/24", "2600:70ff:b3c0::cb:5/112"],
                routes=[
                    pt.KernelRouterRoute(dst="192.168.129.0/24", via="10.43.0.1", metric=100),
                    pt.KernelRouterRoute(
                        dst="2600:70ff:b3c0:129::/64",
                        via="2600:70ff:b3c0::cb:1",
                        metric=100,
                    ),
                ],
            ),
        )
        nodes = list(NODES) + [cp, svc, lrp]
        _, create_hosts = build_scripts(nodes, "create")
        create = create_hosts["chassis-1"]
        # IPv6 route/rule MUST carry `-6`: `ip route`/`ip rule` default to
        # IPv4 and reject a v6 prefix/source with "Invalid source address"
        # (hit live 2026-09-16).
        self.assertRegex(
            create,
            r'ip netns exec "\$container" ip -6 route add 2600:70ff:b3c0:129::/64'
            r" via 2600:70ff:b3c0::cb:1 metric \d+ table 100\d",
        )
        self.assertRegex(
            create,
            r'ip netns exec "\$container" ip -6 rule add from'
            r" 2600:70ff:b3c0::cb:0/112 lookup 100\d",
        )


# Two DISTINCT kernel.service nodes referenced by separate LRPs render two
# independent containers (one per service).
class MultiServiceDeployTest(unittest.TestCase):
    def test_two_services_render_two_containers(self) -> None:
        seg = pt.OvnLsNode(
            id="ls:seg-a",
            kind="ovn.ls",
            key=pt.OvnLsKey(name="seg-a"),
            data=pt.OvnLsData(
                interfaces=[
                    _ls_interface(
                        host="chassis-1",
                        iface={
                            "kind": "vlan",
                            "vlanParent": "eth0",
                            "vlanId": 1128,
                            "shortName": "br-a",
                        },
                    )
                ]
            ),
        )
        seg2 = pt.OvnLsNode(
            id="ls:seg-b",
            kind="ovn.ls",
            key=pt.OvnLsKey(name="seg-b"),
            data=pt.OvnLsData(
                interfaces=[
                    _ls_interface(
                        host="chassis-1",
                        iface={
                            "kind": "vlan",
                            "vlanParent": "eth0",
                            "vlanId": 1129,
                            "shortName": "br-b",
                        },
                    )
                ]
            ),
        )

        def svc(n: str) -> pt.KernelAppContainerNode:
            return pt.KernelAppContainerNode(
                id=f"kernel.app.container:{n}",
                kind="kernel.app.container",
                key=pt.KernelServiceKey(name=n),
                data=pt.KernelAppContainerData(host="host:chassis-1", image=f"img-{n}"),
            )

        def lrp(router: str, seg_name: str, service: str) -> pt.OvnLrpNode:
            name = f"lrp-{router}-left"
            return pt.OvnLrpNode(
                id=f"ovnrouter:{router}|lrp:{name}",
                kind="ovn.lrp",
                key=pt.OvnLrpKey(ovnrouter=router, name=name),
                data=pt.OvnLrpData(
                    l2Segment=f"ls:{seg_name}",
                    addresses=[],
                    mac="02:00:00:00:00:01",
                    serviceRefs=[
                        pt.ServiceRef(
                            service=f"kernel.app.container:{service}",
                            name="seg-a",
                            ipaddrs=["10.0.0.5/24"],
                        )
                    ],
                ),
            )

        nodes = list(NODES) + [
            seg,
            seg2,
            svc("dns-128"),
            svc("dns-129"),
            lrp("router-a", "seg-a", "dns-128"),
            lrp("router-b", "seg-b", "dns-129"),
        ]
        _, create_hosts = build_scripts(nodes, "create")
        create = create_hosts["chassis-1"]
        self.assertIn("ovn-kernel-service-dns-128.sh", create)
        self.assertIn("ovn-kernel-service-dns-129.sh", create)
        self.assertIn('image="img-dns-128"', create)
        self.assertIn('image="img-dns-129"', create)


# End-to-end for the REAL shape: a dns service attached to a real segment
# (management-v2, a localnet vlan) AND the control plane (explicit
# default route) — hydrated from IR JSON and rendered to the host shell.
class DnsServiceEndToEndTest(unittest.TestCase):
    def test_dns_on_real_segment_and_control_plane(self) -> None:
        raw = [
            {
                "id": "host:chassis-1",
                "kind": "infra.host",
                "key": {"host": "chassis-1"},
                "data": {
                    "connectAddress": "10.0.0.6",
                    "ovnRole": "central",
                    "os": {"name": "ubuntu", "version": "26.04"},
                },
            },
            {
                "id": "ls:management-v2",
                "kind": "ovn.ls",
                "key": {"name": "management-v2"},
                "data": {
                    "interfaces": [
                        {
                            "host": "chassis-1",
                            "iface": {
                                "kind": "vlan",
                                "vlanParent": "eth0",
                                "vlanId": 1129,
                                "shortName": "br-management-v2",
                            },
                        }
                    ]
                },
            },
            {
                "id": "ls:control-plane",
                "kind": "ovn.ls",
                "key": {"name": "control-plane"},
                "data": {
                    "interfaces": [
                        {
                            "host": "chassis-1",
                            "iface": {
                                "kind": "vlan",
                                "vlanParent": "eth0",
                                "vlanId": 1143,
                                "shortName": "br-control-plane",
                            },
                        }
                    ]
                },
            },
            {
                "id": "kernel.app.container:dns-129",
                "kind": "kernel.app.container",
                "key": {"name": "dns-129"},
                "data": {
                    "host": "host:chassis-1",
                    "image": "ovn-fabric-dns",
                    "cmd": ["/usr/sbin/dnsmasq", "--no-daemon", "--no-resolv", "--server=1.1.1.1"],
                },
            },
            {
                "id": "ovnrouter:router-management-v2|lrp:lrp-router-management-v2-left",
                "kind": "ovn.lrp",
                "key": {
                    "ovnrouter": "router-management-v2",
                    "name": "lrp-router-management-v2-left",
                },
                "data": {
                    "l2Segment": "ls:management-v2",
                    "addresses": ["192.168.129.1/24"],
                    "mac": "02:00:00:00:00:01",
                    "serviceRefs": [
                        {
                            "service": "kernel.app.container:dns-129",
                            "name": "management-v2",
                            "ipaddrs": ["192.168.129.5/24"],
                        }
                    ],
                },
            },
            {
                "id": "ovnrouter:router-control-plane-v2|lrp:lrp-router-control-plane-v2-left",
                "kind": "ovn.lrp",
                "key": {
                    "ovnrouter": "router-control-plane-v2",
                    "name": "lrp-router-control-plane-v2-left",
                },
                "data": {
                    "l2Segment": "ls:control-plane",
                    "addresses": ["10.43.0.1/24"],
                    "mac": "02:00:00:00:00:02",
                    "serviceRefs": [
                        {
                            "service": "kernel.app.container:dns-129",
                            "name": "control-plane",
                            "ipaddrs": ["10.43.0.129/24"],
                            "routes": [{"dst": "0.0.0.0/0", "via": "10.43.0.1", "metric": 200}],
                        }
                    ],
                },
            },
        ]
        from protocol.hydrate import hydrate_nodes

        nodes = hydrate_nodes(raw)
        _, create_hosts = build_scripts(nodes, "create")
        s = create_hosts["chassis-1"]
        self.assertIn("ovn-kernel-service-dns-129.sh", s)
        # Serving NIC on the real segment, admin NIC on the control plane.
        self.assertIn(
            'ip netns exec "$container" ip addr add 192.168.129.5/24 dev "management-v2"',
            s,
        )
        self.assertIn(
            'ip netns exec "$container" ip addr add 10.43.0.129/24 dev "control-plane"',
            s,
        )
        self.assertRegex(
            s,
            r'ip netns exec "\$container" ip -4 route add 0\.0\.0\.0/0'
            r" via 10\.43\.0\.1 metric \d+ table 100\d",
        )
        # ...and the same default ALSO in `main` (no table), so unbound
        # traffic can source-select (2026-09-19).
        self.assertRegex(
            s,
            r'ip netns exec "\$container" ip -4 route add 0\.0\.0\.0/0'
            r" via 10\.43\.0\.1 metric \d+\n",
        )
        self.assertRegex(
            s,
            r'ip netns exec "\$container" ip -4 rule add from 10\.43\.0\.0/24 lookup 100\d',
        )
        self.assertIn('ovs-vsctl add-port "br-management-v2" "sc-', s)
        self.assertIn('ovs-vsctl add-port "br-control-plane" "sc-', s)
        # Same systemd mechanics as the kernel router: one unit per service.
        self.assertIn(
            "cat > /etc/systemd/system/ovn-kernel-service-dns-129.service << 'OVN'",
            s,
        )
        self.assertIn("systemctl enable --now ovn-kernel-service-dns-129.service", s)


# Flow export (2026-10-10): WHERE to export is host-level
# (infra.host.data.ipfix, from Host.monitoring.ipfix); WHETHER a given
# leg is sampled rides its ovn.ls interface blob as `iface.flow` (only
# present when a leg opts OUT). The deployer turns that into one IPFIX
# sampler per segment bridge: attach on create, clear on delete, and
# clear (not attach) for a bridge any of whose legs opted out.
def _with_ipfix(
    nodes: list[pt.Model], flow_off_domains: Collection[str] = frozenset()
) -> list[pt.Model]:
    out: list[pt.Model] = []
    for n in nodes:
        if n.kind == "infra.host" and n.key.host == "chassis-1":
            n = dataclasses.replace(
                n,
                data=dataclasses.replace(
                    n.data,
                    ipfix=pt.IpfixData(targets="192.168.129.88:4739", sampling=512),
                ),
            )
        if n.kind == "ovn.ls" and n.key.name in flow_off_domains:
            ifaces = []
            for entry in n.data.interfaces:
                iface = dataclasses.replace(entry.iface, flow=False)
                ifaces.append(dataclasses.replace(entry, iface=iface))
            n = dataclasses.replace(n, data=dataclasses.replace(n.data, interfaces=ifaces))
        out.append(n)
    return out


class IpfixExportTest(unittest.TestCase):
    def test_ipfix_attaches_to_the_segment_bridge_on_create(self) -> None:
        _, hosts = build_scripts(_with_ipfix(NODES), "create")
        script = hosts["chassis-1"]
        self.assertIn("# --- flow export (IPFIX) ---", script)
        # Idempotent: reuse the bridge's existing IPFIX row, else create.
        # `get` prints the empty-set literal `[]` when the bridge has no
        # IPFIX, so the reuse test must reject that, not just empty output.
        self.assertIn('[ "$ipfix" != "[]" ]', script)
        # `targets` is a set-of-strings and its value has a `:`, so it must
        # reach ovs-vsctl quoted (escaped here for the `sh -c`).
        self.assertIn('create IPFIX targets=\\"192.168.129.88:4739\\" sampling=512', script)
        self.assertIn("set Bridge br-home ipfix=@ipfix", script)

    def test_a_leg_that_opts_out_clears_its_bridge(self) -> None:
        _, hosts = build_scripts(_with_ipfix(NODES, {"home"}), "create")
        script = hosts["chassis-1"]
        self.assertNotIn("create IPFIX", script)
        self.assertIn("--if-exists clear Bridge br-home ipfix", script)

    def test_delete_clears_the_sampler(self) -> None:
        _, hosts = build_scripts(_with_ipfix(NODES), "delete")
        self.assertIn("--if-exists clear Bridge br-home ipfix", hosts["chassis-1"])

    def test_no_ipfix_config_emits_nothing(self) -> None:
        _, hosts = build_scripts(NODES, "create")
        self.assertNotIn("IPFIX", hosts["chassis-1"])
