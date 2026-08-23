# deployer/ir_common.py — the shared emitters behind BOTH generator
# front-ends (deployer/ir_to_shell.py's `build_scripts` and deployer/
# ir_to_python.py's `generate_python_deployer`, split 2026-08-19): the
# desired-state IR (ovn-fabric's src/ir.ts toIR(), emitted as JSON by
# `deno run src/cli.ts generate-ir`, then hydrated into protocol/
# generated.py's typed dataclasses by protocol/hydrate.py) in,
# RENDERER-AGNOSTIC OPERATIONS out (2026-08-23). Every _emit_* function
# builds through an `Emitter` sink (see below) — the shell front-end
# renders literal shell script from it, the Python front-end renders
# Python source that performs the same operations at runtime with its
# own logging/error handling. Every node this module touches is one of
# protocol.generated.InfraHostNode/OvnLsNode/OvnLrpNode — never a raw
# dict. That's deliberate: the protocol (src/protocol.ts's ArkType
# schema) is the stabilizing factor for the TypeScript/Python boundary,
# and it only actually stabilizes anything if the Python side uses the
# typed nodes it produces — a dict access typo or a stale-shape
# assumption here would otherwise fail silently (KeyError deep inside
# script emission, or worse, a None slipping through) instead of loudly,
# at hydration, before any of this runs.
#
# Python, not TypeScript: every command this module emits is built by
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
# One flag-taking emitter per object, create/delete branches adjacent
# (2026-08-19 — the "write_interface(action)" schema): adding a create
# command forces you to look at its delete right next to it.
#
# Real-world binding for an ovn.ls's `interfaces` (host+iface pairs,
# from RouterEndpoint.ifaces — see src/ir.ts's collisionDomainToIR):
# bridge name (`br-<domain>`) and OVN network_name (`net-<domain>`) are
# both DERIVED from the domain's own name, not carried as separate IR
# fields — toIR() doesn't emit a stable logical-port-name/network-name
# for this yet, so this module fills that gap with the same
# deterministic-naming convention it already uses for lrp-*/lsp-*
# (router:<name>-<side>). Only "vlan"/"physical"/"veth" InterfaceKinds
# are real-world-bindable this way; anything else (wireguard/zerotier/
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
from ladops import netns as netns_ops
from ladops import ovn as ovn_ops
from ladops import ovs as ovs_ops
from protocol import generated as pt

Action = str  # "create" | "delete"

_BINDABLE_IFACE_KINDS = {"vlan", "physical", "veth"}


def _sh(argv: list[str]) -> str:
    return shlex.join(argv)


# ── the emitter interface ─────────────────────────────────────────────
# Every _emit_* function below builds its output through an `Emitter`
# sink, NOT by returning shell lines. The two front-ends each provide
# their own Emitter implementation (deployer/ir_to_shell.py renders
# literal shell script, deployer/ir_to_python.py renders Python source
# that performs the same operations at runtime with logging/error
# handling) — so the shared emitters never render, they only APPEND
# operations. That's what gives the Python deployer real control over
# its own execution (2026-08-23): a `sh()` command becomes a
# run_cmd(...) call, an `append()` file-write becomes a write_file(...)
# call, each with the Python runtime's error/verbose policy — instead of
# the old design where the whole shell block was embedded verbatim and
# the runtime had to re-parse shell syntax (heredocs included) itself.
#
# `append(path, content)` semantics: the file ends up holding `content`
# plus a single trailing newline (the shell backend renders this as a
# `cat > path << 'EOF'` heredoc, whose body always ends in a newline;
# the Python backend's write_file() writes content + "\n" to match).
class Emitter:
    def __init__(self, action: Action) -> None:
        self.action = action
        self.lines: list[str] = []

    def raw(self, text: str) -> None:
        """Append a backend-specific raw line verbatim — used by the
        shell backend for the `#!/bin/sh`/`set -eu` envelope; the Python
        backend has no envelope and never calls this."""
        self.lines.append(text)

    def sh(self, argv: list[str]) -> None:
        raise NotImplementedError

    def append(self, path: str, content: str) -> None:
        raise NotImplementedError

    def comment(self, text: str) -> None:
        self.lines.append(text)

    def blank(self) -> None:
        self.lines.append("")


