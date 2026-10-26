from __future__ import annotations

from ladops import JsonObject
from ladops.ovn import (
    list_logical_routers,
    list_logical_switches,
    list_lrps,
    list_lsps,
    list_static_routes,
)


def _tag(external_ids: JsonObject, key: str) -> str | None:
    value = external_ids.get(f"ovn-fabric.{key}")
    return value if isinstance(value, str) else None


def _security_groups(external_ids: JsonObject) -> list[JsonObject]:
    raw = _tag(external_ids, "securityGroups")
    if not raw:
        return []
    return [{"render": "ovn", "name": name} for name in raw.split(",") if name]


def reconcile(scope: JsonObject, netns: str | None = None) -> dict[str, JsonObject]:
    if netns is not None:
        return {}

    nodes: dict[str, JsonObject] = {}

    for switch in list_logical_switches():
        id_ = f"ls:{switch['name']}"
        nodes[id_] = {
            "id": id_,
            "kind": "ovn.ls",
            "key": {"name": switch["name"]},
            # Host bindings are not visible from the northbound DB; the
            # host-side reconciler (OVS/bridge) owns that half.
            "data": {"interfaces": []},
        }

    for router in list_logical_routers():
        id_ = f"ovnrouter:{router['name']}"
        options = router.get("options", {})
        security_groups = _security_groups(router["external_ids"])
        nodes[id_] = {
            "id": id_,
            "kind": "ovn.lr",
            "key": {"name": router["name"]},
            "data": {
                "options": (
                    {"mac_binding_age_threshold": options["mac_binding_age_threshold"]}
                    if "mac_binding_age_threshold" in options
                    else {}
                ),
                **({"securityGroups": security_groups} if security_groups else {}),
            },
        }

    lsp_by_router_port: dict[str, str] = {}
    for lsp in list_lsps():
        router_port = lsp["options"].get("router-port")
        if router_port:
            lsp_by_router_port[router_port] = lsp["switch"]

    for lrp in list_lrps():
        id_ = f"ovnrouter:{lrp['router']}|lrp:{lrp['name']}"
        ls_name = lsp_by_router_port.get(lrp["name"])
        if ls_name is None:
            continue
        gateway = _tag(lrp["external_ids"], "gatewayChassis")
        ra = lrp.get("ipv6RaConfigs") or {}
        nodes[id_] = {
            "id": id_,
            "kind": "ovn.lrp",
            "key": {"ovnrouter": lrp["router"], "name": lrp["name"]},
            "data": {
                "l2Segment": f"ls:{ls_name}",
                "addresses": list(lrp["networks"]),
                "mac": lrp["mac"],
                **({"gatewayChassis": gateway} if gateway else {}),
                **({"ipv6RaConfigs": ra} if ra else {}),
            },
        }

    for route in list_static_routes():
        prefix = route["prefix"]
        kind = "ipv6.route" if ":" in prefix else "ipv4.route"
        id_ = f"ovnrouter:{route['router']}|route:{prefix}"
        nodes[id_] = {
            "id": id_,
            "kind": kind,
            "key": {"ovnrouter": route["router"], "prefix": prefix},
            "data": {
                "nexthop": route["nexthop"],
                "securityGroups": _security_groups(route["external_ids"]),
                "domain": _tag(route["external_ids"], "domain") or "live",
                "metric": int(_tag(route["external_ids"], "metric") or 0),
            },
        }

    return nodes
