from __future__ import annotations

import socket
from datetime import datetime, timezone
from typing import Any

from ladops.docker import list_containers
from ladops.iptables import list_rules
from ladops.linux_net import list_addrs, list_links_detail, list_routes
from ladops.netns import list_netns
from ladops.ovn import (
    list_acls,
    list_connections,
    list_logical_routers,
    list_logical_switches,
    list_lrps,
    list_lsps,
    list_nats,
    list_static_routes,
)
from ladops.ovs import list_bridge_mappings, list_bridge_ports, list_external_ids
from protocol import raw_generated as raw
from protocol.hydrate import hydrate_as

GLOBAL_NETNS = "*global*"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ns(netns: str | None) -> str:
    return GLOBAL_NETNS if netns is None else netns


def _link_id(ns: str, ifname: str) -> str:
    return f"link:{ns}|{ifname}"


def _netns_nodes(netns_names: list[str]) -> list[dict[str, Any]]:
    nodes = []
    for name in [GLOBAL_NETNS, *netns_names]:
        nodes.append(
            {
                "id": f"netns:{name}",
                "kind": "netns",
                "collection": {
                    "scope": "global" if name == GLOBAL_NETNS else "isolated",
                    "name": name,
                    "linkRefs": [],
                    "routeRefs": [],
                    "ruleRefs": [],
                },
            }
        )
    return nodes


def _link_nodes(
    spaces: list[tuple[str, str | None, int | None]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], str]]:
    nodes: list[dict[str, Any]] = []
    by_index: dict[tuple[str, int], str] = {}
    raw_links: list[tuple[str, dict[str, Any]]] = []
    for ns, netns, pid in spaces:
        for link in list_links_detail(netns, pid):
            raw_links.append((ns, link))
            by_index[(ns, link["ifindex"])] = _link_id(ns, link["ifname"])
    for ns, link in raw_links:
        link_ref = (
            by_index.get((ns, link["linkIndex"])) if link.get("linkIndex") is not None else None
        )
        nodes.append(
            {
                "id": _link_id(ns, link["ifname"]),
                "kind": "link",
                "collection": {
                    "netnsRef": f"netns:{ns}",
                    "ifname": link["ifname"],
                    "ifindex": link["ifindex"],
                    "linkKind": link["kind"],
                    "mac": link.get("mac"),
                    "mtu": link.get("mtu"),
                    "linkRef": link_ref,
                    "peerRefs": [link_ref] if link_ref and link["kind"] == "veth" else [],
                    "masterRef": (_link_id(ns, link["master"]) if link.get("master") else None),
                    "addrRefs": [],
                    "adminUp": link["adminUp"],
                    "operState": link.get("operState"),
                },
            }
        )
    return nodes, by_index


def _addr_nodes(
    spaces: list[tuple[str, str | None, int | None]],
    link_ids: dict[tuple[str, str], str],
) -> list[dict[str, Any]]:
    nodes = []
    for ns, netns, pid in spaces:
        for addr in list_addrs(netns, pid):
            link_id = link_ids.get((ns, addr["interface"]))
            id_ = f"addr:{ns}|{addr['interface']}|{addr['addr']}"
            nodes.append(
                {
                    "id": id_,
                    "kind": "addr",
                    "collection": {
                        "netnsRef": f"netns:{ns}",
                        "linkRef": link_id or f"link:{ns}|{addr['interface']}",
                        "family": addr["family"],
                        "address": addr["addr"].split("/")[0],
                        "prefixlen": int(addr["addr"].split("/")[1]),
                    },
                }
            )
    return nodes


# `ip route` names a default route "default" in both families; RawIR
# names it as the deployer does.
_DEFAULT = {"ipv4": "0.0.0.0/0", "ipv6": "::/0"}


