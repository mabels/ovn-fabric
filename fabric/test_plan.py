from __future__ import annotations

import dataclasses
import json
import unittest
from collections.abc import Callable

from deployer.ir_common import Emitter
from fabric.ir_fixture import NODES
from protocol import generated as pt
from protocol.hydrate import hydrate_nodes
from runtime.test_message import IR_SAMPLE

from . import generated as g
from .lifecycle import LifecycleAction
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
            ],
        )
        self.assertEqual(
            [u.kind for u in units][5:],
            ["kernel router", "kernel router side", "kernel router side"],
        )

    def test_the_chassis_id_is_a_ref_until_it_runs(self) -> None:
        units = {u.name: u for u in plan_units(NODES, "central-1")}
        port = units["router port: lrp-router-home-left (home)"]  # the gateway port
        argvs = [c.argv for s in port.steps if isinstance(s, g.PrimitiveStep) for c in s.create]
        refs = [a for argv in argvs for a in argv if isinstance(a, g.RefArg)]
        self.assertTrue(refs)
        self.assertEqual(refs[0].key, "CHASSIS_ID")

    def test_a_workload_file_carries_its_owner_and_mode(self) -> None:
        nodes = hydrate_nodes(IR_SAMPLE)
        for i, node in enumerate(nodes):
            if isinstance(node, pt.KernelAppContainerNode):
                files = [
                    dataclasses.replace(f, user=101, group=102, mode="0640")
                    for f in node.data.files or []
                ]
                data = dataclasses.replace(node.data, files=files)
                nodes[i] = dataclasses.replace(node, data=data)
        units = {u.name: u for u in plan_units(nodes, "net-test-hh-b")}
        workload = units["workload: cp-mam-hh-platform-platform.dns"]
        written = [s for s in workload.steps if isinstance(s, g.WriteFileStep)]
        self.assertEqual(
            [(s.path, s.mode, s.uid, s.gid) for s in written],
            [
                (
                    "/var/lib/ovn-fabric/cp-mam-hh-platform-platform.dns/etc/coredns/Corefile",
                    0o640,
                    101,
                    102,
                )
            ],
        )
        # Without them: written as before.
        plain = {u.name: u for u in plan_units(hydrate_nodes(IR_SAMPLE), "net-test-hh-b")}
        written = [
            s
            for s in plain["workload: cp-mam-hh-platform-platform.dns"].steps
            if isinstance(s, g.WriteFileStep)
        ]
        self.assertEqual([(s.mode, s.uid, s.gid) for s in written], [(None, None, None)])

    def test_a_workload_is_its_container_then_its_nics(self) -> None:
        units = plan_units(hydrate_nodes(IR_SAMPLE), "net-test-hh-b")
        workload = units[-1]
        self.assertEqual(workload.name, "workload: cp-mam-hh-platform-platform.dns")
        kinds = [s.kind for s in workload.steps]
        self.assertIn("Capture", kinds)  # the container's PID …
        self.assertIn("SymlinkNetns", kinds)  # … linked as its netns

        def argvs(unit: g.Unit) -> list[list[str]]:
            return [
                [a for a in c.argv if isinstance(a, str)]
                for s in unit.steps
                if isinstance(s, g.PrimitiveStep)
                for c in s.create
            ]

        # Its image is a unit of its own, right before it (ADR 0012 §4).
        image = units[-2]
        self.assertEqual((image.kind, image.scope), ("image", workload.scope))
        self.assertTrue(any(a[:2] == ["/usr/bin/docker", "build"] for a in argvs(image)))
        self.assertFalse(any(a[:2] == ["/usr/bin/docker", "build"] for a in argvs(workload)))

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

    def test_an_app_s_own_device_is_wired_from_the_host_around_its_start(self) -> None:
        state = "/var/lib/z"
        app = pt.DockerApp(
            kind="docker",
            image="img",
            name="c",
            cmd=["/up"],
            volumes=[pt.Volume(host=state, container="/z")],
            stateFiles=[
                pt.StateFile(path=f"{state}/devicemap", content="n=zt0\n"),
                pt.StateFile(path=f"{state}/networks.d/n.conf"),
                pt.StateFile(path=f"{state}/authkey", content="k\n", mode="600"),
            ],
            securityGroups=[pt.SecurityGroupRef(render=pt.Render.kernel, name="sg")],
            tunnel=pt.Tunnel(iface="zt0", routes=["192.168.0.0/16"]),
        )
        group = pt.SecurityGroupNode(
            id="securitygroup:sg",
            kind="security.group",
            key=pt.SecurityGroupKey(name="sg"),
            data=pt.SecurityGroupData(
                rules=[pt.SecurityGroupRule2(family=pt.Family.ipv4, kind="masq")]
            ),
        )
        nodes = [
            group,
            *[
                (
                    dataclasses.replace(n, data=dataclasses.replace(n.data, apps=[app]))
                    if n.kind == "kernel.router" and n.key.side == pt.Side.right
                    else n
                )
                for n in NODES
            ],
        ]
        unit = next(
            u for u in plan_units(nodes, "chassis-1") if u.name == "kernel router: kernel-0"
        )
        lines = [
            line
            for step in unit.steps
            for line in to_operation(step, "chassis-1").describe(LifecycleAction.CREATE)
        ]
        joined = "\n".join(lines)

        def at(text: str) -> int:
            return next(i for i, line in enumerate(lines) if text in line)

        self.assertLess(at("devicemap"), at("docker run"))
        self.assertLess(at("touch"), at("docker run"))
        self.assertLess(at("MASQUERADE"), at("docker exec"))
        self.assertLess(at("docker exec"), at("ip link set dev zt0 up"))
        self.assertLess(at("ip link set dev zt0 up"), at("192.168.0.0/16"))
        for line in (at("MASQUERADE"), at("ip link set dev zt0 up"), at("192.168.0.0/16")):
            self.assertIn("ip netns exec c", lines[line])  # from the host, not in the image
        self.assertNotIn("docker exec -d c iptables", joined)
        # A key file is written with its mode, before the daemon reads it.
        self.assertIn("mode 600", lines[at(f"write_file {state}/authkey")])
        self.assertLess(at(f"write_file {state}/authkey"), at("docker run"))


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
    def test_no_host_plans_a_boot_unit_the_runtime_replays(self) -> None:
        # ADR 0012 §6: the runtime replays the deployed IR after a reboot
        # (fabric/boot.py); the oneshot of before is no unit any more.
        for host in ("central-1", "chassis-1"):
            self.assertNotIn("boot", {u.scope for u in plan_units(NODES, host)})


if __name__ == "__main__":
    unittest.main()
