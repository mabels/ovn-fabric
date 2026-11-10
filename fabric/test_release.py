from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from . import release

G = "*global*"


def _live() -> dict[str, dict[str, Any]]:
    """The 131 case: the VLAN still a port of the old bridge (a leftover)."""
    nodes: list[dict[str, Any]] = [
        {
            "id": f"link:{G}|ovnkern",
            "kind": "link",
            "linkKind": "physical",
            "mac": "bc:24:11:e8:68:a8",
        },
        {
            "id": f"link:{G}|ovnkern.1131",
            "kind": "link",
            "linkKind": "vlan",
            "vlanId": 1131,
            "linkRef": f"link:{G}|ovnkern",
        },
        {"id": "ovs.iface:ovnkern.1131", "kind": "ovs.iface", "name": "ovnkern.1131"},
        {
            "id": "ovs.port:ovnkern.1131",
            "kind": "ovs.port",
            "ifaceRefs": ["ovs.iface:ovnkern.1131"],
        },
        {"id": "ovs.bridge:br-old", "kind": "ovs.bridge", "portRefs": ["ovs.port:ovnkern.1131"]},
        {"id": "ovs.bridge:br-keep", "kind": "ovs.bridge", "portRefs": []},
    ]
    return {n["id"]: n for n in nodes}


class PlannedTest(unittest.TestCase):
    def test_a_moved_tunnel_releases_the_old_one(self) -> None:
        from ladops.linux_net import key_hash

        from .primitives.host import HostPrimitive

        host = HostPrimitive("h")
        conf = "[Interface]\nPrivateKey = c2VjcmV0\nAddress = 10.64.0.2/32\n"
        wg = host.netns("ns-new").wireguard("wg0", "/etc/wg0.conf", conf)
        zt = host.docker("home-vpn-zerotier", "img", natural_key="zt:17167e57d7")
        live = {
            "link:ns-old|wg0": {
                "id": "link:ns-old|wg0",
                "kind": "link",
                "linkKind": "wireguard",
                "wgKeyHash": key_hash("c2VjcmV0"),
            },
            "docker.container:router-zerotier-v2": {
                "id": "docker.container:router-zerotier-v2",
                "kind": "docker.container",
                "naturalKeyLabel": "zt:17167e57d7",
            },
        }
        found = release.find([wg, zt], live, list(live))
        self.assertEqual(
            sorted(r.node_id for r in found),
            ["docker.container:router-zerotier-v2", "link:ns-old|wg0"],
        )
        # The secret is never part of a key.
        self.assertTrue(all("c2VjcmV0" not in r.key for r in found))


class ClaimantTest(unittest.TestCase):
    def test_the_planned_node_itself_is_never_released(self) -> None:
        from .primitives.host import HostPrimitive

        zt = HostPrimitive("h").docker("home-vpn-zerotier", "img", natural_key="zt:17167e57d7")
        live = {
            "docker.container:home-vpn-zerotier": {
                "id": "docker.container:home-vpn-zerotier",
                "kind": "docker.container",
                "naturalKeyLabel": "zt:17167e57d7",
            }
        }
        self.assertEqual(release.find([zt], live, list(live)), [])


class FindTest(unittest.TestCase):
    def test_the_old_bridge_holding_a_claimed_vlan_is_released(self) -> None:
        claimed = {"vlan:nic:bc:24:11:e8:68:a8|1131": f"link:{G}|ovnkern.1131"}
        left = ["ovs.bridge:br-old", "ovs.bridge:br-keep", "ovs.port:ovnkern.1131"]
        with mock.patch.object(release, "claims", return_value=claimed):
            found = release.find([], _live(), left)
        # The port is released; the old bridge stays a leftover for the end.
        self.assertEqual([r.node_id for r in found], ["ovs.port:ovnkern.1131"])
        self.assertTrue(all(r.key == "vlan:nic:bc:24:11:e8:68:a8|1131" for r in found))

    def test_a_port_the_plan_wants_too_is_released_off_the_old_bridge(self) -> None:
        # The new plan claims the same port id; only the old bridge is left.
        claimed = {"vlan:nic:bc:24:11:e8:68:a8|1131": f"link:{G}|ovnkern.1131"}
        with mock.patch.object(release, "claims", return_value=claimed):
            found = release.find([], _live(), ["ovs.bridge:br-old"])
        self.assertEqual([r.node_id for r in found], ["ovs.port:ovnkern.1131"])

    def test_a_netns_is_no_holder_of_its_links(self) -> None:
        live = _live()
        live["netns:ns-x"] = {
            "id": "netns:ns-x",
            "kind": "netns",
            "linkRefs": [f"link:{G}|ovnkern.1131"],
        }
        claimed = {"vlan:nic:bc:24:11:e8:68:a8|1131": f"link:{G}|ovnkern.1131"}
        with mock.patch.object(release, "claims", return_value=claimed):
            found = release.find([], live, ["netns:ns-x"])
        self.assertEqual(found, [])

    def test_nothing_is_released_when_its_holder_is_still_wanted(self) -> None:
        claimed = {"vlan:nic:bc:24:11:e8:68:a8|1131": f"link:{G}|ovnkern.1131"}
        with mock.patch.object(release, "claims", return_value=claimed):
            self.assertEqual(release.find([], _live(), ["ovs.bridge:br-keep"]), [])

    def test_nothing_claimed_nothing_released(self) -> None:
        with mock.patch.object(release, "claims", return_value={}):
            self.assertEqual(release.find([], _live(), ["ovs.bridge:br-old"]), [])


if __name__ == "__main__":
    unittest.main()
