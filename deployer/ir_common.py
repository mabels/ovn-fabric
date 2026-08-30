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

import hashlib
import shlex

from ladops import linux_net as linux_net_ops
from ladops import netns as netns_ops
from ladops import ovn as ovn_ops
from ladops import ovs as ovs_ops
from protocol import KernelApp
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

    def sh(self, argv: list[str], background: bool = False) -> None:
        raise NotImplementedError

    def append(self, path: str, content: str) -> None:
        raise NotImplementedError

    def comment(self, text: str) -> None:
        self.lines.append(text)

    def blank(self) -> None:
        self.lines.append("")


class _ShellBody(Emitter):
    """A nested SHELL-ONLY emitter used to build a kernel router's
    up/down script body. The router script is a literal shell file no
    matter which outer front-end (shell or Python) installed it, so this
    renders shlex-joined command lines (with an optional trailing ` &` to
    background a long-running/daemon app inside the netns) and heredoc
    appends, never Python. `background=True` is only meaningful here —
    the outer front-ends ignore it."""

    def sh(self, argv: list[str], background: bool = False) -> None:
        self.lines.append(shlex.join(argv) + (" &" if background else ""))

    def append(self, path: str, content: str) -> None:
        # A delimiter distinct from the outer front-ends' `OVN`, so the
        # router script (itself written via an outer heredoc) can nest
        # these app/conf heredocs without the outer heredoc terminating
        # on the first inner `OVN` line.
        self.lines.append(f"cat > {path} << 'OVNKERNEL'")
        self.lines.append(content)
        self.lines.append("OVNKERNEL")


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


def _docker_wire_script(app: KernelApp, router: str, netns: str) -> str:
    """The wire script for one docker app: run the container with
    `--network none`, then inject one veth into the router netns
    (CNI/Multus-style) — the container's eth0 gets `app.ip`, the router
    end `app.routerIp` (the subnet's first host, resolved in
    define.ts), and the container's default route is via the router.
    Ends in `exec docker wait <container>` — the router `up` backgrounds
    the script (the wait keeps the container's netns symlink live), and
    the router `down` tears it down with `docker rm -f`."""
    container = app.name or f"{router}-docker"
    veth = app.vethName or ("ve-" + hashlib.md5(container.encode()).hexdigest()[:8])
    gateway = app.routerIp.split("/")[0]
    cmd_tokens = " ".join(shlex.quote(t) for t in (app.cmd or []))
    return "\n".join(
        [
            "#!/bin/sh",
            f"# ovn-fabric kernel app docker: {container} in {netns} — one veth",
            "# injected into the router netns (CNI/Multus-style interface",
            "# injection, 2026-08-23). Backgrounded by the router up.",
            "set -eu",
            f'container="{container}"',
            f'netns="{netns}"',
            f'veth="{veth}"',
            'peer="${veth}-c"',
            f'image="{app.image}"',
            f'router_ip="{app.routerIp}"',
            f'container_ip="{app.ip}"',
            f'gateway="{gateway}"',
            "",
            '/usr/bin/docker rm -f "$container" 2>/dev/null || true',
            '/usr/bin/docker run -d --network none --name "$container" "$image"'
            + (f" {cmd_tokens}" if cmd_tokens else ""),
            "pid=$(/usr/bin/docker inspect -f '{{.State.Pid}}' \"$container\")",
            "mkdir -p /var/run/netns",
            'ln -sf "/proc/$pid/ns/net" "/var/run/netns/$container"',
            'ip link add "$veth" type veth peer name "$peer"',
            'ip link set "$peer" netns "$container"',
            'ip link set "$veth" netns "$netns"',
            'ip netns exec "$netns" ip link set "$veth" up',
            'ip netns exec "$netns" ip addr add "$router_ip" dev "$veth"',
            'ip netns exec "$container" ip link set "$peer" name eth0',
            'ip netns exec "$container" ip link set eth0 up',
            'ip netns exec "$container" ip addr add "$container_ip" dev eth0',
            'ip netns exec "$container" ip route add default via "$gateway" dev eth0',
            'exec /usr/bin/docker wait "$container"',
        ]
    )


