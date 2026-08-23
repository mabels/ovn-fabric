# protocol/hydrate.py — hand-written, NOT regenerated (generated.py is
# — this file survives every `generate-pytypes` run). Converts a raw IR
# JSON node (dict, as loaded from generate-ir's output) into its typed
# dataclass (generated.InfraHostNode/OvnLsNode/OvnLrpNode), dispatched
# by `kind`.
#
# A KeyError (unknown `kind`) or TypeError (a required field missing —
# a real Python dataclass constructor call, not a hand-rolled check) IS
# the intended failure mode for version skew: this data is internal,
# produced by this project's own generator, not adversarial external
# input, so what actually needs catching is a stale deployer bundle
# receiving newer-shaped IR data, not defensive-grade validation (see
# ADR 0002, "Type stability across the TypeScript/Python boundary").
#
# Every OTHER Python component (deployer/ir_to_shell.py, deployer/
# cli.py) works with these typed nodes exclusively — this module is the
# ONE place a raw dict is still indexed by string key, precisely so it
# can be the one place that changes if the envelope shape ever does.

from __future__ import annotations

from . import generated as pt


def _hydrate_infra_host(raw: dict) -> pt.InfraHostNode:
    data = raw["data"]
    return pt.InfraHostNode(
        id=raw["id"],
        kind=raw["kind"],
        key=pt.InfraHostKey(host=raw["key"]["host"]),
        data=pt.InfraHostData(
            connectAddress=data["connectAddress"],
            encapIp=data.get("encapIp"),
            ovnRole=pt.OvnRole(data["ovnRole"]) if data.get("ovnRole") is not None else None,
        ),
    )


def _hydrate_ovn_ls(raw: dict) -> pt.OvnLsNode:
    data = raw["data"]
    return pt.OvnLsNode(
        id=raw["id"],
        kind=raw["kind"],
        key=pt.OvnLsKey(name=raw["key"]["name"]),
        data=pt.OvnLsData(
            interfaces=[pt.Interface(host=e["host"], iface=e["iface"]) for e in data["interfaces"]],
        ),
    )


def _hydrate_ovn_lrp(raw: dict) -> pt.OvnLrpNode:
    data = raw["data"]
    return pt.OvnLrpNode(
        id=raw["id"],
        kind=raw["kind"],
        key=pt.OvnLrpKey(ovnrouter=raw["key"]["ovnrouter"], side=pt.Side(raw["key"]["side"])),
        data=pt.OvnLrpData(
            l2Segment=data["l2Segment"],
            addresses=data["addresses"],
            mac=data["mac"],
            gatewayChassis=data.get("gatewayChassis"),
            ipv6RaConfigs=data.get("ipv6RaConfigs"),
        ),
    )


def _route_key_and_data(raw: dict) -> tuple[pt.RouteKey, pt.RouteData]:
    return (
        pt.RouteKey(ovnrouter=raw["key"]["ovnrouter"], prefix=raw["key"]["prefix"]),
        pt.RouteData(
            nexthop=raw["data"]["nexthop"],
            masq=raw["data"]["masq"],
            domain=raw["data"]["domain"],
        ),
    )


def _hydrate_ipv4_route(raw: dict) -> pt.Ipv4RouteNode:
    key, data = _route_key_and_data(raw)
    return pt.Ipv4RouteNode(id=raw["id"], kind=raw["kind"], key=key, data=data)


def _hydrate_ipv6_route(raw: dict) -> pt.Ipv6RouteNode:
    key, data = _route_key_and_data(raw)
    return pt.Ipv6RouteNode(id=raw["id"], kind=raw["kind"], key=key, data=data)


def _hydrate_kernel_router(raw: dict) -> pt.KernelRouterNode:
    data = raw["data"]
    side = raw["key"].get("side")
    routes = data.get("routes")
    ifaces = data.get("ifaces")
    return pt.KernelRouterNode(
        id=raw["id"],
        kind=raw["kind"],
        key=pt.KernelRouterKey(
            name=raw["key"]["name"],
            side=pt.Side(side) if side is not None else None,
        ),
        data=pt.KernelRouterData(
            host=data["host"],
            ipaddrs=data.get("ipaddrs"),
            routes=(
                [pt.Route(dst=r["dst"], via=r["via"]) for r in routes]
                if routes is not None
                else None
            ),
            ifaces=(
                [pt.Interface(host=e["host"], iface=e["iface"]) for e in ifaces]
                if ifaces is not None
                else None
            ),
            securityGroup=data.get("securityGroup"),
        ),
    )


def _hydrate_security_group(raw: dict) -> pt.SecurityGroupNode:
    data = raw["data"]
    return pt.SecurityGroupNode(
        id=raw["id"],
        kind=raw["kind"],
        key=pt.SecurityGroupKey(name=raw["key"]["name"]),
        data=pt.SecurityGroupData(
            rules=[pt.Rule(family=pt.Family(r["family"]), kind=r["kind"]) for r in data["rules"]],
        ),
    )


_HYDRATORS = {
    "infra.host": _hydrate_infra_host,
    "ovn.ls": _hydrate_ovn_ls,
    "ovn.lrp": _hydrate_ovn_lrp,
    "ipv4.route": _hydrate_ipv4_route,
    "ipv6.route": _hydrate_ipv6_route,
    "kernel.router": _hydrate_kernel_router,
    "security.group": _hydrate_security_group,
}


def hydrate_node(raw: dict) -> pt.Model:
    kind = raw.get("kind")
    hydrator = _HYDRATORS.get(kind)
    if hydrator is None:
        raise ValueError(
            f"unknown IR node kind {kind!r} (id={raw.get('id')!r}) — protocol/generated.py "
            "has no matching dataclass for it. Either this IR JSON is from a newer "
            "ovn-fabric than this deployer bundle knows about, or generate-pytypes "
            "(deno run -A src/cli.ts generate-pytypes) needs to be re-run for a "
            "genuinely new kind."
        )
    return hydrator(raw)


def hydrate_nodes(raw_nodes: list[dict]) -> list[pt.Model]:
    return [hydrate_node(n) for n in raw_nodes]
