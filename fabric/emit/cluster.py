"""The cluster-wide OVN pass: logical switches, routers, routes, ACLs, gateway chassis."""

from __future__ import annotations

import dataclasses

from fabric.addresses import Addresses
from fabric.primitives.ovn import (
    OVNNbCtlAcl,
    OVNNbCtlLr,
    OVNNbCtlLrp,
    OVNNbCtlLs,
    OVNNbCtlLsp,
    OVNNbCtlNat,
    OVNNbCtlRoute,
)
from ladops import ovn as ovn_ops
from protocol import generated as pt

from .emitter import Emitter, OvnSettings, Ref
from .lookups import (
    _domain_name_by_id,
    _domains_with_bindable_interfaces,
    _group_router_ports,
    _host_name_by_id,
    _network_name,
)
from .security_groups import _security_groups_from_refs, _selectors

# ── cluster script: ovn-nbctl only ───────────────────────────────────


def _emit_logical_switch(node: pt.OvnLsNode, emit: Emitter) -> None:
    name = node.key.name
    switch_unit = emit.unit("logical switch", name)
    switch_unit.ovn(OVNNbCtlLs(name))
    switch_unit.blank()


# Create-only: on delete, ls_del_argv already cascades every LSP the
# switch owns (including this localnet one), same reasoning as
# _emit_router not needing a separate lsp_del_argv call.
def _emit_localnet_lsp_create(name: str, emit: Emitter) -> None:
    lsp = f"lsp-{name}-localnet"
    localnet_unit = emit.unit("localnet port", name)
    localnet_unit.ovn(
        OVNNbCtlLsp(
            switch=name,
            name=lsp,
            type="localnet",
            addresses=("unknown",),
            options=(("network_name", _network_name(name)),),
        )
    )
    localnet_unit.blank()


RouteNode = pt.Ipv4RouteNode | pt.Ipv6RouteNode


def _group_routes_by_router(nodes: list[pt.Model]) -> dict[str, list[RouteNode]]:
    by_router: dict[str, list[RouteNode]] = {}
    for node in nodes:
        if node.kind not in ("ipv4.route", "ipv6.route"):
            continue
        by_router.setdefault(node.key.ovnrouter, []).append(node)
    return by_router


# Grouped by `domain` within a router's own block — every route belongs
# to exactly one net.routingDomain() (src/ir.ts's computeRoutes AND
# computeInterconnectRoutes are both scoped to a domain's own
# participants now — "interconnect only exists if a routing domain
# exists", confirmed live 2026-08-12, after a blind peer-mesh across
# every router sharing the backbone leaked routes between unrelated
# sites/tenants) — so each domain gets its own labeled sub-block
# instead of one flat, unlabeled dump of lr-route-add lines. Purely
# mechanical otherwise — data.nexthop is already the FINAL resolved
# address by the time this runs (src/ir.ts, not this module); this only
# turns an already-computed fact into the command that applies it.
# `masq` is carried in the IR but not yet acted on here — NAT has no
# home in this pipeline yet (see src/ir.ts's own header comment).
def _emit_router_routes(
    router: str, router_var: str, routes: list[RouteNode], addrs: Addresses, emit: Emitter
) -> None:
    if not routes:
        return
    # A next hop is a reference to its carrier (ADR 0011): resolved here.
    hop = {id(n): addrs.ref(n.data.nexthop) for n in routes}
    # OVN's FIB is one table per router: the same prefix cannot be added
    # twice (ovn-nbctl rejects it; --ecmp is the only duplicate form, and
    # the IR has no ECMP intent). Several domains can legitimately learn
    # the same prefix via the same next hop — dedupe to ONE node using the
    # lowest metric. Same prefix with different next hops at the same
    # metric is ambiguous, so fail loudly instead of silently keeping one.
    by_prefix: dict[str, list[RouteNode]] = {}
    for node in routes:
        by_prefix.setdefault(node.key.prefix, []).append(node)
    best: list[RouteNode] = []
    for prefix, group in by_prefix.items():
        group.sort(key=lambda n: (n.data.metric, hop[id(n)]))
        lowest = group[0].data.metric
        nexthops = {hop[id(n)] for n in group if n.data.metric == lowest}
        if len(nexthops) > 1:
            raise ValueError(
                f"router {router}: prefix {prefix} has ECMP next hops "
                f"{sorted(nexthops)} at metric {lowest}; the IR has no ECMP "
                "intent, so refusing an ambiguous route"
            )
        best.append(group[0])
    by_domain: dict[str, list[RouteNode]] = {}
    for node in best:
        by_domain.setdefault(node.data.domain, []).append(node)
    for domain in sorted(by_domain):
        routes_unit = emit.unit("routes", f"{router} ({domain})")
        for node in sorted(by_domain[domain], key=lambda n: n.key.prefix):
            routes_unit.ovn(
                OVNNbCtlRoute(router, node.key.prefix, hop[id(node)]),
                parent=router_var,
            )


