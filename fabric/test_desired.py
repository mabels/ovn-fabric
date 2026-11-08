from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from .desired import (
    Choice,
    NodeState,
    Reconciled,
    StepState,
    choose,
    compare,
    consumed_later,
    desired_rawir,
    step_state,
)
from .lifecycle import CaptureOperation, RuntimeRef
from .primitives.docker import DockerPull
from .primitives.fs import DirectoryPrimitive
from .primitives.host import HostPrimitive
from .primitives.ovn import OVNNbCtlLs
from .primitives.ovs import OVSBridge
from .rawir_learned import rawir

HOST = HostPrimitive("h")


class DesiredTest(unittest.TestCase):
    def test_footprints_merge_and_changes_land_on_their_nodes(self) -> None:
        netns = HOST.netns("ns-x")
        desired = desired_rawir(
            [
                ("netns", netns),
                ("addr", netns.add_addr("eth0", "10.1.2.3/24")),
                ("switch", OVNNbCtlLs("backbone")),
            ]
        )
        self.assertIn("netns:ns-x", desired.nodes)
        self.assertIn("addr:ns-x|eth0|10.1.2.3/24", desired.nodes)
        self.assertIn("ovn.ls:backbone", desired.nodes)
        # The routes that follow from the address are the kernel's: no part of it.
        self.assertFalse([i for i in desired.nodes if i.startswith("route:")])
        # The link eth0 itself is no primitive here: its change is kept apart.
        self.assertIn("link:ns-x|eth0", desired.changes)

    def test_what_cannot_be_predicted_is_counted(self) -> None:
        desired = desired_rawir(
            [
                ("image", DockerPull("h", "busybox")),  # not learned (an image)
                ("image", DockerPull("h", "alpine")),
                ("dir", DirectoryPrimitive("h", "/etc/x")),
                ("capture", CaptureOperation("pid", "pid", ("docker", "inspect"))),
                ("ls", OVNNbCtlLs(RuntimeRef("name"))),  # type: ignore[arg-type]
            ]
        )
        self.assertEqual(desired.not_learned["DockerPull"], 2)
        self.assertEqual(desired.not_rawir["Directory"], 1)
        self.assertEqual(desired.not_rawir["Capture"], 1)
        self.assertEqual(desired.runtime_values, ["ls: OVNNbCtlLs"])
        self.assertEqual(desired.nodes, {})


class CompareTest(unittest.TestCase):
    def test_applied_to_apply_only_observed_and_differ(self) -> None:
        desired: dict[str, dict[str, Any]] = {
            "ovn.ls:a": {"kind": "ovn.ls", "name": "a"},
            "ovn.ls:b": {"kind": "ovn.ls", "name": "b"},
            "ovs.bridge:c": {"kind": "ovs.bridge", "name": "c", "failMode": "standalone"},
            "link:*global*|d": {"kind": "link", "ifname": "d", "ifindex": 7},
        }
        live: dict[str, dict[str, Any]] = {
            "ovn.ls:a": {"kind": "ovn.ls", "name": "a"},
            "ovs.bridge:c": {"kind": "ovs.bridge", "name": "c", "failMode": None},
            "link:*global*|d": {"kind": "link", "ifname": "d", "ifindex": 42},  # volatile
            "ovn.ls:z": {"kind": "ovn.ls", "name": "z"},
        }
        result = compare(desired, live)
        self.assertEqual(result.applied, ["link:*global*|d", "ovn.ls:a"])
        self.assertEqual(result.to_apply, ["ovn.ls:b"])
        self.assertEqual(result.only_observed, ["ovn.ls:z"])
        self.assertEqual(result.differ, {"ovs.bridge:c": ["failMode"]})