def _iface_real_name(iface: dict) -> str:
    if iface["kind"] == "vlan":
        return iface.get("ifaceName") or f"{iface['vlanParent']}.{iface['vlanId']}"
    if iface["kind"] == "veth":
        # The ROOT-side leg — the device the transit domain's bridge
        # attaches to (the netns-side leg, `ifaceName`, is created/moved
        # by _emit_kernel_router instead).
        return iface["peerName"]
    return iface["name"]  # "physical"


def _network_name(domain: str) -> str:
    return f"net-{domain}"


# infra.host node id (`host:<name>`) -> its real, bare host name — every
# cross-node reference in the IR (OvnLrpData.gatewayChassis,
# KernelRouterData.host) carries the referenced node's own id, not the
# bare name (2026-08-12, src/ir.ts), so anything that needs the REAL
# name for an actual ovn-nbctl/ovs-vsctl/ip argument resolves it back
# through this map first — same "generator computes the fact, this
# module only ever reads it" boundary as mac/routes, just one lookup
# removed from being a plain attribute access.
def _host_name_by_id(nodes: list[pt.Model]) -> dict[str, str]:
    return {n.id: n.key.host for n in nodes if n.kind == "infra.host"}


# ovn.ls node id (`ls:<name>`) -> its real logical switch name — same
# reasoning as _host_name_by_id above, for OvnLrpData.l2Segment.
def _domain_name_by_id(nodes: list[pt.Model]) -> dict[str, str]:
    return {n.id: n.key.name for n in nodes if n.kind == "ovn.ls"}


# host -> [(domain, iface), ...] — every real (host, interface) pair
# across every ovn.ls node's `interfaces`, grouped by the host that
# actually owns the real hardware/VLAN (this drives that host's own
# script, not the cluster one — bridges/kernel VLANs are host-local).
# `iface` carries its own `shortName` (src/ir.ts's own
# collisionDomainToIR, moved there from OvnLsData 2026-08-12) — every
# entry for the same domain resolves to the same value, so reading it
# off whichever entry is at hand is enough, no separate per-domain map.
def _host_bindings(nodes: list[pt.Model]) -> dict[str, list[tuple[str, dict]]]:
    by_host: dict[str, list[tuple[str, dict]]] = {}
    for node in nodes:
        if node.kind != "ovn.ls":
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
        (n for n in nodes if n.kind == "infra.host" and n.data.ovnRole == pt.OvnRole.central),
        None,
    )


# A chassis is gateway-eligible only if some ovn.lrp actually pins a
# port to it (OVN's own `ovn-cms-options=enable-chassis-as-gw`
# requirement) — NOT unconditionally for every host: a real
# multi-chassis cluster has chassis that never host a gateway LRP.
# gatewayChassis itself is fully resolved upstream, in src/define.ts
# (RouterEndpoint's single-binding-host default, same boundary
# reasoning as mac/encapIp — see src/ir.ts) — this module only reads
# data.gatewayChassis, it never derives one. Returns real host names
# (resolved via _host_name_by_id), matching every OTHER host-name set
# this module builds.
def _gateway_eligible_hosts(nodes: list[pt.Model]) -> set[str]:
    host_names = _host_name_by_id(nodes)
    hosts: set[str] = set()
    for node in nodes:
        if node.kind != "ovn.lrp":
            continue
        if node.data.gatewayChassis is not None:
            hosts.add(host_names[node.data.gatewayChassis])
    return hosts


def _group_router_ports(nodes: list[pt.Model]) -> dict[str, list[pt.OvnLrpNode]]:
    by_router: dict[str, list[pt.OvnLrpNode]] = {}
    for node in nodes:
        if node.kind != "ovn.lrp":
            continue
        by_router.setdefault(node.key.ovnrouter, []).append(node)
    return by_router


# ── cluster script: ovn-nbctl only ───────────────────────────────────


def _emit_logical_switch(node: pt.OvnLsNode, emit: Emitter) -> None:
    name = node.key.name
    argv = ovn_ops.ls_add_argv(name) if emit.action == "create" else ovn_ops.ls_del_argv(name)
    emit.comment(f"# --- logical switch: {name} ---")
    emit.sh(argv)
    emit.blank()


