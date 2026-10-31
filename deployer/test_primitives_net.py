from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from deployer.context import Ctx
from deployer.lifecycle import LifecycleAction, LifecycleController, Operation
from deployer.primitives_host import HostPrimitive


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
    lc.lazy.clear()


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
                netns.add_rule(
                    "iptables",
                    "nat",
                    "POSTROUTING",
                    ("-o", "veth-krn", "-j", "MASQUERADE"),
                ),
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

    def test_rule_create_appends_and_delete_uses_delete(self) -> None:
        # Strict create: no `-C` existence probe; a duplicate rule is the
        # caller's error, not something create papers over.
        async def run() -> None:
            netns = HostPrimitive("h").netns("ns-router")
            rule = netns.add_rule("iptables", "filter", "INPUT", ("-j", "ACCEPT"))
            prefix = ("ip", "netns", "exec", "ns-router", "iptables", "-t", "filter")
            for action, flag in ((LifecycleAction.CREATE, "-A"), (LifecycleAction.DELETE, "-D")):
                job = FakeJob()
                lc = LifecycleController(Ctx(), job, action, "h")
                await _now(lc, rule)
                self.assertEqual(
                    [command[1] for command in job.commands],
                    [(*prefix, flag, "INPUT", "-j", "ACCEPT")],
                )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