class ReconciledTest(unittest.TestCase):
    def test_states_and_what_run_did(self) -> None:
        netns = HOST.netns("ns-x")
        live: dict[str, dict[str, Any]] = {
            "ovn.ls:a": {"id": "ovn.ls:a", "kind": "ovn.ls", "name": "a"},
            "link:*global*|eth9": {"id": "link:*global*|eth9", "kind": "link"},
            "link:ns-x|eth0": {"id": "link:ns-x|eth0", "kind": "link", "addrRefs": []},
        }
        graph = Reconciled.of(live, {"ovn.ls:a": {}})
        self.assertEqual(graph.state["ovn.ls:a"], NodeState.present)
        self.assertEqual(graph.state["link:*global*|eth9"], NodeState.observed)

        graph.applied(netns.add_addr("eth0", "10.1.2.3/24"))
        self.assertEqual(graph.state["addr:ns-x|eth0|10.1.2.3/24"], NodeState.applied)
        self.assertEqual(graph.nodes["addr:ns-x|eth0|10.1.2.3/24"]["address"], "10.1.2.3")
        # the footprint's change landed on the link that was there
        self.assertEqual(graph.nodes["link:ns-x|eth0"]["addrRefs"], ["addr:ns-x|eth0|10.1.2.3/24"])

        graph.failed(OVNNbCtlLs("b"))
        self.assertEqual(graph.state["ovn.ls:b"], NodeState.missing)
        self.assertEqual(graph.counts()["applied"], 1)  # the address


P, M, X, R, U = (
    StepState.present,
    StepState.missing,
    StepState.partial,
    StepState.read,
    StepState.unknown,
)


class ChooseTest(unittest.TestCase):
    def test_a_unit_that_is_gone_runs_completely_unknown_steps_too(self) -> None:
        self.assertEqual(choose([M, U, M, R]), Choice(True, [], "not there: all of it"))

    def test_a_unit_that_is_there_runs_only_its_reads(self) -> None:
        self.assertEqual(choose([P, U]), Choice(False, [], "there"))
        self.assertEqual(choose([P, R, U]), Choice(True, [0, 2], "there: only its reads"))

    def test_partly_there_runs_the_missing_steps_when_all_can_be_checked(self) -> None:
        self.assertEqual(choose([P, M, R]).skip, [0])
        self.assertTrue(choose([P, M, R]).run)
        self.assertFalse(choose([P, M, U]).run)
        self.assertFalse(choose([M, X]).run)

    def test_what_cannot_be_told_is_marked_unchecked(self) -> None:
        self.assertTrue(choose([U]).unchecked)
        self.assertTrue(choose([P, M, U]).unchecked)
        self.assertTrue(choose([M, X]).unchecked)
        self.assertFalse(choose([P, U]).unchecked)  # there: left alone, but known

    def test_without_a_learned_step_only_reads_run(self) -> None:
        self.assertTrue(choose([R]).run)
        self.assertFalse(choose([U]).run)
        self.assertFalse(choose([R, U]).run)
        self.assertFalse(choose([]).run)


D, A = StepState.decides, StepState.starts_afresh


F = StepState.follows


class FollowsTest(unittest.TestCase):
    def test_it_runs_when_its_unit_has_work_and_not_when_all_is_there(self) -> None:
        self.assertEqual(choose([M, F, M]), Choice(True, [], "not there: all of it"))
        self.assertEqual(choose([P, F, M]).skip, [0])  # a route missing: the link gets it too
        self.assertEqual(choose([P, F, P]), Choice(False, [], "there"))
        # the unit is there, its reads run: it does not
        self.assertEqual(choose([P, F, R]).skip, [0, 1])
        self.assertEqual(choose([D, P, F]).skip, [1, 2])  # with a decider too

    def test_its_step_says_so(self) -> None:
        sysctl = HOST.netns("ns-x").set_sysctl_file("/proc/sys/x", "2")
        self.assertEqual(step_state(sysctl, {})[0], StepState.follows)


class ChooseWithDeciderTest(unittest.TestCase):
    def test_a_workload_without_learned_steps_is_sent_its_symlink_waits(self) -> None:
        # image, container, capture, symlink: the executor runs the symlink
        # only when the container starts afresh
        self.assertEqual(choose([D, A, R, U]).run, True)
        self.assertEqual(choose([D, A, R, U]).skip, [3])
        self.assertFalse(choose([D, A, R, U]).unchecked)

    def test_a_network_change_does_not_touch_the_container(self) -> None:
        # container, the symlink, an address there, an address missing
        choice = choose([A, U, P, M])
        self.assertEqual((choice.run, choice.skip), (True, [1, 2]))

    def test_a_unit_that_is_gone_runs_completely_unless_a_container_decides(self) -> None:
        self.assertEqual(choose([D, U, M]).skip, [])  # a sysctl, a link, an address
        self.assertEqual(choose([A, U, M]).skip, [1])  # with its container: when it starts

    def test_what_is_there_with_a_decider_is_skipped_partly_too(self) -> None:
        self.assertEqual(choose([D, P, X]).skip, [1, 2])


