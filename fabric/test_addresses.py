"""The address metadata and its allocator (ADR 0011 §3, §4a, §5)."""

from __future__ import annotations

import unittest

from protocol import generated as pt

from .addresses import (
    AddressError,
    Addresses,
    AllocationRequest,
    allocate,
    mac_from_name,
    mac_from_v4,
)
from .ir_fixture import LRP_BACKBONE, LRP_HOME, NODES


def _request(**changes: object) -> AllocationRequest:
    base: dict[str, object] = {
        "domain": "ipnet:ls:core",
        "prefixes": {"ipv4": "172.21.0.0/24", "ipv6": "2600:70ff:b3c0::21:0/112"},
        "strategy": {"ipv4": "low-high", "ipv6": "low-high"},
        "pinned": {},
        "names": [],
    }
    base.update(changes)
    return AllocationRequest(**base)  # type: ignore[arg-type]


BOTH = ("ipv4", "ipv6")


class AllocateTest(unittest.TestCase):
    def test_new_names_are_placed_sorted_with_one_number_for_both_families(self) -> None:
        got = allocate(_request(names=[("b-nat", BOTH), ("a-nat", BOTH)]))
        self.assertEqual(got["a-nat"], {"ipv4": 1, "ipv6": 1})
        self.assertEqual(got["b-nat"], {"ipv4": 2, "ipv6": 2})

    def test_declaration_order_does_not_matter(self) -> None:
        # The VM 302 incident: adding a name moved every name after it.
        one = allocate(_request(names=[("a", BOTH), ("c", BOTH)]))
        two = allocate(_request(names=[("c", BOTH), ("a", BOTH)]))
        self.assertEqual(one, two)

    def test_pinned_numbers_are_kept_and_skipped(self) -> None:
        got = allocate(_request(pinned={"dns": {"ipv4": 1, "ipv6": 1}}, names=[("a", BOTH)]))
        self.assertEqual(got["dns"], {"ipv4": 1, "ipv6": 1})
        self.assertEqual(got["a"], {"ipv4": 2, "ipv6": 2})

    def test_live_numbers_of_names_still_wanted_are_kept(self) -> None:
        got = allocate(
            _request(
                names=[("a", BOTH), ("b", BOTH)],
                live={"b": {"ipv4": 5, "ipv6": 5}, "gone": {"ipv4": 1, "ipv6": 1}},
                held={"ipv4": [1, 5], "ipv6": [1, 5]},
            )
        )
        self.assertEqual(got["b"], {"ipv4": 5, "ipv6": 5})
        # .1 is still held by a name that left the IR: never handed out
        self.assertEqual(got["a"], {"ipv4": 2, "ipv6": 2})
        self.assertNotIn("gone", got)

    def test_high_low_starts_at_the_top(self) -> None:
        got = allocate(
            _request(
                prefixes={"ipv4": "10.0.0.0/28"},
                strategy={"ipv4": "high-low", "ipv6": "high-low"},
                names=[("a", ("ipv4",))],
            )
        )
        self.assertEqual(got["a"], {"ipv4": 14})

    def test_random_is_deterministic(self) -> None:
        req = _request(
            strategy={"ipv4": "random", "ipv6": "random"}, names=[("a", BOTH), ("b", BOTH)]
        )
        self.assertEqual(allocate(req), allocate(req))

    def test_differing_strategies_allocate_each_family_on_its_own(self) -> None:
        got = allocate(
            _request(strategy={"ipv4": "low-high", "ipv6": "high-low"}, names=[("a", BOTH)])
        )
        self.assertEqual(got["a"], {"ipv4": 1, "ipv6": 65535})

    def test_two_names_on_one_pinned_number_is_an_error(self) -> None:
        with self.assertRaisesRegex(AddressError, "pinned by both"):
            allocate(_request(pinned={"a": {"ipv4": 3}, "b": {"ipv4": 3}}))

    def test_a_pinned_number_taking_a_live_name_s_number_is_an_error(self) -> None:
        with self.assertRaisesRegex(AddressError, "already uses"):
            allocate(
                _request(
                    pinned={"new": {"ipv4": 4}},
                    names=[("old", ("ipv4",))],
                    live={"old": {"ipv4": 4}},
                )
            )

    def test_an_exhausted_pool_names_who_could_not_be_placed(self) -> None:
        with self.assertRaisesRegex(AddressError, "no free ipv4 address left for c"):
            allocate(
                _request(
                    prefixes={"ipv4": "10.0.0.0/30"},
                    names=[("a", ("ipv4",)), ("b", ("ipv4",)), ("c", ("ipv4",))],
                )
            )


class AddressesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.addrs = Addresses.resolve(NODES)
        self.lrps = {n.id: n for n in NODES if isinstance(n, pt.OvnLrpNode)}
        self.right = next(
            n for n in NODES if isinstance(n, pt.KernelRouterNode) and n.key.side == pt.Side.right
        )

    def test_a_pinned_address_with_its_prefix(self) -> None:
        self.assertEqual(self.addrs.list(self.lrps[LRP_HOME].data.addresses), ["192.168.1.1/24"])

    def test_an_allocated_address_gets_the_first_free_host(self) -> None:
        self.assertEqual(self.addrs.list(self.lrps[LRP_BACKBONE].data.addresses), ["172.22.0.1/16"])

    def test_a_next_hop_no_carrier_holds_is_its_pinned_number(self) -> None:
        route = (self.right.data.routes or [])[0]
        assert route.via is not None
        self.assertEqual(self.addrs.ref(route.via), "192.168.132.1")

    def test_a_reference_resolves_to_its_carrier_bare(self) -> None:
        ref = pt.AddrRef(net="ipnet:ls:backbone", owner=LRP_BACKBONE, family=pt.Family.ipv4)
        self.assertEqual(self.addrs.ref(ref), "172.22.0.1")

    def test_an_address_that_is_not_the_fabric_s_stays_as_written(self) -> None:
        self.assertEqual(self.addrs.ref("1.1.1.1"), "1.1.1.1")

    def test_the_mac_folds_the_resolved_ipv4(self) -> None:
        ips = self.lrps[LRP_BACKBONE].data.addresses
        self.assertEqual(self.addrs.mac(ips, "x"), "00:00:ac:16:00:01")


class MacTest(unittest.TestCase):
    def test_from_v4(self) -> None:
        self.assertEqual(mac_from_v4("192.168.128.2/24"), "00:00:c0:a8:80:02")

    def test_from_name_matches_the_typescript_side(self) -> None:
        # src/addressing.ts macFromName("internet-cable-starlink")
        self.assertEqual(mac_from_name("internet-cable-starlink"), "02:5f:6d:c0:99:7b")


if __name__ == "__main__":
    unittest.main()
