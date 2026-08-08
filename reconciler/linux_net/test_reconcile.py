# reconciler/linux_net/test_reconcile.py — stdlib unittest, no pytest:
# keeps the "zero installs" principle this whole stack is built on
# consistent even for tests, not just the runtime.
#
# Fixtures below are synthetic but shape-accurate — not captured real
# data. They reproduce the exact patterns found by testing against a
# real router (docs/adr/0002-intermediate-representation.md,
# "Reconciler/deployer runtime"): a bare "default" destination in BOTH
# the v4 and v6 route lists (no colon in the string either way, so
# family can't be sniffed from the value), and the same prefix
# (fe80::/64) legitimately present as a distinct route on more than one
# interface — the two cases that silently collapsed 77 real facts down
# to 53 nodes before the fix this test guards against regressing.
#
# reconcile(scope, netns) is called once per namespace here, matching
# how reconciler/cli.py actually drives it — the netns sweep itself
# lives in cli.py/reconciler/netns.py, not in this reconciler.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

# NOT `from . import reconcile as mod` — this package's __init__.py does
# `from .reconcile import reconcile`, which rebinds the package's own
# `reconcile` attribute to the FUNCTION, shadowing the submodule of the
# same name. importlib.import_module reaches the real submodule
# directly, bypassing that rebinding — needed here specifically because
# the test has to monkeypatch _run_ip_json, an implementation detail
# only the submodule (not the re-exported function) exposes.
mod = importlib.import_module("reconciler.linux_net.reconcile")

FAKE_LINK = [
    {"ifname": "lo", "link_type": "loopback", "operstate": "UNKNOWN"},
    {"ifname": "ens19", "link_type": "ether", "operstate": "UP"},
    {"ifname": "veth-ovn-0", "link_type": "ether", "operstate": "UP"},
]

FAKE_ADDR = [
    {
        "ifname": "lo",
        "addr_info": [
            {"family": "inet", "local": "127.0.0.1", "prefixlen": 8},
            {"family": "inet6", "local": "::1", "prefixlen": 128},
        ],
    },
    {
        "ifname": "ens19",
        "addr_info": [
            {"family": "inet", "local": "192.168.129.20", "prefixlen": 24},
            {"family": "inet6", "local": "fd00:192:168:129::20", "prefixlen": 64},
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
        ],
    },
    {
        "ifname": "veth-ovn-0",
        "addr_info": [
            {"family": "inet", "local": "10.99.0.1", "prefixlen": 28},
            {"family": "inet6", "local": "fe80::2", "prefixlen": 64},
        ],
    },
]

# two DIFFERENT "default" entries, one per family, same bare literal —
# the case that silently mislabeled every v6 default as ipv4.route
FAKE_ROUTE4 = [
    {"dst": "default", "gateway": "192.168.129.1", "dev": "ens19"},
    {"dst": "192.168.129.0/24", "dev": "ens19", "prefsrc": "192.168.129.20"},
]

# fe80::/64 on TWO different devices — the case that collapsed distinct
# per-interface routes into one node when keyed by prefix alone
FAKE_ROUTE6 = [
    {"dst": "default", "gateway": "fd00:192:168:129::1", "dev": "ens19"},
    {"dst": "fe80::/64", "dev": "ens19"},
    {"dst": "fe80::/64", "dev": "veth-ovn-0"},
]

FAKE_NETNS_LINK = [
    {"ifname": "lo", "link_type": "loopback", "operstate": "UNKNOWN"},
    {"ifname": "veth-krn-0", "link_type": "ether", "operstate": "UP"},
]
FAKE_NETNS_ADDR = [
    {
        "ifname": "veth-krn-0",
        "addr_info": [{"family": "inet", "local": "10.99.0.2", "prefixlen": 28}],
    },
]
FAKE_NETNS_ROUTE4 = [{"dst": "default", "gateway": "10.99.0.1", "dev": "veth-krn-0"}]
FAKE_NETNS_ROUTE6: list[dict] = []


