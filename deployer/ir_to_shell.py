# deployer/ir_to_shell.py — the desired-state IR (ovn-fabric's src/
# ir.ts toIR(), emitted as JSON by `deno run src/cli.ts generate-ir`,
# then hydrated into protocol/generated.py's typed dataclasses by
# protocol/hydrate.py) in, shell script text out. Every node this
# module touches is one of protocol.generated.InfraHostNode/OvnLsNode/
# OvnLrpNode — never a raw dict. That's deliberate: the protocol (src/
# protocol.ts's ArkType schema) is the stabilizing factor for the
# TypeScript/Python boundary, and it only actually stabilizes anything
# if the Python side uses the typed nodes it produces — a dict access
# typo or a stale-shape assumption here would otherwise fail silently
# (KeyError deep inside script emission, or worse, a None slipping
# through) instead of loudly, at hydration, before any of this runs.
#
# Python, not TypeScript: every command this module prints is built by
# calling ladops.ovn/ladops.ovs's own *_argv() builders — the SAME
# functions a future live deployer would call to execute those actions
# for real — so ladops (not this module, not a TypeScript generator) is
# the one place that knows the exact ovn-nbctl/ovs-vsctl invocation
# syntax. This module only decides ORDER and WHICH nodes get WHICH
# action.
#
# Never executes anything itself — every line here is text, built from
# argv lists via shlex.join(), never passed to subprocess. Matches this
# project's rule everywhere else a generator exists (ADR 0001 §2,
# generate-ovn.ts): a generator produces a script to review/copy/run
# manually, it doesn't touch the live router.
#
# HYBRID split, not one script and not N independent per-host scripts:
# ovn.ls/ovn.lrp are cluster-wide facts (ADR 0003 — one shared NB/SB
# control plane), so their ovn-nbctl commands belong in ONE script, run
# ONCE from whichever chassis reaches the NB DB (typically central).
# infra.host facts (chassis registration) are inherently per-host —
# those go in that host's own script, run ON that host.
#
# action="create": plain adds (ladops.ovn.lr_add_argv/ls_add_argv/
# lrp_add_argv/lsp_add_router_argv — none of them take --may-exist). A
# caller that doesn't know whether an object already exists should diff
# against live state first (the reconciler's job), not paper over it
# with an idempotency flag baked into the generated script.
# action="delete": ladops' own --if-exists deletes (lr_del_argv/
# ls_del_argv — both cascade their own ports, see ladops/ovn.py) — a
# delete-everything pass naturally revisits objects an earlier pass
# already removed, and erroring on that would make delete non-
# idempotent for no benefit.
#
# Real-world binding for an ovn.ls's `interfaces` (host+iface pairs,
# from RouterEndpoint.ifaces — see src/ir.ts's collisionDomainToIR):
# bridge name (`br-<domain>`) and OVN network_name (`net-<domain>`) are
# both DERIVED from the domain's own name, not carried as separate IR
# fields — toIR() doesn't emit a stable logical-port-name/network-name
# for this yet, so this module fills that gap with the same
# deterministic-naming convention it already uses for lrp-*/lsp-*
# (router:<name>-<side>). Only "vlan"/"physical" InterfaceKinds are
# real-world-bindable this way; anything else (wireguard/zerotier/
# dummy — legacy Uplink-only kinds that don't belong on a collision
# domain's ifaces in the new model) is skipped with a comment, not
# silently dropped or hard-failed. `Interface.iface` itself stays a
# plain dict (protocol.ts's own InterfaceKind isn't modeled in the
# cross-language protocol — see that file's header) — this module reads
# it by string key, same as before.
#
# Scope, still: no NAT/routes/slaac/backdoor — no home for those in the
# IR at all yet, same boundary src/ir.ts documents.

from __future__ import annotations

import shlex

from ladops import linux_net as linux_net_ops
from ladops import ovn as ovn_ops
from ladops import ovs as ovs_ops
from protocol import generated as pt

Action = str  # "create" | "delete"

_BINDABLE_IFACE_KINDS = {"vlan", "physical"}


