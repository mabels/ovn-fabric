from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from . import owners

G = "*global*"


def _nodes() -> dict[str, dict[str, Any]]:
    raw: list[dict[str, Any]] = [
        {"id": f"netns:{G}", "kind": "netns"},
        {"id": "netns:ns-r", "kind": "netns"},
        # the host's NIC, a VLAN of ours on it, the loopback in two netns
        {"id": f"link:{G}|host", "kind": "link", "ifname": "host"},
        {"id": f"addr:{G}|host|192.168.128.32/24", "kind": "addr", "linkRef": f"link:{G}|host"},
        {"id": f"link:{G}|ovnkern", "kind": "link", "ifname": "ovnkern"},
        {"id": f"link:{G}|ovnkern.1128", "kind": "link", "ifname": "ovnkern.1128"},
        {"id": f"link:{G}|lo", "kind": "link", "ifname": "lo"},
        {"id": "link:ns-r|lo", "kind": "link", "ifname": "lo"},
        {"id": "addr:ns-r|veth|fe80::1/64", "kind": "addr", "address": "fe80::1"},
        {"id": "route:ns-r|fe80::/64", "kind": "route", "prefix": "fe80::/64"},
        # docker's bridge, a route on it, its rules — and one of ours
        {"id": f"link:{G}|docker0", "kind": "link", "ifname": "docker0"},
        {"id": f"route:{G}|172.17.0.0/16", "kind": "route", "devRef": f"link:{G}|docker0"},
        {
            "id": "iptables:a",
            "kind": "iptables.rule",
            "netnsRef": f"netns:{G}",
            "chain": "DOCKER-FORWARD",
            "rule": {"action": "ACCEPT", "fields": {}},
        },
        {
            "id": "iptables:b",
            "kind": "iptables.rule",
            "netnsRef": f"netns:{G}",
            "chain": "FORWARD",
            "rule": {"action": "JUMP", "target": "DOCKER-USER", "fields": {}},
        },
        {
            "id": "iptables:c",
            "kind": "iptables.rule",
            "netnsRef": f"netns:{G}",
            "chain": "POSTROUTING",
            "rule": {"action": "MASQUERADE", "fields": {"src": "172.17.0.0/16"}},
        },
        {
            "id": "iptables:ours",
            "kind": "iptables.rule",
            "netnsRef": "netns:ns-r",
            "chain": "POSTROUTING",
            "rule": {"action": "MASQUERADE", "fields": {"oif": "wan0"}},
        },
        # OVS's datapath, OVN's bridge and patch ports, a bridge of ours
        {"id": f"link:{G}|ovs-system", "kind": "link", "ifname": "ovs-system"},
        {"id": f"link:{G}|genev_sys_6081", "kind": "link", "ifname": "genev_sys_6081"},
        {"id": "ovs.external-id:system-id", "kind": "ovs.external-id"},
        {"id": "ovs.external-id:ovn-remote", "kind": "ovs.external-id"},
        {"id": "ovs.bridge:br-int", "kind": "ovs.bridge", "portRefs": ["ovs.port:br-int"]},
        {"id": "ovs.port:br-int", "kind": "ovs.port", "ifaceRefs": ["ovs.iface:br-int"]},
        {"id": "ovs.iface:br-int", "kind": "ovs.iface", "name": "br-int"},
        {"id": f"link:{G}|br-int", "kind": "link", "ifname": "br-int"},
        {"id": "ovs.bridge:br-home", "kind": "ovs.bridge", "portRefs": ["ovs.port:patch-x"]},
        {"id": "ovs.port:patch-x", "kind": "ovs.port", "ifaceRefs": ["ovs.iface:patch-x"]},
        {
            "id": "ovs.iface:patch-x",
            "kind": "ovs.iface",
            "name": "patch-x",
            "type": "patch",
            "portRef": "ovs.port:patch-x",
        },
    ]
    return {node["id"]: {**node, "owner": "?"} for node in raw}