# Create-only: on delete, ls_del_argv already cascades every LSP the
# switch owns (including this localnet one), same reasoning as
# _emit_router not needing a separate lsp_del_argv call.
def _emit_localnet_lsp_create(name: str, emit: Emitter) -> None:
    lsp = f"lsp-{name}-localnet"
    emit.comment(f"# --- localnet port: {name} ---")
    for argv in ovn_ops.lsp_add_localnet_argv(name, lsp, _network_name(name)):
        emit.sh(argv)
    emit.blank()


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
def _emit_router_routes(router: str, routes: list[RouteNode], emit: Emitter) -> None:
    if not routes:
        return
    by_domain: dict[str, list[RouteNode]] = {}
    for node in routes:
        by_domain.setdefault(node.data.domain, []).append(node)

    for domain, domain_routes in by_domain.items():
        emit.comment(f"# --- routes: {router} ({domain}) ---")
        for node in domain_routes:
            emit.sh(ovn_ops.lr_route_add_argv(router, node.key.prefix, node.data.nexthop))


# One flag-taking function per object, create/delete branches adjacent
# (2026-08-19 — the "write_interface(action)" schema): adding a create
# command forces you to look at its delete right next to it.
def _emit_router(
    router: str,
    ports: list[pt.OvnLrpNode],
    routes: list[RouteNode],
    host_names: dict[str, str],
    domain_names: dict[str, str],
    emit: Emitter,
) -> None:
    if emit.action == "delete":
        # lr_del_argv cascades — deletes the router's own LRPs AND its
        # own static routes (both are strongly referenced from
        # Logical_Router, same as ports), and (once the matching
        # ls_del_argv runs too) the router-type peer LSPs go with their
        # owning switch. No separate lrp_del_argv/lsp_del_argv/route-
        # delete calls needed here — see ladops/ovn.py's own doc
        # comments.
        emit.comment(f"# --- router: {router} ---")
        emit.sh(ovn_ops.lr_del_argv(router))
        emit.blank()
        return

    emit.comment(f"# --- router: {router} ---")
    emit.sh(ovn_ops.lr_add_argv(router))
    for node in ports:
        side = node.key.side.value
        data = node.data
        lrp = f"lrp-{router}-{side}"
        lsp = f"lsp-{router}-{side}"
        # data.l2Segment/gatewayChassis are the referenced ovn.ls/
        # infra.host node's own id (`ls:<name>`/`host:<name>` —
        # 2026-08-12, src/ir.ts), resolved back to the real name every
        # ovn-nbctl argument needs via domain_names/host_names.
        domain = domain_names[data.l2Segment]
        # No "is mac None" check here anymore: OvnLrpData.mac is a
        # required (non-Optional) str field — a raw IR node missing it
        # already failed inside protocol/hydrate.py's hydrate_node(),
        # long before this function ever ran. That's the typed
        # protocol actually doing its job, not this module trusting
        # blindly: the failure moved earlier and got clearer, it didn't
        # disappear.
        emit.comment(f"# --- router port: {lrp} ({domain}) ---")
        emit.sh(ovn_ops.lrp_add_argv(router, lrp, data.mac, data.addresses))
        for argv in ovn_ops.lsp_add_router_argv(domain, lsp, lrp):
            emit.sh(argv)
        if data.gatewayChassis is not None:
            chassis = host_names[data.gatewayChassis]
            emit.sh(ovn_ops.lrp_set_gateway_chassis_argv(lrp, chassis))
            emit.sh(ovn_ops.lrp_set_redirect_chassis_argv(lrp))
        if data.ipv6RaConfigs is not None:
            for key, value in data.ipv6RaConfigs.items():
                emit.sh(ovn_ops.lrp_set_ipv6_ra_config_argv(lrp, key, value))
    _emit_router_routes(router, routes, emit)
    emit.blank()


def _emit_cluster_body(nodes: list[pt.Model], emit: Emitter) -> None:
    action = emit.action
    switches = [n for n in nodes if n.kind == "ovn.ls"]
    router_groups = _group_router_ports(nodes)
    routes_by_router = _group_routes_by_router(nodes)
    # Resolved for both actions — _emit_router's delete branch simply
    # doesn't read them (2026-08-19, flag schema).
    host_names = _host_name_by_id(nodes)
    domain_names = _domain_name_by_id(nodes)

    if action == "create":
        bindable_domains = _domains_with_bindable_interfaces(nodes)
        for node in switches:
            _emit_logical_switch(node, emit)
            if node.key.name in bindable_domains:
                _emit_localnet_lsp_create(node.key.name, emit)
        for router, ports in router_groups.items():
            _emit_router(
                router,
                ports,
                routes_by_router.get(router, []),
                host_names,
                domain_names,
                emit,
            )
    else:
        for router, ports in router_groups.items():
            _emit_router(
                router,
                ports,
                routes_by_router.get(router, []),
                host_names,
                domain_names,
                emit,
            )
        for node in switches:
            _emit_logical_switch(node, emit)