def _sh(argv: list[str]) -> str:
    return shlex.join(argv)


def _iface_real_name(iface: dict) -> str:
    if iface["kind"] == "vlan":
        return iface.get("ifaceName") or f"{iface['vlanParent']}.{iface['vlanId']}"
    return iface["name"]  # "physical"


_IFNAMSIZ_MAX = 15  # IFNAMSIZ (16) minus the NUL terminator


def _fnv1a_32(s: str) -> int:
    h = 0x811C9DC5
    for byte in s.encode("utf-8"):
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _bridge_name(domain: str) -> str:
    # Readable (br-<domain>) whenever it fits — only falls back to a
    # short deterministic hash when it doesn't. A real OVS bridge is a
    # real kernel netdev, capped at IFNAMSIZ = 15 usable characters —
    # confirmed live, 2026-08-10: "br-voda-modem-v2" (16 chars) failed
    # with ofproto "Invalid argument" on a real container while
    # "br-voda-avm-v2" (14 chars) right next to it succeeded. FNV-1a
    # (not a running counter/slot): this stays purely a deployer/
    # ir_to_shell.py concern — the IR itself carries only the domain's
    # real name, nothing about how a name might eventually get
    # shortened for a kernel object.
    candidate = f"br-{domain}"
    if len(candidate) <= _IFNAMSIZ_MAX:
        return candidate
    return f"br-{_fnv1a_32(domain):08x}"


def _network_name(domain: str) -> str:
    return f"net-{domain}"


# host -> [(domain, iface), ...] — every real (host, interface) pair
# across every ovn.ls node's `interfaces`, grouped by the host that
# actually owns the real hardware/VLAN (this drives that host's own
# script, not the cluster one — bridges/kernel VLANs are host-local).
def _host_bindings(nodes: list[pt.Model]) -> dict[str, list[tuple[str, dict]]]:
    by_host: dict[str, list[tuple[str, dict]]] = {}
    for node in nodes:
        if not isinstance(node, pt.OvnLsNode):
            continue
        domain = node.key.name
        for entry in node.data.interfaces:
            by_host.setdefault(entry.host, []).append((domain, entry.iface))
    return by_host


def _domains_with_bindable_interfaces(nodes: list[pt.Model]) -> set[str]:
    domains: set[str] = set()
    for bindings in _host_bindings(nodes).values():
        for domain, iface in bindings:
            if iface.get("kind") in _BINDABLE_IFACE_KINDS:
                domains.add(domain)
    return domains


def _find_central_host(nodes: list[pt.Model]) -> pt.InfraHostNode | None:
    return next(
        (
            n
            for n in nodes
            if isinstance(n, pt.InfraHostNode) and n.data.ovnRole == pt.OvnRole.central
        ),
        None,
    )


# A chassis is gateway-eligible only if some ovn.lrp actually pins a
# port to it (OVN's own `ovn-cms-options=enable-chassis-as-gw`
# requirement) — NOT unconditionally for every host: a real
# multi-chassis cluster has chassis that never host a gateway LRP.
# gatewayChassis itself is fully resolved upstream, in src/define.ts
# (RouterEndpoint's single-binding-host default, same boundary
# reasoning as mac/encapIp — see src/ir.ts) — this module only reads
# data.gatewayChassis, it never derives one.
def _gateway_eligible_hosts(nodes: list[pt.Model]) -> set[str]:
    hosts: set[str] = set()
    for node in nodes:
        if not isinstance(node, pt.OvnLrpNode):
            continue
        if node.data.gatewayChassis is not None:
            hosts.add(node.data.gatewayChassis)
    return hosts


def _group_router_ports(nodes: list[pt.Model]) -> dict[str, list[pt.OvnLrpNode]]:
    by_router: dict[str, list[pt.OvnLrpNode]] = {}
    for node in nodes:
        if not isinstance(node, pt.OvnLrpNode):
            continue
        by_router.setdefault(node.key.router, []).append(node)
    return by_router


# ── cluster script: ovn-nbctl only ───────────────────────────────────


