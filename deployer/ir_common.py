# deployer/ir_common.py — the emitters behind the generated deployer
# (deployer/ir_to_python.py's `generate_python_deployer`): the
# desired-state IR (ovn-fabric's src/ir.ts toIR(), emitted as JSON by
# `deno run src/cli.ts generate-ir`, then hydrated into protocol/
# generated.py's typed dataclasses by protocol/hydrate.py) in,
# RENDERER-AGNOSTIC OPERATIONS out (2026-08-23). Every _emit_* function
# builds through an `Emitter` sink (see below) — the Python front-end
# renders Python source from it that performs the operations at runtime
# with its own logging/error handling. (A second front-end that rendered
# the same operations as plain shell scripts was removed 2026-10-11; the
# tests still read the operations through a text trace of that shape.)
# Every node this module touches is one of
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
# action="create": idempotent adds — ladops' builders carry --may-exist
# (ls-add/lr-add/lrp-add/lsp-add/lr-route-add) and the host side carries
# it on ovs-vsctl add-br/add-port, with a `ip link show || ip link add`
# guard for host-level VLANs. The create pass is meant to be re-run as a
# blunt "make it so" (README's idempotency guarantee): a second run used
# to abort on the first object that already existed (confirmed live
# 2026-09-19). Diffing real drift is still the reconciler's job; the
# generated pass just never errors on "already there".
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

import dataclasses
import hashlib
import ipaddress
import shlex
from collections.abc import Sequence
from typing import Any, Protocol

from ladops import linux_net as linux_net_ops
from ladops import netns as netns_ops
from ladops import ovn as ovn_ops
from ladops import ovs as ovs_ops
from protocol import KernelApp
from protocol import generated as pt

Action = str  # "create" | "delete"

_BINDABLE_IFACE_KINDS = {"vlan", "physical", "veth", "xdp"}


class Ref:
    """A runtime VARIABLE reference inside an emitted command's argv —
    e.g. a chassis id resolved at runtime via `ovs-vsctl`/`ovn-sbctl`
    (see lrp_set_gateway_chassis below). Rendering is backend-specific:
    the Python front-end emits the Python variable `NAME`, a shell
    rendering (_ShellBody, the tests' trace) emits `$NAME` — never a
    quoted literal. Everything else in an argv is a plain string."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _shell_token(token: str | Ref) -> str:
    """One argv token for the SHELL front-end: a `Ref` becomes `"$NAME"`
    (left for the shell to expand, double-quoted so word splitting/globbing
    can't touch the value), anything else is quoted."""
    return f'"${token.name}"' if isinstance(token, Ref) else shlex.quote(token)


def _python_token(token: str | Ref) -> str:
    """One argv token for the PYTHON front-end: a `Ref` becomes the bare
    Python variable name, anything else a `repr` literal."""
    return token.name if isinstance(token, Ref) else repr(token)


def _sh(argv: list[str]) -> str:
    return shlex.join(argv)


# ── the emitter interface ─────────────────────────────────────────────
# Every _emit_* function below builds its output through an `Emitter`
# sink, NOT by returning shell lines. A front-end provides the Emitter
# implementation (deployer/ir_to_python.py renders Python source that
# performs the operations at runtime with logging/error handling; the
# tests provide a text trace) — so the emitters never render, they only
# APPEND operations. That's what gives the Python deployer real control over
# its own execution (2026-08-23): a `sh()` command becomes a
# run_cmd(...) call, an `append()` file-write becomes a write_file(...)
# call, an `install()` a run_install(...) call, each with the Python
# runtime's error/verbose policy — instead of
# the old design where the whole shell block was embedded verbatim and
# the runtime had to re-parse shell syntax (heredocs included) itself.
#
# `append(path, content)` semantics: the file ends up holding `content`
# plus a single trailing newline (the Python backend's write_file()
# writes content + "\n"; the nested _ShellBody renders a
# `cat > path << 'EOF'` heredoc, whose body always ends in a newline).
class Emitter:
    label: str

    def __init__(self, action: Action) -> None:
        self.action = action
        self.lines: list[str] = []

    def sh(
        self,
        argv: Sequence[str | Ref],
        background: bool = False,
        optional: bool | None = None,
    ) -> None:
        """`optional=None`: the action policy — DELETE never aborts (it is
        tearing down whatever is left; a missing object is not an error),
        CREATE does. `optional=True` forces non-fatal in either action.
        `optional=True`: this command failing must NOT stop the rest of
        the pass — it is reported and skipped. Used for per-unit
        `systemctl enable --now` so one broken workload/router can't block
        every other operation (2026-09-16). An argv token may be a `Ref`
        (see above), rendered as a runtime variable."""
        raise NotImplementedError

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        """Run `argv` and bind its trimmed stdout to the runtime variable
        `var`, for later use as a `Ref(var)` in an argv — the only way a
        command can consume a value that only exists on the target host
        (e.g. a chassis's OVS `system-id`)."""
        raise NotImplementedError

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        """Like capture(), but waits (bounded) for the value to appear —
        used for a chassis that may still be registering."""
        self.capture(var, argv)

    def py(self, line: str) -> None:
        """A raw Python statement, for the few body operations that move a
        runtime value into a path/expression (Python-bodied jobs only)."""
        raise NotImplementedError

    def append(self, path: str, content: str) -> None:
        raise NotImplementedError

    def install_self(self, path: str) -> None:
        """Copy the running generated program to `path` — what the boot
        restore unit execs (Python: install_self(); the tests' trace
        renders a comment; there is no shell rendering)."""
        raise NotImplementedError

    def install(self, host: str) -> None:
        """Run the host's merged install object (`INSTALL[host]`): its
        packages, then its images, before the rest of the pass."""
        raise NotImplementedError

    def enable_unit(self, unit: str, optional: bool = True) -> None:
        """Enable a unit and start it now — EXCEPT during the boot restore
        pass (`--no-start`), which must only enable: the fabric units are
        ordered After= the restore unit, so starting them from inside it
        would deadlock (`systemctl` waits for a unit that waits for the
        restore to finish)."""
        raise NotImplementedError

    def comment(self, text: str) -> None:
        self.lines.append(text)

    def blank(self) -> None:
        self.lines.append("")

    def begin_block(self, header: str) -> None:
        """Open an indented block whose guard `header` is backend-specific
        (Python: an `if ...:` line) — every operation emitted until the
        matching end_block() nests one level deeper."""
        raise NotImplementedError

    def end_block(self) -> None:
        raise NotImplementedError


class _ShellBody(Emitter):
    """A nested SHELL-ONLY emitter used to build a kernel router's
    up/down script body. The router script is a literal shell file on
    the host (systemd runs it) even though the Python deployer installs
    it, so this renders shlex-joined command lines (with an optional
    trailing ` &` to background a long-running/daemon app inside the
    netns) and heredoc appends, never Python. `background=True` is only
    meaningful here — the outer front-end ignores it."""

    def sh(
        self,
        argv: Sequence[str | Ref],
        background: bool = False,
        optional: bool | None = None,
    ) -> None:
        if optional is None:
            optional = self.action == "delete"
        line = " ".join(_shell_token(token) for token in argv) + (" &" if background else "")
        self.lines.append(line + (" || true" if optional else ""))

    def append(self, path: str, content: str) -> None:
        # A delimiter distinct from the outer front-ends' `OVN`, so the
        # router script (itself written via an outer heredoc) can nest
        # these app/conf heredocs without the outer heredoc terminating
        # on the first inner `OVN` line.
        self.lines.append(f"cat > {path} << 'OVNKERNEL'")
        self.lines.append(content)
        self.lines.append("OVNKERNEL")


# An interface binding as either side of the IR carries it: an ovn.ls
# `interfaces[].iface` or a kernel.router `ifaces[].iface` (protocol.ts's
# OvnLsIface/KernelIface — the same kinds, each with its own IR-resolved
# extras).
Iface = pt.OvnLsIface | pt.KernelIface

# The closed OVN smap records of the IR — a Logical_Router's `options`, a
# Logical_Router_Port's `ipv6_ra_configs` (see _ovn_settings).
OvnSettings = pt.OvnLrOptions | pt.Ipv6RaConfigs


def _iface_real_name(iface: Iface) -> str:
    if iface.kind == "vlan":
        return iface.ifaceName or f"{iface.vlanParent.name}.{iface.vlanId}"
    if iface.kind == "xdp":
        # The AF_XDP port is the real netdev; there is no kernel
        # `<parent>.<vlan>` device when a VLAN rides it (see
        # _emit_xdp_leg, which creates the per-segment veth instead).
        return iface.name
    if iface.kind == "veth":
        # The ROOT-side leg — the device the transit domain's bridge
        # attaches to (the netns-side leg, `ifaceName`, is created/moved
        # by the kernel router job instead).
        return iface.peerName
    if iface.kind == "physical":
        return iface.name
    # Callers only reach here for a kind in _BINDABLE_IFACE_KINDS.
    raise ValueError(f'interface kind "{iface.kind}" has no real device name to bind')


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
def _host_bindings(nodes: list[pt.Model]) -> dict[str, list[tuple[str, pt.OvnLsIface]]]:
    by_host: dict[str, list[tuple[str, pt.OvnLsIface]]] = {}
    for node in nodes:
        if node.kind != "ovn.ls":
            continue
        domain = node.key.name
        for entry in node.data.interfaces:
            by_host.setdefault(entry.host, []).append((domain, entry.iface))
    return by_host


def _host_bridge_bindings(
    nodes: list[pt.Model],
) -> dict[str, list[tuple[str, pt.OvnLsIface]]]:
    """Host-local `bridge` nodes' ports, keyed by host. Same shape as
    _host_bindings so the bindings pass can treat a bridge exactly like a
    segment bridge; these never contribute an ovn-bridge-mappings entry."""
    by_host: dict[str, list[tuple[str, pt.OvnLsIface]]] = {}
    for node in nodes:
        if node.kind != "bridge":
            continue
        for entry in node.data.ports:
            by_host.setdefault(entry.host, []).append((node.key.name, entry.iface))
    return by_host


def _domains_with_bindable_interfaces(nodes: list[pt.Model]) -> set[str]:
    domains: set[str] = set()
    for bindings in _host_bindings(nodes).values():
        for domain, iface in bindings:
            if iface.kind in _BINDABLE_IFACE_KINDS:
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
        group.sort(key=lambda n: (n.data.metric, n.data.nexthop))
        lowest = group[0].data.metric
        nexthops = {n.data.nexthop for n in group if n.data.metric == lowest}
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
        emit.comment(f"# --- routes: {router} ({domain}) ---")
        for node in sorted(by_domain[domain], key=lambda n: n.key.prefix):
            emit.sh(ovn_ops.lr_route_add_argv(router, node.key.prefix, node.data.nexthop))


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


# One flag-taking function per object, create/delete branches adjacent
# (2026-08-19 — the "write_interface(action)" schema): adding a create
# command forces you to look at its delete right next to it.
def _ovn_acl_match(rule: pt.FilterRule, port: pt.Port | None) -> str:
    parts: list[str] = []
    family = "ip4" if rule.family == pt.Family.ipv4 else "ip6"
    if rule.sources:
        parts.append(f"{family}.src == {{{', '.join(rule.sources)}}}")
    if rule.excludeSources:
        parts.append(f"{family}.src != {{{', '.join(rule.excludeSources)}}}")
    if rule.destinations:
        parts.append(f"{family}.dst == {{{', '.join(rule.destinations)}}}")
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
    ports: list[pt.OvnLrpNode],
    lr: pt.OvnLrNode | None,
    nodes: list[pt.Model],
    domain_names: dict[str, str],
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
    emit.comment(f"# --- router ACL: {router} ---")
    # This router OWNS the ACL set on these switches/directions. Delete the
    # old set first: an older build added a broad allow-related at a higher
    # priority, and an exact-match acl-del would leave it in place forever.
    used_directions: set[str] = set()
    for group in groups:
        for rule in group.data.rules:
            if rule.kind == "filter":
                used_directions.add(direction_map[rule.direction])
        if group.data.default is not None:
            used_directions.update(("to-lport", "from-lport"))
    for switch in switches:
        for direction in sorted(used_directions):
            emit.sh(
                ovn_ops.acl_del_argv(switch, direction),
                optional=True,
            )
    for switch in switches:
        for direction in sorted(used_directions):
            emit.sh(
                ovn_ops.acl_add_argv(
                    switch,
                    direction,
                    30000,
                    "ct.est == 1 || ct.rel == 1",
                    "allow",
                )
            )
    seen: set[tuple[str, str, int, str, str]] = set()

    def add_acl(switch: str, direction: str, priority: int, match: str, action: str) -> None:
        key = (switch, direction, priority, match, action)
        if key in seen:
            return
        seen.add(key)
        emit.sh(ovn_ops.acl_add_argv(switch, direction, priority, match, action))

    for group in groups:
        filters = [r for r in group.data.rules if r.kind == "filter"]
        for switch in switches:
            for rule in filters:
                direction = direction_map[rule.direction]
                priority = rule.priority if rule.priority is not None else 100
                ports_iter: list[pt.Port | None] = list(rule.ports) if rule.ports else [None]
                for port in ports_iter:
                    match = _ovn_acl_match(rule, port)
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
    emit: Emitter,
) -> None:
    if emit.action == "delete":
        # lr_del_argv cascades, but NAT is removed EXPLICITLY first: the
        # SNAT rows are referenced from the LR, and an operator reading
        # the teardown should see them go rather than relying on the
        # cascade. De-duplicated per external IP.
        if lr is not None and lr.data.securityGroups:
            groups = _security_groups_from_refs(
                list(lr.data.securityGroups),
                nodes,
                pt.Render.ovn,
                f"router {router}",
            )
            externals = sorted(
                {
                    rule.externalIp
                    for group in groups
                    for rule in group.data.rules
                    if rule.kind == "snat"
                }
            )
            if externals:
                emit.comment(f"# --- router NAT teardown: {router} ---")
                for external in externals:
                    emit.sh(
                        ovn_ops.lr_nat_del_argv(router, "snat", external),
                        optional=True,
                    )
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
    if lr is not None and lr.data.securityGroups:
        emit.sh(
            ovn_ops.set_lr_external_id_argv(
                router,
                "ovn-fabric.securityGroups",
                ",".join(ref.name for ref in lr.data.securityGroups),
            )
        )
    # The router's own options (its ovn.lr node). lr-del cascades them
    # away with the router — no delete twin.
    if lr is not None:
        for key, value in _ovn_settings(lr.data.options):
            emit.sh(ovn_ops.lr_set_option_argv(router, key, value))
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
            # A runtime `Ref` to the chassis's system-id, resolved once in
            # _emit_gateway_chassis_refs — NOT the IR host name, which
            # lrp-set-gateway-chassis can never match.
            chassis = chassis_refs[data.gatewayChassis]
            emit.sh(ovn_ops.lrp_set_gateway_chassis_argv(lrp, chassis))
            emit.sh(ovn_ops.lrp_set_redirect_chassis_argv(lrp))
        if data.ipv6RaConfigs is not None:
            for key, value in _ovn_settings(data.ipv6RaConfigs):
                emit.sh(ovn_ops.lrp_set_ipv6_ra_config_argv(lrp, key, value))
    # OVN SNAT lives in the security.group rules referenced by the LR.
    # lr-nat-add has no --may-exist, so replace per external IP first;
    # lr-del removes them on teardown.
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
                by_external.setdefault(rule.externalIp, []).append(rule)
        if by_external:
            emit.comment(f"# --- router NAT: {router} ---")
            for external in sorted(by_external):
                # lr-nat-del is idempotent (exit 0 when nothing matched),
                # so a MISSING delete is a real failure: let it abort.
                emit.sh(ovn_ops.lr_nat_del_argv(router, "snat", external))
                for rule in sorted(by_external[external], key=lambda r: r.logicalIps):
                    for logical in rule.logicalIps:
                        emit.sh(ovn_ops.lr_nat_add_argv(router, "snat", external, logical))
    _emit_router_acl(router, ports, lr, nodes, domain_names, emit)
    _emit_router_routes(router, routes, emit)
    emit.blank()


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
    `external_ids:hostname=<IR host name>` (see _emit_host), so the SB
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
        emit.comment(f"# --- chassis id for {host_name} (resolved on this host) ---")
        emit.wait_capture(var, ovn_ops.sb_chassis_id_by_hostname_argv(host_name))
        refs[host_id] = Ref(var)
    if refs:
        emit.blank()
    return refs