# ── per-host scripts: kernel/OVS chassis registration ────────────────


def _netns_name(name: str) -> str:
    return f"ns-{name}"


def _dummy_name(side: pt.Side) -> str:
    return f"dummy-{side.value}"


# The router-level kernel.router node (`key.side` absent — src/ir.ts's
# kernelRouterToIR) — one per KernelRouter, carrying just the host
# reference. `host_id` (`host:<name>`, the owning infra.host node's own
# id, not the bare name — 2026-08-12, src/ir.ts) is what data.host
# actually carries: filtering on it directly here needs no separate
# id->name lookup, unlike gatewayChassis/l2Segment above.
def _kernel_router_owners(host_id: str, nodes: list[pt.Model]) -> list[pt.KernelRouterNode]:
    return [
        n
        for n in nodes
        if n.kind == "kernel.router" and n.key.side is None and n.data.host == host_id
    ]


# The two per-side kernel.router nodes (`key.side` present) for one
# KernelRouter, by its own `name` — same shape convention as ovn.lrp's
# own per-side nodes.
def _kernel_router_sides(name: str, nodes: list[pt.Model]) -> list[pt.KernelRouterNode]:
    return [
        n
        for n in nodes
        if n.kind == "kernel.router" and n.key.side is not None and n.key.name == name
    ]


# A `security.group` node (implementation-abstract — src/ir.ts's
# securityGroupToIR), by name. Found by the name a kernel.router side
# node carries in `data.securityGroup`: the side node is the ATTACHMENT
# point, the group node carries the rules.
def _security_group_by_name(name: str, nodes: list[pt.Model]) -> pt.SecurityGroupNode | None:
    return next(
        (n for n in nodes if n.kind == "security.group" and n.key.name == name),
        None,
    )


# A security group's rules, applied INSIDE a kernel router's netns on a
# specific real interface (the `right` side's WAN iface, by
# construction). This is the services emit function for `security.group`
# (see src/ir.ts's securityGroupToIR): today it only knows how to turn
# the two masq rule shapes into iptables/ip6tables commands, and
# anything else is skipped loudly with a comment rather than silently
# dropped — future rule kinds (docker containers, wireguard, ...) extend
# the same if-chain without touching anything else.
#
# Plain `-A` add, same as every other create emitter in this module:
# iptables has no --may-exist, but a `-C ... || -A` conditional must NOT
# be emitted here — the generated Python deployer executes each line via
# shlex.split() as argv, so `2>/dev/null`/`||` would be handed to
# iptables as literal arguments (confirmed live, 2026-08-23: "Bad
# argument `2>/dev/null'"). Diffing against live state is the
# reconciler's job, not a shell conditional's.
#
# No delete branch: a `security.group` is pure netfilter state INSIDE
# the netns, and `ip netns delete` (the kernel router's own delete path
# in _emit_kernel_router) destroys the whole netns, its rules included —
# nothing left to tear down separately.
def _emit_security_group_rules(
    group: pt.SecurityGroupNode, netns: str, dev: str, emit: Emitter
) -> None:
    emit.comment(f"# --- security group: {group.key.name} in {netns} on {dev} ---")
    for rule in group.data.rules:
        if rule.kind != "masq":
            emit.comment(
                f'# unsupported security group rule kind "{rule.kind}" for '
                f"{group.key.name} on {dev} — skipped, see deployer/ir_to_shell.py"
            )
            continue
        iptables = "iptables" if rule.family == pt.Family.ipv4 else "ip6tables"
        argv = [iptables, "-t", "nat", "-A", "POSTROUTING", "-o", dev, "-j", "MASQUERADE"]
        emit.sh(netns_ops.netns_exec_argv(argv, netns))