def _wg_conf_lines(app: KernelApp) -> list[str]:
    """The wg-quick conf body for a wireguard app — mirrors
    generate-netns.ts's emitWireguardInterface byte-for-byte."""
    cfg = app.config
    lines = [
        "[Interface]",
        f"PrivateKey = {cfg.privateKey}",
        f"Address = {cfg.address}",
    ]
    if cfg.listenPort is not None:
        lines.append(f"ListenPort = {cfg.listenPort:g}")
    if cfg.dns is not None:
        lines.append(f"DNS = {cfg.dns}")
    lines.extend(
        [
            "[Peer]",
            f"PublicKey = {cfg.peer.publicKey}",
            f"AllowedIPs = {cfg.peer.allowedIps}",
            f"Endpoint = {cfg.peer.endpoint}",
        ]
    )
    if cfg.peer.persistentKeepalive is not None:
        lines.append(f"PersistentKeepalive = {cfg.peer.persistentKeepalive:g}")
    return lines


def _zerotier_wire_script(app: KernelApp, netns: str) -> str:
    """The zerotier app's up/down script — mirrors generate-netns.ts's
    emitZerotierInterface: start zerotier-one (its OWN instanceDir, so it
    never collides with a host-level daemon), wait for its control
    socket, join the network, then wait for ZeroTier to name the real
    interface (`portDeviceName` — the name is ONLY known at runtime) and
    bring it up + MASQUERADE the declared families out it, and push the
    tunnel's via-less routes (`app.routes` — e.g. the ztnet mesh supernet
    192.168.0.0/16) out it as `ip route add <dst> dev "$var"`. The daemon
     is persistent, so the router `up` backgrounds the script; down just
     kills the daemon (no `zerotier-cli leave`) BEFORE the netns teardown
     (a running daemon holds its netns alive, 2026-08-23)."""
    network = app.networkId or ""
    home = app.instanceDir or ""
    masq_lines: list[str] = []
    for family in app.masq or []:
        iptables = "iptables" if family == pt.MasqEnum.ipv4 else "ip6tables"
        masq_lines.append(
            f'ip netns exec "$netns" {iptables} -t nat -A POSTROUTING -o "$var" -j MASQUERADE'
        )
    route_lines: list[str] = []
    for route in app.routes or []:
        route_lines.append(f'ip netns exec "$netns" ip route add {route.dst} dev "$var"')
    return "\n".join(
        [
            "#!/bin/sh",
            f"# ovn-fabric kernel app zerotier: {network} in {netns} (home {home})",
            "set -eu",
            'action="${1:-up}"',
            f'netns="{netns}"',
            f'home="{home}"',
            f'network="{network}"',
            'case "$action" in',
            "  up)",
            '    mkdir -p "$home"',
            '    ip netns exec "$netns" pgrep -f "zerotier-one -d $home" >/dev/null 2>&1 || \\',
            '      ip netns exec "$netns" zerotier-one -d "$home"',
            "    for i in $(seq 1 10); do",
            '      ip netns exec "$netns" zerotier-cli -D"$home" info >/dev/null 2>&1 && break',
            "      sleep 1",
            "    done",
            '    ip netns exec "$netns" zerotier-cli -D"$home" join "$network"',
            "    for i in $(seq 1 30); do",
            '      var=$(ip netns exec "$netns" zerotier-cli -D"$home" -j listnetworks '
            '2>/dev/null | jq -r ".[] | select(.nwid==\\"$network\\") | .portDeviceName")',
            '      [ -n "$var" ] && [ "$var" != "null" ] && break',
            "      sleep 1",
            "    done",
            '    ip netns exec "$netns" ip link set "$var" up',
            *masq_lines,
            *route_lines,
            "    ;;",
            "  down)",
            # Just kill the daemon — no `zerotier-cli leave`, the mesh
            # membership is irrelevant once the daemon is gone (2026-08-30).
            '    ip netns exec "$netns" pkill -f "zerotier-one -d $home" || true',
            "    ;;",
            "esac",
        ]
    )


