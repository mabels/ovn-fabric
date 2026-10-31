from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from deployer.context import Ctx
from deployer.lifecycle import LifecycleAction, LifecycleController, Operation
from deployer.primitives_host import HostPrimitive
from deployer.primitives_ovs import OVSBridge, OVSExternalId, OVSMapping, OVSPort


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
        # `br-exists` / `port-to-br` report "missing" so create runs.
        return tuple(argv[:2]) not in {
            ("ovs-vsctl", "br-exists"),
            ("ovs-vsctl", "port-to-br"),
            ("ip", "link"),
        }

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


class OvsPrimitiveTest(unittest.TestCase):
    def test_bridge_port_vlan_and_mapping_are_keyed_nodes(self) -> None:
        async def run() -> None:
            job = FakeJob()
            host = HostPrimitive("h")
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, host.ovs_bridge("br-home"))
            await _now(lc, host.netns(None))
            await _now(lc, host.netns(None).add_vlan("eth0", "eth0.129", 129))
            await _now(lc, host.ovs_port("br-home", "eth0.129"))
            await _now(lc, host.ovs_mapping(("net-home:br-home",)))

            self.assertIn("ovs.bridge:br-home", lc.graph.nodes)
            self.assertIn("netns:*global*", lc.graph.nodes)
            self.assertIn("link:*global*|eth0.129", lc.graph.nodes)
            self.assertIn("ovs.port:eth0.129", lc.graph.nodes)
            self.assertIn("ovs.mapping:ovn-bridge-mappings", lc.graph.nodes)
            lc.graph.validate()

            argv = [command[1] for command in job.commands]
            self.assertIn(("ovs-vsctl", "add-br", "br-home"), argv)
            self.assertIn(
                (
                    "ip",
                    "link",
                    "add",
                    "link",
                    "eth0",
                    "name",
                    "eth0.129",
                    "type",
                    "vlan",
                    "id",
                    "129",
                ),
                argv,
            )
            self.assertIn(("ovs-vsctl", "add-port", "br-home", "eth0.129"), argv)
            self.assertIn(
                (
                    "ovs-vsctl",
                    "set",
                    "open_vswitch",
                    ".",
                    "external-ids:ovn-bridge-mappings=net-home:br-home",
                ),
                argv,
            )

        asyncio.run(run())

    def test_xdp_leg_creates_veth_ports_and_trunks(self) -> None:
        async def run() -> None:
            job = FakeJob()
            host = HostPrimitive("h")
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(
                lc, host.netns(None).add_if("veth-xdp-a", kind="veth", peer="veth-seg-a", up=True)
            )
            await _now(lc, host.ovs_afxdp_port("br-xdp", "ovnxdp", "native", 4, trunks=(129,)))
            await _now(lc, host.ovs_port("br-home", "veth-seg-a", tag=129))

            argv = [command[1] for command in job.commands]
            self.assertIn(
                (
                    "ip",
                    "link",
                    "add",
                    "veth-xdp-a",
                    "type",
                    "veth",
                    "peer",
                    "name",
                    "veth-seg-a",
                ),
                argv,
            )
            self.assertIn(
                (
                    "ovs-vsctl",
                    "add-port",
                    "br-xdp",
                    "ovnxdp",
                    "--",
                    "set",
                    "Interface",
                    "ovnxdp",
                    "type=afxdp",
                    "options:xdp-mode=native",
                    "options:n_rxq=4",
                ),
                argv,
            )

        asyncio.run(run())

    def test_mapping_delete_removes_the_global_external_id(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(lc, OVSMapping(("net-home:br-home",)))
            self.assertEqual(
                job.commands[-1][1],
                (
                    "ovs-vsctl",
                    "remove",
                    "open_vswitch",
                    ".",
                    "external-ids",
                    "ovn-bridge-mappings",
                ),
            )

        asyncio.run(run())

    def test_external_id_set_and_remove(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, OVSExternalId("hostname", "chassis-1"))
            self.assertEqual(
                job.commands[-1][1],
                (
                    "ovs-vsctl",
                    "set",
                    "open_vswitch",
                    ".",
                    "external-ids:hostname=chassis-1",
                ),
            )
            lc_delete = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(lc_delete, OVSExternalId("hostname", ""))
            self.assertEqual(
                job.commands[-1][1],
                (
                    "ovs-vsctl",
                    "remove",
                    "open_vswitch",
                    ".",
                    "external-ids",
                    "hostname",
                ),
            )

        asyncio.run(run())

    def test_bridge_and_port_delete_use_if_exists(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.DELETE, "h")
            await _now(lc, OVSPort("br-home", "eth0.129"))
            await _now(lc, OVSBridge("br-home"))
            argv = [command[1] for command in job.commands]
            self.assertEqual(
                argv[0], ("ovs-vsctl", "--if-exists", "del-port", "br-home", "eth0.129")
            )
            self.assertEqual(argv[1], ("ovs-vsctl", "--if-exists", "del-br", "br-home"))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