# A kernel router's apps (KernelApp, types.ts — resolved from the
# `kernel.app.*` services by buildKernelRouterEndpoint, define.ts) run
# INSIDE the netns on the side's real interface, supervised by a real
# systemd unit (Restart=always) — the deployer INSTALLS that unit and
# starts it on create, and stops/disables/removes it on delete (the
# design intent from generate-netns.ts, 2026-08-23: an app daemon holds
# its netns alive past `ip netns delete` and keeps its lease, so the
# delete pass must bring it down explicitly BEFORE the netns teardown).
#
# The unit file is written via a `cat > ... << 'EOF'` heredoc — the ONE
# construct in the block the shell generator renders as-is and the
# generated Python deployer handles natively (its runtime recognizes the
# `cat > PATH << 'DELIM'` line and writes the file directly, since it
# executes commands via shlex.split and cannot do shell redirection).
#
# The client is forced to the FOREGROUND inside the unit (`-d` for
# dhclient, `-B` for dhcpcd) so systemd's Type=simple actually has a
# process to supervise — a daemonizing client would exit the unit's main
# process immediately and Restart=always would respawn it in a loop.
# Unknown kinds/styles are skipped loudly with a comment, never silently
# dropped — same philosophy as the security-group rules.
def _kernel_app_unit_name(router: str, kind: str) -> str:
    return f"ovn-kernel-{router}-{kind}.service"


def _emit_kernel_app_rules(
    apps: list[pt.App], router: str, netns: str, dev: str, emit: Emitter
) -> None:
    for app in apps:
        if app.kind != "dhcp-client":
            emit.comment(
                f'# unsupported kernel app kind "{app.kind}" on {dev} in {netns} — '
                "skipped, see deployer/ir_to_shell.py"
            )
            continue
        if app.style == pt.Style.dhclient:
            client_argv = ["/usr/sbin/dhclient", "-d", dev]
            client = "dhclient"
        elif app.style == pt.Style.dhcpcd:
            client_argv = ["/usr/sbin/dhcpcd", "-B", dev]
            client = "dhcpcd"
        else:
            emit.comment(
                f'# unsupported dhcp-client style "{app.style}" on {dev} in {netns} — '
                "skipped, see deployer/ir_to_shell.py"
            )
            continue
        unit = _kernel_app_unit_name(router, app.kind)
        unit_path = f"/etc/systemd/system/{unit}"
        exec_start = _sh(["/usr/bin/ip", "netns", "exec", netns, *client_argv])
        emit.comment(
            f"# --- kernel app: {app.kind} ({app.style.value}) in {netns} on {dev} "
            f"(systemd unit {unit}) ---"
        )
        if emit.action == "delete":
            emit.sh(["systemctl", "disable", "--now", unit])
            emit.sh(["rm", "-f", unit_path])
            emit.sh(["systemctl", "daemon-reload"])
            continue
        emit.append(
            unit_path,
            "\n".join(
                [
                    "[Unit]",
                    f"Description=ovn-fabric kernel app {app.kind} ({client}) for "
                    f"{router} in {netns} on {dev}",
                    "After=network-pre.target",
                    "Wants=network-pre.target",
                    "",
                    "[Service]",
                    "Type=simple",
                    f"ExecStart={exec_start}",
                    "Restart=always",
                    "RestartSec=5",
                    "",
                    "[Install]",
                    "WantedBy=multi-user.target",
                ]
            ),
        )
        emit.sh(["systemctl", "daemon-reload"])
        emit.sh(["systemctl", "enable", "--now", unit])