def _emit_cluster_body(nodes: list[pt.Model], emit: Emitter) -> None:
    action = emit.action
    # Explicit sorts make the emitted script byte-stable regardless of
    # the IR's input order (routers, switches; ports/routes are sorted
    # where they are emitted).
    switches = sorted((n for n in nodes if n.kind == "ovn.ls"), key=lambda n: n.key.name)
    router_groups = _group_router_ports(nodes)
    routes_by_router = _group_routes_by_router(nodes)
    router_nodes = _router_nodes(nodes)
    # Resolved for both actions — _emit_router's delete branch simply
    # doesn't read them (2026-08-19, flag schema).
    host_names = _host_name_by_id(nodes)
    domain_names = _domain_name_by_id(nodes)
    routers = sorted(router_groups)

    if action == "create":
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
                emit,
            )
    else:
        for router in routers:
            _emit_router(
                router,
                router_groups[router],
                routes_by_router.get(router, []),
                {},
                domain_names,
                router_nodes.get(router),
                nodes,
                emit,
            )
        for node in switches:
            _emit_logical_switch(node, emit)


# ── per-host scripts: kernel/OVS chassis registration ────────────────


def _netns_name(name: str) -> str:
    return f"ns-{name}"


def _dummy_name(side: pt.Side) -> str:
    return f"dummy-{side.value}"


def _side(side_node: pt.KernelRouterNode) -> pt.Side:
    """The side of a PER-SIDE kernel.router node (see
    _kernel_router_sides). `key.side` is optional in the protocol only
    because the router-level node of the same kind has none."""
    if side_node.key.side is None:
        raise ValueError(f"{side_node.id}: a per-side kernel.router node without a side")
    return side_node.key.side


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


