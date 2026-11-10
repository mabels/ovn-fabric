from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from fabric.context import Ctx
from fabric.lifecycle import LifecycleAction, LifecycleController, Operation
from fabric.primitives.ovn import (
    OVNNbCtlAcl,
    OVNNbCtlConnection,
    OVNNbCtlLr,
    OVNNbCtlLrp,
    OVNNbCtlLs,
    OVNNbCtlLsp,
    OVNNbCtlNat,
    OVNNbCtlRoute,
    OVNSbCtlConnection,
)


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
        timeout: float | None = None,
    ) -> bool:
        self.commands.append((label, tuple(argv), abort_on_error))
        # `exists` probes use `get`; report "missing" so create runs.
        return tuple(argv[:2]) != ("ovn-nbctl", "get")

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


class OvnnPrimitiveTest(unittest.TestCase):
    def test_ls_adds_typed_command_and_raw_node(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, OVNNbCtlLs("backbone"))
            self.assertIn(
                ("ovn-nbctl", "ls-add", "backbone"),
                [c[1] for c in job.commands],
            )
            self.assertEqual(lc.graph.nodes["ovn.ls:backbone"]["kind"], "ovn.ls")

        asyncio.run(run())

    def test_lsp_emits_type_addresses_and_options(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(
                lc,
                OVNNbCtlLsp(
                    switch="backbone",
                    name="lsp-localnet",
                    type="localnet",
                    addresses=("unknown",),
                    options=(("network_name", "net-backbone"),),
                ),
            )
            argv = [c[1] for c in job.commands]
            self.assertIn(("ovn-nbctl", "lsp-add", "backbone", "lsp-localnet"), argv)
            self.assertIn(("ovn-nbctl", "lsp-set-type", "lsp-localnet", "localnet"), argv)
            self.assertIn(
                ("ovn-nbctl", "lsp-set-options", "lsp-localnet", "network_name=net-backbone"),
                argv,
            )
            node = lc.graph.nodes["ovn.lsp:backbone|lsp-localnet"]
            self.assertEqual(node["switchRef"], "ovn.ls:backbone")

        asyncio.run(run())

    def test_connections_set_and_delete(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, OVNNbCtlConnection())
            await _now(lc, OVNSbCtlConnection())
            argv = [command[1] for command in job.commands]
            self.assertIn(
                ("ovn-nbctl", "set-connection", "ptcp:6641:0.0.0.0"),
                argv,
            )
            self.assertIn(
                ("ovn-sbctl", "set-connection", "ptcp:6642:0.0.0.0"),
                argv,
            )
            self.assertIn("ovn.nb.connection", lc.graph.nodes)
            self.assertIn("ovn.sb.connection", lc.graph.nodes)

        asyncio.run(run())

    def test_lr_carries_options_external_ids_and_cascading_delete(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(
                lc,
                OVNNbCtlLr(
                    "router-home",
                    options=(("mac_binding_age_threshold", "300"),),
                    external_ids=(("ovn-fabric.securityGroups", "web"),),
                ),
            )
            argv = [c[1] for c in job.commands]
            self.assertIn(("ovn-nbctl", "lr-add", "router-home"), argv)
            self.assertIn(
                (
                    "ovn-nbctl",
                    "set",
                    "logical_router",
                    "router-home",
                    "options:mac_binding_age_threshold=300",
                ),
                argv,
            )
            node = lc.graph.nodes["ovn.lr:router-home"]
            self.assertEqual(node["options"], {"mac_binding_age_threshold": "300"})

        asyncio.run(run())

    def test_lrp_pins_a_runtime_gateway_chassis(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(
                lc,
                OVNNbCtlLrp(
                    "router-home",
                    "lrp-left",
                    "00:00:00:00:01:01",
                    ("192.168.1.1/24",),
                    gateway_chassis="chassis-uuid",
                    ipv6_ra_configs=(("send_periodic", "true"),),
                ),
            )
            argv = [c[1] for c in job.commands]
            self.assertIn(
                (
                    "ovn-nbctl",
                    "lrp-add",
                    "router-home",
                    "lrp-left",
                    "00:00:00:00:01:01",
                    "192.168.1.1/24",
                ),
                argv,
            )
            self.assertIn(
                (
                    "ovn-nbctl",
                    "lrp-set-gateway-chassis",
                    "lrp-left",
                    "chassis-uuid",
                    "100",
                ),
                argv,
            )
            node = lc.graph.nodes["ovn.lrp:router-home|lrp-left"]
            self.assertEqual(node["routerRef"], "ovn.lr:router-home")
            self.assertEqual(node["gatewayChassis"], ["chassis-uuid"])

        asyncio.run(run())

    def test_an_snat_may_name_its_gateway_port(self) -> None:
        nat = OVNNbCtlNat("r", "snat", "10.43.0.12", "192.168.129.0/24", gateway_port="lrp-x")
        argv = nat.as_primitive().create_cmds[0].argv
        self.assertLess(argv.index("--gateway-port=lrp-x"), argv.index("lr-nat-add"))
        plain = OVNNbCtlNat("r", "snat", "10.43.0.12", "192.168.129.0/24")
        self.assertFalse(any("gateway-port" in a for a in plain.as_primitive().create_cmds[0].argv))

    def test_an_snat_is_deleted_by_its_logical_ip_a_dnat_by_its_external_ip(self) -> None:
        snat = OVNNbCtlNat("r", "snat", "203.0.113.1", "10.0.0.0/24")
        dnat = OVNNbCtlNat("r", "dnat", "203.0.113.1", "10.0.0.5")
        self.assertEqual(snat.as_primitive().delete_cmds[0].argv[-3:], ("r", "snat", "10.0.0.0/24"))
        self.assertEqual(dnat.as_primitive().delete_cmds[0].argv[-3:], ("r", "dnat", "203.0.113.1"))

    def test_route_nat_and_acl_use_semantic_keys(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(Ctx(), job, LifecycleAction.CREATE, "h")
            await _now(lc, OVNNbCtlRoute("router-home", "10.0.0.0/24", "192.168.1.254"))
            await _now(lc, OVNNbCtlNat("router-home", "snat", "203.0.113.1", "10.0.0.0/24"))
            await _now(lc, OVNNbCtlAcl("home", "to-lport", 100, "ip4", "allow-related"))

            self.assertIn(
                "ovn.route:router-home|10.0.0.0/24|192.168.1.254",
                lc.graph.nodes,
            )
            self.assertIn(
                "ovn.nat:router-home|snat|203.0.113.1|10.0.0.0/24",
                lc.graph.nodes,
            )
            self.assertIn(
                "ovn.acl:home|to-lport|100|ip4|allow-related",
                lc.graph.nodes,
            )
            argv = [c[1] for c in job.commands]
            self.assertIn(
                ("ovn-nbctl", "lr-route-add", "router-home", "10.0.0.0/24", "192.168.1.254"),
                argv,
            )
            self.assertIn(
                ("ovn-nbctl", "lr-nat-add", "router-home", "snat", "203.0.113.1", "10.0.0.0/24"),
                argv,
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
