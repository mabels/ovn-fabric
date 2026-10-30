from __future__ import annotations

import json
import unittest

from deployer.test_ir_to_python import NODES
from protocol.hydrate import hydrate_nodes
from runtime.test_message import IR_SAMPLE

from . import generated as g
from .plan import PlanError, plan_units
from .units import to_operation


class PlanTest(unittest.TestCase):
    def test_the_central_gets_its_bindings_and_the_cluster(self) -> None:
        names = [u.name for u in plan_units(NODES, "central-1")]
        self.assertEqual(names[0], "chassis registration: central-1")
        for expected in (
            "logical switch: backbone",
            "localnet port: home",
            "chassis id for chassis-1 (resolved on this host)",
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
                "bridge: br-home on chassis-1",
                "interface: home (eth0.129)",
                "kernel router: kernel-0",
                "kernel router side: kernel-0 (left)",
                "kernel router side: kernel-0 (right)",
            ],
        )
        self.assertEqual(
            [u.kind for u in units][3:],
            ["kernel router", "kernel router side", "kernel router side"],
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
        workload = units[-1]
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

    def test_an_unknown_host(self) -> None:
        with self.assertRaises(PlanError):
            plan_units(NODES, "nowhere")


if __name__ == "__main__":
    unittest.main()
