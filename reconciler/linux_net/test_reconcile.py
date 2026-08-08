# reconciler/linux_net/test_reconcile.py — stdlib unittest, no pytest.
#
# This module is a thin IR-shaping layer over ladops.linux_net now —
# these tests mock ladops.linux_net.list_links/list_addrs/list_routes
# directly (the reconciler/ladops boundary) rather than the underlying
# `ip -j` calls, which ladops/test_linux_net.py already covers on its
# own. reconcile(scope, netns) is called once per namespace here,
# matching how reconciler/cli.py actually drives it.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.linux_net.reconcile")

SCOPE = {"host": "test"}

FAKE_LINKS = [
    {"ifname": "lo", "linkType": "loopback", "operstate": "UNKNOWN", "peerInOtherNetns": False},
    {"ifname": "ens19", "linkType": "ether", "operstate": "UP", "peerInOtherNetns": False},
    {"ifname": "veth-ovn-0", "linkType": "ether", "operstate": "UP", "peerInOtherNetns": True},
]

FAKE_ADDRS = [
    {"addr": "192.168.129.20/24", "interface": "ens19", "family": "ipv4"},
    {"addr": "fd00:192:168:129::20/64", "interface": "ens19", "family": "ipv6"},
    {"addr": "fe80::1/64", "interface": "ens19", "family": "ipv6"},
    {"addr": "fe80::2/64", "interface": "veth-ovn-0", "family": "ipv6"},
]

FAKE_ROUTES4 = [{"prefix": "default", "dev": "ens19", "nexthop": "192.168.129.1", "family": "ipv4"}]
FAKE_ROUTES6 = [
    {"prefix": "default", "dev": "ens19", "nexthop": "fd00:192:168:129::1", "family": "ipv6"},
    {"prefix": "fe80::/64", "dev": "ens19", "nexthop": "ens19", "family": "ipv6"},
    {"prefix": "fe80::/64", "dev": "veth-ovn-0", "nexthop": "veth-ovn-0", "family": "ipv6"},
]

FAKE_NETNS_LINKS = [
    {"ifname": "lo", "linkType": "loopback", "operstate": "UNKNOWN", "peerInOtherNetns": False},
    {"ifname": "veth-krn-0", "linkType": "ether", "operstate": "UP", "peerInOtherNetns": True},
]
FAKE_NETNS_ADDRS = [{"addr": "10.99.0.2/28", "interface": "veth-krn-0", "family": "ipv4"}]
FAKE_NETNS_ROUTES4 = [{"prefix": "default", "dev": "veth-krn-0", "nexthop": "10.99.0.1", "family": "ipv4"}]
FAKE_NETNS_ROUTES6: list[dict] = []


def _fake_list_links(netns):
    return FAKE_NETNS_LINKS if netns is not None else FAKE_LINKS


def _fake_list_addrs(netns):
    return FAKE_NETNS_ADDRS if netns is not None else FAKE_ADDRS


def _fake_list_routes(netns, family):
    if netns is not None:
        return FAKE_NETNS_ROUTES4 if family == "-4" else FAKE_NETNS_ROUTES6
    return FAKE_ROUTES4 if family == "-4" else FAKE_ROUTES6


def _patch_ladops():
    return (
        mock.patch.object(mod, "list_links", side_effect=_fake_list_links),
        mock.patch.object(mod, "list_addrs", side_effect=_fake_list_addrs),
        mock.patch.object(mod, "list_routes", side_effect=_fake_list_routes),
    )


# every reconcile() call adds exactly one net.netns node (the namespace
# itself), on top of whatever links/addrs/routes it found.
NETNS_NODE_COUNT = 1


class GlobalNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in _patch_ladops():
            self.addCleanup(patcher.stop)
            patcher.start()
        self.nodes = mod.reconcile(SCOPE, None)

    def test_no_facts_lost(self) -> None:
        self.assertEqual(
            len(self.nodes),
            len(FAKE_LINKS) + NETNS_NODE_COUNT + len(FAKE_ADDRS) + len(FAKE_ROUTES4) + len(FAKE_ROUTES6),
        )

    def test_v4_and_v6_default_routes_both_survive_with_correct_kind(self) -> None:
        matches = [
            n
            for n in self.nodes.values()
            if n["data"].get("prefix") == "default" and n["data"].get("dev") == "ens19"
        ]
        self.assertEqual(len(matches), 2)
        kinds = {n["kind"] for n in matches}
        self.assertEqual(kinds, {"ipv4.route", "ipv6.route"})

    def test_same_prefix_different_device_are_distinct_routes(self) -> None:
        a = self.nodes["host:test|netns:*global*|route:ipv6.route:ens19:fe80::/64"]
        b = self.nodes["host:test|netns:*global*|route:ipv6.route:veth-ovn-0:fe80::/64"]
        self.assertEqual(a["kind"], "ipv6.route")
        self.assertEqual(b["kind"], "ipv6.route")
        self.assertNotEqual(a["id"], b["id"])
        self.assertNotEqual(a["key"], b["key"])

    def test_link_local_addresses_are_not_filtered(self) -> None:
        link_local = [
            n for n in self.nodes.values() if n["data"].get("addr", "").startswith("fe80:")
        ]
        self.assertEqual(len(link_local), 2)

    def test_addr_node_uses_addr_and_interface_field_names(self) -> None:
        node = self.nodes["host:test|netns:*global*|addr:192.168.129.20/24"]
        self.assertEqual(node["key"], {"host": "test", "netns": "*global*", "addr": "192.168.129.20/24"})
        self.assertEqual(
            node["data"], {"addr": "192.168.129.20/24", "interface": "ens19", "role": "interface"}
        )

    def test_bare_devices_with_no_address_are_still_captured_as_iface_facts(self) -> None:
        node = self.nodes["host:test|netns:*global*|link:lo"]
        self.assertEqual(node["kind"], "net.iface")
        self.assertEqual(node["data"]["linkType"], "loopback")

    def test_key_never_duplicates_kind(self) -> None:
        # kind is already the node's own top-level field — key only
        # carries scope + the kind-specific local identity, not kind
        # again inside it.
        for node in self.nodes.values():
            self.assertNotIn("kind", node["key"])

    def test_netns_node_lists_every_interface_in_the_namespace(self) -> None:
        node = self.nodes["host:test|netns:*global*"]
        self.assertEqual(node["kind"], "net.netns")
        self.assertEqual(node["key"], {"host": "test", "netns": "*global*"})
        self.assertEqual(
            node["data"]["interfaces"],
            [
                {"ifname": "ens19", "peerInOtherNetns": False},
                {"ifname": "lo", "peerInOtherNetns": False},
                {"ifname": "veth-ovn-0", "peerInOtherNetns": True},
            ],
        )


class RealNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in _patch_ladops():
            self.addCleanup(patcher.stop)
            patcher.start()
        self.nodes = mod.reconcile(SCOPE, "ns-uplink-voda-avm")

    def test_key_is_a_sub_scope_of_host_not_a_separate_scope(self) -> None:
        node = self.nodes["host:test|netns:ns-uplink-voda-avm|addr:10.99.0.2/28"]
        self.assertEqual(node["key"], {"host": "test", "netns": "ns-uplink-voda-avm", "addr": "10.99.0.2/28"})

    def test_devices_moved_into_the_namespace_are_captured_as_iface_facts(self) -> None:
        moved = [n for n in self.nodes.values() if n["kind"] == "net.iface" and n["data"]["ifname"] != "lo"]
        self.assertEqual([n["data"]["ifname"] for n in moved], ["veth-krn-0"])

    def test_netns_node_lists_the_moved_in_devices_too(self) -> None:
        # this is the actual answer to "which devices were added to this
        # namespace from global" — a single, direct roster, not
        # something a caller has to reconstruct by filtering net.iface
        # nodes by scope itself.
        node = self.nodes["host:test|netns:ns-uplink-voda-avm"]
        self.assertEqual(
            node["data"]["interfaces"],
            [
                {"ifname": "lo", "peerInOtherNetns": False},
                {"ifname": "veth-krn-0", "peerInOtherNetns": True},
            ],
        )

    def test_moved_device_is_marked_peer_in_other_netns_lo_is_not(self) -> None:
        # the real, verifiable marker for "added" — a device with a real
        # link/peer relationship to something in another namespace,
        # confirmed via `ip -j link show`'s own link_netnsid field
        # (ladops/linux_net.py) — not just "isn't lo".
        moved = self.nodes["host:test|netns:ns-uplink-voda-avm|link:veth-krn-0"]
        lo = self.nodes["host:test|netns:ns-uplink-voda-avm|link:lo"]
        self.assertTrue(moved["data"]["peerInOtherNetns"])
        self.assertFalse(lo["data"]["peerInOtherNetns"])

    def test_no_facts_lost(self) -> None:
        self.assertEqual(
            len(self.nodes),
            len(FAKE_NETNS_LINKS)
            + NETNS_NODE_COUNT
            + len(FAKE_NETNS_ADDRS)
            + len(FAKE_NETNS_ROUTES4)
            + len(FAKE_NETNS_ROUTES6),
        )


if __name__ == "__main__":
    unittest.main()