# router name -> the router's own ovn.lr node (src/ir.ts's routerToIR). A
# router with no ovn.lr node (an IR written before that node existed)
# simply gets no options set.
def _router_nodes(nodes: list[pt.Model]) -> dict[str, pt.OvnLrNode]:
    return {n.key.name: n for n in nodes if n.kind == "ovn.lr"}


def _ovn_value(value: object) -> str:
    """One value as OVN stores it in an smap column: `true`/`false` for a
    boolean, the plain text of anything else."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _ovn_settings(record: OvnSettings) -> list[tuple[str, str]]:
    """(key, value) for every field of an OVN settings record that is set,
    sorted by key. The record's field names ARE OVN's own keys (protocol.ts
    declares them that way), so a setting added to the protocol is applied
    without this module knowing its name."""
    return [
        (field.name, _ovn_value(getattr(record, field.name)))
        for field in sorted(dataclasses.fields(record), key=lambda f: f.name)
        if getattr(record, field.name) is not None
    ]


def _ovn_acl_match(rule: pt.FilterRule, port: pt.Port | None, addrs: Addresses) -> str:
    parts: list[str] = []
    family = "ip4" if rule.family == pt.Family.ipv4 else "ip6"
    sources = _selectors(rule.sources, addrs)
    excluded = _selectors(rule.excludeSources, addrs)
    destinations = _selectors(rule.destinations, addrs)
    if sources:
        parts.append(f"{family}.src == {{{', '.join(sources)}}}")
    if excluded:
        parts.append(f"{family}.src != {{{', '.join(excluded)}}}")
    if destinations:
        parts.append(f"{family}.dst == {{{', '.join(destinations)}}}")
    proto = rule.protocol.value if rule.protocol is not None else None
    if proto is not None:
        if proto == "icmp":
            parts.append("icmp4")
        elif proto == "icmpv6":
            parts.append("icmp6")
        else:
            parts.append(proto)
    if port is not None:
        if proto not in ("tcp", "udp"):
            raise ValueError("ACL ports require tcp/udp")
        if port.from_ == port.to:
            parts.append(f"{proto}.dst == {port.from_}")
        else:
            parts.append(f"{proto}.dst >= {port.from_}")
            parts.append(f"{proto}.dst <= {port.to}")
    return " && ".join(parts) if parts else "1"


def _emit_router_acl(
    router: str,
    router_var: str,
    ports: list[pt.OvnLrpNode],
    lr: pt.OvnLrNode | None,
    nodes: list[pt.Model],
    domain_names: dict[str, str],
    addrs: Addresses,
    emit: Emitter,
) -> None:
    if lr is None or not lr.data.securityGroups:
        return
    groups = _security_groups_from_refs(
        list(lr.data.securityGroups), nodes, pt.Render.ovn, f"router {router}"
    )
    if not any(
        rule.kind == "filter" or group.data.default is not None
        for group in groups
        for rule in group.data.rules
    ):
        return
    switches = sorted({domain_names[p.data.l2Segment] for p in ports})
    if not switches:
        return
    action_map = {
        pt.Action.allow: "allow",
        pt.Action.deny: "drop",
        pt.Action.reject: "reject",
    }
    direction_map = {
        pt.Direction.input: "to-lport",
        pt.Direction.output: "from-lport",
        pt.Direction.forward: "to-lport",
    }
    acl_unit = emit.unit("router ACL", router)
    # The router owns the ACL set on these switches/directions. Pruning an
    # older set (including a broad allow-related at a higher priority)
    # belongs to the LifecycleController's planned delete pass, not to the
    # create emitter.
    used_directions: set[str] = set()
    for group in groups:
        for rule in group.data.rules:
            if rule.kind == "filter":
                used_directions.add(direction_map[rule.direction])
        if group.data.default is not None:
            used_directions.update(("to-lport", "from-lport"))
    for switch in switches:
        for direction in sorted(used_directions):
            acl_unit.ovn(
                OVNNbCtlAcl(
                    switch,
                    direction,
                    30000,
                    "ct.est == 1 || ct.rel == 1",
                    "allow",
                ),
                parent=router_var,
            )
    seen: set[tuple[str, str, int, str, str]] = set()

    def add_acl(switch: str, direction: str, priority: int, match: str, action: str) -> None:
        key = (switch, direction, priority, match, action)
        if key in seen:
            return
        seen.add(key)
        acl_unit.ovn(
            OVNNbCtlAcl(switch, direction, priority, match, action),
            parent=router_var,
        )

    for group in groups:
        filters = [r for r in group.data.rules if r.kind == "filter"]
        for switch in switches:
            for rule in filters:
                direction = direction_map[rule.direction]
                priority = rule.priority if rule.priority is not None else 100
                ports_iter: list[pt.Port | None] = list(rule.ports) if rule.ports else [None]
                for port in ports_iter:
                    match = _ovn_acl_match(rule, port, addrs)
                    action = action_map[rule.action]
                    if rule.action == pt.Action.allow:
                        action = "allow-related"
                    add_acl(switch, direction, priority, match, action)
            default = group.data.default
            if default is not None:
                for attr, direction in (
                    ("input", "to-lport"),
                    ("output", "from-lport"),
                    ("forward", "to-lport"),
                ):
                    default_action = getattr(default, attr, None)
                    if default_action is None:
                        continue
                    add_acl(
                        switch,
                        direction,
                        1,
                        "1",
                        {
                            "allow": "allow",
                            "deny": "drop",
                            "reject": "reject",
                        }[getattr(default_action, "value", default_action)],
                    )


def _emit_router(
    router: str,
    ports: list[pt.OvnLrpNode],
    routes: list[RouteNode],
    chassis_refs: dict[str, Ref],
    domain_names: dict[str, str],
    lr: pt.OvnLrNode | None,
    nodes: list[pt.Model],
    addrs: Addresses,
    emit: Emitter,
) -> None:
    external_ids: list[tuple[str, str]] = []
    if lr is not None and lr.data.securityGroups:
        external_ids.append(
            (
                "ovn-fabric.securityGroups",
                ",".join(ref.name for ref in lr.data.securityGroups),
            )
        )
    router_var = emit.local(router)
    router_unit = emit.unit("router", router)
    router_unit.ovn(
        OVNNbCtlLr(
            router,
            options=tuple(_ovn_settings(lr.data.options)) if lr is not None else (),
            external_ids=tuple(external_ids),
        ),
        bind=router_var,
    )
    # Sorted by the port's own name so the emitted block is stable
    # regardless of the IR's input order (the IR out of src/ir.ts is
    # already sorted, but the deployer must not depend on that).
    for node in sorted(ports, key=lambda n: n.key.name):
        data = node.data
        # The port name is the endpoint's own identity
        # (EndpointBase.name, src/ir.ts's routerEndpointToIR) — carried in
        # the KEY, like `side` was, never duplicated into data. The peer
        # LSP name is derived from it (the router-type LSP that binds the
        # switch's port to this LRP).
        lrp = node.key.name
        lsp = f"lsp-{lrp.removeprefix('lrp-')}"
        # data.l2Segment/gatewayChassis are the referenced ovn.ls/
        # infra.host node's own id (`ls:<name>`/`host:<name>` —
        # 2026-08-12, src/ir.ts), resolved back to the real name every
        # ovn-nbctl argument needs via domain_names/host_names.
        domain = domain_names[data.l2Segment]
        # The MAC: an explicit one from the IR, else the fold of the
        # resolved IPv4 (ADR 0011 §5).
        port_unit = router_unit.unit("router port", f"{lrp} ({domain})")
        gateway_chassis = (
            chassis_refs[data.gatewayChassis] if data.gatewayChassis is not None else None
        )
        lrp_var = port_unit.local(lrp)
        port_unit.ovn(
            OVNNbCtlLrp(
                router,
                lrp,
                data.mac or addrs.mac(data.addresses, lrp),
                tuple(addrs.list(data.addresses)),
                gateway_chassis=gateway_chassis,
                ipv6_ra_configs=(
                    tuple(_ovn_settings(data.ipv6RaConfigs))
                    if data.ipv6RaConfigs is not None
                    else ()
                ),
            ),
            bind=lrp_var,
            parent=router_var,
        )
        port_unit.ovn(
            OVNNbCtlLsp(
                switch=domain,
                name=lsp,
                type="router",
                addresses=("router",),
                options=(("router-port", lrp),),
            ),
            parent=lrp_var,
        )
    # OVN SNAT lives in the security.group rules referenced by the LR.
    # Replacing an existing set per external IP (lr-nat-add has no
    # --may-exist) belongs to the LifecycleController's planned delete
    # pass; this emitter only adds the desired rows.
    if lr is not None and lr.data.securityGroups:
        groups = _security_groups_from_refs(
            list(lr.data.securityGroups), nodes, pt.Render.ovn, f"router {router}"
        )
        by_external: dict[str, list[pt.SecurityGroupRule1]] = {}
        for group in groups:
            for rule in group.data.rules:
                if rule.kind != "snat":
                    # filter rules in the same group are handled by
                    # _emit_router_acl; NAT only consumes snat.
                    continue
                by_external.setdefault(addrs.ref(rule.externalIp), []).append(rule)
        if by_external:
            nat_unit = router_unit.unit("router NAT", router)
            for external in sorted(by_external):
                for rule in sorted(by_external[external], key=lambda r: r.logicalIps):
                    for logical in rule.logicalIps:
                        nat_unit.ovn(
                            OVNNbCtlNat(
                                router, "snat", external, logical, gateway_port=rule.gatewayPort
                            ),
                            parent=router_var,
                        )
    _emit_router_acl(router, router_var, ports, lr, nodes, domain_names, addrs, router_unit)
    _emit_router_routes(router, router_var, routes, addrs, router_unit)
    router_unit.blank()


def _chassis_var(host_name: str) -> str:
    """A safe runtime-variable name for one host's chassis id (`Chassis.name`,
    i.e. the OVS `system-id`) — see `_emit_gateway_chassis_refs`."""
    safe = "".join(c if c.isalnum() else "_" for c in host_name)
    return f"OVN_CHASSIS_{safe}"


def _emit_gateway_chassis_refs(
    nodes: list[pt.Model], host_names: dict[str, str], emit: Emitter
) -> dict[str, Ref]:
    """Resolve every host a router port pins as gateway chassis to its
    RUNTIME `Chassis.name` (the OVS `system-id`), one captured variable
    per distinct host, and return host-id -> `Ref`.

    `lrp-set-gateway-chassis` matches `Chassis.name` — never the host
    name or the reported hostname — and that name is a per-host generated
    UUID, so it cannot be baked into the generated script (confirmed live
    2026-09-19: every pin naming a host name silently created no SB
    `Gateway_Chassis` row, leaving the chassis with zero local datapaths
    and the whole localnet fabric with no flows). Each host registers
    `external_ids:hostname=<IR host name>` (see _emit_chassis_registration), so the SB
    lookup by that hostname is what bridges the IR's stable host name to
    the runtime system-id. The `test -n` guard makes an unregistered
    chassis fail loudly instead of pinning an empty name."""
    host_ids = sorted(
        {
            n.data.gatewayChassis
            for n in nodes
            if n.kind == "ovn.lrp" and n.data.gatewayChassis is not None
        }
    )
    refs: dict[str, Ref] = {}
    for host_id in host_ids:
        host_name = host_names[host_id]
        var = _chassis_var(host_name)
        chassis_unit = emit.unit("chassis id", f"{host_name} (resolved on this host)")
        chassis_unit.wait_capture(var, ovn_ops.sb_chassis_id_by_hostname_argv(host_name))
        refs[host_id] = Ref(var)
    if refs:
        emit.blank()
    return refs


def _emit_cluster_body(nodes: list[pt.Model], addrs: Addresses, emit: Emitter) -> None:
    """The cluster declaration. DELETE is the same declaration rewound
    by the LifecycleController."""
    # Explicit sorts make the emitted script byte-stable regardless of
    # the IR's input order (routers, switches; ports/routes are sorted
    # where they are emitted).
    switches = sorted((n for n in nodes if n.kind == "ovn.ls"), key=lambda n: n.key.name)
    router_groups = _group_router_ports(nodes)
    routes_by_router = _group_routes_by_router(nodes)
    router_nodes = _router_nodes(nodes)
    host_names = _host_name_by_id(nodes)
    domain_names = _domain_name_by_id(nodes)
    routers = sorted(router_groups)

    bindable_domains = _domains_with_bindable_interfaces(nodes)
    for node in switches:
        _emit_logical_switch(node, emit)
        if node.key.name in bindable_domains:
            _emit_localnet_lsp_create(node.key.name, emit)
    # Runtime chassis-id lookups must run before any router pins a
    # gateway chassis to them.
    chassis_refs = _emit_gateway_chassis_refs(nodes, host_names, emit)
    for router in routers:
        _emit_router(
            router,
            router_groups[router],
            routes_by_router.get(router, []),
            chassis_refs,
            domain_names,
            router_nodes.get(router),
            nodes,
            addrs,
            emit,
        )