def _fake_run_ip_json(args: list[str], netns: str | None):
    if netns is not None:
        assert netns == "ns-uplink-voda-avm", netns
        if args == ["link", "show"]:
            return FAKE_NETNS_LINK
        if args == ["addr", "show"]:
            return FAKE_NETNS_ADDR
        if args == ["route", "show"]:
            return FAKE_NETNS_ROUTE4
        if args == ["-6", "route", "show"]:
            return FAKE_NETNS_ROUTE6
        raise AssertionError(f"unexpected ip invocation inside {netns}: {args}")
    if args == ["link", "show"]:
        return FAKE_LINK
    if args == ["addr", "show"]:
        return FAKE_ADDR
    if args == ["route", "show"]:
        return FAKE_ROUTE4
    if args == ["-6", "route", "show"]:
        return FAKE_ROUTE6
    raise AssertionError(f"unexpected ip invocation: {args}")


class GlobalNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "_run_ip_json", side_effect=_fake_run_ip_json)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile("host:test", None)

    def test_no_facts_lost(self) -> None:
        link_count = len(FAKE_LINK)
        addr_count = sum(len(e["addr_info"]) for e in FAKE_ADDR)
        route_count = len(FAKE_ROUTE4) + len(FAKE_ROUTE6)
        self.assertEqual(len(self.nodes), link_count + addr_count + route_count)

    def test_v4_and_v6_default_routes_both_survive_with_correct_kind(self) -> None:
        # both defaults share a device (ens19) in this fixture, so they'd
        # collide on key alone if kind weren't also distinguishing them —
        # confirm both are actually present and correctly typed instead
        # of one silently overwriting the other.
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
        self.assertNotEqual(a["key"], b["key"])

    def test_link_local_addresses_are_not_filtered(self) -> None:
        # "get all data in the reconciler, let the deployer decide what
        # matters" — fe80:: addresses are captured, not dropped.
        link_local = [
            n for n in self.nodes.values() if n["data"].get("value", "").startswith("fe80:")
        ]
        self.assertEqual(len(link_local), 2)

    def test_global_namespace_scope_is_literal_asterisk_global(self) -> None:
        node = self.nodes["host:test|netns:*global*|addr:192.168.129.20/24"]
        self.assertEqual(node["scope"], "host:test|netns:*global*")

    def test_bare_devices_with_no_address_are_still_captured_as_iface_facts(self) -> None:
        node = self.nodes["host:test|netns:*global*|link:lo"]
        self.assertEqual(node["kind"], "net.iface")
        self.assertEqual(node["data"]["linkType"], "loopback")


class RealNamespaceTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "_run_ip_json", side_effect=_fake_run_ip_json)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.nodes = mod.reconcile("host:test", "ns-uplink-voda-avm")

    def test_scope_is_a_sub_scope_of_host_not_a_separate_scope(self) -> None:
        node = self.nodes["host:test|netns:ns-uplink-voda-avm|addr:10.99.0.2/28"]
        self.assertEqual(node["scope"], "host:test|netns:ns-uplink-voda-avm")
        self.assertTrue(node["scope"].startswith("host:test|"))

    def test_devices_moved_into_the_namespace_are_captured_as_iface_facts(self) -> None:
        # By netns semantics a namespace starts with nothing but `lo` —
        # every other net.iface node under this scope IS the record of
        # what was added to it from the global namespace.
        moved = [n for n in self.nodes.values() if n["kind"] == "net.iface" and n["data"]["ifname"] != "lo"]
        self.assertEqual([n["data"]["ifname"] for n in moved], ["veth-krn-0"])

    def test_no_facts_lost(self) -> None:
        link_count = len(FAKE_NETNS_LINK)
        addr_count = sum(len(e["addr_info"]) for e in FAKE_NETNS_ADDR)
        route_count = len(FAKE_NETNS_ROUTE4) + len(FAKE_NETNS_ROUTE6)
        self.assertEqual(len(self.nodes), link_count + addr_count + route_count)


if __name__ == "__main__":
    unittest.main()