class StepStateTest(unittest.TestCase):
    def test_states_from_the_live_rawir(self) -> None:
        switch = OVNNbCtlLs("a")
        self.assertEqual(step_state(switch, {})[0], StepState.missing)
        live = {"ovn.ls:a": {"id": "ovn.ls:a", "kind": "ovn.ls", "name": "a"}}
        self.assertEqual(step_state(switch, live)[0], StepState.present)
        capture = CaptureOperation("pid", "pid", ("docker", "inspect"))
        self.assertEqual(step_state(capture, {})[0], StepState.read)
        self.assertEqual(step_state(DockerPull("h", "busybox"), {})[0], StepState.unknown)
        named_later = OVNNbCtlLs(RuntimeRef("name"))  # type: ignore[arg-type]
        self.assertEqual(step_state(named_later, {})[0], StepState.unknown)

    def test_a_step_that_only_changes_fields_is_checked_by_them(self) -> None:
        footprint = {
            "added": {},
            "changed": {
                "ovs.bridge:br-a": {
                    "externalIds": {"from": {}, "to": {"ovn-bridge-mappings": "a"}}
                },
                "ovs.bridge:br-b": {"portRefs": {"added": ["ovs.port:p"], "removed": []}},
            },
        }
        bridge_a: dict[str, Any] = {"externalIds": {"ovn-bridge-mappings": "a", "other": "x"}}
        bridge_b: dict[str, Any] = {"portRefs": ["ovs.port:p", "ovs.port:q"]}
        with mock.patch("fabric.desired.rawir", return_value=footprint):
            state = step_state(OVNNbCtlLs("x"), {"ovs.bridge:br-a": bridge_a})[0]
            self.assertEqual(state, StepState.partial)
            live = {"ovs.bridge:br-a": bridge_a, "ovs.bridge:br-b": bridge_b}
            self.assertEqual(step_state(OVNNbCtlLs("x"), live)[0], StepState.present)
            empty: dict[str, dict[str, Any]] = {"ovs.bridge:br-a": {"externalIds": {}}}
            self.assertEqual(step_state(OVNNbCtlLs("x"), empty)[0], StepState.missing)

    def test_what_a_later_step_took_away_counts_as_there(self) -> None:
        side = OVNNbCtlLs("side")  # stands for a kernel router side
        move = OVNNbCtlLs("move")  # stands for the move into the container
        prints = {
            "side": {
                "added": {
                    "link:ns|veth": {"netnsRef": "netns:ns"},
                    "addr:ns|veth|10.0.0.1/24": {"linkRef": "link:ns|veth"},
                },
                "changed": {},
            },
            "move": {
                "added": {"link:c|veth": {"netnsRef": "netns:c"}},
                "changed": {},
                "removed": ["link:ns|veth"],
            },
        }
        with mock.patch("fabric.desired.rawir", side_effect=lambda op: prints[op.name]):
            moved: dict[str, dict[str, Any]] = {"link:c|veth": {}}
            later = consumed_later([[side], [move]], moved)
            self.assertEqual(later, [{"link:ns|veth": True}, {}])
            # the link and the address on it went with the move: there
            self.assertEqual(step_state(side, moved, later[0])[0], StepState.present)
            # the move did not happen: the side is checked where it put things
            empty: dict[str, dict[str, Any]] = {}
            later = consumed_later([[side], [move]], empty)
            self.assertEqual(step_state(side, empty, later[0])[0], StepState.missing)
            # and the desired RawIR has the link where the move left it
            desired = desired_rawir([("side", side), ("move", move)])
            self.assertIn("link:c|veth", desired.nodes)
            self.assertNotIn("link:ns|veth", desired.nodes)

    def test_partly_there(self) -> None:
        bridge = OVSBridge("br-x")
        ids = list(rawir(bridge)["added"])
        self.assertGreater(len(ids), 1)
        live = {ids[0]: {"id": ids[0]}}
        self.assertEqual(step_state(bridge, live)[0], StepState.partial)


if __name__ == "__main__":
    unittest.main()