def _emit_kernel_app_rules(
    apps: list[KernelApp], router: str, netns: str, dev: str, emit: Emitter
) -> None:
    """Start (create) / stop (delete) the apps attached to one side of a
    kernel router, INSIDE the router's netns — no per-app systemd units.
    The router service (one unit per router, _emit_kernel_router) owns
    them: its `up` starts them, its `down` stops them. Starts that are
    daemons or block (dhclient -d, the docker wire script which ends in
    `docker wait`, the zerotier `up` that waits ~40s for the interface)
    are backgrounded with `&` so the router `up`/`enable --now` returns
    as soon as the netns + veths exist (the transit veth must be present
    before _emit_iface_bindings binds it); wg-quick is in-kernel and
    returns immediately, so it stays in the foreground. Delete stops the
    apps BEFORE `ip netns delete` — a running app daemon holds its netns
    alive (2026-08-23)."""
    for app in apps:
        if app.kind == "dhcp-client":
            if app.style == pt.Style.dhclient:
                # `-lf` gives each router's dhclient its OWN lease database
                # (per-router path, not the host's shared
                # /var/lib/dhcp/dhclient.leases) — runtime-configurable,
                # nothing compiled in (2026-08-30).
                lease = f"/var/lib/dhcp/dhclient.{router}.leases"
                client_argv = ["/usr/sbin/dhclient", "-lf", lease, "-d", dev]
                stop_argv = ["/usr/sbin/dhclient", "-lf", lease, "-r", dev]
                client = "dhclient"
            elif app.style == pt.Style.dhcpcd:
                client_argv = ["/usr/sbin/dhcpcd", "-B", dev]
                stop_argv = ["/usr/sbin/dhcpcd", "-k", dev]
                client = "dhcpcd"
            else:
                emit.comment(
                    f'# unsupported dhcp-client style "{app.style}" on {dev} in {netns} — '
                    "skipped, see deployer/ir_to_shell.py"
                )
                continue
            emit.comment(f"# --- kernel app: dhcp-client ({client}) on {dev} in {netns} ---")
            if emit.action == "create":
                # `-d` runs dhclient as a foreground daemon, so background it.
                emit.sh(netns_ops.netns_exec_argv(["mkdir", "-p", "/var/lib/dhcp"], netns))
                emit.sh(netns_ops.netns_exec_argv(client_argv, netns), background=True)
            else:
                # Release the lease explicitly — SIGTERM through
                # `ip netns exec` is not a reliable dhclient shutdown.
                emit.sh(netns_ops.netns_exec_argv(stop_argv, netns))
        elif app.kind == "docker":
            # The container gets ONE veth injected INTO the router netns
            # (CNI/Multus-style interface injection — the same primitive
            # a k8s pod + Multus uses, 2026-08-23). `--network host`
            # would share the docker DAEMON's netns (root), not the
            # router's, so the container runs `--network none` and a
            # wire script attaches it: docker run (none) -> get its PID
            # -> veth pair -> one end into the container's netns (eth0),
            # the other into the router netns with `router_ip`; the
            # container's default route is via the router. The script
            # is a single `/bin/sh <script>` argv, so it survives both
            # front-ends (shell substitutions are not argv-safe). It ends
            # in `exec docker wait <container>`, so the router `up`
            # backgroundS it (the wait keeps the container's netns live,
            # but `up` must not block on it).
            container = app.name or f"{router}-docker"
            script_path = f"/usr/local/sbin/ovn-kernel-{router}-docker-wire.sh"
            if app.ip is None or app.routerIp is None:
                raise ValueError(
                    f'kernel app docker "{container}": the veth injection needs an `ip` '
                    "(the container's address on the router's services segment)"
                )
            emit.comment(f"# --- kernel app: docker ({container}) on {dev} in {netns} ---")
            if emit.action == "create":
                emit.append(script_path, _docker_wire_script(app, router, netns))
                emit.sh(["/bin/sh", script_path], background=True)
            else:
                # Stopping a docker container is done THE HARD WAY — an
                # explicit `docker rm -f` via the daemon (SIGTERM through
                # the router's ExecStop is not a reliable container
                # shutdown, confirmed live 2026-08-23).
                emit.sh(["/usr/bin/docker", "rm", "-f", container])
                emit.sh(["rm", "-f", script_path])
                emit.sh(["rm", "-f", f"/var/run/netns/{container}"])
        elif app.kind == "wireguard":
            # A WireGuard tunnel inside the netns (the tunnelRouterEndpoint's
            # middle leg): write the conf (PrivateKey included — the
            # documented deliberate credential-in-git exception), `wg-quick
            # up` inside the netns (wg is in-kernel, returns immediately, no
            # daemon to supervise — so no backgrounding), and MASQUERADE the
            # declared families out the TUNNEL iface (not the upstream veth
            # — the tunnel is the egress, 2026-08-23). Down brings it down
            # and removes the rules before `ip netns delete`.
            if app.ifaceName is None or app.config is None:
                raise ValueError(
                    f"kernel app wireguard on {dev} in {netns}: missing ifaceName/config"
                )
            conf_path = f"/etc/wireguard/{app.ifaceName}.conf"
            emit.comment(f"# --- kernel app: wireguard ({app.ifaceName}) in {netns} ---")
            if emit.action == "delete":
                emit.sh(netns_ops.netns_exec_argv(["wg-quick", "down", conf_path], netns))
                for family in app.masq or []:
                    iptables = "iptables" if family == pt.MasqEnum.ipv4 else "ip6tables"
                    emit.sh(
                        netns_ops.netns_exec_argv(
                            [
                                iptables,
                                "-t",
                                "nat",
                                "-D",
                                "POSTROUTING",
                                "-o",
                                app.ifaceName,
                                "-j",
                                "MASQUERADE",
                            ],
                            netns,
                        )
                    )
                continue
            emit.sh(["mkdir", "-p", "/etc/wireguard"])
            emit.append(conf_path, "\n".join(_wg_conf_lines(app)))
            emit.sh(["chmod", "600", conf_path])
            emit.sh(netns_ops.netns_exec_argv(["wg-quick", "up", conf_path], netns))
            for family in app.masq or []:
                iptables = "iptables" if family == pt.MasqEnum.ipv4 else "ip6tables"
                emit.sh(
                    netns_ops.netns_exec_argv(
                        [
                            iptables,
                            "-t",
                            "nat",
                            "-A",
                            "POSTROUTING",
                            "-o",
                            app.ifaceName,
                            "-j",
                            "MASQUERADE",
                        ],
                        netns,
                    )
                )
        elif app.kind == "zerotier":
            # A ZeroTier tunnel inside the netns (the tunnelRouterEndpoint's
            # middle leg): the zerotier-one daemon runs confined to the
            # netns with its OWN instanceDir, joins the network, and the
            # real interface name is captured at RUNTIME (ZeroTier names
            # it itself) — so the wiring is the script (like docker). The
            # router `up` runs it in the background (its up waits up to
            # ~40s for ZeroTier to name the interface, so it must not
            # block `enable --now`); the router `down` runs it with `down`
            # to kill the daemon (no `zerotier-cli leave`) BEFORE `ip
            # netns delete` (a running daemon holds its netns alive,
            # 2026-08-23).
            if app.networkId is None or app.instanceDir is None:
                raise ValueError(
                    f"kernel app zerotier on {dev} in {netns}: missing networkId/instanceDir"
                )
            script_path = f"/usr/local/sbin/ovn-kernel-{router}-zerotier-wire.sh"
            emit.comment(f"# --- kernel app: zerotier in {netns} ---")
            if emit.action == "delete":
                emit.sh(["/bin/sh", script_path, "down"])
                emit.sh(["rm", "-f", script_path])
                continue
            emit.append(script_path, _zerotier_wire_script(app, netns))
            emit.sh(["/bin/sh", script_path, "up"], background=True)
        else:
            emit.comment(
                f'# unsupported kernel app kind "{app.kind}" on {dev} in {netns} — '
                "skipped, see deployer/ir_to_shell.py"
            )
            continue