# One real netns per KernelRouter (types.ts) bound to this host, then —
# per side — a real interface when the side declares `ifaces`
# (KernelRouterSide.ifaces, types.ts — populated for `left` by the
# implicit transit veth and for `right` by buildKernelRouterEndpoint,
# define.ts, 2026-08-18): CREATE creates the kernel VLAN sub-interface
# or the transit veth pair (if any) in the root namespace, moves the
# netns-side device into the netns, and binds that side's
# already-resolved addresses/routes to it. A side with no `ifaces` keeps
# the dummy-device stand-in (the pre-2026-08-18 behavior). DELETE removes
# the moved-in vlans from INSIDE the netns first (an explicit `ip link
# del` from inside cleans the parent's 8021q registry, while `ip netns
# delete` alone leaks a stale entry — hit live 2026-08-19 on an LXC),
# then tears down the netns (which destroys everything still inside it —
# the veth's netns-side leg), then removes the transit veth's ROOT-side
# leg, which lives in the global namespace (2026-08-19).
#
# `ip link add ... type vlan/type veth/type dummy` runs OUTSIDE the netns
# (a
# device is only visible to `ip link set <dev> netns <netns>` from
# whichever namespace it's CURRENTLY in — same reasoning ladops.linux_
# net.add_if_to_netns already documents, and the same create-in-global/
# move-in sequence this module already uses for a real host's VLAN
# sub-interfaces), THEN moved into the netns with that same `ip link
# set ... netns` call — confirmed live, 2026-08-12. Only after the move
# does anything run via `ip netns exec`: bring the link up, THEN assign
# addresses/routes to it — up before addr/route, not after (a route
# through a device that isn't up yet fails; an address on one is
# pointless until it is).
def _emit_kernel_router(host_id: str, nodes: list[pt.Model], emit: Emitter) -> None:
    before = len(emit.lines)
    action = emit.action
    for owner in _kernel_router_owners(host_id, nodes):
        netns = _netns_name(owner.key.name)
        emit.comment(f"# --- kernel netns: {netns} ({owner.key.name}) ---")
        if action == "delete":
            # Delete the moved-in vlans from INSIDE the netns FIRST —
            # `ip netns delete` alone destroys the netdev but leaks a
            # stale 8021q registration on the parent (hit live 2026-08-19
            # on an LXC: recreating the same vlan afterwards failed with
            # "VLAN device already exists" until a host reboot), while an
            # explicit `ip link del` from inside the netns goes through
            # the normal unregister path and cleans the registry up.
            for side_node in _kernel_router_sides(owner.key.name, nodes):
                iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
                # Stop the side's apps BEFORE `ip netns delete` — an app
                # daemon holds its netns alive past deletion and keeps its
                # lease, so it must be released from inside explicitly
                # (2026-08-23).
                if side_node.data.apps:
                    dev = (
                        f"veth-krn-{iface['shortName']}"
                        if iface is not None and iface["kind"] == "veth"
                        else (
                            _iface_real_name(iface)
                            if iface is not None
                            else _dummy_name(side_node.key.side)
                        )
                    )
                    _emit_kernel_app_rules(
                        side_node.data.apps,
                        owner.key.name,
                        netns,
                        dev,
                        emit,
                    )
                if iface is not None and iface["kind"] == "vlan":
                    real_name = _iface_real_name(iface)
                    emit.comment(
                        f"# --- kernel router vlan: remove {real_name} from inside the netns ---"
                    )
                    emit.sh(
                        netns_ops.netns_exec_argv(linux_net_ops.delete_link_argv(real_name), netns)
                    )
            # `ip netns delete` tears down every remaining device inside
            # a namespace regardless of whether it was created there or
            # moved in later, same reasoning ovn-nbctl's own cascading
            # del- commands already rely on elsewhere in this module.
            emit.sh(netns_ops.delete_netns_argv(netns))
            for side_node in _kernel_router_sides(owner.key.name, nodes):
                iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
                if iface is not None and iface["kind"] == "veth":
                    root_leg = f"veth-ovn-{iface['shortName']}"
                    emit.comment(
                        f"# --- kernel router transit veth: remove root leg {root_leg} ---"
                    )
                    emit.sh(linux_net_ops.delete_link_argv(root_leg))
            continue

        emit.sh(netns_ops.add_netns_argv(netns))
        # A kernel router IS a router: Linux won't forward between its
        # two sides until the netns's own forwarding knobs say so, per
        # family (2026-08-19).
        emit.comment("# kernel router: enable IPv4/IPv6 forwarding in the netns")
        emit.sh(
            netns_ops.netns_exec_argv(
                linux_net_ops.set_sysctl_argv("net.ipv4.ip_forward", "1"),
                netns,
            )
        )
        emit.sh(
            netns_ops.netns_exec_argv(
                linux_net_ops.set_sysctl_argv("net.ipv6.conf.all.forwarding", "1"),
                netns,
            )
        )
        for side_node in _kernel_router_sides(owner.key.name, nodes):
            iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
            if iface is not None and iface.get("kind") not in _BINDABLE_IFACE_KINDS:
                emit.comment(
                    f'# unsupported kernel router side interface kind "{iface.get("kind")}" '
                    f"for {owner.key.name} ({side_node.key.side.value}) — skipped, "
                    "see deployer/ir_to_shell.py"
                )
                continue
            if iface is not None and iface["kind"] == "veth":
                # The netns-side leg — `peerName` is the root-side leg
                # the transit domain's bridge attaches to (see
                # _emit_iface_bindings, which runs after this
                # block and finds the pair already created here). The
                # real device names come from the IFNAMSIZ-safe
                # `shortName` (src/ir.ts's kernelRouterSideToIR), not
                # the long `ifaceName`/`peerName` the model carries.
                dev = f"veth-krn-{iface['shortName']}"
                peer = f"veth-ovn-{iface['shortName']}"
            else:
                dev = (
                    _iface_real_name(iface)
                    if iface is not None
                    else _dummy_name(side_node.key.side)
                )
                peer = None
            emit.comment(
                f"# --- kernel router side: {owner.key.name} ({side_node.key.side.value}) ---"
            )
            if iface is not None:
                if iface["kind"] == "vlan":
                    emit.sh(linux_net_ops.add_vlan_argv(iface["vlanParent"], dev, iface["vlanId"]))
                elif iface["kind"] == "veth":
                    emit.sh(linux_net_ops.add_veth_argv(peer, dev))
                # "physical": the device must already exist in the root
                # namespace — nothing to create, only the move below.
            else:
                emit.sh(linux_net_ops.add_dummy_argv(dev))
            emit.sh(linux_net_ops.add_if_to_netns_argv(dev, netns))
            emit.sh(netns_ops.netns_exec_argv(linux_net_ops.set_link_up_argv(dev), netns))
            for addr in side_node.data.ipaddrs or []:
                argv = linux_net_ops.add_addr_argv(addr, dev)
                emit.sh(netns_ops.netns_exec_argv(argv, netns))
            for route in side_node.data.routes or []:
                argv = linux_net_ops.add_route_argv(route.dst, dev, route.via)
                emit.sh(netns_ops.netns_exec_argv(argv, netns))
            # The side's attached security group (right side in practice)
            # applies its rules to this real interface, inside the netns.
            # The `security.group` node is implementation-abstract; the
            # side node is where it's ATTACHED (2026-08-22).
            if side_node.data.securityGroup is not None:
                group = _security_group_by_name(side_node.data.securityGroup, nodes)
                if group is None:
                    raise ValueError(
                        f"kernel router {owner.key.name} ({side_node.key.side.value}) "
                        f'references security group "{side_node.data.securityGroup}" '
                        f"but no such security.group node"
                    )
                _emit_security_group_rules(group, netns, dev, emit)
            # The side's apps (right side in practice) run inside the
            # netns on this interface — started after the interface is up
            # and bound, so the DHCP client can immediately acquire a
            # lease on a ready device.
            if side_node.data.apps:
                _emit_kernel_app_rules(
                    side_node.data.apps,
                    owner.key.name,
                    netns,
                    dev,
                    emit,
                )
    if len(emit.lines) > before:
        emit.blank()