def _security_groups_from_refs(
    refs: list[pt.SecurityGroupRef],
    nodes: list[pt.Model],
    render: pt.Render,
    where: str,
) -> list[pt.SecurityGroupNode]:
    groups: list[pt.SecurityGroupNode] = []
    for ref in refs:
        if ref.render != render:
            raise ValueError(
                f"{where}: security group {ref.name!r} renders for "
                f"{ref.render.value}, not {render.value}"
            )
        group = _security_group_by_name(ref.name, nodes)
        if group is None:
            raise ValueError(
                f'{where} references security group "{ref.name}", but no such '
                "security.group node"
            )
        groups.append(group)
    return groups


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
# in the kernel router job) destroys the whole netns, its rules included —
# nothing left to tear down separately.
def _emit_security_group_rules(
    group: pt.SecurityGroupNode, netns: str, dev: str, emit: Emitter
) -> None:
    emit.comment(f"# --- security group: {group.key.name} in {netns} on {dev} ---")
    # Every filter rule is stateful: allow replies first, per family for
    # every chain the group actually uses.
    chains: dict[str, set[str]] = {"iptables": set(), "ip6tables": set()}
    for rule in group.data.rules:
        if rule.kind == "filter":
            chains["iptables" if rule.family == pt.Family.ipv4 else "ip6tables"].add(
                {
                    pt.Direction.input: "INPUT",
                    pt.Direction.output: "OUTPUT",
                    pt.Direction.forward: "FORWARD",
                }[rule.direction]
            )
    for iptables, used in chains.items():
        for chain in sorted(used):
            emit.sh(
                netns_ops.netns_exec_argv(
                    [
                        iptables,
                        "-A",
                        chain,
                        "-m",
                        "conntrack",
                        "--ctstate",
                        "ESTABLISHED,RELATED",
                        "-j",
                        "ACCEPT",
                    ],
                    netns,
                )
            )
    action_map = {
        pt.Action.allow: "ACCEPT",
        pt.Action.deny: "DROP",
        pt.Action.reject: "REJECT",
    }
    for rule in group.data.rules:
        if rule.kind == "masq":
            iptables = "iptables" if rule.family == pt.Family.ipv4 else "ip6tables"
            argv = [
                iptables,
                "-t",
                "nat",
                "-A",
                "POSTROUTING",
                "-o",
                dev,
                "-j",
                "MASQUERADE",
            ]
            emit.sh(netns_ops.netns_exec_argv(argv, netns))
            continue
        if rule.kind != "filter":
            raise ValueError(
                f"{group.key.name}: rule kind {rule.kind!r} is not a kernel "
                "rule; a kernel reference may not carry it"
            )
        iptables = "iptables" if rule.family == pt.Family.ipv4 else "ip6tables"
        chain = {
            pt.Direction.input: "INPUT",
            pt.Direction.output: "OUTPUT",
            pt.Direction.forward: "FORWARD",
        }[rule.direction]
        proto = rule.protocol.value if rule.protocol is not None else None
        ports: list[pt.Port | None] = list(rule.ports) if rule.ports else [None]
        sources: list[str | None] = list(rule.sources) if rule.sources else [None]
        destinations: list[str | None] = list(rule.destinations) if rule.destinations else [None]
        for source in sources:
            for destination in destinations:
                for port in ports:
                    argv = [iptables, "-A", chain]
                    if source is not None:
                        argv += ["-s", source]
                    for excluded in rule.excludeSources or []:
                        argv += ["!", "-s", excluded]
                    if destination is not None:
                        argv += ["-d", destination]
                    if proto is not None:
                        argv += ["-p", proto]
                    if port is not None:
                        if proto not in ("tcp", "udp"):
                            raise ValueError(f"{group.key.name}: ports require tcp/udp")
                        argv += [
                            "--dport",
                            str(port.from_) if port.from_ == port.to else f"{port.from_}:{port.to}",
                        ]
                    argv += ["-j", action_map[rule.action]]
                    emit.sh(netns_ops.netns_exec_argv(argv, netns))
    default = group.data.default
    if default is not None:
        for direction, chain in (
            ("input", "INPUT"),
            ("output", "OUTPUT"),
            ("forward", "FORWARD"),
        ):
            action = getattr(default, direction, None)
            if action is None:
                continue
            for iptables in ("iptables", "ip6tables"):
                emit.sh(
                    netns_ops.netns_exec_argv(
                        [iptables, "-A", chain, "-j", action_map[action]], netns
                    )
                )


def _masq_families(security_groups: list[pt.SecurityGroupRef], nodes: list[pt.Model]) -> list[str]:
    """Resolve security-group references to the address families their
    `masq` rules cover — the app/nat consumers carry refs, not families."""
    families: set[str] = set()
    for ref in security_groups:
        group = _security_group_by_name(ref.name, nodes)
        if group is None:
            raise ValueError(
                f'kernel app references security group "{ref.name}", which is '
                "not present in the IR"
            )
        for rule in group.data.rules:
            if rule.kind == "masq":
                families.add("ipv4" if rule.family == pt.Family.ipv4 else "ipv6")
    return sorted(families)


def _apply_side_network(
    groups: list[pt.SecurityGroupNode],
    ipaddrs: list[str] | None,
    routes: list[pt.KernelRouterRoute] | None,
    netns: str,
    dev: str,
    emit: Emitter,
) -> None:
    """Apply a kernel router side's static addressing (addresses + routes)
    and security-group rules (masq) to a target namespace — the router's
    netns OR a docker container's netns (the dhcpcd-in-docker path). The
    ONLY difference between the two is the namespace name, so both go
    through this one function (2026-08-31)."""
    for addr in ipaddrs or []:
        argv = linux_net_ops.add_addr_argv(addr, dev)
        emit.sh(netns_ops.netns_exec_argv(argv, netns))
    for route in routes or []:
        argv = linux_net_ops.add_route_argv(route.dst, dev, route.via, route.metric)
        emit.sh(netns_ops.netns_exec_argv(argv, netns))
    for group in groups:
        _emit_security_group_rules(group, netns, dev, emit)


def _docker_dockerfile(build: pt.DockerBuild) -> str:
    """The Dockerfile body for a docker app's optional `build` — a full
    `dockerfile` wins; otherwise FROM <from> + install the packages with the
    manager the base image implies (apk for alpine/arch, apt otherwise)
    (2026-08-31)."""
    if build.dockerfile:
        return build.dockerfile
    pkgs = " ".join(build.packages or [])
    base = build.from_
    if "alpine" in base or "arch" in base:
        return f"FROM {base}\nRUN apk add --no-cache {pkgs}"
    return f"FROM {base}\nRUN apt-get update && apt-get install -y {pkgs}"


def _wg_conf_lines(app: pt.WireguardApp) -> list[str]:
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


@dataclasses.dataclass
class _SideContext:
    """The facts a kernel router SIDE carries into its apps — the target
    netns, the side's real interface `dev`, the transit veth's netns leg
    (left side) and its addressing, plus the side's own static addressing
    and security group (masq)."""

    netns: str
    dev: str
    transit_leg: str | None
    groups: list[pt.SecurityGroupNode]
    ipaddrs: list[str]
    routes: list[pt.KernelRouterRoute]
    transit_ipaddrs: list[str]
    transit_routes: list[pt.KernelRouterRoute]


