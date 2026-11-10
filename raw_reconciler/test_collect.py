from __future__ import annotations

import importlib
import unittest
from typing import Any
from unittest import mock

# The module (the package exports a function of the same name).
collect = importlib.import_module("raw_reconciler.collect")


def _routes(_netns: str | None, family: str, _pid: int | None = None) -> list[dict[str, Any]]:
    name = "ipv6" if family == "-6" else "ipv4"
    via = "fd00::1" if family == "-6" else "10.0.0.1"
    return [
        {"prefix": "default", "dev": "eth0", "nexthop": via, "family": name, "metric": None},
        {"prefix": "10.9.0.0/16", "dev": "eth0", "nexthop": "eth0", "family": name},
    ]


class RouteNodesTest(unittest.TestCase):
    def test_a_default_route_is_named_per_family(self) -> None:
        # `ip route` says "default" in both families; RawIR says it as the
        # deployer does.
        with mock.patch.object(collect, "list_routes", side_effect=_routes):
            nodes = collect._route_nodes([("ns-x", "ns-x", None)])
        ids = {node["id"]: node["collection"]["prefix"] for node in nodes}
        self.assertEqual(ids["route:ns-x|ipv4|main|0.0.0.0/0|eth0|10.0.0.1|None"], "0.0.0.0/0")
        self.assertEqual(ids["route:ns-x|ipv6|main|::/0|eth0|fd00::1|None"], "::/0")
        self.assertIn("route:ns-x|ipv4|main|10.9.0.0/16|eth0|eth0|None", ids)


class LinkNodesTest(unittest.TestCase):
    def test_a_vlan_has_its_parent_by_name_and_its_id(self) -> None:
        links = [
            {
                "ifname": "ovnkern",
                "ifindex": 2,
                "kind": "physical",
                "mac": "bc:24:11:e8:68:a8",
                "permMac": "bc:24:11:e8:68:a8",
                "adminUp": True,
            },
            {
                "ifname": "ovnkern.1131",
                "ifindex": 9,
                "kind": "vlan",
                "linkName": "ovnkern",
                "vlanId": 1131,
                "mac": "bc:24:11:e8:68:a8",
                "adminUp": True,
            },
        ]
        with mock.patch.object(collect, "list_links_detail", return_value=links):
            nodes, _ = collect._link_nodes([("*global*", None, None)])
        vlan = next(n for n in nodes if n["id"].endswith("ovnkern.1131"))["collection"]
        self.assertEqual(vlan["linkRef"], "link:*global*|ovnkern")
        self.assertEqual(vlan["vlanId"], 1131)


class MovedVlanTest(unittest.TestCase):
    def test_a_vlan_in_a_netns_finds_its_parent_in_the_root_one(self) -> None:
        def links(netns: str | None, pid: int | None = None) -> list[dict[str, Any]]:
            if netns is None:
                return [
                    {
                        "ifname": "ovnkern",
                        "ifindex": 2,
                        "kind": "physical",
                        "mac": "bc:24:11:e8:68:a8",
                        "adminUp": True,
                    }
                ]
            return [
                {
                    "ifname": "ovnkern.2280",
                    "ifindex": 5,
                    "kind": "vlan",
                    "linkIndex": 2,
                    "vlanId": 2280,
                    "mac": "bc:24:11:e8:68:a8",
                    "adminUp": True,
                }
            ]

        with mock.patch.object(collect, "list_links_detail", side_effect=links):
            nodes, _ = collect._link_nodes([("*global*", None, None), ("ns-wan", "ns-wan", None)])
        vlan = next(n for n in nodes if n["id"] == "link:ns-wan|ovnkern.2280")["collection"]
        self.assertEqual(vlan["linkRef"], "link:*global*|ovnkern")


class EnterableTest(unittest.TestCase):
    def test_a_link_of_a_container_that_is_gone_is_left_out(self) -> None:
        with mock.patch.object(collect.os.path, "exists", side_effect=lambda p: "alive" in p):
            self.assertEqual(collect._enterable(["alive", "gone"]), ["alive"])


class IptablesNodesTest(unittest.TestCase):
    def test_a_rule_is_keyed_by_what_it_matches_and_does(self) -> None:
        rule = {"family": "ip", "table": "nat", "chain": "POSTROUTING", "action": "MASQUERADE"}
        rules = [
            {**rule, "fields": {"oif": "wan0"}, "order": "000"},
            {**rule, "fields": {"src": "10.0.0.0/8", "oif": "wan1"}, "order": "001"},
            {**rule, "fields": {"oif": "wan0"}, "order": "002"},
        ]
        with mock.patch.object(collect, "list_rules", return_value=rules):
            nodes = collect._iptables_nodes([("ns-x", "ns-x", None)])
        self.assertEqual(
            [(node["id"], node["collection"]["handle"]) for node in nodes],
            [
                ("iptables:ns-x|ipv4|nat|POSTROUTING|oif=wan0|MASQUERADE", "000"),
                ("iptables:ns-x|ipv4|nat|POSTROUTING|oif=wan1,src=10.0.0.0/8|MASQUERADE", "001"),
                ("iptables:ns-x|ipv4|nat|POSTROUTING|oif=wan0|MASQUERADE#2", "002"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