# One real netns per KernelRouter (types.ts) bound to this host, then —
# per side — a real interface when the side declares `ifaces`
# (KernelRouterSide.ifaces, types.ts — populated for `left` by the
# implicit transit veth and for `right` by buildKernelRouterEndpoint,
# define.ts, 2026-08-18): `up` creates the kernel VLAN sub-interface
# or the transit veth pair (if any) in the root namespace, moves the
# netns-side device into the netns, and binds that side's
# already-resolved addresses/routes to it. A side with no `ifaces` keeps
# the dummy-device stand-in (the pre-2026-08-18 behavior). `down` removes
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
def _emit_kernel_router_owner(
    owner: pt.KernelRouterNode, netns: str, nodes: list[pt.Model], action: Action, emit: Emitter
) -> None:
    """One kernel router's up (create) or down (stop/teardown) body, emitted
    into the router script's body emitter (always a _ShellBody — the router
    script is shell regardless of the outer front-end). See the module doc
    block above for the netns/side semantics."""
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
                emit.sh(netns_ops.netns_exec_argv(linux_net_ops.delete_link_argv(real_name), netns))
        # `ip netns delete` tears down every remaining device inside
        # a namespace regardless of whether it was created there or
        # moved in later, same reasoning ovn-nbctl's own cascading
        # del- commands already rely on elsewhere in this module.
        emit.sh(netns_ops.delete_netns_argv(netns))
        for side_node in _kernel_router_sides(owner.key.name, nodes):
            iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
            if iface is not None and iface["kind"] == "veth":
                root_leg = f"veth-ovn-{iface['shortName']}"
                emit.comment(f"# --- kernel router transit veth: remove root leg {root_leg} ---")
                emit.sh(linux_net_ops.delete_link_argv(root_leg))
        return

    emit.sh(netns_ops.add_netns_argv(netns))
    # A fresh netns starts with lo DOWN; bring it up so loopback works
    # inside (2026-08-30).
    emit.sh(netns_ops.netns_exec_argv(linux_net_ops.set_link_up_argv("lo"), netns))
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
            dev = _iface_real_name(iface) if iface is not None else _dummy_name(side_node.key.side)
            peer = None
        emit.comment(f"# --- kernel router side: {owner.key.name} ({side_node.key.side.value}) ---")
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
        # A kernel router's REAL-WORLD side (right) learns its IPv6 via
        # RA/SLAAC — the config's via-less `::/0` means exactly that.
        # `accept_ra=2` forces it to keep accepting RAs even though the
        # netns has forwarding on (which would otherwise force
        # accept_ra=0) — without it the WAN leg never gets an IPv6
        # default/address and clients' v6 dies at the kernel router
        # (confirmed live, 2026-08-30). Written to the proc file
        # directly: `sysctl` splits its key on every dot, so a vlan
        # iface name (`eth0.2280`) can't be a sysctl key component
        # ("cannot stat .../eth0/2280/accept_ra").
        if side_node.key.side == pt.Side.right:
            emit.sh(
                netns_ops.netns_exec_argv(
                    linux_net_ops.set_sysctl_file_argv(
                        f"/proc/sys/net/ipv6/conf/{dev}/accept_ra", "2"
                    ),
                    netns,
                )
            )
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