def _route_nodes(
    spaces: list[tuple[str, str | None, int | None]],
) -> list[dict[str, Any]]:
    nodes = []
    for ns, netns, pid in spaces:
        for family in ("-4", "-6"):
            for route in list_routes(netns, family, pid):
                prefix = (
                    _DEFAULT[route["family"]] if route["prefix"] == "default" else route["prefix"]
                )
                id_ = (
                    f"route:{ns}|{route['family']}|{route.get('table') or 'main'}|"
                    f"{prefix}|{route['dev']}|{route['nexthop']}|"
                    f"{route.get('metric')}"
                )
                nodes.append(
                    {
                        "id": id_,
                        "kind": "route",
                        "collection": {
                            "netnsRef": f"netns:{ns}",
                            "family": route["family"],
                            "prefix": prefix,
                            "devRef": _link_id(ns, route["dev"]),
                            "via": route["nexthop"],
                            "metric": route.get("metric"),
                            "table": route.get("table") or "main",
                            "protocol": route.get("protocol"),
                        },
                    }
                )
    return nodes


def _iptables_nodes(
    spaces: list[tuple[str, str | None, int | None]],
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for ns, netns, pid in spaces:
        # nft has no nsenter helper here yet; skip docker for firewall
        # discovery until the same pid path exists in ladops.iptables.
        if pid is not None:
            continue
        seen: dict[str, int] = {}
        for rule in list_rules(netns):
            family = "ipv4" if rule["family"] == "ip" else "ipv6"
            # By content — what it matches and what it does — so a rule is
            # found wherever it sits in its chain (its position: `handle`);
            # the same rule twice gets "#2".
            matches = ",".join(f"{key}={value}" for key, value in sorted(rule["fields"].items()))
            id_ = (
                f"iptables:{ns}|{family}|{rule['table']}|{rule['chain']}|"
                f"{matches}|{rule['action']}"
            )
            seen[id_] = seen.get(id_, 0) + 1
            if seen[id_] > 1:
                id_ += f"#{seen[id_]}"
            nodes.append(
                {
                    "id": id_,
                    "kind": "iptables.rule",
                    "collection": {
                        "netnsRef": f"netns:{ns}",
                        "family": family,
                        "table": rule["table"],
                        "chain": rule["chain"],
                        "rule": {
                            "fields": rule["fields"],
                            "action": rule["action"],
                        },
                        "handle": rule["order"],
                    },
                }
            )
    return nodes


def _docker_nodes(containers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": f"docker.container:{container['containerId']}",
            "kind": "docker.container",
            "collection": {
                "containerId": container["containerId"],
                "name": container["name"],
                "image": container["image"],
                "pid": container["pid"],
                "state": container["state"],
                "netnsRef": container["netnsRef"],
            },
        }
        for container in containers
    ]


def _ovs_nodes() -> list[dict[str, Any]]:
    mappings = list_bridge_mappings()
    rows = list_bridge_ports()
    bridges = sorted({row["bridge"] for row in rows})
    # The host's own settings; the bridge mappings are a field of each
    # bridge instead (below).
    nodes: list[dict[str, Any]] = [
        {
            "id": f"ovs.external-id:{key}",
            "kind": "ovs.external-id",
            "collection": {"key": key, "value": value},
        }
        for key, value in sorted(list_external_ids().items())
        if key != "ovn-bridge-mappings"
    ]
    for bridge in bridges:
        port_names = sorted({row["port"] for row in rows if row["bridge"] == bridge})
        nodes.append(
            {
                "id": f"ovs.bridge:{bridge}",
                "kind": "ovs.bridge",
                "collection": {
                    "name": bridge,
                    "portRefs": [f"ovs.port:{p}" for p in port_names],
                    "externalIds": (
                        {"ovn-bridge-mappings": mappings[bridge]} if bridge in mappings else {}
                    ),
                },
            }
        )
        for port in port_names:
            ifaces = sorted(
                {row["name"] for row in rows if row["bridge"] == bridge and row["port"] == port}
            )
            nodes.append(
                {
                    "id": f"ovs.port:{port}",
                    "kind": "ovs.port",
                    "collection": {
                        "bridgeRef": f"ovs.bridge:{bridge}",
                        "name": port,
                        "trunks": [],
                        "ifaceRefs": [f"ovs.iface:{i}" for i in ifaces],
                        "externalIds": {},
                    },
                }
            )
            for iface in ifaces:
                row = next(
                    r
                    for r in rows
                    if r["bridge"] == bridge and r["port"] == port and r["name"] == iface
                )
                nodes.append(
                    {
                        "id": f"ovs.iface:{iface}",
                        "kind": "ovs.iface",
                        "collection": {
                            "portRef": f"ovs.port:{port}",
                            "netnsRef": f"netns:{GLOBAL_NETNS}",
                            "name": iface,
                            "type": row.get("type", ""),
                            "mac": row.get("mac"),
                            "options": row.get("options", {}),
                            "externalIds": row.get("external_ids", {}),
                        },
                    }
                )
    return nodes