def _emit_kernel_app_rules(
    apps: list[KernelApp],
    router: str,
    emit: Emitter,
    ctx: _SideContext,
    nodes: list[pt.Model],
) -> None:
    """Start (create) / stop (delete) the apps attached to one side of a
    kernel router, INSIDE the router's netns — no per-app systemd units.
    The kernel router job owns
    them: its `up` starts them, its `down` stops them. Starts that are
    daemons or block (dhclient -d, the docker wire script which ends in
    `docker wait`, the zerotier `up` that waits ~40s for the interface)
    are backgrounded with `&` so the router `up`/`enable --now` returns
    as soon as the netns + veths exist (the transit veth must be present
    before _emit_iface_bindings binds it); wg-quick is in-kernel and
    returns immediately, so it stays in the foreground. Delete stops the
    apps BEFORE `ip netns delete` — a running app daemon holds its netns
    alive (2026-08-23)."""
    netns = ctx.netns
    dev = ctx.dev
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
                emit.comment(f"# --- kernel app: dhcp-client (dhclient) on {dev} in {netns} ---")
                if emit.action == "create":
                    # `-d` runs dhclient as a foreground daemon, so background it.
                    emit.sh(netns_ops.netns_exec_argv(["mkdir", "-p", "/var/lib/dhcp"], netns))
                    emit.sh(netns_ops.netns_exec_argv(client_argv, netns))
                else:
                    # Release the lease explicitly — SIGTERM through
                    # `ip netns exec` is not a reliable dhclient shutdown.
                    emit.sh(netns_ops.netns_exec_argv(stop_argv, netns))
            else:
                emit.comment(
                    f'# unsupported dhcp-client style "{app.style}" on {dev} in {netns} — '
                    "skipped, see deployer/ir_common.py"
                )
                continue
        elif app.kind == "docker":
            # The container gets ONE veth injected INTO the router netns
            # (CNI/Multus-style interface injection — the same primitive
            # a k8s pod + Multus uses, 2026-08-23). `--network host`
            # would share the docker DAEMON's netns (root), not the
            # router's, so the container runs `--network none` and the
            # generated Python attaches it: docker run (none) -> get its
            # PID -> veth pair -> one end into the container's netns
            # (eth0), the other into the router netns with `router_ip`;
            # the container's default route is via the router.
            container = app.name or f"{router}-docker"
            # A docker that OWNS the router's interfaces ("container IS the
            # router" mode, dhcpcd-in-docker) carries NO veth addressing in
            # the IR — that structural fact (no `ip`) is how the TS side
            # says "own the interfaces", so this is decided here, not by a
            # TS concept name (2026-08-31).
            if app.ip is None:
                emit.comment(
                    f"# --- kernel app: docker (owns the interfaces) ({container}) "
                    f"on {dev} in {netns} ---"
                )
                if emit.action == "delete":
                    emit.sh(["/usr/bin/docker", "rm", "-f", container])
                    emit.sh(["rm", "-f", f"/var/run/netns/{container}"])
                    continue
                image = app.image or f"ovn-fabric-{container}"
                volumes = [
                    {"host": volume.host, "container": volume.container}
                    for volume in (app.volumes or [])
                ]
                identity = (
                    {"public": app.identity.public, "secret": app.identity.secret}
                    if app.identity
                    else None
                )
                emit.py(
                    f"await _docker_owns_start(job, {emit.label!r}, {container!r}, "
                    f"{image!r}, {(_docker_dockerfile(app.build) if app.build else None)!r}, "
                    f"{netns!r}, {dev!r}, {ctx.transit_leg!r}, "
                    f"{volumes!r}, {list(app.run or [])!r}, "
                    f"{identity!r})"
                )
                if ctx.transit_leg:
                    _apply_side_network(
                        [],
                        ctx.transit_ipaddrs,
                        ctx.transit_routes,
                        container,
                        ctx.transit_leg,
                        emit,
                    )
                _apply_side_network(ctx.groups, ctx.ipaddrs, ctx.routes, container, dev, emit)
                emit.py(
                    f"await _docker_exec_wait(job, {emit.label!r}, {container!r}, "
                    f"{list(app.cmd or ['sh'])!r}, {dev!r})"
                )
                continue
            # Else: the client mode — one veth injected into the router netns
            # (CNI/Multus-style interface injection — the same primitive
            # a k8s pod + Multus uses, 2026-08-23). `--network host`
            # would share the docker DAEMON's netns (root), not the
            # router's, so the container runs `--network none` and the
            # generated Python attaches it: docker run (none) -> get its
            # PID -> veth pair -> one end into the container's netns
            # (eth0), the other into the router netns with `router_ip`;
            # the container's default route is via the router.
            if app.ip is None or app.routerIp is None:
                raise ValueError(
                    f'kernel app docker "{container}": the veth injection needs an `ip` '
                    "(the container's address on the router's services segment)"
                )
            emit.comment(f"# --- kernel app: docker ({container}) on {dev} in {netns} ---")
            if emit.action == "delete":
                # Stopping a docker container is done THE HARD WAY — an
                # explicit `docker rm -f` via the daemon (SIGTERM through
                # the router's stop is not a reliable container shutdown).
                emit.sh(["/usr/bin/docker", "rm", "-f", container])
                emit.sh(["rm", "-f", f"/var/run/netns/{container}"])
                continue
            veth = app.vethName or ("ve-" + hashlib.md5(container.encode()).hexdigest()[:8])
            gateway = app.routerIp.split("/")[0]
            image = app.image or f"ovn-fabric-{container}"
            emit.py(
                f"await _docker_veth_up(job, {emit.label!r}, {container!r}, "
                f"{image!r}, {(_docker_dockerfile(app.build) if app.build else None)!r}, "
                f"{list(app.cmd or [])!r}, {netns!r}, {veth!r}, "
                f"{app.routerIp!r}, {app.ip!r}, {gateway!r})"
            )
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
                for family in _masq_families(list(app.securityGroups), nodes):
                    iptables = "iptables" if family == "ipv4" else "ip6tables"
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
            for family in _masq_families(list(app.securityGroups), nodes):
                iptables = "iptables" if family == "ipv4" else "ip6tables"
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
            emit.comment(f"# --- kernel app: zerotier in {netns} ---")
            masq = _masq_families(list(app.securityGroups), nodes)
            routes = [route.dst for route in (app.routes or [])]
            if emit.action == "delete":
                # The daemon is the job's tracked foreground child; the
                # runtime kills it on stop.
                continue
            emit.py(
                f"await _zerotier_up(job, {emit.label!r}, {netns!r}, "
                f"{app.instanceDir!r}, {app.networkId!r}, {masq!r}, {routes!r})"
            )
        else:
            emit.comment(
                f'# unsupported kernel app kind "{app.kind}" on {dev} in {netns} — '
                "skipped, see deployer/ir_common.py"
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
def _veth_root_bridge(root_leg: str, nodes: list[pt.Model]) -> str | None:
    """The OVS bridge a kernel-router veth's ROOT leg belongs to, from the
    same ovn.ls interface entry _emit_iface_bindings uses."""
    for node in nodes:
        if node.kind == "ovn.ls":
            entries = node.data.interfaces or []
        elif node.kind == "bridge":
            entries = node.data.ports or []
        else:
            continue
        for entry in entries:
            iface = entry.iface
            if iface.kind != "veth":
                continue
            if root_leg in (iface.ifaceName, iface.peerName):
                return iface.bridge
    return None


def _emit_kernel_router_owner(
    owner: pt.KernelRouterNode, netns: str, nodes: list[pt.Model], action: Action, emit: Emitter
) -> None:
    """One kernel router's up (create) or down (stop/teardown) body, emitted
    into the router script's body emitter (always a _ShellBody — the router
    script is shell regardless of the outer front-end). See the module doc
    block above for the netns/side semantics."""
    # The transit veth's netns-side leg (the router's mesh uplink, on the
    # LEFT side) plus its static addressing — handed to container-owning
    # apps (dhcpcd-in-docker) so the container becomes the router and
    # carries this leg and its addresses too (2026-08-31).
    transit_leg = None
    transit_ipaddrs: list[str] = []
    transit_routes: list[pt.KernelRouterRoute] = []
    for side_node in _kernel_router_sides(owner.key.name, nodes):
        if side_node.key.side == pt.Side.left:
            iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
            if iface is not None and iface.kind == "veth":
                transit_leg = f"veth-krn-{iface.shortName}"
            transit_ipaddrs = list(side_node.data.ipaddrs or [])
            transit_routes = list(side_node.data.routes or [])
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
                    f"veth-krn-{iface.shortName}"
                    if iface is not None and iface.kind == "veth"
                    else (
                        _iface_real_name(iface)
                        if iface is not None
                        else _dummy_name(_side(side_node))
                    )
                )
                _emit_kernel_app_rules(
                    side_node.data.apps,
                    owner.key.name,
                    emit,
                    _SideContext(
                        netns=netns,
                        dev=dev,
                        transit_leg=transit_leg,
                        groups=_security_groups_from_refs(
                            list(side_node.data.securityGroups or []),
                            nodes,
                            pt.Render.kernel,
                            f"kernel router {owner.key.name}",
                        ),
                        ipaddrs=list(side_node.data.ipaddrs or []),
                        routes=list(side_node.data.routes or []),
                        transit_ipaddrs=transit_ipaddrs,
                        transit_routes=transit_routes,
                    ),
                    nodes,
                )
            if iface is not None and iface.kind == "vlan":
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
            if iface is not None and iface.kind == "veth":
                root_leg = f"veth-ovn-{iface.shortName}"
                emit.comment(f"# --- kernel router transit veth: remove root leg {root_leg} ---")
                emit.sh(linux_net_ops.delete_link_argv(root_leg))
        return

    emit.py(f"await ensure_netns(job, {emit.label!r}, {netns!r}, abort_on_error)")
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
        if iface is not None and iface.kind not in _BINDABLE_IFACE_KINDS:
            emit.comment(
                f'# unsupported kernel router side interface kind "{iface.kind}" '
                f"for {owner.key.name} ({_side(side_node).value}) — skipped, "
                "see deployer/ir_common.py"
            )
            continue
        if iface is not None and iface.kind == "veth":
            # The netns-side leg — `peerName` is the root-side leg
            # the transit domain's bridge attaches to (see
            # _emit_iface_bindings, which runs after this
            # block and finds the pair already created here). The
            # real device names come from the IFNAMSIZ-safe
            # `shortName` (src/ir.ts's kernelRouterSideToIR), not
            # the long `ifaceName`/`peerName` the model carries.
            dev = f"veth-krn-{iface.shortName}"
        else:
            dev = _iface_real_name(iface) if iface is not None else _dummy_name(_side(side_node))
        emit.comment(f"# --- kernel router side: {owner.key.name} ({_side(side_node).value}) ---")
        if iface is not None:
            if iface.kind == "vlan":
                emit.sh(linux_net_ops.add_vlan_argv(iface.vlanParent.name, dev, iface.vlanId))
            elif iface.kind == "veth":
                # `mac` (src/ir.ts's kernelRouterSideToIR) pins the
                # netns-side leg's MAC so the OVN router's MAC_Binding
                # for this side stays valid across a down/up.
                root_leg = f"veth-ovn-{iface.shortName}"
                emit.sh(linux_net_ops.add_veth_argv(root_leg, dev, iface.mac))
                # The ROOT leg is born DOWN and nothing else touches it
                # at boot: _emit_iface_bindings also upped it, but that
                # only runs in the deploy pass. Without this the netns
                # leg stays LOWERLAYERDOWN and every route through it
                # linkdown after a reboot (confirmed live, 2026-10-20).
                emit.sh(linux_net_ops.set_link_up_argv(root_leg))
                bridge = _veth_root_bridge(root_leg, nodes)
                if bridge is not None:
                    emit.sh(ovs_ops.add_port_argv(bridge, root_leg))
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
            emit.py(
                f"await write_sysctl_in_netns(job, {emit.label!r}, {netns!r}, "
                f"{f'/proc/sys/net/ipv6/conf/{dev}/accept_ra'!r}, '2', "
                "abort_on_error)"
            )
        # The side's static addressing + security-group (masq) rules,
        # applied inside the netns — SAME code as the dhcpcd-in-docker
        # container path (only the target namespace differs).
        groups = _security_groups_from_refs(
            list(side_node.data.securityGroups or []),
            nodes,
            pt.Render.kernel,
            f"kernel router {owner.key.name} ({_side(side_node).value})",
        )
        _apply_side_network(
            groups,
            list(side_node.data.ipaddrs or []),
            list(side_node.data.routes or []),
            netns,
            dev,
            emit,
        )
        # The side's apps (right side in practice) run inside the
        # netns on this interface — started after the interface is up
        # and bound, so the DHCP client can immediately acquire a
        # lease on a ready device.
        if side_node.data.apps:
            _emit_kernel_app_rules(
                side_node.data.apps,
                owner.key.name,
                emit,
                _SideContext(
                    netns=netns,
                    dev=dev,
                    transit_leg=transit_leg,
                    groups=_security_groups_from_refs(
                        list(side_node.data.securityGroups or []),
                        nodes,
                        pt.Render.kernel,
                        f"kernel router {owner.key.name}",
                    ),
                    ipaddrs=list(side_node.data.ipaddrs or []),
                    routes=list(side_node.data.routes or []),
                    transit_ipaddrs=transit_ipaddrs,
                    transit_routes=transit_routes,
                ),
                nodes,
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
        "# ExecStop=<script> down. Generated by deployer/ir_common.py.",
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
# ── XDP trunk ────────────────────────────────────────────────────────
# One netdev bridge per host owns every AF_XDP port. A single AF_XDP
# port can only live on ONE bridge, so it cannot also be the per-domain
# segment bridge (there is one of those per collision domain): each
# segment instead gets a veth pair, tagged on the trunk side, whose
# other end is the segment bridge — which therefore stays `system` and
# keeps its patch to `br-int`. The VLAN split lives in OVS (`tag=`), not
# in the kernel: an xdp parent has no `<parent>.<vlan>` device.
_XDP_TRUNK_BRIDGE = "br-xdp"
# The per-segment veths are kernel netdevs; OVS's netdev datapath accepts
# kernel netdevs (the same pattern as its `br-phy` underlay bridge), so
# only this one bridge is netdev and the rest of the fabric stays `system`.


def _short_suffix(name: str) -> str:
    """FNV-1a 32 of `name`, first 6 hex chars — the IFNAMSIZ-safe suffix
    for the per-segment legs (`veth-xdp-<6>` / `veth-seg-<6>`, 15 chars
    each). Same fold src/ir.ts uses for over-long bridge names."""
    h = 0x811C9DC5
    for byte in name.encode():
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"[:6]


class _XdpPort(Protocol):
    """What an AF_XDP port looks like to this module, whether it arrives
    as a VLAN parent (`generated.VlanParent`) or as a standalone
    `ovn.ls` iface (`generated.OvnLsIface1` — datamodel-codegen's name
    for the anonymous `OvnLsIfaceXdp` component). Structural so the
    emitter never depends on that generated class name."""

    name: str
    mode: pt.Mode
    nRxq: int


def _xdp_trunks(
    bindings: list[tuple[str, pt.OvnLsIface]],
) -> dict[str, list[int]]:
    """VLAN ids each AF_XDP parent must trunk. The parent is a trunk (the
    wire's tagged frames arrive on it); the per-segment veth legs are the
    access ports (`tag=<vid>`)."""
    trunks: dict[str, list[int]] = {}
    for _domain, iface in bindings:
        if iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
            trunks.setdefault(iface.vlanParent.name, []).append(iface.vlanId)
    return trunks


def _xdp_ports(
    bindings: list[tuple[str, pt.OvnLsIface]],
) -> dict[str, _XdpPort]:
    """Every AF_XDP port this host owns, keyed by netdev name — one
    entry whether the port appears as a VLAN parent or as a standalone
    `xdp` interface. Deduping here is what makes "five segments, one
    port" possible."""
    ports: dict[str, _XdpPort] = {}
    for _domain, iface in bindings:
        if iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
            ports.setdefault(iface.vlanParent.name, iface.vlanParent)
        elif iface.kind == "xdp":
            ports.setdefault(iface.name, iface)
    return ports


def _emit_xdp_leg(segment_bridge: str, vlan_id: int | None, emit: Emitter) -> None:
    suffix = _short_suffix(segment_bridge)
    trunk_leg = f"veth-xdp-{suffix}"
    seg_leg = f"veth-seg-{suffix}"
    emit.py(
        f"await ensure_veth(job, {emit.label!r}, {trunk_leg!r}, {seg_leg!r}, " "abort_on_error)"
    )
    emit.sh(linux_net_ops.set_link_up_argv(trunk_leg))
    emit.sh(linux_net_ops.set_link_up_argv(seg_leg))
    emit.sh(ovs_ops.add_port_argv(_XDP_TRUNK_BRIDGE, trunk_leg))
    if vlan_id is not None:
        emit.sh(ovs_ops.set_port_tag_argv(trunk_leg, vlan_id))
    emit.sh(ovs_ops.add_port_argv(segment_bridge, seg_leg))


def _emit_xdp_leg_delete(segment_bridge: str, emit: Emitter) -> None:
    suffix = _short_suffix(segment_bridge)
    trunk_leg = f"veth-xdp-{suffix}"
    seg_leg = f"veth-seg-{suffix}"
    emit.sh(ovs_ops.del_port_argv(segment_bridge, seg_leg))
    emit.sh(ovs_ops.del_port_argv(_XDP_TRUNK_BRIDGE, trunk_leg))
    # Deleting one end removes the pair.
    emit.sh(linux_net_ops.delete_link_argv(trunk_leg))
    emit.sh(ovs_ops.del_br_argv(segment_bridge))


def _emit_iface_bindings(host_name: str, nodes: list[pt.Model], emit: Emitter) -> None:
    """Same body for the deploy pass and the boot bindings unit. It only
    ensures the OVS bridge/port rows and the netdevices it OWNS (kernel
    VLANs, XDP veth legs); the transit veths are created and upped by
    their kernel-router units, which may not have run yet here."""
    before = len(emit.lines)
    bindings = _host_bindings(nodes).get(host_name, [])
    bridge_bindings = _host_bridge_bindings(nodes).get(host_name, [])
    ovn_domains = {domain for domain, _iface in bindings}
    all_bindings = bindings + bridge_bindings
    if emit.action == "create":
        mappings: list[str] = []
        xdp_ports = _xdp_ports(all_bindings)
        xdp_trunks = _xdp_trunks(all_bindings)
        if xdp_ports:
            emit.comment("# --- XDP trunk: userspace datapath, one port for all segments ---")
            emit.sh(ovs_ops.add_br_argv(_XDP_TRUNK_BRIDGE))
            emit.sh(ovs_ops.set_bridge_datapath_argv(_XDP_TRUNK_BRIDGE, "netdev"))
            emit.sh(ovs_ops.set_bridge_fail_mode_argv(_XDP_TRUNK_BRIDGE))
            for name, port in xdp_ports.items():
                emit.sh(
                    ovs_ops.add_afxdp_port_argv(_XDP_TRUNK_BRIDGE, name, port.mode.value, port.nRxq)
                )
                trunk_vlans = sorted(set(xdp_trunks.get(name, [])))
                if trunk_vlans:
                    emit.sh(ovs_ops.set_port_trunks_argv(name, trunk_vlans))
            emit.blank()
        # The IR carries one node per OVS bridge with all of its ports
        # (`ovn.ls.interfaces` or `bridge.ports`). Group by bridge name and
        # emit add-br/fail-mode ONCE, then only the ports.
        by_bridge: dict[str, list[tuple[str, pt.OvnLsIface]]] = {}
        for domain, iface in all_bindings:
            if iface.kind not in _BINDABLE_IFACE_KINDS:
                emit.comment(
                    f'# unsupported interface kind "{iface.kind}" for domain '
                    f"{domain} on {host_name} — skipped, see deployer/ir_common.py"
                )
                continue
            by_bridge.setdefault(iface.bridge, []).append((domain, iface))
        for bridge in sorted(by_bridge):
            emit.comment(f"# --- bridge: {bridge} on {host_name} ---")
            for domain, iface in by_bridge[bridge]:
                is_xdp = iface.kind == "xdp" or (
                    iface.kind == "vlan" and iface.vlanParent.kind == "xdp"
                )
                if iface.kind == "xdp":
                    label = iface.name
                elif iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
                    label = f"{iface.vlanParent.name} vlan {iface.vlanId}"
                else:
                    label = _iface_real_name(iface)
                emit.comment(f"# --- interface: {domain} ({label}) ---")
                if iface.kind == "vlan" and not is_xdp:
                    emit.py(
                        f"await ensure_vlan(job, {emit.label!r}, "
                        f"{iface.vlanParent.name!r}, {_iface_real_name(iface)!r}, "
                        f"{iface.vlanId}, abort_on_error)"
                    )
                    emit.sh(linux_net_ops.set_link_up_argv(_iface_real_name(iface)))
                # A transit veth's ROOT leg is created AND upped by its own
                # kernel-router job (_emit_kernel_router_owner). This block
                # must not touch it (2026-10-20, boot-restore ordering).
                if is_xdp:
                    vlan_id = iface.vlanId if iface.kind == "vlan" else None
                    _emit_xdp_leg(bridge, vlan_id, emit)
                elif iface.kind == "veth":
                    pass
                else:
                    emit.sh(ovs_ops.add_port_argv(bridge, _iface_real_name(iface)))
                if domain in ovn_domains:
                    mappings.append(f"{_network_name(domain)}:{bridge}")
        if mappings:
            emit.sh(ovs_ops.set_external_id_argv("ovn-bridge-mappings", ",".join(mappings)))
    else:
        any_bound = False
        xdp_ports = _xdp_ports(all_bindings)
        for domain, iface in all_bindings:
            if iface.kind not in _BINDABLE_IFACE_KINDS:
                continue
            if domain in ovn_domains:
                any_bound = True
            bridge = iface.bridge
            is_xdp = iface.kind == "xdp" or (
                iface.kind == "vlan" and iface.vlanParent.kind == "xdp"
            )
            if iface.kind == "xdp":
                label = iface.name
            elif iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
                label = f"{iface.vlanParent.name} vlan {iface.vlanId}"
            else:
                label = _iface_real_name(iface)
            emit.comment(f"# --- interface: {domain} on {host_name} ({label}) ---")
            if is_xdp:
                _emit_xdp_leg_delete(bridge, emit)
            else:
                if iface.kind == "vlan":
                    emit.sh(linux_net_ops.delete_link_argv(_iface_real_name(iface)))
        if xdp_ports:
            emit.sh(ovs_ops.del_br_argv(_XDP_TRUNK_BRIDGE))
        if any_bound:
            emit.sh(ovs_ops.remove_external_id_argv("ovn-bridge-mappings"))
    if len(emit.lines) > before:
        emit.blank()


# ── the one host service ──────────────────────────────────────────────
# Everything a host runs at boot is the generated job tree: bindings,
# chassis registration, kernel routers and workload containers. One
# systemd service starts the tree; there are no per-router/service units.
_RESTORE_SCRIPT = "/usr/local/sbin/ovn-fabric-deployer.py"
_FABRIC_UNIT = "ovn-fabric.service"
_FABRIC_UNIT_PATH = f"/etc/systemd/system/{_FABRIC_UNIT}"
_LEGACY_UNITS = (
    "ovn-fabric-bindings.service",
    "ovn-fabric-restore.service",
    "ovn-fabric-install.service",
)


def _fabric_unit(host_name: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric ({host_name})",
            # The OVS daemons own the datapath the bindings/kernel jobs
            # attach to.
            "After=network-pre.target ovsdb-server.service ovs-vswitchd.service",
            "Wants=network-pre.target ovsdb-server.service ovs-vswitchd.service",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={_RESTORE_SCRIPT} --job ovn-fabric-{host_name} --job-action up",
            f"ExecStop={_RESTORE_SCRIPT} --job ovn-fabric-{host_name} --job-action down",
            "Restart=on-failure",
            "RestartSec=5",
            # SIGTERM -> the deployer stops the tree (kernel `down` bodies)
            # before exiting.
            "KillMode=mixed",
            "TimeoutStopSec=300",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ]
    )