def _kernel_router_script(router: str, up_lines: list[str], down_lines: list[str]) -> str:
    """One self-contained per-router script: `up` (create) and `down`
    (stop/teardown). The case labels and bodies sit at column 0 so the
    up/down bodies' own heredocs (the app wire scripts) keep their bodies
    and delimiters unindented. `down` runs with `set -u` only (a stop
    runs against whatever state is there and must not abort on a missing
    object); `up` re-enables `set -e` (a failed create really is an
    error). systemd calls this as `ExecStart=<script> up` and
    `ExecStop=<script> down`."""
    lines = [
        "#!/bin/sh",
        f"# ovn-fabric kernel router {router}: 'up' creates the netns, its",
        "# veths/vlans, security-group rules and apps; 'down' stops the apps",
        "# then tears the netns and the transit veth's root leg down. One",
        "# service per router; systemd runs ExecStart=<script> up and",
        "# ExecStop=<script> down. Generated by deployer/ir_to_shell.py.",
        "set -u",
        'action="${1:-up}"',
        'case "$action" in',
        "up)",
        "set -e",
    ]
    lines += up_lines
    lines += [";;", "down)"]
    lines += down_lines
    lines += [";;", "esac"]
    return "\n".join(lines)


def _kernel_router_unit(router: str, script_path: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric kernel router {router}",
            "After=network-pre.target",
            "Wants=network-pre.target",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={script_path} up",
            f"ExecStop={script_path} down",
            "RemainAfterExit=yes",
            "TimeoutStopSec=60",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ]
    )