# The real (host, interface) pairs this host owns: CREATE creates the
# kernel VLAN sub-interface (if any) or brings the transit veth's root
# leg up, creates the bridge, adds the interface as a port, and collects
# one ovn-bridge-mappings entry per binding — a single comma-joined
# external-ids write, not one per domain (ovs-vsctl's column is one
# string; a second `set` would overwrite the first, not append to it).
# DELETE tears the same down: del-br cascades its own ports (no separate
# del-port call — same reasoning as ladops.ovn.ls_del_argv), plus an
# explicit vlan link delete, and removes the ovn-bridge-mappings entry.
# Create/delete branches adjacent, 2026-08-19 (flag schema).
def _emit_iface_bindings(host_name: str, nodes: list[pt.Model], emit: Emitter) -> None:
    before = len(emit.lines)
    bindings = _host_bindings(nodes).get(host_name, [])
    if emit.action == "create":
        mappings: list[str] = []
        for domain, iface in bindings:
            if iface.get("kind") not in _BINDABLE_IFACE_KINDS:
                emit.comment(
                    f'# unsupported interface kind "{iface.get("kind")}" for domain '
                    f"{domain} on {host_name} — skipped, see deployer/ir_to_shell.py"
                )
                continue
            real_name = _iface_real_name(iface)
            bridge = iface["shortName"]
            emit.comment(f"# --- interface: {domain} on {host_name} ({real_name}) ---")
            if iface["kind"] == "vlan":
                emit.sh(
                    linux_net_ops.add_vlan_argv(iface["vlanParent"], real_name, iface["vlanId"])
                )
                emit.sh(linux_net_ops.set_link_up_argv(real_name))
            elif iface["kind"] == "veth":
                # The ROOT-side leg of the kernel router's transit veth —
                # the kernel router block brings the netns-side leg up,
                # but the peer stays DOWN in root until told otherwise
                # (confirmed live, 2026-08-19: `ip l | grep 9adb1d`
                # showed state DOWN until `ip l set dev veth-ovn-... up`),
                # so bring it up before it becomes the bridge's port.
                emit.sh(linux_net_ops.set_link_up_argv(real_name))
            emit.sh(ovs_ops.add_br_argv(bridge))
            emit.sh(ovs_ops.set_bridge_fail_mode_argv(bridge))
            emit.sh(ovs_ops.add_port_argv(bridge, real_name))
            mappings.append(f"{_network_name(domain)}:{bridge}")
        if mappings:
            emit.sh(ovs_ops.set_external_id_argv("ovn-bridge-mappings", ",".join(mappings)))
    else:
        any_bound = False
        for domain, iface in bindings:
            if iface.get("kind") not in _BINDABLE_IFACE_KINDS:
                continue
            any_bound = True
            real_name = _iface_real_name(iface)
            bridge = iface["shortName"]
            emit.comment(f"# --- interface: {domain} on {host_name} ({real_name}) ---")
            emit.sh(ovs_ops.del_br_argv(bridge))
            if iface["kind"] == "vlan":
                emit.sh(linux_net_ops.delete_link_argv(real_name))
        if any_bound:
            emit.sh(ovs_ops.remove_external_id_argv("ovn-bridge-mappings"))
    if len(emit.lines) > before:
        emit.blank()