def _emit_fabric_unit(host_node: pt.InfraHostNode, emit: Emitter) -> None:
    """CREATE installs the generated program and the ONE service that runs
    its job tree at boot. DELETE stops that service FIRST (its tree stop
    runs the kernel/router down bodies), then removes the unit and the
    old per-purpose units/scripts."""
    host_name = host_node.key.host
    emit.comment(f"# --- fabric service: {_FABRIC_UNIT} ---")
    if emit.action == "delete":
        emit.sh(["systemctl", "disable", "--now", _FABRIC_UNIT], optional=True)
        for unit in _LEGACY_UNITS:
            emit.sh(["systemctl", "disable", "--now", unit], optional=True)
        emit.sh(["rm", "-f", _FABRIC_UNIT_PATH])
        for unit in _LEGACY_UNITS:
            emit.sh(["rm", "-f", f"/etc/systemd/system/{unit}"])
        emit.sh(["rm", "-f", "/usr/local/sbin/ovn-fabric-bindings.sh"])
        emit.sh(["rm", "-f", "/usr/local/sbin/ovn-fabric-install"])
        emit.sh(["rm", "-rf", "/var/spool/ovn-fabric/install"])
        emit.sh(["rm", "-f", _RESTORE_SCRIPT])
        emit.sh(["systemctl", "daemon-reload"])
        emit.blank()
        return
    emit.install_self(_RESTORE_SCRIPT)
    emit.append(_FABRIC_UNIT_PATH, _fabric_unit(host_name))
    emit.sh(["systemctl", "daemon-reload"])
    emit.enable_unit(_FABRIC_UNIT)
    emit.blank()