def _emit_logical_switch(node: pt.OvnLsNode, action: Action) -> list[str]:
    name = node.key.name
    argv = ovn_ops.ls_add_argv(name) if action == "create" else ovn_ops.ls_del_argv(name)
    return [f"# --- logical switch: {name} ---", _sh(argv), ""]


# Create-only: on delete, ls_del_argv already cascades every LSP the
# switch owns (including this localnet one), same reasoning as
# _emit_router_delete not needing a separate lsp_del_argv call.
def _emit_localnet_lsp_create(name: str) -> list[str]:
    lsp = f"lsp-{name}-localnet"
    lines = [f"# --- localnet port: {name} ---"]
    for argv in ovn_ops.lsp_add_localnet_argv(name, lsp, _network_name(name)):
        lines.append(_sh(argv))
    lines.append("")
    return lines


RouteNode = pt.Ipv4RouteNode | pt.Ipv6RouteNode


def _group_routes_by_router(nodes: list[pt.Model]) -> dict[str, list[RouteNode]]:
    by_router: dict[str, list[RouteNode]] = {}
    for node in nodes:
        if not isinstance(node, (pt.Ipv4RouteNode, pt.Ipv6RouteNode)):
            continue
        by_router.setdefault(node.key.router, []).append(node)
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
def _emit_router_routes(router: str, routes: list[RouteNode]) -> list[str]:
    if not routes:
        return []
    by_domain: dict[str, list[RouteNode]] = {}
    for node in routes:
        by_domain.setdefault(node.data.domain, []).append(node)

    lines: list[str] = []
    for domain, domain_routes in by_domain.items():
        lines.append(f"# --- routes: {router} ({domain}) ---")
        for node in domain_routes:
            lines.append(_sh(ovn_ops.lr_route_add_argv(router, node.key.prefix, node.data.nexthop)))
    return lines


def _emit_router_create(
    router: str, ports: list[pt.OvnLrpNode], routes: list[RouteNode]
) -> list[str]:
    lines = [f"# --- router: {router} ---", _sh(ovn_ops.lr_add_argv(router))]
    for node in ports:
        side = node.key.side.value
        data = node.data
        lrp = f"lrp-{router}-{side}"
        lsp = f"lsp-{router}-{side}"
        # No "is mac None" check here anymore: OvnLrpData.mac is a
        # required (non-Optional) str field — a raw IR node missing it
        # already failed inside protocol/hydrate.py's hydrate_node(),
        # long before this function ever ran. That's the typed
        # protocol actually doing its job, not this module trusting
        # blindly: the failure moved earlier and got clearer, it didn't
        # disappear.
        lines.append(f"# --- router port: {lrp} ({data.l2Segment}) ---")
        lines.append(_sh(ovn_ops.lrp_add_argv(router, lrp, data.mac, data.addresses)))
        for argv in ovn_ops.lsp_add_router_argv(data.l2Segment, lsp, lrp):
            lines.append(_sh(argv))
        if data.gatewayChassis is not None:
            lines.append(_sh(ovn_ops.lrp_set_gateway_chassis_argv(lrp, data.gatewayChassis)))
            lines.append(_sh(ovn_ops.lrp_set_redirect_chassis_argv(lrp)))
        if data.ipv6RaConfigs is not None:
            for key, value in data.ipv6RaConfigs.items():
                lines.append(_sh(ovn_ops.lrp_set_ipv6_ra_config_argv(lrp, key, value)))
    lines.extend(_emit_router_routes(router, routes))
    lines.append("")
    return lines


def _emit_router_delete(router: str) -> list[str]:
    # lr_del_argv cascades — deletes the router's own LRPs AND its own
    # static routes (both are strongly referenced from Logical_Router,
    # same as ports), and (once the matching ls_del_argv runs too) the
    # router-type peer LSPs go with their owning switch. No separate
    # lrp_del_argv/lsp_del_argv/route-delete calls needed here — see
    # ladops/ovn.py's own doc comments.
    return [f"# --- router: {router} ---", _sh(ovn_ops.lr_del_argv(router)), ""]