def _emit_kernel_router(host_id: str, nodes: list[pt.Model], emit: Emitter) -> None:
    """Deploy one systemd unit PER kernel router this host owns: CREATE
    writes the router's up/down script and unit, then `systemctl enable
    --now` runs `up` — which creates the netns + veths and backgrounds
    the apps, so the transit veth exists when _emit_iface_bindings binds
    it right after this block. DELETE does `systemctl disable --now`,
    which runs `down` — stopping the apps and tearing the netns + transit
    veth down FIRST, before _emit_iface_bindings removes the root-side
    bridges (2026-08-18 ordering: the netns owns the moved-in WAN vlan
    and the veth's netns leg, so it must die before the bridges/root legs
    below it)."""
    before = len(emit.lines)
    action = emit.action
    for owner in _kernel_router_owners(host_id, nodes):
        netns = _netns_name(owner.key.name)
        router = owner.key.name
        script_path = f"/usr/local/sbin/ovn-kernel-{router}.sh"
        unit = f"ovn-kernel-{router}.service"
        unit_path = f"/etc/systemd/system/{unit}"
        emit.comment(f"# --- kernel netns service: {netns} ({router}) ---")
        if action == "delete":
            emit.sh(["systemctl", "disable", "--now", unit])
            emit.sh(["rm", "-f", script_path])
            emit.sh(["rm", "-f", unit_path])
            emit.sh(["systemctl", "daemon-reload"])
            continue
        up = _ShellBody("create")
        down = _ShellBody("delete")
        _emit_kernel_router_owner(owner, netns, nodes, "create", up)
        _emit_kernel_router_owner(owner, netns, nodes, "delete", down)
        emit.append(script_path, _kernel_router_script(router, up.lines, down.lines))
        # The heredoc/write_file leaves the script non-executable, but
        # ExecStart runs it directly — chmod +x or systemd aborts with
        # 203/EXEC "Permission denied" (hit live 2026-08-30).
        emit.sh(["chmod", "+x", script_path])
        emit.append(unit_path, _kernel_router_unit(router, script_path))
        emit.sh(["systemctl", "daemon-reload"])
        emit.sh(["systemctl", "enable", "--now", unit])
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


# Abstract dependency -> concrete distro package (Ubuntu/Debian apt
# names). `ovn` is role-dependent (ovn-central vs ovn-host) and
# `zerotier` is its curl installer — both handled specially below.
_PACKAGE_BY_DEP = {
    "ovs": "openvswitch-switch",
    "ip": "iproute2",
    "iptables": "iptables",
    "dhclient": "isc-dhcp-client",
    "dhcpcd": "dhcpcd",
    "wireguard": "wireguard-tools",
    "docker": "docker.io",
}


def _emit_os_dependencies(host_node: pt.InfraHostNode, emit: Emitter) -> None:
    """Install the host's ABSTRACT dependencies (the IR's `dependencies`
    list, computed by src/ir.ts's hostDependencies — `ovn`/`ovs`/`ip`/
    `iptables`/`dhclient`/`dhcpcd`/`wireguard`/`zerotier`/`docker`,
    deliberately not distro packages) at the very START of the create
    pass, mapped to the host OS's install form. Always resolved to a
    concrete OS (assume Ubuntu when the config leaves it unset,
    2026-08-23); no deinstall ever happens."""
    deps = host_node.data.dependencies or []
    if not deps:
        return
    os_name = host_node.data.os.name
    emit.comment(f"# --- OS dependencies ({os_name} {host_node.data.os.version}) ---")
    if os_name in ("ubuntu", "debian"):
        emit.sh(["apt-get", "update"])
        for dep in deps:
            if dep == "zerotier":
                # the zerotier install script — run via `sh -c` so the
                # pipe survives both front-ends (argv-safe).
                emit.sh(["sh", "-c", "curl -s https://install.zerotier.com | bash"])
                continue
            pkg = _PACKAGE_BY_DEP.get(dep)
            if dep == "ovn":
                pkg = "ovn-central" if host_node.data.ovnRole == pt.OvnRole.central else "ovn-host"
            if pkg is None:
                emit.comment(f'# unknown dependency "{dep}" — skipped')
                continue
            emit.sh(["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", pkg])
    else:
        emit.comment(f"# unknown OS {os_name!r} — cannot install dependencies, skipping")
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

    # Dependencies FIRST — nothing below (OVS/OVN, netns, services)
    # works without them; no deinstall ever (2026-08-23).
    _emit_os_dependencies(host_node, emit)
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