# ── flow export (IPFIX) ───────────────────────────────────────────────
# Per-bridge IPFIX sampling (2026-10-10). WHERE to export is host-level
# (infra.host.data.ipfix, from Host.monitoring.ipfix); WHETHER a given
# leg is sampled rides the ovn.ls interface blob as `iface.flow` (only
# present when a leg opts OUT — src/ir.ts collisionDomainToIR). The
# concrete unit is the SEGMENT's OVS bridge (br-<shortName>): IPFIX
# attaches to a Bridge, not a port, so a bridge shared by several legs
# is off if ANY of them carries flow=false (off wins — same reduction
# _ipfix_bridge_flags below does). Backbone legs (br-int) and
# kernel/tunnel legs (netns, no OVS bridge) have no bridge here and so
# can't be switched this way; that partial effect is by design.
def _ipfix_bridge_flags(host_name: str, nodes: list[pt.Model]) -> dict[str, bool]:
    """bridge -> sampled? for this host. OFF if any bindable interface
    bound into that bridge opted out (iface.flow is False); a bridge
    with no interfaces is absent entirely."""
    flags: dict[str, bool] = {}
    for _domain, iface in _host_bindings(nodes).get(host_name, []):
        if iface.kind not in _BINDABLE_IFACE_KINDS:
            continue
        bridge = iface.bridge
        flags[bridge] = flags.get(bridge, True) and iface.flow is not False
    return flags


def _ipfix_setargs(ipfix: pt.IpfixData) -> str:
    # OVS IPFIX column=value args; only what the config set (OVS keeps
    # its own defaults for the rest). `targets` is a set-of-strings and
    # its "<ip>:<port>" value contains `:`, which OVSDB reads as the
    # map-entry separator and rejects in a set element ("unexpected \":\"
    # parsing set of strings"). Quote it so it parses as one atom; the
    # backslash survives the outer shlex quoting and is unescaped by the
    # `sh -c` both front-ends run, handing ovs-vsctl literal quotes.
    args = [f'targets=\\"{ipfix.targets}\\"']
    if ipfix.sampling is not None:
        args.append(f"sampling={int(ipfix.sampling)}")
    if ipfix.cacheActiveTimeout is not None:
        args.append(f"cache_active_timeout={int(ipfix.cacheActiveTimeout)}")
    if ipfix.cacheMaxFlows is not None:
        args.append(f"cache_max_flows={int(ipfix.cacheMaxFlows)}")
    return " ".join(args)


def _ipfix_enable_script(bridge: str, ipfix: pt.IpfixData) -> str:
    # Idempotent attach, run via `sh -c` (same argv-safe precedent as the
    # zerotier installer): REUSE this bridge's existing IPFIX row if it
    # has one (just update its columns), else create one and point the
    # bridge at it. `ovs-vsctl create IPFIX` has no --may-exist, so
    # without this a re-run would leak a row and re-bounce sampling.
    setargs = _ipfix_setargs(ipfix)
    return (
        f"ipfix=$(ovs-vsctl --if-exists get Bridge {bridge} ipfix 2>/dev/null);"
        f' if [ -n "$ipfix" ] && [ "$ipfix" != "[]" ]; then'
        f' ovs-vsctl set IPFIX "$ipfix" {setargs};'
        f" else ovs-vsctl -- --id=@ipfix create IPFIX {setargs}"
        f" -- set Bridge {bridge} ipfix=@ipfix; fi"
    )