def _emit_cluster_script(nodes: list[pt.Model], action: Action) -> str:
    lines = [
        "#!/bin/sh",
        f"# Cluster-wide OVN topology (ovn-nbctl only, action={action}) —",
        "# run ONCE, from whichever chassis reaches the shared NB DB",
        "# (typically the central one). Generated by",
        "# deployer/ir_to_shell.py. Deciding WHICH nodes actually need",
        "# this action (i.e. diffing against live state) is the",
        "# reconciler's job, not this script's — this is one blunt",
        f"# {action}-everything pass.",
        "set -eu",
        "",
    ]

    switches = [n for n in nodes if isinstance(n, pt.OvnLsNode)]
    router_groups = _group_router_ports(nodes)
    routes_by_router = _group_routes_by_router(nodes)

    if action == "create":
        bindable_domains = _domains_with_bindable_interfaces(nodes)
        for node in switches:
            lines.extend(_emit_logical_switch(node, action))
            if node.key.name in bindable_domains:
                lines.extend(_emit_localnet_lsp_create(node.key.name))
        for router, ports in router_groups.items():
            lines.extend(_emit_router_create(router, ports, routes_by_router.get(router, [])))
    else:
        for router in router_groups:
            lines.extend(_emit_router_delete(router))
        for node in switches:
            lines.extend(_emit_logical_switch(node, action))

    return "\n".join(lines)


# ── per-host scripts: kernel/OVS chassis registration ────────────────


# The real (host, interface) pairs this host owns: create the kernel
# VLAN sub-interface (if any), the bridge, add the interface as a port,
# and collect one ovn-bridge-mappings entry per binding — a single
# comma-joined external-ids write, not one per domain (ovs-vsctl's
# column is one string; a second `set` would overwrite the first, not
# append to it).
def _emit_iface_bindings_create(host_name: str, nodes: list[pt.Model]) -> list[str]:
    bindings = _host_bindings(nodes).get(host_name, [])
    lines: list[str] = []
    mappings: list[str] = []
    for domain, iface in bindings:
        if iface.get("kind") not in _BINDABLE_IFACE_KINDS:
            lines.append(
                f'# unsupported interface kind "{iface.get("kind")}" for domain '
                f"{domain} on {host_name} — skipped, see deployer/ir_to_shell.py"
            )
            continue
        real_name = _iface_real_name(iface)
        bridge = _bridge_name(domain)
        lines.append(f"# --- interface: {domain} on {host_name} ({real_name}) ---")
        if iface["kind"] == "vlan":
            lines.append(
                _sh(linux_net_ops.add_vlan_argv(iface["vlanParent"], real_name, iface["vlanId"]))
            )
            lines.append(_sh(linux_net_ops.set_link_up_argv(real_name)))
        lines.append(_sh(ovs_ops.add_br_argv(bridge)))
        lines.append(_sh(ovs_ops.set_bridge_fail_mode_argv(bridge)))
        lines.append(_sh(ovs_ops.add_port_argv(bridge, real_name)))
        mappings.append(f"{_network_name(domain)}:{bridge}")
    if mappings:
        lines.append(_sh(ovs_ops.set_external_id_argv("ovn-bridge-mappings", ",".join(mappings))))
    if lines:
        lines.append("")
    return lines


def _emit_iface_bindings_delete(host_name: str, nodes: list[pt.Model]) -> list[str]:
    bindings = _host_bindings(nodes).get(host_name, [])
    lines: list[str] = []
    any_bound = False
    for domain, iface in bindings:
        if iface.get("kind") not in _BINDABLE_IFACE_KINDS:
            continue
        any_bound = True
        real_name = _iface_real_name(iface)
        bridge = _bridge_name(domain)
        lines.append(f"# --- interface: {domain} on {host_name} ({real_name}) ---")
        # del-br cascades its own ports — no separate del-port call
        # needed (same reasoning as ladops.ovn.ls_del_argv).
        lines.append(_sh(ovs_ops.del_br_argv(bridge)))
        if iface["kind"] == "vlan":
            lines.append(_sh(linux_net_ops.delete_link_argv(real_name)))
    if any_bound:
        lines.append(_sh(ovs_ops.remove_external_id_argv("ovn-bridge-mappings")))
        lines.append("")
    return lines


