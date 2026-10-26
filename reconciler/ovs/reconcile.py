from __future__ import annotations

from ladops import JsonObject
from ladops.linux_net import find_veth_peer
from ladops.ovs import list_bridge_mappings, list_bridge_ports


def _iface(
    name: str,
    mac: str,
    iface_type: str,
    options: JsonObject,
    bridge: str,
) -> JsonObject | None:
    if name == bridge or bridge == "br-int":
        return None
    if iface_type == "patch":
        return None
    if iface_type == "internal" and name.startswith("br-"):
        return None
    if iface_type == "veth" or (options.get("peer") and iface_type != "patch"):
        return {
            "kind": "veth",
            "ifaceName": name,
            "peerName": options["peer"],
            "bridge": bridge,
            **({"mac": mac} if mac else {}),
        }
    if "." in name:
        parent, _, vlan = name.rpartition(".")
        if parent and vlan.isdigit():
            return {
                "kind": "vlan",
                "vlanParent": {"kind": "physical", "name": parent},
                "vlanId": int(vlan),
                "ifaceName": name,
                "bridge": bridge,
            }
    return {"kind": "physical", "name": name, "bridge": bridge}


def reconcile(scope: JsonObject, netns: str | None = None) -> dict[str, JsonObject]:
    if netns is not None:
        return {}

    host = scope["host"]
    mappings = list_bridge_mappings()
    by_bridge: dict[str, list[JsonObject]] = {}
    seen_peers: set[str] = set()
    seen_names: set[str] = set()
    for row in list_bridge_ports():
        name = row["name"]
        if name in seen_names or name in seen_peers:
            continue
        if row.get("type") == "patch" or (row.get("type") == "internal" and name.startswith("br-")):
            continue
        peer = find_veth_peer(name)
        iface: JsonObject | None
        if peer is not None:
            iface = {
                "kind": "veth",
                "ifaceName": name,
                "peerName": peer["ifname"],
                "bridge": row["bridge"],
                **({"mac": peer["mac"]} if peer["mac"] else {}),
            }
        else:
            iface = _iface(
                name,
                row.get("mac", ""),
                row.get("type", ""),
                row.get("options", {}),
                row["bridge"],
            )
        if iface is None:
            continue
        seen_names.add(name)
        peer = iface.get("peerName")
        if isinstance(peer, str):
            seen_peers.add(peer)
        by_bridge.setdefault(row["bridge"], []).append(iface)

    nodes: dict[str, JsonObject] = {}
    for bridge in sorted(by_bridge):
        if bridge == "br-int":
            continue
        ports = by_bridge[bridge]
        network = mappings.get(bridge)
        if network is None:
            id_ = f"bridge:{bridge}"
            nodes[id_] = {
                "id": id_,
                "kind": "bridge",
                "key": {"name": bridge},
                "data": {
                    "host": f"host:{host}",
                    "ports": [{"host": host, "iface": iface} for iface in ports],
                },
            }
            continue
        domain = network.removeprefix("net-")
        id_ = f"ls:{domain}"
        nodes[id_] = {
            "id": id_,
            "kind": "ovn.ls",
            "key": {"name": domain},
            "data": {
                "interfaces": [{"host": host, "iface": iface} for iface in ports],
            },
        }

    # Interface-level reconciliation is intentionally silent for now:
    # the collision-domain graph above is the first target. The
    # list_interfaces()/linux_net readers remain available for the next
    # step.
    return nodes