class OwnersTest(unittest.TestCase):
    def _discovered(self) -> dict[str, dict[str, Any]]:
        nodes = _nodes()
        networks = [{"name": "bridge", "bridge": "docker0", "subnets": ["172.17.0.0/16"]}]
        datapath = {"datapaths": ["ovs-system"], "tunnels": ["genev_sys_6081"]}
        with (
            mock.patch.object(owners, "list_networks", return_value=networks),
            mock.patch.object(owners, "list_containers", return_value=[]),
            mock.patch.object(owners, "list_datapath", return_value=datapath),
            mock.patch.object(owners, "list_external_ids", return_value={}),
        ):
            owners.discover(nodes)
        return nodes

    def test_each_owner_is_found_with_what_is_on_its_interfaces(self) -> None:
        owner = {node_id: node["owner"] for node_id, node in self._discovered().items()}
        for node_id in ("netns:*global*", "addr:ns-r|veth|fe80::1/64", "route:ns-r|fe80::/64"):
            self.assertEqual(owner[node_id], "kernel", node_id)
        for node_id in (
            f"link:{G}|docker0",
            f"route:{G}|172.17.0.0/16",  # on its bridge
            "iptables:a",  # its chain
            "iptables:b",  # a jump into it
            "iptables:c",  # the masquerade of its subnet
        ):
            self.assertEqual(owner[node_id], "docker", node_id)
        for node_id in (f"link:{G}|ovs-system", f"link:{G}|genev_sys_6081"):
            self.assertEqual(owner[node_id], "openvswitch", node_id)
        self.assertEqual(owner["ovs.external-id:system-id"], "openvswitch")
        for node_id in (
            "ovs.bridge:br-int",
            "ovs.port:br-int",
            "ovs.iface:br-int",
            f"link:{G}|br-int",
            "ovs.iface:patch-x",
            "ovs.port:patch-x",
        ):
            self.assertEqual(owner[node_id], "ovn-controller", node_id)
        for node_id in ("iptables:ours", "ovs.external-id:ovn-remote", "ovs.bridge:br-home"):
            self.assertEqual(owner[node_id], "ovn-fabric", node_id)

    def test_what_the_kernel_and_a_container_make_is_theirs(self) -> None:
        extra: list[dict[str, Any]] = [
            {"id": "route:r|kernel", "kind": "route", "prefix": "10.0.0.0/8", "protocol": "kernel"},
            {"id": "route:r|ra", "kind": "route", "prefix": "::/0", "protocol": "ra"},
            {"id": "route:r|static", "kind": "route", "prefix": "10.1.0.0/16", "protocol": None},
            {"id": "addr:r|slaac", "kind": "addr", "address": "fd00::1", "dynamic": True},
            {"id": "link:ns-r|sit0", "kind": "link", "ifname": "sit0"},
            {"id": "docker.container:ours", "kind": "docker.container"},
            {"id": "docker.container:theirs", "kind": "docker.container"},
            {"id": "netns:ours", "kind": "netns"},
            {"id": "netns:docker:ours", "kind": "netns"},
            {"id": "link:ours|zt0", "kind": "link", "ifname": "zt0", "netnsRef": "netns:ours"},
        ]
        nodes = {node["id"]: {**node, "owner": "?"} for node in extra}
        containers = [
            {"name": "ours", "labels": {"ovn-fabric.spec": "abc"}},
            {"name": "theirs", "labels": {}},
        ]
        with (
            mock.patch.object(owners, "list_networks", return_value=[]),
            mock.patch.object(owners, "list_containers", return_value=containers),
            mock.patch.object(
                owners, "list_datapath", return_value={"datapaths": [], "tunnels": []}
            ),
            mock.patch.object(owners, "list_external_ids", return_value={}),
        ):
            owners.discover(nodes)
        owner = {node_id: node["owner"] for node_id, node in nodes.items()}
        for node_id in ("route:r|kernel", "route:r|ra", "addr:r|slaac", "link:ns-r|sit0"):
            self.assertEqual(owner[node_id], "kernel", node_id)
        self.assertEqual(owner["route:r|static"], "ovn-fabric")
        self.assertEqual(owner["docker.container:ours"], "ovn-fabric")  # we started it
        self.assertEqual(owner["docker.container:theirs"], "docker")
        for node_id in ("netns:ours", "netns:docker:ours", "link:ours|zt0"):
            self.assertEqual(owner[node_id], "docker", node_id)  # inside a container

    def test_a_physical_nic_is_the_host_s_with_what_is_on_it(self) -> None:
        nodes = self._discovered()
        for node in nodes.values():
            node["owner"] = "ovn-fabric"
        nic = f"link:{G}|nic0"
        nodes[nic] = {
            "id": nic,
            "kind": "link",
            "ifname": "nic0",
            "linkKind": "physical",
            "owner": "ovn-fabric",
        }
        addr = f"addr:{G}|nic0|10.0.0.1/24"
        nodes[addr] = {"id": addr, "kind": "addr", "linkRef": nic, "owner": "ovn-fabric"}
        owners._physical(nodes)
        self.assertEqual((nodes[nic]["owner"], nodes[addr]["owner"]), ("host", "host"))

    def test_the_host_s_interfaces_not_what_is_stacked_on_them(self) -> None:
        nodes = self._discovered()
        owners.mark_host(nodes, ["host", "lo"], parents=["ovnkern"])
        owner = {node_id: node["owner"] for node_id, node in nodes.items()}
        for node_id in (
            f"link:{G}|host",
            f"addr:{G}|host|192.168.128.32/24",  # on it
            f"link:{G}|lo",
            "link:ns-r|lo",  # the loopback in every netns
            f"link:{G}|ovnkern",  # a VLAN's NIC
        ):
            self.assertEqual(owner[node_id], "host", node_id)
        self.assertEqual(owner[f"link:{G}|ovnkern.1128"], "ovn-fabric")  # the VLAN: ours
        self.assertNotIn(f"link:{G}|host", owners.ours(nodes))


if __name__ == "__main__":
    unittest.main()