def _emit_host_create(host_node: pt.InfraHostNode, nodes: list[pt.Model]) -> list[str]:
    host_name = host_node.key.host
    data = host_node.data
    lines: list[str] = _emit_iface_bindings_create(host_name, nodes)

    if data.ovnRole == pt.OvnRole.central:
        lines.append("# central: expose the shared NB/SB DBs to every other chassis")
        lines.append(_sh(ovn_ops.nb_set_connection_argv()))
        lines.append(_sh(ovn_ops.sb_set_connection_argv()))
        lines.append(
            _sh(ovs_ops.set_external_id_argv("ovn-remote", "unix:/var/run/ovn/ovnsb_db.sock"))
        )
    else:
        central = _find_central_host(nodes)
        if central is None:
            raise ValueError(
                f"host {host_name}: no central chassis declared in this network — "
                "a chassis needs one to point ovn-remote at"
            )
        if central.data.encapIp is None:
            raise ValueError(
                f"central host {central.key.host}: no encapIp/address.ipv4/"
                "address.ipv6 to build ovn-remote from"
            )
        lines.append(
            _sh(ovs_ops.set_external_id_argv("ovn-remote", f"tcp:{central.data.encapIp}:6642"))
        )

    if data.encapIp is not None:
        lines.append(_sh(ovs_ops.set_external_id_argv("ovn-encap-type", "geneve")))
        lines.append(_sh(ovs_ops.set_external_id_argv("ovn-encap-ip", data.encapIp)))

    if host_name in _gateway_eligible_hosts(nodes):
        lines.append(_sh(ovs_ops.set_external_id_argv("ovn-cms-options", "enable-chassis-as-gw")))

    return lines


def _emit_host_delete(host_node: pt.InfraHostNode, nodes: list[pt.Model]) -> list[str]:
    host_name = host_node.key.host
    data = host_node.data
    lines: list[str] = []

    if data.ovnRole == pt.OvnRole.central:
        lines.append("# central: stop exposing the shared NB/SB DBs")
        lines.append(_sh(ovn_ops.nb_del_connection_argv()))
        lines.append(_sh(ovn_ops.sb_del_connection_argv()))

    lines.append(_sh(ovs_ops.remove_external_id_argv("ovn-remote")))
    if data.encapIp is not None:
        lines.append(_sh(ovs_ops.remove_external_id_argv("ovn-encap-type")))
        lines.append(_sh(ovs_ops.remove_external_id_argv("ovn-encap-ip")))
    if host_name in _gateway_eligible_hosts(nodes):
        lines.append(_sh(ovs_ops.remove_external_id_argv("ovn-cms-options")))

    lines.append("")
    lines.extend(_emit_iface_bindings_delete(host_name, nodes))

    return lines


def _emit_host_script(host_node: pt.InfraHostNode, nodes: list[pt.Model], action: Action) -> str:
    host_name = host_node.key.host
    body = (
        _emit_host_create(host_node, nodes)
        if action == "create"
        else _emit_host_delete(host_node, nodes)
    )
    lines = [
        "#!/bin/sh",
        f"# Host-local OVS/chassis registration for {host_name}",
        f"# (action={action}) — run ON this host directly, not against",
        "# the cluster script's target. Generated by",
        "# deployer/ir_to_shell.py.",
        "set -eu",
        "",
        *body,
    ]
    return "\n".join(lines)


def build_scripts(nodes: list[pt.Model], action: Action) -> tuple[str, dict[str, str]]:
    if action not in ("create", "delete"):
        raise ValueError(f'action must be "create" or "delete", got {action!r}')

    host_scripts = {
        node.key.host: _emit_host_script(node, nodes, action)
        for node in nodes
        if isinstance(node, pt.InfraHostNode)
    }
    return _emit_cluster_script(nodes, action), host_scripts