def _ovn_nodes() -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    router_by_lrp = {lrp["name"]: lrp["router"] for lrp in list_lrps()}
    switch_routers: dict[str, set[str]] = {}
    for lsp in list_lsps():
        lrp_name = lsp["options"].get("router-port")
        router = router_by_lrp.get(lrp_name) if lrp_name else None
        if router is not None:
            switch_routers.setdefault(lsp["switch"], set()).add(router)
    for switch in list_logical_switches():
        nodes.append(
            {
                "id": f"ovn.ls:{switch['name']}",
                "kind": "ovn.ls",
                "collection": {
                    "name": switch["name"],
                    "portRefs": [],
                    "externalIds": switch.get("external_ids", {}),
                },
            }
        )
    for lsp in list_lsps():
        nodes.append(
            {
                "id": f"ovn.lsp:{lsp['switch']}|{lsp['name']}",
                "kind": "ovn.lsp",
                "collection": {
                    "switchRef": f"ovn.ls:{lsp['switch']}",
                    "name": lsp["name"],
                    "type": lsp.get("type", ""),
                    "addresses": lsp.get("addresses", []),
                    "options": lsp.get("options", {}),
                    "externalIds": lsp.get("external_ids", {}),
                },
            }
        )
    for router in list_logical_routers():
        nodes.append(
            {
                "id": f"ovn.lr:{router['name']}",
                "kind": "ovn.lr",
                "collection": {
                    "name": router["name"],
                    "options": router.get("options", {}),
                    "portRefs": [],
                    "routeRefs": [],
                    "natRefs": [],
                    "aclRefs": [],
                    "externalIds": router.get("external_ids", {}),
                },
            }
        )
    for lrp in list_lrps():
        nodes.append(
            {
                "id": f"ovn.lrp:{lrp['router']}|{lrp['name']}",
                "kind": "ovn.lrp",
                "collection": {
                    "routerRef": f"ovn.lr:{lrp['router']}",
                    "name": lrp["name"],
                    "mac": lrp["mac"],
                    "networks": lrp["networks"],
                    "gatewayChassis": lrp.get("gatewayChassis", []),
                    "ipv6RaConfigs": lrp.get("ipv6RaConfigs", {}),
                    "externalIds": lrp.get("external_ids", {}),
                },
            }
        )
    for route in list_static_routes():
        nodes.append(
            {
                "id": f"ovn.route:{route['router']}|{route['prefix']}|{route['nexthop']}",
                "kind": "ovn.route",
                "collection": {
                    "routerRef": f"ovn.lr:{route['router']}",
                    "prefix": route["prefix"],
                    "nexthop": route["nexthop"],
                    "externalIds": route.get("external_ids", {}),
                },
            }
        )
    for nat in list_nats():
        nodes.append(
            {
                # Keyed by what it is, not its UUID (new on every create):
                # the same key the NAT primitive declares, so it can be
                # predicted (fabric/rawir_learned.py, ADR 0008).
                "id": (
                    f"ovn.nat:{nat['router']}|{nat['type']}|{nat['externalIp']}|"
                    f"{nat['logicalIp']}"
                ),
                "kind": "ovn.nat",
                "collection": {
                    "routerRef": f"ovn.lr:{nat['router']}",
                    "type": nat["type"],
                    "externalIp": nat["externalIp"],
                    "logicalIp": nat["logicalIp"],
                    "externalMac": nat["externalMac"] or None,
                    "logicalPort": nat["logicalPort"] or None,
                    "externalIds": nat.get("external_ids", {}),
                },
            }
        )
    for db in ("nb", "sb"):
        targets = list_connections(db)
        if targets:  # a database that does not listen has no node
            nodes.append(
                {
                    "id": f"ovn.connection:{db}",
                    "kind": "ovn.connection",
                    "collection": {"db": db, "targets": targets},
                }
            )
    for acl in list_acls():
        # By content, like the ACL primitive's key (see the NAT above).
        acl_id = (
            f"ovn.acl:{acl['switch']}|{acl['direction']}|{acl['priority']}|"
            f"{acl['match']}|{acl['action']}"
        )
        nodes.append(
            {
                "id": acl_id,
                "kind": "ovn.acl",
                "collection": {
                    "switchRef": f"ovn.ls:{acl['switch']}",
                    "direction": acl["direction"],
                    "priority": acl["priority"],
                    "match": acl["match"],
                    "action": acl["action"],
                    "externalIds": acl.get("external_ids", {}),
                },
            }
        )
        # OVN stores ACLs on the switch, but the router that owns the
        # switch's router-port is the semantic owner (same role as the
        # netns owning its iptables rules).
        for router in switch_routers.get(acl["switch"], set()):
            lr_id = f"ovn.lr:{router}"
            lr = next(
                (n for n in nodes if n["id"] == lr_id),
                None,
            )
            if lr is not None:
                lr["collection"]["aclRefs"].append(acl_id)
    return nodes