def _emit_ipfix(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """Attach (create) or clear (delete / leg opted out) one IPFIX
    sampler per sampled bridge on this host. No-op when the host has no
    `ipfix` config or no bindable segment bridges."""
    ipfix = host_node.data.ipfix
    if ipfix is None:
        return
    flags = _ipfix_bridge_flags(host_node.key.host, nodes)
    if not flags:
        return
    emit.comment("# --- flow export (IPFIX) ---")
    for bridge in sorted(flags):
        if emit.action == "delete" or not flags[bridge]:
            # Clearing a weak ref lets OVSDB GC the now-orphaned IPFIX
            # row — this is both the delete and the "leg switched off"
            # path (an idempotent no-op when nothing was attached).
            emit.sh(["ovs-vsctl", "--if-exists", "clear", "Bridge", bridge, "ipfix"])
        else:
            emit.sh(["sh", "-c", _ipfix_enable_script(bridge, ipfix)])
    emit.blank()


# Abstract dependency -> concrete distro package (Ubuntu/Debian apt
# names). `ovn` is role-dependent (always ovn-host, plus ovn-central on
# the central) and `zerotier` is its curl installer — both handled
# specially below.
_PACKAGE_BY_DEP = {
    "ovs": "openvswitch-switch",
    "ip": "iproute2",
    "iptables": "iptables",
    "dhclient": "isc-dhcp-client",
    "dhcpcd": "dhcpcd",
    "wireguard": "wireguard-tools",
    "docker": "docker.io",
    # zerotier's generated wire script parses `zerotier-cli -j` with jq
    # (src/ir.ts's hostDependencies adds it for the same reason).
    "jq": "jq",
    # base tooling — abstract names the generator adds to every host
    # (src/ir.ts's hostDependencies), mapped here like every other dep.
    "arp": "net-tools",
    "ping": "iputils-ping",
    "traceroute": "traceroute",
    "curl": "curl",
    "python": "python-is-python3",
}


# ── the install plan ─────────────────────────────────────────────────
# Deploying a router can cut the host's own internet access while the
# pass runs. The IR already carries everything the host needs — its
# abstract `dependencies` (mapped to distro packages) and the docker
# images its units run — so the generator merges that data into ONE
# per-host install object (`INSTALL[host]` in the generated deployer).
# The host pass executes it directly, before OVS/OVN: no spool directory,
# no queue consumer, no separate service.
_APT_OS = ("ubuntu", "debian")


def _package_jobs(host_node: pt.InfraHostNode) -> tuple[list[dict[str, Any]], list[str]]:
    """One install entry per ABSTRACT dependency of this host (the IR's
    `dependencies` list, computed by src/ir.ts's hostDependencies — base
    tooling plus `ovn`/`ovs`/`ip`/`iptables`/`dhclient`/`dhcpcd`/
    `wireguard`/`zerotier`/`docker`, deliberately not distro packages),
    mapped to the host OS's packages and install command — plus a note per
    dependency that could NOT be mapped. Always resolved to a concrete OS
    (assume Ubuntu when the config leaves it unset, 2026-08-23); no
    deinstall ever happens."""
    names = host_node.data.dependencies or []
    if names and host_node.data.os.name not in _APT_OS:
        os_name = host_node.data.os.name
        return [], [f"unknown OS {os_name!r} — cannot install dependencies, skipping"]
    jobs: list[dict[str, Any]] = []
    notes: list[str] = []
    for name in names:
        job: dict[str, Any]
        if name == "zerotier":
            # Not a system package: the deployer has a dedicated
            # install-zerotier job gated on the concrete curl dependency.
            continue
        else:
            if name == "ovn":
                # EVERY OVN host runs ovn-controller (ovn-host) — a
                # central chassis included: it owns OVS bridges and
                # registers as a gateway chassis too. The central one
                # ADDITIONALLY hosts the shared NB/SB DBs + northd
                # (ovn-central). Installing only ovn-central left the
                # central with no ovn-controller, so no chassis ever
                # joined the SB DB and the whole fabric carried no
                # traffic (confirmed live 2026-09-19).
                packages = (
                    ["ovn-central", "ovn-host"]
                    if host_node.data.ovnRole == pt.OvnRole.central
                    else ["ovn-host"]
                )
            else:
                package = _PACKAGE_BY_DEP.get(name)
                if package is None:
                    notes.append(f'unknown dependency "{name}" — skipped')
                    continue
                packages = [package]
            job = {
                "kind": "package",
                "name": name,
                "packages": packages,
                "install": [
                    "env",
                    "DEBIAN_FRONTEND=noninteractive",
                    "apt-get",
                    "install",
                    "-y",
                    *packages,
                ],
                "apt": True,
            }
        jobs.append(job)
    return jobs, notes


def _image_jobs(host_id: str, nodes: list[pt.Model]) -> list[dict[str, Any]]:
    """One install entry per docker image a unit on this host runs — a
    kernel router's docker apps and the host's workload containers — with
    the Dockerfile to build it from when the IR declares a `build` (else
    it is pulled)."""
    by_name: dict[str, dict[str, Any]] = {}

    def add(image: str, build: pt.DockerBuild | None) -> None:
        by_name.setdefault(
            image,
            {
                "kind": "image",
                "image": image,
                "dockerfile": _docker_dockerfile(build) if build else None,
            },
        )

    for owner in _kernel_router_owners(host_id, nodes):
        for side_node in _kernel_router_sides(owner.key.name, nodes):
            for app in side_node.data.apps or []:
                if app.kind == "docker":
                    container = app.name or f"{owner.key.name}-docker"
                    add(app.image or f"ovn-fabric-{container}", app.build)
    for node in nodes:
        if node.kind == "kernel.app.container" and node.data.host == host_id:
            add(node.data.image, node.data.build)
    return [by_name[name] for name in sorted(by_name)]


def _install_plan(host_node: pt.InfraHostNode, nodes: list[pt.Model]) -> dict[str, Any]:
    """The one install object for this host: every package and image the
    IR says it needs, in dependency-before-image order."""
    packages, notes = _package_jobs(host_node)
    os_data = host_node.data.os
    return {
        "host": host_node.key.host,
        "os": f"{os_data.name} {os_data.version}",
        "notes": notes,
        "resolved": _dependency_resolution(host_node),
        "jobs": packages + _image_jobs(host_node.id, nodes),
    }


def _abstract(name: str) -> dict[str, str]:
    return {"kind": "abstract", "name": name}


def _docker_image(image: str) -> dict[str, str]:
    return {"kind": "docker-image", "image": image}


def _dependency_resolution(
    host_node: pt.InfraHostNode,
) -> dict[str, dict[str, Any]]:
    """The two-way table's IR side: for each abstract dependency, the
    concrete action the package resolver will run."""
    packages, _notes = _package_jobs(host_node)
    return {
        entry["name"]: (
            {"kind": "apt", "packages": entry["packages"]}
            if entry["apt"]
            else {
                "kind": "sys-cmd",
                "cmd": entry["install"],
                "requires": entry.get("requires", []),
            }
        )
        for entry in packages
    }


def _configure_dependencies(
    host_node: pt.InfraHostNode, nodes: list[pt.Model]
) -> list[dict[str, str]]:
    """Deps the configure job itself needs: OVN/OVS CLI + ip."""
    has_kernel = bool(_kernel_router_owners(host_node.id, nodes)) or any(
        n.kind == "kernel.app.container" and n.data.host == host_node.id for n in nodes
    )
    if host_node.data.ovnRole is not None:
        return [_abstract("ovn"), _abstract("ovs"), _abstract("ip")]
    if has_kernel:
        return [_abstract("ovs"), _abstract("ip")]
    return []


def _job_dependencies(
    host_node: pt.InfraHostNode, nodes: list[pt.Model]
) -> dict[str, list[dict[str, str]]]:
    """Per-job dependency descriptors, derived from the IR nodes on the
    host. Abstract package names are emitted by the package resolver;
    docker-image descriptors by the image resolver."""
    deps: dict[str, list[dict[str, str]]] = {}
    for owner in _kernel_router_owners(host_node.id, nodes):
        wanted = [_abstract("configure"), _abstract("ip"), _abstract("iptables")]
        for side in _kernel_router_sides(owner.key.name, nodes):
            for app in side.data.apps or []:
                if app.kind == "dhcp-client":
                    wanted.append(_abstract(app.style.value))
                elif app.kind == "wireguard":
                    wanted.append(_abstract("wireguard"))
                elif app.kind == "zerotier":
                    wanted.extend((_abstract("zerotier"), _abstract("jq")))
                elif app.kind == "docker":
                    wanted.append(_abstract("docker"))
                    container = app.name or f"{owner.key.name}-docker"
                    wanted.append(_docker_image(app.image or f"ovn-fabric-{container}"))
        deps[owner.key.name] = wanted
    for node in nodes:
        if node.kind != "kernel.app.container" or node.data.host != host_node.id:
            continue
        deps[node.key.name] = [
            _abstract("configure"),
            _abstract("docker"),
            _docker_image(node.data.image),
        ]
    return deps


def _has_install_plan(host_node: pt.InfraHostNode, nodes: list[pt.Model]) -> bool:
    plan = _install_plan(host_node, nodes)
    # A note (unknown OS/dependency) still belongs in the pass even when
    # there is nothing installable.
    return bool(plan["jobs"] or plan["notes"])


def _emit_install_plan(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """CREATE, at the START of the host pass: run this host's install
    object (packages first, then images). DELETE removes what an older
    deploy left of the now-gone queue (consumer, unit, spool directory)
    FIRST, so it cannot restart a unit the delete pass is tearing down."""
    if emit.action == "delete":
        emit.comment("# --- install queue (older deploys): remove ---")
        emit.sh(["systemctl", "disable", "--now", "ovn-fabric-install.service"])
        emit.sh(["rm", "-f", "/usr/local/sbin/ovn-fabric-install"])
        emit.sh(["rm", "-f", "/etc/systemd/system/ovn-fabric-install.service"])
        emit.sh(["rm", "-rf", "/var/spool/ovn-fabric/install"])
        emit.sh(["systemctl", "daemon-reload"])
        emit.blank()
        return
    plan = _install_plan(host_node, nodes)
    emit.comment(f"# --- install ({plan['os']}) ---")
    for note in plan["notes"]:
        emit.comment(f"# {note}")
    if plan["jobs"]:
        emit.install(host_node.key.host)
    emit.blank()


# The OVS bridge (`br-<shortname>`) a segment is bound to ON this host — the
# ovn.ls node's interface shortName (same bridge the segment's localnet iface
# binds into via _emit_iface_bindings). A container face's veth root leg
# attaches there, putting that NIC on the segment's L2 (2026-09-08).
def _segment_bridge(host_name: str, l2_segment: str, nodes: list[pt.Model]) -> str:
    # ovn.ls interface `host` is the bare host NAME (not the `host:` id) —
    # same convention _host_bindings/_emit_iface_bindings use.
    for ls in nodes:
        if ls.kind != "ovn.ls" or ls.id != l2_segment:
            continue
        for binding in ls.data.interfaces:
            if binding.host == host_name and binding.iface.bridge:
                return binding.iface.bridge
    raise ValueError(
        f"no ovn.ls interface on {host_name} for {l2_segment} to bind a " f"container veth to"
    )


# One NIC of a workload: (index, serviceRef, bridge, root-side leg, the
# in-container NIC name) — see _service_faces.
Face = tuple[int, pt.ServiceRef, str, str, str]


def _service_faces(service_name: str, host_name: str, nodes: list[pt.Model]) -> list[Face]:
    """One tuple per NIC of a workload: (index, serviceRef, bridge, dev,
    nic) — `dev` is the root-side leg; `nic` is the IN-CONTAINER interface
    name, already RESOLVED by the generator (ServiceRef.name — the deployer
    never shortens). Derived from every ovn.lrp that REFERENCES this
    service (`kernel.app.container:<name>`) — the endpoint carries the attachment."""
    service_id = f"kernel.app.container:{service_name}"
    base = hashlib.md5(service_name.encode()).hexdigest()[:8]
    out: list[Face] = []
    i = 0
    for lrp in nodes:
        if lrp.kind != "ovn.lrp":
            continue
        for ref in lrp.data.serviceRefs or []:
            if ref.service != service_id:
                continue
            bridge = _segment_bridge(host_name, lrp.data.l2Segment, nodes)
            out.append((i, ref, bridge, f"sc-{base}-{i}", ref.name))
            i += 1
    return out


def _emit_service_up(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
    emit: Emitter,
) -> None:
    """The `up` body: build/run the container, then for EACH NIC (from a
    referencing endpoint) move a veth into its netns, name it after the
    segment, apply that reference's ipaddrs/routes, and bind the root leg
    into the segment's OVS bridge."""
    if node.kind != "kernel.app.container":
        raise NotImplementedError(
            f"'{node.kind}' service '{node.key.name}': rendering this "
            f"workload is not implemented yet"
        )
    name = node.key.name
    container = f"ovn-fabric-{name}"
    # The workload's in-memory FILESYSTEM (Service.files -> IR): written on
    # the host and bind-mounted back READ-ONLY, one mount per file — not a
    # directory mount, so whatever else lives in that container directory
    # isn't shadowed. Host state lives under /var/lib/ovn-fabric/<service>/
    # and is removed on `down`.
    file_mounts = [
        (f"/var/lib/ovn-fabric/{name}{f.path}", f.path, f.content) for f in (node.data.files or [])
    ]
    emit.py(f"container = {container!r}")
    emit.py(f"image = {node.data.image!r}")
    for host_path, _container_path, content in file_mounts:
        parent = host_path.rsplit("/", 1)[0]
        body = "\n".join(content) if isinstance(content, list) else content
        emit.sh(["mkdir", "-p", parent])
        emit.append(host_path, body)
    if node.data.build is not None:
        emit.begin_block("if not await _docker_image_exists(image):")
        emit.py("d = tempfile.mkdtemp()")
        dockerfile = _docker_dockerfile(node.data.build)
        emit.py(
            f"await write_file({emit.label!r}, os.path.join(d, 'Dockerfile'), "
            f"{dockerfile!r}, verbose, True)"
        )
        emit.py(
            f"await run_cmd({emit.label!r}, "
            "['/usr/bin/docker', 'build', '-t', image, d], verbose, True)"
        )
        emit.py("shutil.rmtree(d)")
        emit.end_block()
    emit.sh(["/usr/bin/docker", "rm", "-f", Ref("container")], optional=True)
    # The service IS the container's command — `docker run -d` runs it
    # in the background (no sleep + docker exec dance).
    argv: list[str | Ref] = [
        "/usr/bin/docker",
        "run",
        "-d",
        "--privileged",
        "--network",
        "none",
        "--name",
        Ref("container"),
    ]
    for key, value in sorted((node.data.env or {}).items()):
        argv += ["-e", f"{key}={value}"]
    for host_path, container_path, _content in file_mounts:
        argv += ["-v", f"{host_path}:{container_path}:ro"]
    # DNS resolver (ServiceRef.resolver, resolved by the generator:
    # attachment > control-plane global > CoreDNS). Docker writes
    # /etc/resolv.conf from --dns; we never bind-mount over it. A container
    # has ONE resolv.conf, so the FIRST NIC that carries a resolver wins.
    for _i, ref, _bridge, _dev, _nic in faces:
        resolver = ref.resolver
        if resolver and resolver.nameservers:
            for server in resolver.nameservers:
                argv += ["--dns", server]
            break
    argv.append(Ref("image"))
    argv += list(node.data.cmd or [])
    emit.sh(argv)
    emit.capture("pid", ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", Ref("container")])
    emit.sh(["mkdir", "-p", "/var/run/netns"])
    emit.py('netns_link = f"/var/run/netns/{container}"')
    emit.py("if os.path.lexists(netns_link): os.unlink(netns_link)")
    emit.py('os.symlink(f"/proc/{pid}/ns/net", netns_link)')
    for i, ref, bridge, dev, nic in faces:
        peer = f"{dev}c"
        # Per-NIC routing table + a source rule: source-based policy
        # routing. Two NICs of the same workload may legitimately reach the
        # SAME destination; a separate table per NIC, entered by a rule
        # matching that NIC's own source prefix, keeps every path intact.
        table = 1000 + i
        networks = [str(ipaddress.ip_interface(a).network) for a in (ref.ipaddrs or [])]
        emit.comment(f"# --- NIC {nic} on {bridge} ---")
        add_argv: list[str] = ["ip", "link", "add", dev, "type", "veth", "peer", "name", peer]
        if ref.mac:
            add_argv += ["address", ref.mac]
        emit.sh(add_argv)
        emit.sh(["ip", "link", "set", peer, "netns", Ref("container")])
        emit.sh(["ip", "netns", "exec", Ref("container"), "ip", "link", "set", peer, "name", nic])
        emit.sh(["ip", "netns", "exec", Ref("container"), "ip", "link", "set", nic, "up"])
        for address in ref.ipaddrs or []:
            emit.sh(
                ["ip", "netns", "exec", Ref("container"), "ip", "addr", "add", address, "dev", nic]
            )
        if ref.routes:
            for route in ref.routes:
                family = "-6" if ":" in route.dst else "-4"
                hop = ["via", route.via] if route.via is not None else ["dev", nic]
                metric = [] if route.metric is None else ["metric", str(int(route.metric))]
                emit.sh(
                    [
                        "ip",
                        "netns",
                        "exec",
                        Ref("container"),
                        "ip",
                        family,
                        "route",
                        "add",
                        route.dst,
                        *hop,
                        *metric,
                        "table",
                        str(table),
                    ]
                )
                if route.dst in ("0.0.0.0/0", "::/0"):
                    emit.sh(
                        [
                            "ip",
                            "netns",
                            "exec",
                            Ref("container"),
                            "ip",
                            family,
                            "route",
                            "add",
                            route.dst,
                            *hop,
                            *metric,
                        ]
                    )
            for network in networks:
                family = "-6" if ":" in network else "-4"
                emit.sh(
                    [
                        "ip",
                        "netns",
                        "exec",
                        Ref("container"),
                        "ip",
                        family,
                        "rule",
                        "add",
                        "from",
                        network,
                        "lookup",
                        str(table),
                    ]
                )
        emit.sh(["ip", "link", "set", dev, "up"])
        emit.sh(["/usr/bin/ovs-vsctl", "add-port", bridge, dev])


def _emit_service_down(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
    emit: Emitter,
) -> None:
    """The `down` body: remove the container (which destroys its netns and
    the moved-in NICs), then its root legs and bridge ports."""
    name = node.key.name
    emit.sh(["/usr/bin/docker", "rm", "-f", f"ovn-fabric-{name}"], optional=True)
    for _i, _ref, bridge, dev, _nic in faces:
        emit.sh(["/usr/bin/ovs-vsctl", "--if-exists", "del-port", bridge, dev])
        emit.sh(["ip", "link", "delete", dev], optional=True)
    emit.sh(["rm", "-f", f"/var/run/netns/ovn-fabric-{name}"])
    emit.sh(["rm", "-rf", f"/var/lib/ovn-fabric/{name}"])


def _emit_chassis_registration(
    host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter
) -> None:
    """The OVS `external_ids` a chassis needs to register with OVN —
    emitted EARLY in the create pass (right after the packages land), so
    the just-installed `ovn-controller` has time to register while the
    heavier kernel-router/OVS/service steps below run; the cluster pass
    then resolves this chassis's runtime `system-id` by the `hostname`
    set here (see _emit_gateway_chassis_refs)."""
    data = host_node.data
    host_name = host_node.key.host
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

    # The topology's own host name (NOT the auto FQDN OVS reports): the
    # cluster pass looks this chassis up by it to get the system-id, the
    # only identifier `lrp-set-gateway-chassis` matches (2026-09-19).
    emit.sh(ovs_ops.set_external_id_argv("hostname", host_name))

    if data.encapIp is not None:
        emit.sh(ovs_ops.set_external_id_argv("ovn-encap-type", "geneve"))
        emit.sh(ovs_ops.set_external_id_argv("ovn-encap-ip", data.encapIp))

    if host_name in _gateway_eligible_hosts(nodes):
        emit.sh(ovs_ops.set_external_id_argv("ovn-cms-options", "enable-chassis-as-gw"))


def _emit_host_configure(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """The idempotent part of the host pass: chassis registration,
    interface bindings and IPFIX. The job tree's configure job."""
    host_name = host_node.key.host
    _emit_chassis_registration(host_node, nodes, emit)
    _emit_iface_bindings(host_name, nodes, emit)
    _emit_ipfix(host_node, nodes, emit)


def _emit_host(
    host_node: pt.InfraHostNode,
    nodes: list[pt.Model],
    emit: Emitter,
) -> None:
    host_name = host_node.key.host
    data = host_node.data

    if emit.action == "delete":
        # The one service goes FIRST: its tree stop runs every kernel
        # router/service `down` body before we remove the bindings.
        _emit_fabric_unit(host_node, emit)
        if data.ovnRole == pt.OvnRole.central:
            emit.comment("# central: stop exposing the shared NB/SB DBs")
            emit.sh(ovn_ops.nb_del_connection_argv())
            emit.sh(ovn_ops.sb_del_connection_argv())

        emit.sh(ovs_ops.remove_external_id_argv("ovn-remote"))
        emit.sh(ovs_ops.remove_external_id_argv("hostname"))
        if data.encapIp is not None:
            emit.sh(ovs_ops.remove_external_id_argv("ovn-encap-type"))
            emit.sh(ovs_ops.remove_external_id_argv("ovn-encap-ip"))
        if host_name in _gateway_eligible_hosts(nodes):
            emit.sh(ovs_ops.remove_external_id_argv("ovn-cms-options"))

        emit.blank()
        # Clear IPFIX BEFORE the segment bridges are torn down.
        _emit_ipfix(host_node, nodes, emit)
        _emit_iface_bindings(host_name, nodes, emit)
        return

    # Install the generated program and the ONE service that runs its
    # tree (packages, images, bindings, chassis, kernel jobs) at boot.
    _emit_fabric_unit(host_node, emit)


# The per-host body emitter — the one entry point a front-end calls (the
# Python generator hands it a PythonEmitter, the tests a trace emitter).
def _emit_host_script(
    host_node: pt.InfraHostNode,
    nodes: list[pt.Model],
    emit: Emitter,
) -> None:
    _emit_host(host_node, nodes, emit)
