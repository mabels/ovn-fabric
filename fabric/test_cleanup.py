from __future__ import annotations

import unittest
from typing import Any

from . import cleanup

ROUTE: dict[str, Any] = {
    "id": "route:ns|ipv4|main|10.9.0.0/16|eth0|10.0.0.1|None",
    "kind": "route",
    "netnsRef": "netns:ns",
    "family": "ipv4",
    "prefix": "10.9.0.0/16",
    "devRef": "link:ns|eth0",
    "via": "10.0.0.1",
    "table": "main",
}
LINK: dict[str, Any] = {
    "id": "link:ns|eth0",
    "kind": "link",
    "netnsRef": "netns:ns",
    "ifname": "eth0",
}
NETNS: dict[str, Any] = {"id": "netns:ns", "kind": "netns", "name": "ns"}
RULE: dict[str, Any] = {
    "id": "iptables:ns|ipv4|nat|POSTROUTING|oif=eth0|MASQUERADE",
    "kind": "iptables.rule",
    "netnsRef": "netns:ns",
    "family": "ipv4",
    "table": "nat",
    "chain": "POSTROUTING",
    "nftHandle": 7,
}


def _delete_argv(node: dict[str, Any]) -> list[str]:
    ((argv),) = cleanup.DELETERS[node["kind"]](node)
    return argv


class CleanupTest(unittest.TestCase):
    def test_what_is_fulfilled_stays_the_rest_goes_dependents_first(self) -> None:
        nodes = {n["id"]: n for n in (NETNS, LINK, ROUTE, RULE)}
        left = cleanup.leftovers(nodes, {NETNS["id"]})
        self.assertEqual([n["kind"] for n in left], ["iptables.rule", "route", "link"])

    def test_they_go_in_the_reverse_of_a_create(self) -> None:
        self.assertEqual(set(cleanup.CREATE_ORDER), set(cleanup.DELETERS))
        kinds = (
            "netns",
            "ovs.bridge",
            "link",
            "ovs.port",
            "ovn.ls",
            "ovn.lr",
            "ovn.lrp",
            "ovn.lsp",
            "ovn.route",
            "addr",
            "route",
            "iptables.rule",
            "docker.container",
        )
        nodes = {kind: {"id": kind, "kind": kind, "name": "x"} for kind in kinds}
        removed = [node["kind"] for node in cleanup.leftovers(nodes, set())]
        self.assertEqual(removed, list(reversed(kinds)))

    def test_within_a_kind_the_reverse_of_how_they_were_found(self) -> None:
        nodes = {i: {"id": i, "kind": "route"} for i in ("first", "second", "third")}
        left = cleanup.leftovers(nodes, set())
        self.assertEqual([n["id"] for n in left], ["third", "second", "first"])

    def test_a_bridge_s_own_link_goes_with_the_bridge(self) -> None:
        nodes = {
            "ovs.bridge:br": {"id": "ovs.bridge:br", "kind": "ovs.bridge", "name": "br"},
            "link:*global*|br": {"id": "link:*global*|br", "kind": "link", "ifname": "br"},
            "link:*global*|v": {"id": "link:*global*|v", "kind": "link", "ifname": "v"},
        }
        left = [n["id"] for n in cleanup.leftovers(nodes, set())]
        self.assertEqual(left, ["link:*global*|v", "ovs.bridge:br"])

    def test_each_kind_is_removed_by_its_command(self) -> None:
        netns = ["ip", "netns", "exec", "ns"]
        self.assertEqual(
            _delete_argv(ROUTE),
            [*netns, "ip", "-4", "route", "del", "10.9.0.0/16", "dev", "eth0"]
            + ["via", "10.0.0.1", "table", "main"],
        )
        self.assertEqual(
            _delete_argv(RULE),
            [*netns, "nft", "delete", "rule", "ip", "nat", "POSTROUTING", "handle", "7"],
        )
        self.assertEqual(_delete_argv(LINK), [*netns, "ip", "link", "delete", "eth0"])
        self.assertEqual(_delete_argv(NETNS), ["ip", "netns", "delete", "ns"])
        snat = {
            "kind": "ovn.nat",
            "routerRef": "ovn.lr:r",
            "type": "snat",
            "externalIp": "1.1.1.1",
            "logicalIp": "10.0.0.0/24",
        }
        self.assertIn("10.0.0.0/24", _delete_argv(snat))  # an snat by its logical address
        self.assertEqual(cleanup.DELETERS["ovs.iface"]({}), [])  # goes with its port

    def test_every_owned_kind_has_a_deleter_and_the_unit_runs_rewound(self) -> None:
        left = cleanup.leftovers({n["id"]: n for n in (LINK, ROUTE)}, set())
        steps = cleanup.unit(left).steps
        # a delete rewinds its steps: the route is removed before the link
        self.assertEqual([s.key for s in steps], [LINK["id"], ROUTE["id"]])  # type: ignore[union-attr]
        self.assertTrue(all(c.optional for s in steps for c in s.delete))  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
