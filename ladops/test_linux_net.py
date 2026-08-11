# ladops/test_linux_net.py — stdlib unittest, no pytest, same reasoning
# as every other test file in this project: no installs needed to run
# it, consistent with the reconciler/deployer's own zero-footprint goal.
#
# Fixtures reproduce the exact patterns found by testing against a real
# router (docs/adr/0002-intermediate-representation.md, "Reconciler/
# deployer runtime"): a bare "default" destination in BOTH the v4 and v6
# route lists (no colon in the string either way, so family can't be
# sniffed from the value), and the same prefix (fe80::/64) legitimately
# present as a distinct route on more than one interface.
#
# add_*/delete_* are never run for real here (or anywhere in this
# session) — this project's only real router is live production
# infrastructure (mullvad/starlink/voda uplinks); these are verified by
# asserting the exact argv built for `ladops.netns.run`, not by
# executing it.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("ladops.linux_net")

FAKE_LINK = [
    {"ifname": "lo", "link_type": "loopback", "operstate": "UNKNOWN"},
    {"ifname": "ens19", "link_type": "ether", "operstate": "UP"},
    # a veth end whose peer lives in another namespace — real `ip -j
    # link show` sets link_netnsid (and link_index) for this case,
    # confirmed against the real router for both a veth peer and a
    # moved VLAN sub-interface.
    {"ifname": "veth-ovn-0", "link_type": "ether", "operstate": "UP", "link_index": 26, "link_netnsid": 0},
]

FAKE_ADDR = [
    {
        "ifname": "ens19",
        "addr_info": [
            {"family": "inet", "local": "192.168.129.20", "prefixlen": 24},
            {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
        ],
    },
]

FAKE_ROUTE4 = [
    {"dst": "default", "gateway": "192.168.129.1", "dev": "ens19"},
]
FAKE_ROUTE6 = [
    {"dst": "default", "gateway": "fd00:192:168:129::1", "dev": "ens19"},
    {"dst": "fe80::/64", "dev": "ens19"},
]


def _fake_run_ip_json(args, netns):
    if args == ["link", "show"]:
        return FAKE_LINK
    if args == ["addr", "show"]:
        return FAKE_ADDR
    if args == ["route", "show"]:
        return FAKE_ROUTE4
    if args == ["-6", "route", "show"]:
        return FAKE_ROUTE6
    raise AssertionError(f"unexpected ip invocation: {args}")


class ListTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "_run_ip_json", side_effect=_fake_run_ip_json)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_list_links_returns_plain_facts_not_ir_nodes(self) -> None:
        links = mod.list_links(None)
        self.assertEqual(
            links,
            [
                {"ifname": "lo", "linkType": "loopback", "operstate": "UNKNOWN", "peerInOtherNetns": False},
                {"ifname": "ens19", "linkType": "ether", "operstate": "UP", "peerInOtherNetns": False},
                {"ifname": "veth-ovn-0", "linkType": "ether", "operstate": "UP", "peerInOtherNetns": True},
            ],
        )

    def test_list_addrs_tags_family_from_the_addr_not_a_kind_string(self) -> None:
        addrs = mod.list_addrs(None)
        self.assertEqual(
            addrs,
            [
                {"addr": "192.168.129.20/24", "interface": "ens19", "family": "ipv4"},
                {"addr": "fe80::1/64", "interface": "ens19", "family": "ipv6"},
            ],
        )

    def test_list_routes_tags_family_from_which_command_produced_it(self) -> None:
        v4 = mod.list_routes(None, "-4")
        v6 = mod.list_routes(None, "-6")
        self.assertEqual(v4, [{"prefix": "default", "dev": "ens19", "nexthop": "192.168.129.1", "family": "ipv4"}])
        self.assertEqual(
            v6,
            [
                {"prefix": "default", "dev": "ens19", "nexthop": "fd00:192:168:129::1", "family": "ipv6"},
                {"prefix": "fe80::/64", "dev": "ens19", "nexthop": "ens19", "family": "ipv6"},
            ],
        )


class WriteTest(unittest.TestCase):
    def test_add_addr_builds_the_real_ip_command(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_addr("10.99.0.2/28", "veth-krn-0", "ns-uplink-voda-avm")
        run.assert_called_once_with(
            ["ip", "addr", "add", "10.99.0.2/28", "dev", "veth-krn-0"], "ns-uplink-voda-avm"
        )

    def test_delete_addr_builds_the_real_ip_command(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.delete_addr("10.99.0.2/28", "veth-krn-0", None)
        run.assert_called_once_with(["ip", "addr", "del", "10.99.0.2/28", "dev", "veth-krn-0"], None)

    def test_add_route_with_a_real_gateway_includes_via(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_route("default", "veth-krn-0", "10.99.0.1", "ns-uplink-voda-avm")
        run.assert_called_once_with(
            ["ip", "route", "add", "default", "dev", "veth-krn-0", "via", "10.99.0.1"],
            "ns-uplink-voda-avm",
        )

    def test_add_route_with_no_real_gateway_omits_via(self) -> None:
        # list_routes()'s own "nexthop" defaults to `dev` itself when
        # there's no real gateway (on-link routes, e.g. fe80::/64) — `via
        # <dev-name>` isn't a real gateway address, so it must not be
        # passed through as one.
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_route("fe80::/64", "ens19", "ens19", None)
        run.assert_called_once_with(["ip", "route", "add", "fe80::/64", "dev", "ens19"], None)

    def test_delete_route_builds_the_real_ip_command(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.delete_route("default", "ens19", None)
        run.assert_called_once_with(["ip", "route", "del", "default", "dev", "ens19"], None)

    def test_add_if_to_netns_runs_from_global_targeting_the_real_netns(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_if_to_netns("veth-krn-0", "ns-uplink-voda-avm")
        run.assert_called_once_with(["ip", "link", "set", "veth-krn-0", "netns", "ns-uplink-voda-avm"], None)

    def test_delete_if_to_netns_runs_from_inside_the_netns_targeting_pid_1(self) -> None:
        # the inverse of add_if_to_netns — must run from inside the
        # namespace the device is currently in (a device is only
        # visible to `ip link set` from its current namespace), and
        # "netns 1" (PID 1's namespace) is the standard idiom for "the
        # root/global namespace" from in there.
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.delete_if_to_netns("veth-krn-0", "ns-uplink-voda-avm")
        run.assert_called_once_with(["ip", "link", "set", "veth-krn-0", "netns", "1"], "ns-uplink-voda-avm")


class VlanWriteTest(unittest.TestCase):
    def test_add_vlan_builds_the_real_ip_link_command(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_vlan("ens18", "ens18.128", 128)
        run.assert_called_once_with(
            ["ip", "link", "add", "link", "ens18", "name", "ens18.128", "type", "vlan", "id", "128"], None
        )

    def test_set_link_up(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.set_link_up("ens18.128")
        run.assert_called_once_with(["ip", "link", "set", "ens18.128", "up"], None)

    def test_delete_link(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.delete_link("ens18.128")
        run.assert_called_once_with(["ip", "link", "delete", "ens18.128"], None)


if __name__ == "__main__":
    unittest.main()
