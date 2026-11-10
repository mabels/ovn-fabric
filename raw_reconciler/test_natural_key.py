from __future__ import annotations

import unittest
from typing import Any

from .natural_key import natural_key, natural_keys

G = "*global*"


def _link(name: str, kind: str, **fields: Any) -> dict[str, Any]:
    return {"id": f"link:{G}|{name}", "kind": "link", "ifname": name, "linkKind": kind, **fields}


class NaturalKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.live = {
            n["id"]: n
            for n in (
                _link("ovnkern", "physical", mac="bc:24:11:e8:68:a8"),
                _link(
                    "ovnkern.1131",
                    "vlan",
                    mac="bc:24:11:e8:68:a8",  # a VLAN has its parent's MAC
                    vlanId=1131,
                    linkRef=f"link:{G}|ovnkern",
                ),
                _link("veth-krn-1", "veth", mac="00:00:0a:0c:56:0e"),
                _link("br-x", "bridge", mac="aa:aa:aa:aa:aa:aa"),
            )
        }

    def test_a_vlan_is_its_parent_and_its_id_not_its_name(self) -> None:
        self.assertEqual(
            natural_key(self.live[f"link:{G}|ovnkern.1131"], self.live),
            "vlan:nic:bc:24:11:e8:68:a8|1131",
        )
        # Renamed — the NIC and the VLAN: the same key.
        renamed = {
            n["id"]: n
            for n in (
                _link("ens18", "physical", mac="bc:24:11:e8:68:a8"),
                _link("ens18.1131", "vlan", vlanId=1131, linkRef=f"link:{G}|ens18"),
            )
        }
        self.assertEqual(
            natural_key(renamed[f"link:{G}|ens18.1131"], renamed),
            "vlan:nic:bc:24:11:e8:68:a8|1131",
        )

    def test_a_vlan_moved_into_a_netns_keeps_its_key(self) -> None:
        moved = {
            "id": "link:ns-wan|ovnkern.1131",
            "kind": "link",
            "netnsRef": "netns:ns-wan",
            "linkKind": "vlan",
            "vlanId": 1131,
            "linkRef": f"link:{G}|ovnkern",
        }
        self.assertEqual(natural_key(moved, self.live), "vlan:nic:bc:24:11:e8:68:a8|1131")

    def test_a_tunnel_is_its_identity_not_its_name_or_netns(self) -> None:
        wg = {"id": "link:ns-a|wg0", "kind": "link", "linkKind": "wireguard", "wgKeyHash": "ab12"}
        zt = {
            "id": "docker.container:x",
            "kind": "docker.container",
            "naturalKeyLabel": "zt:17167e57d7",
        }
        self.assertEqual(natural_key(wg, {}), "wg:ab12")
        self.assertEqual(natural_key(zt, {}), "zt:17167e57d7")
        plain = {"id": "docker.container:y", "kind": "docker.container"}
        self.assertIsNone(natural_key(plain, {}))

    def test_a_route_is_one_per_prefix_in_its_router_whatever_its_next_hop(self) -> None:
        old = {
            "id": "ovn.route:r|0.0.0.0/0|172.22.12.80",
            "kind": "ovn.route",
            "routerRef": "ovn.lr:r",
            "prefix": "0.0.0.0/0",
            "nexthop": "172.22.12.80",
        }
        new = {**old, "id": "ovn.route:r|0.0.0.0/0|172.21.0.1", "nexthop": "172.21.0.1"}
        self.assertEqual(natural_key(old, {}), natural_key(new, {}))
        kernel = {
            "id": "route:ns|ipv4|main|0.0.0.0/0|eth0|10.0.0.1|None",
            "kind": "route",
            "netnsRef": "netns:ns",
            "prefix": "0.0.0.0/0",
            "via": "10.0.0.1",
        }
        self.assertEqual(natural_key(kernel, {}), "route:netns:ns|main|0.0.0.0/0|0")
        v6 = {**kernel, "family": "ipv6", "prefix": "::/0"}
        self.assertEqual(natural_key(v6, {}), natural_key({**v6, "metric": 1024}, {}))

    def test_a_nic_is_its_permanent_mac(self) -> None:
        changed = _link("nic0", "physical", mac="02:00:00:00:00:01", permMac="bc:24:11:00:00:01")
        self.assertEqual(natural_key(changed, {}), "nic:bc:24:11:00:00:01")

    def test_only_single_resources_have_one(self) -> None:
        keys = natural_keys(self.live)
        self.assertEqual(
            keys,
            {
                f"link:{G}|ovnkern": "nic:bc:24:11:e8:68:a8",
                f"link:{G}|ovnkern.1131": "vlan:nic:bc:24:11:e8:68:a8|1131",
                f"link:{G}|veth-krn-1": "veth:00:00:0a:0c:56:0e",
            },
        )

    def test_a_vlan_without_its_parent_has_none(self) -> None:
        orphan = _link("x.5", "vlan", vlanId=5, linkRef=f"link:{G}|gone")
        self.assertIsNone(natural_key(orphan, {}))

    def test_a_collected_node_has_its_fields_under_collection(self) -> None:
        collected = {
            "id": f"link:{G}|nic0",
            "kind": "link",
            "collection": {"linkKind": "physical", "mac": "bc:24:11:00:00:02"},
        }
        self.assertEqual(natural_key(collected, {}), "nic:bc:24:11:00:00:02")


if __name__ == "__main__":
    unittest.main()