def _emit_host(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    host_name = host_node.key.host
    data = host_node.data

    if emit.action == "delete":
        if data.ovnRole == pt.OvnRole.central:
            emit.comment("# central: stop exposing the shared NB/SB DBs")
            emit.sh(ovn_ops.nb_del_connection_argv())
            emit.sh(ovn_ops.sb_del_connection_argv())

        emit.sh(ovs_ops.remove_external_id_argv("ovn-remote"))
        if data.encapIp is not None:
            emit.sh(ovs_ops.remove_external_id_argv("ovn-encap-type"))
            emit.sh(ovs_ops.remove_external_id_argv("ovn-encap-ip"))
        if host_name in _gateway_eligible_hosts(nodes):
            emit.sh(ovs_ops.remove_external_id_argv("ovn-cms-options"))

        emit.blank()
        # The kernel router's netns is removed FIRST — every device it
        # owns (the transit veth's netns leg, the moved-in WAN vlan)
        # lives inside it and dies with it; only then do the
        # root-namespace bindings (bridges, host-level vlan
        # subinterfaces) get torn down. Deleting the other way round
        # would `ip link delete` a device that's still in the netns
        # (2026-08-18).
        _emit_kernel_router(host_node.id, nodes, emit)
        _emit_iface_bindings(host_name, nodes, emit)
        return

    _emit_kernel_router(host_node.id, nodes, emit)
    _emit_iface_bindings(host_name, nodes, emit)

    if data.ovnRole == pt.OvnRole.central:
        emit.comment("# central: expose the shared NB/SB DBs to every other chassis")
        emit.sh(ovn_ops.nb_set_connection_argv())
        emit.sh(ovn_ops.sb_set_connection_argv())
        emit.sh(ovs_ops.set_external_id_argv("ovn-remote", "unix:/var/run/ovn/ovnsb_db.sock"))
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
        emit.sh(ovs_ops.set_external_id_argv("ovn-remote", f"tcp:{central.data.encapIp}:6642"))

    if data.encapIp is not None:
        emit.sh(ovs_ops.set_external_id_argv("ovn-encap-type", "geneve"))
        emit.sh(ovs_ops.set_external_id_argv("ovn-encap-ip", data.encapIp))

    if host_name in _gateway_eligible_hosts(nodes):
        emit.sh(ovs_ops.set_external_id_argv("ovn-cms-options", "enable-chassis-as-gw"))


# The shared per-host body emitter — the one entry point both front-ends
# call (the shell generator hands it a ShellEmitter, the Python generator
# a PythonEmitter). The `#!/bin/sh`/`set -eu` envelope is NOT part of
# this: that's shell-script structure, owned by the shell front-end (see
# deployer/ir_to_shell.py); the Python front-end has no envelope.
def _emit_host_script(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    _emit_host(host_node, nodes, emit)
