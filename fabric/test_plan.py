from __future__ import annotations

import json
import unittest
from collections.abc import Callable

from deployer.ir_common import Emitter
from deployer.test_ir_to_python import NODES
from protocol.hydrate import hydrate_nodes
from runtime.test_message import IR_SAMPLE

from . import generated as g
from .plan import PlanEmitter, PlanError, plan_units
from .primitives.host import HostPrimitive
from .units import to_operation

HOST = HostPrimitive("h")


class PlanTest(unittest.TestCase):
    def test_the_central_gets_its_bindings_and_the_cluster(self) -> None:
        names = [u.name for u in plan_units(NODES, "central-1")]
        self.assertEqual(names[0], "chassis registration: central-1")
        for expected in (
            "logical switch: backbone",
            "localnet port: home",
            "chassis id: chassis-1 (resolved on this host)",
            "router: router-home",
            "router port: lrp-router-home-left (home)",
        ):
            self.assertIn(expected, names)

    def test_a_chassis_gets_bindings_and_its_kernel_router_but_no_cluster(self) -> None:
        units = plan_units(NODES, "chassis-1")
        names = [u.name for u in units]
        self.assertEqual(
            names,
            [
                "chassis registration: chassis-1",
                "root namespace: chassis-1",
                "bridge: br-home on chassis-1",
                "interface: home (eth0.129)",
                "bridge mappings: chassis-1",  # host-wide: after every bridge, its own
                "kernel router: kernel-0",
                "kernel router side: kernel-0 (left)",
                "kernel router side: kernel-0 (right)",
                "boot: reconcile",
            ],
        )
        self.assertEqual(
            [u.kind for u in units][5:],
            ["kernel router", "kernel router side", "kernel router side", "boot"],
        )

    def test_the_chassis_id_is_a_ref_until_it_runs(self) -> None:
        units = {u.name: u for u in plan_units(NODES, "central-1")}
        port = units["router port: lrp-router-home-left (home)"]  # the gateway port
        argvs = [c.argv for s in port.steps if isinstance(s, g.PrimitiveStep) for c in s.create]
        refs = [a for argv in argvs for a in argv if isinstance(a, g.RefArg)]
        self.assertTrue(refs)
        self.assertEqual(refs[0].key, "CHASSIS_ID")

    def test_a_workload_is_its_container_then_its_nics(self) -> None:
        units = plan_units(hydrate_nodes(IR_SAMPLE), "net-test-hh-b")
        workload = units[-2]  # the last one is the boot's
        self.assertEqual(workload.name, "workload: cp-mam-hh-platform-platform.dns")
        kinds = [s.kind for s in workload.steps]
        self.assertIn("Capture", kinds)  # the container's PID …
        self.assertIn("SymlinkNetns", kinds)  # … linked as its netns
        argvs = [
            [a for a in c.argv if isinstance(a, str)]
            for s in workload.steps
            if isinstance(s, g.PrimitiveStep)
            for c in s.create
        ]
        self.assertTrue(any(argv[:2] == ["/usr/bin/docker", "build"] for argv in argvs))

    def test_every_step_is_data_and_turns_back_into_its_operation(self) -> None:
        for host in ("central-1", "chassis-1"):
            for unit in plan_units(NODES, host):
                json.dumps([repr(step) for step in unit.steps])
                for step in unit.steps:
                    to_operation(step)

    def test_every_unit_knows_its_scope(self) -> None:
        scopes = {u.name: u.scope for u in plan_units(NODES, "central-1")}
        self.assertEqual(scopes["chassis registration: central-1"], "configure")
        self.assertEqual(scopes["router: router-home"], "cluster")
        chassis = {u.name: u.scope for u in plan_units(NODES, "chassis-1")}
        self.assertEqual(chassis["kernel router side: kernel-0 (left)"], "kernel-0")

    def test_a_unit_comes_with_its_ir_node(self) -> None:
        units = {u.name: u for u in plan_units(NODES, "chassis-1")}
        self.assertEqual(units["chassis registration: chassis-1"].key, "host:chassis-1")
        self.assertTrue(units["kernel router: kernel-0"].key)

    def test_an_unknown_host(self) -> None:
        with self.assertRaises(PlanError):
            plan_units(NODES, "nowhere")


