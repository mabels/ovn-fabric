from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from fabric.context import Ctx
from fabric.lifecycle import LifecycleAction, LifecycleController, Operation
from fabric.primitives.host import HostPrimitive


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
        timeout: float | None = None,
    ) -> bool:
        self.commands.append((label, tuple(argv)))
        if argv and argv[-1] == "true":
            return False
        if "-C" in argv or "show" in argv:
            return False
        return True

    async def capture_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        timeout: float | None = None,
    ) -> str:
        return ""

    def info(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        pass


async def _now(lc: LifecycleController, operation: Operation) -> None:
    """Run one operation right away, in the controller's direction."""
    await lc.run(operation)
    await lc.execute()
    lc.queue.clear()


class NetnsPrimitiveTest(unittest.TestCase):
    def test_kernel_is_the_root_netns(self) -> None:
        host = HostPrimitive("h")
        self.assertIsNone(host.kernel.name)
        self.assertEqual(host.kernel.host, "h")

    def test_netns_baseline_links_addrs_routes_and_rules(self) -> None:
        async def run() -> None:
            job = FakeJob()
            host = HostPrimitive("h")
            netns = host.netns("ns-router")
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, host.netns(None))
            await _now(lc, netns)
            await _now(lc, host.ovs_bridge("br-home"))
            await _now(
                lc,
                netns.add_link(
                    "veth-krn",
                    "veth",
                    mac="00:00:00:00:01:02",
                    root_leg="veth-ovn",
                    bridge="br-home",
                ),
            )
            await _now(lc, netns.add_addr("veth-krn", "10.0.0.1/24"))
            await _now(lc, netns.add_route("0.0.0.0/0", "veth-krn", "10.0.0.254"))
            await _now(lc, netns.set_sysctl("net.ipv4.ip_forward", "1"))
            await _now(
                lc,
                netns.add_masq("ipv4", "veth-krn"),
            )

            for node in (
                "netns:ns-router",
                "link:ns-router|veth-krn",
                "link:*global*|veth-ovn",
                "addr:ns-router|veth-krn|10.0.0.1/24",
                "route:ns-router|any|main|0.0.0.0/0|veth-krn|10.0.0.254|",
                "sysctl:ns-router|net.ipv4.ip_forward",
            ):
                self.assertIn(node, lc.graph.nodes)
            rule = next(
                key
                for key in lc.graph.nodes
                if key.startswith("iptables.rule:ns-router|iptables|nat|POSTROUTING|")
            )
            self.assertIn(rule, lc.graph.nodes)
            lc.graph.validate()

            argv = [command[1] for command in job.commands]
            self.assertIn(("ip", "netns", "add", "ns-router"), argv)
            self.assertIn(
                (
                    "ip",
                    "link",
                    "add",
                    "veth-ovn",
                    "type",
                    "veth",
                    "peer",
                    "name",
                    "veth-krn",
                    "address",
                    "00:00:00:00:01:02",
                ),
                argv,
            )
            self.assertIn(("ip", "link", "set", "veth-krn", "netns", "ns-router"), argv)
            self.assertIn(
                (
                    "ip",
                    "netns",
                    "exec",
                    "ns-router",
                    "ip",
                    "addr",
                    "add",
                    "10.0.0.1/24",
                    "dev",
                    "veth-krn",
                ),
                argv,
            )
            self.assertIn(
                (
                    "ip",
                    "netns",
                    "exec",
                    "ns-router",
                    "iptables",
                    "-t",
                    "nat",
                    "-A",
                    "POSTROUTING",
                    "-o",
                    "veth-krn",
                    "-j",
                    "MASQUERADE",
                ),
                argv,
            )

        asyncio.run(run())

    def test_wireguard_up_down_inside_the_netns(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            netns = HostPrimitive("h").netns("ns-router")
            await _now(lc, netns.wireguard("mullvad-us", "/etc/wireguard/mullvad-us.conf"))
            self.assertIn(
                (
                    "ip",
                    "netns",
                    "exec",
                    "ns-router",
                    "wg-quick",
                    "up",
                    "/etc/wireguard/mullvad-us.conf",
                ),
                [command[1] for command in job.commands],
            )
            self.assertIn("wireguard:ns-router|mullvad-us", lc.graph.nodes)

        asyncio.run(run())

    def test_container_nic_rename_and_root_port_cleanup(self) -> None:
        async def run() -> None:
            host = HostPrimitive("h")
            netns = host.container_netns("ovn-fabric-dns")
            link = netns.add_link(
                "sc-1c",
                "veth",
                rename_to="management-v2",
                root_leg="sc-1",
                bridge="br-mgmt",
            )
            create = [command.argv for command in link.as_primitive().create_cmds]
            self.assertIn(
                (
                    "ip",
                    "netns",
                    "exec",
                    "ovn-fabric-dns",
                    "ip",
                    "link",
                    "set",
                    "sc-1c",
                    "name",
                    "management-v2",
                ),
                create,
            )
            delete = [command.argv for command in link.as_primitive().delete_cmds]
            self.assertIn(
                ("ovs-vsctl", "--if-exists", "del-port", "br-mgmt", "sc-1"),
                delete,
            )

        asyncio.run(run())

    def test_a_lent_link_moves_to_the_container_and_back(self) -> None:
        async def run() -> None:
            host = HostPrimitive("h")
            router_ns = host.netns("ns-router")
            container_ns = host.docker("c", "img").get_netns()
            move = router_ns.move_link("eth0.2280", container_ns)

            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, move)
            self.assertEqual(
                [argv for _label, argv in job.commands],
                [
                    (
                        "ip",
                        "netns",
                        "exec",
                        "ns-router",
                        "ip",
                        "link",
                        "set",
                        "eth0.2280",
                        "netns",
                        "c",
                    ),
                    ("ip", "netns", "exec", "c", "ip", "link", "set", "eth0.2280", "up"),
                ],
            )
            # It is the container's link now.
            self.assertEqual(lc.graph.nodes["link:c|eth0.2280"]["netnsRef"], "netns:c")

            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(lc, move)
            self.assertEqual(
                [argv for _label, argv in job.commands],
                [
                    (
                        "ip",
                        "netns",
                        "exec",
                        "c",
                        "ip",
                        "link",
                        "set",
                        "eth0.2280",
                        "netns",
                        "ns-router",
                    ),
                    # back up, as its owner had it
                    ("ip", "netns", "exec", "ns-router", "ip", "link", "set", "eth0.2280", "up"),
                ],
            )

        asyncio.run(run())

    def test_moving_a_link_needs_two_named_namespaces(self) -> None:
        host = HostPrimitive("h")
        with self.assertRaises(ValueError):
            host.rootNs().move_link("eth0", host.netns("ns-x"))

    def test_rule_create_appends_and_delete_uses_delete(self) -> None:
        # Strict create: no `-C` existence probe; a duplicate rule is the
        # caller's error, not something create papers over.
        async def run() -> None:
            netns = HostPrimitive("h").netns("ns-router")
            # A default covers both families; delete removes in reverse.
            rule = netns.add_default("input", "allow")
            prefix = ("ip", "netns", "exec", "ns-router")
            for action, flag, binaries in (
                (LifecycleAction.CREATE, "-A", ("iptables", "ip6tables")),
                (LifecycleAction.DELETE, "-D", ("ip6tables", "iptables")),
            ):
                job = FakeJob()
                lc = LifecycleController(Ctx(), job, action, "h")
                await _now(lc, rule)
                self.assertEqual(
                    [command[1] for command in job.commands],
                    [
                        (*prefix, binary, "-t", "filter", flag, "INPUT", "-j", "ACCEPT")
                        for binary in binaries
                    ],
                )

        asyncio.run(run())

    def test_each_named_rule_says_what_it_is(self) -> None:
        netns = HostPrimitive("h").netns("ns-x")
        cases = [
            (
                netns.add_stateful_accept("ipv6", "forward"),
                (
                    "ip6tables",
                    "-t",
                    "filter",
                    "-A",
                    "FORWARD",
                    "-m",
                    "conntrack",
                    "--ctstate",
                    "ESTABLISHED,RELATED",
                    "-j",
                    "ACCEPT",
                ),
            ),
            (
                netns.add_masq("ipv4", "wg0"),
                ("iptables", "-t", "nat", "-A", "POSTROUTING", "-o", "wg0", "-j", "MASQUERADE"),
            ),
            (
                netns.add_filter(
                    "ipv4",
                    "input",
                    "allow",
                    sources=["10.0.0.0/8"],
                    exclude_sources=["10.9.0.0/16"],
                    protocol="tcp",
                    ports=[(8000, 8100)],
                ),
                (
                    "iptables",
                    "-t",
                    "filter",
                    "-A",
                    "INPUT",
                    "-s",
                    "10.0.0.0/8",
                    "!",
                    "-s",
                    "10.9.0.0/16",
                    "-p",
                    "tcp",
                    "--dport",
                    "8000:8100",
                    "-j",
                    "ACCEPT",
                ),
            ),
        ]
        for rule, argv in cases:
            with self.subTest(rule=type(rule).__name__):
                self.assertEqual(
                    rule.as_primitive().create_cmds[0].argv, ("ip", "netns", "exec", "ns-x", *argv)
                )

    def test_a_filter_rule_is_its_whole_cross_product(self) -> None:
        rule = (
            HostPrimitive("h")
            .netns("ns-x")
            .add_filter(
                "ipv6",
                "forward",
                "deny",
                sources=["fd00::/8", "fc00::/8"],
                protocol="udp",
                ports=[(53, 53), (5000, 5010)],
            )
        )
        primitive = rule.as_primitive()
        self.assertEqual(
            [c.argv[4:] for c in primitive.create_cmds],
            [
                ("ip6tables", "-t", "filter", "-A", "FORWARD", "-s", source, "-p", "udp",
                 "--dport", port, "-j", "DROP")
                for source in ("fd00::/8", "fc00::/8")
                for port in ("53", "5000:5010")
            ],
        )  # fmt: skip
        self.assertEqual(len(primitive.nodes), 4)
        self.assertEqual(
            [c.argv[4:] for c in primitive.delete_cmds],
            [
                tuple("-D" if a == "-A" else a for a in c.argv[4:])
                for c in reversed(primitive.create_cmds)
            ],
        )

    def test_ports_need_tcp_or_udp(self) -> None:
        with self.assertRaises(ValueError):
            HostPrimitive("h").netns("ns-x").add_filter("ipv4", "input", "allow", ports=[(22, 22)])


if __name__ == "__main__":
    unittest.main()