def _wire_references(nodes: list[dict[str, Any]]) -> None:
    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        payload = node
        kind = node["kind"]
        if kind == "link":
            ns = by_id.get(payload["netnsRef"])
            if ns is not None:
                ns["linkRefs"].append(node["id"])
        elif kind == "addr":
            link = by_id.get(payload["linkRef"])
            if link is not None:
                link["addrRefs"].append(node["id"])
        elif kind == "route":
            ns = by_id.get(payload["netnsRef"])
            if ns is not None:
                ns["routeRefs"].append(node["id"])
        elif kind == "iptables.rule":
            ns = by_id.get(payload["netnsRef"])
            if ns is not None:
                ns["ruleRefs"].append(node["id"])
        elif kind == "ovn.lsp":
            switch = by_id.get(payload["switchRef"])
            if switch is not None:
                switch["portRefs"].append(node["id"])
        elif kind == "ovn.lrp":
            router = by_id.get(payload["routerRef"])
            if router is not None:
                router["portRefs"].append(node["id"])
        elif kind == "ovn.route":
            router = by_id.get(payload["routerRef"])
            if router is not None:
                router["routeRefs"].append(node["id"])
        elif kind == "ovn.nat":
            router = by_id.get(payload["routerRef"])
            if router is not None:
                router["natRefs"].append(node["id"])


def _wire(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    host = socket.gethostname()
    time = _now()
    for node in nodes:
        node.setdefault("host", host)
        node.setdefault("time", time)
    return nodes


def collect(host: str | None = None) -> raw.RawSnapshot:
    netns_names = list_netns()
    containers = [c for c in list_containers() if c["pid"] > 0]
    spaces: list[tuple[str, str | None, int | None]] = [
        (_ns(netns), netns, None) for netns in [None, *netns_names]
    ] + [(f"docker:{c['name']}", None, c["pid"]) for c in containers]
    links, by_index = _link_nodes(spaces)
    # Build the (netns, ifname) -> id map directly from the link nodes.
    link_ids = {}
    for node in links:
        collection = node["collection"]
        link_ids[(collection["netnsRef"].removeprefix("netns:"), collection["ifname"])] = node["id"]

    nodes = [
        *_netns_nodes([*netns_names, *(f"docker:{c['name']}" for c in containers)]),
        *links,
        *_addr_nodes(spaces, link_ids),
        *_route_nodes(spaces),
        *_iptables_nodes(spaces),
        *_docker_nodes(containers),
        *_ovs_nodes(),
        *_ovn_nodes(),
    ]
    # The schema is flat: identity/payload live on one object.
    for node in nodes:
        node.update(node.pop("collection", {}))
    _wire_references(nodes)
    wired = _wire(nodes)
    if host is not None:
        for node in wired:
            node["host"] = host
    return hydrate_as(
        raw.RawSnapshot,
        {
            "host": host or socket.gethostname(),
            "time": _now(),
            "collections": wired,
        },
    )