def _plan(declare: Callable[[Emitter], None]) -> list[g.Unit]:
    emit = PlanEmitter("test")
    declare(emit)
    return emit.units


class UnitBoundaryTest(unittest.TestCase):
    """The units are what the declarations open — and nothing is guessed."""

    def test_operations_belong_to_the_unit_they_are_declared_on(self) -> None:
        def declare(emit: Emitter) -> None:
            bridge = emit.unit("bridge", "br-a")
            bridge.ovn(HOST.ovs_bridge("br-a"))
            port = bridge.unit("interface", "eth0")  # opened from a unit: the next one
            port.ovn(HOST.ovs_port("br-a", "eth0"))
            emit.unit("nothing", "to do")

        units = _plan(declare)
        self.assertEqual(
            [(u.name, u.kind, len(u.steps)) for u in units],
            [
                ("bridge: br-a", "bridge", 1),
                ("interface: eth0", "interface", 1),
                ("nothing: to do", "nothing", 0),
            ],
        )
        self.assertEqual({u.scope for u in units}, {"test"})

    def test_an_operation_outside_any_unit_is_an_error(self) -> None:
        def declare(emit: Emitter) -> None:
            emit.ovn(HOST.ovs_bridge("br-a"))

        with self.assertRaisesRegex(PlanError, "declared outside any unit"):
            _plan(declare)

    def test_an_operation_on_an_older_unit_is_an_error(self) -> None:
        def declare(emit: Emitter) -> None:
            bridge = emit.unit("bridge", "br-a")
            port = bridge.unit("interface", "eth0")
            port.ovn(HOST.ovs_port("br-a", "eth0"))
            bridge.ovn(HOST.ovs_mapping(("net:br-a",)))  # would run before the port

        with self.assertRaisesRegex(PlanError, "bridge: br-a: .* after the unit 'interface: eth0'"):
            _plan(declare)

    def test_comments_cut_nothing(self) -> None:
        def declare(emit: Emitter) -> None:
            bridge = emit.unit("bridge", "br-a")
            bridge.comment("# --- looks like a section ---")
            bridge.ovn(HOST.ovs_bridge("br-a"))

        self.assertEqual([u.name for u in _plan(declare)], ["bridge: br-a"])


class BootTest(unittest.TestCase):
    def test_every_host_comes_back_at_boot_as_the_plan_s_last_unit(self) -> None:
        for host in ("central-1", "chassis-1"):
            unit = plan_units(NODES, host)[-1]
            self.assertEqual((unit.name, unit.scope), ("boot: reconcile", "boot"))
            kinds = [step.kind for step in unit.steps]
            self.assertEqual(kinds, ["WriteFile", "Chmod", "WriteFile", "Primitive"])
            script, _mode, service, enable = unit.steps
            self.assertIn("ir-apply run deployed --no-cleanup", script.content)  # type: ignore[union-attr]
            self.assertIn("grep -q '\"deployed\"'", script.content)  # type: ignore[union-attr]
            self.assertEqual(
                service.path, "/etc/systemd/system/ovn-fabric-reconcile.service"  # type: ignore[union-attr]
            )
            self.assertIn("WantedBy=multi-user.target", service.content)  # type: ignore[union-attr]
            # enabled every run; a delete disables it again
            self.assertEqual(enable.decide.kind, "always")  # type: ignore[union-attr]
            self.assertEqual(
                [c.argv for c in enable.delete],  # type: ignore[union-attr]
                [["systemctl", "disable", "--quiet", "ovn-fabric-reconcile.service"]],
            )


if __name__ == "__main__":
    unittest.main()
