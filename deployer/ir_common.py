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
import json
import pathlib
import shlex
from collections.abc import Sequence

from ladops import linux_net as linux_net_ops
from ladops import netns as netns_ops
from ladops import ovn as ovn_ops
from ladops import ovs as ovs_ops
from protocol import KernelApp
from protocol import generated as pt

from .install_queue import QUEUE_DIR, Job

Action = str  # "create" | "delete"

_BINDABLE_IFACE_KINDS = {"vlan", "physical", "veth"}


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
# call, an `enqueue()` a queue_file(...) call, each with the Python
# runtime's error/verbose policy — instead of
# the old design where the whole shell block was embedded verbatim and
# the runtime had to re-parse shell syntax (heredocs included) itself.
#
# `append(path, content)` semantics: the file ends up holding `content`
# plus a single trailing newline (the Python backend's write_file()
# writes content + "\n"; the nested _ShellBody renders a
# `cat > path << 'EOF'` heredoc, whose body always ends in a newline).
class Emitter:
    def __init__(self, action: Action) -> None:
        self.action = action
        self.lines: list[str] = []

    def sh(
        self, argv: Sequence[str | Ref], background: bool = False, optional: bool = False
    ) -> None:
        """`optional=True`: this command failing must NOT stop the rest of
        the pass — it is reported and skipped. Used for per-unit
        `systemctl enable --now` so one broken workload/router can't block
        every other operation (2026-09-16). An argv token may be a `Ref`
        (see above), rendered as a runtime variable."""
        raise NotImplementedError

    def capture(self, var: str, argv: list[str]) -> None:
        """Run `argv` and bind its trimmed stdout to the runtime variable
        `var`, for later use as a `Ref(var)` in an argv — the only way a
        command can consume a value that only exists on the target host
        (e.g. a chassis's OVS `system-id`)."""
        raise NotImplementedError

    def append(self, path: str, content: str) -> None:
        raise NotImplementedError

    def enqueue(self, directory: str, name: str, content: str) -> None:
        """Queue a file: write `content` to
        `<directory>/<timestamp>-<name>`, the timestamp being the moment
        the deployer RUNS this operation (UTC, to the microsecond, never
        the same twice in one run) — so the files of a queue sort by name
        into the order they were queued in, whatever their names say.
        The file appears under that name complete or not at all."""
        raise NotImplementedError

    def install_self(self, path: str) -> None:
        """Copy the running generated program to `path` — what the boot
        restore unit execs (Python: install_self(); the tests' trace
        renders a comment; there is no shell rendering)."""
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
        (Python: `if not skip_deps:`) — every operation emitted until the
        matching end_block() nests one level deeper. Only the Python
        front-end implements this (its generated file needs a real
        conditional for `--skip-deps`); it is only called when the caller
        asked for that guard."""
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
        self, argv: Sequence[str | Ref], background: bool = False, optional: bool = False
    ) -> None:
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
        return iface.ifaceName or f"{iface.vlanParent}.{iface.vlanId}"
    if iface.kind == "veth":
        # The ROOT-side leg — the device the transit domain's bridge
        # attaches to (the netns-side leg, `ifaceName`, is created/moved
        # by _emit_kernel_router instead).
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
    by_domain: dict[str, list[RouteNode]] = {}
    for node in routes:
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
def _emit_router(
    router: str,
    ports: list[pt.OvnLrpNode],
    routes: list[RouteNode],
    chassis_refs: dict[str, Ref],
    domain_names: dict[str, str],
    lr: pt.OvnLrNode | None,
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
        emit.capture(var, ovn_ops.sb_chassis_id_by_hostname_argv(host_name))
        emit.sh(["test", "-n", Ref(var)])
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
                f"{group.key.name} on {dev} — skipped, see deployer/ir_common.py"
            )
            continue
        iptables = "iptables" if rule.family == pt.Family.ipv4 else "ip6tables"
        argv = [iptables, "-t", "nat", "-A", "POSTROUTING", "-o", dev, "-j", "MASQUERADE"]
        emit.sh(netns_ops.netns_exec_argv(argv, netns))


def _apply_side_network(
    group: pt.SecurityGroupNode | None,
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
    if group is not None:
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


def _docker_image_ensure_lines(build: pt.DockerBuild | None, image: str) -> list[str]:
    """The shell block that builds the docker app's image on first use (when
    the app declares a `build`), so a fresh host doesn't need a manual
    `docker build`. Empty when the app uses a prebuilt image. The build
    context is a `mktemp -d` dir, removed right after — nothing is scattered
    on the host (2026-08-31)."""
    if build is None:
        return []
    return [
        f'/usr/bin/docker image inspect "{image}" >/dev/null 2>&1 || {{',
        "  d=$(mktemp -d)",
        "  cat > \"$d/Dockerfile\" <<'DOCKERFILE'",
        _docker_dockerfile(build),
        "DOCKERFILE",
        f'  /usr/bin/docker build -t "{image}" "$d" >/dev/null',
        '  rm -rf "$d"',
        "}",
    ]


def _docker_wire_script(app: pt.DockerApp, router: str, netns: str, ctx: _SideContext) -> str:
    """The wire script for one docker app (client mode): run the container
    with `--network none`, then inject one veth into the router netns
    (CNI/Multus-style) — the container's eth0 gets `app.ip`, the router
    end `app.routerIp` (the subnet's first host, resolved in
    define.ts), and the container's default route is via the router.
    `ctx` is the endpoint, handed to EVERY docker app so it can decide its
    own interface setup (2026-08-31). Ends in `exec docker wait
    <container>` — the router `up` backgrounds the script (the wait keeps
    the container's netns symlink live), and the router `down` tears it
    down with `docker rm -f`."""
    container = app.name or f"{router}-docker"
    veth = app.vethName or ("ve-" + hashlib.md5(container.encode()).hexdigest()[:8])
    if app.routerIp is None:
        # The caller only routes a docker app here once it has checked both
        # `ip` and `routerIp` (the veth-client mode).
        raise ValueError(f'kernel app docker "{container}": veth injection needs a `routerIp`')
    gateway = app.routerIp.split("/")[0]
    cmd_tokens = " ".join(shlex.quote(t) for t in (app.cmd or []))
    build_lines = _docker_image_ensure_lines(app.build, app.image)
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
            *build_lines,
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


@dataclasses.dataclass
class _SideContext:
    """The facts a kernel router SIDE carries into its apps — the target
    netns, the side's real interface `dev`, the transit veth's netns leg
    (left side) and its addressing, plus the side's own static addressing
    and security group (masq). Bundled so builders take ONE object instead
    of a long positional list (2026-08-31)."""

    netns: str
    dev: str
    transit_leg: str | None
    group: pt.SecurityGroupNode | None
    ipaddrs: list[str]
    routes: list[pt.KernelRouterRoute]
    transit_ipaddrs: list[str]
    transit_routes: list[pt.KernelRouterRoute]


def _docker_inject_script(app: pt.DockerApp, container: str, ctx: _SideContext) -> str:
    """The wire script for a docker app that OWNS the router's interfaces
    (signaled in the IR by the ABSENCE of veth addressing — the "container
    IS the router" mode): the container OWNS the router's
    interfaces — the transit veth's netns-side leg (`ctx.transit_leg`, the
    mesh uplink, with its own addressing) AND the side's real interface
    `ctx.dev` — both moved in from the router netns (an interface lives in
    exactly one netns). The static addressing (addresses + routes) and
    security-group masq rules are applied to the CONTAINER netns from
    OUTSIDE via _apply_side_network (the SAME code the router netns uses,
    only the target namespace differs — a device's addresses don't
    reliably carry across a netns move, confirmed live 2026-08-31). The
    app's `cmd` then runs against the real WAN device (dhcpcd for the
    dhcp-client service, which resolves to this docker form). The image is
    auto-built on first use when `app.build` is set; the container runs
    `--privileged` (NET_ADMIN) so `cmd` can configure the moved-in
    interface (2026-08-31)."""
    netns = ctx.netns
    dev = ctx.dev
    transit_leg = ctx.transit_leg
    image = app.image or f"ovn-fabric-{container}"
    cmd_tokens = " ".join(shlex.quote(t) for t in (app.cmd or ["sh"]))
    build_lines = _docker_image_ensure_lines(app.build, image)
    move_lines: list[str] = []
    if transit_leg:
        move_lines += [
            'ip netns exec "$netns" ip link set "$transit_leg" netns "$container"',
            'ip netns exec "$container" ip link set "$transit_leg" up',
        ]
    move_lines += [
        'ip netns exec "$netns" ip link set "$dev" netns "$container"',
        'ip netns exec "$container" ip link set "$dev" up',
    ]
    # A kernel router IS a router: enable IPv4/IPv6 forwarding in the
    # container's netns, same as the router netns path (2026-08-31).
    sysctl_lines = [
        'ip netns exec "$container" sysctl -w net.ipv4.ip_forward=1',
        'ip netns exec "$container" sysctl -w net.ipv6.conf.all.forwarding=1',
    ]
    # The static addressing + masq — the SAME helper the router netns path
    # uses, only targeting the container's netns: the transit veth's own
    # addresses/routes (left side) on `transit_leg`, then the side's on `dev`.
    body = _ShellBody("create")
    if transit_leg:
        _apply_side_network(
            None, ctx.transit_ipaddrs, ctx.transit_routes, container, transit_leg, body
        )
    _apply_side_network(ctx.group, ctx.ipaddrs, ctx.routes, container, dev, body)
    return "\n".join(
        [
            "#!/bin/sh",
            f"# ovn-fabric kernel app docker (owns the interfaces): {dev} in {netns}",
            f"# (container {container}). Backgrounded by the router up.",
            "set -eu",
            f'container="{container}"',
            f'netns="{netns}"',
            f'dev="{dev}"',
            f'transit_leg="{transit_leg or ""}"',
            f'image="{image}"',
            "",
            *build_lines,
            '/usr/bin/docker rm -f "$container" 2>/dev/null || true',
            # --privileged so the container can move/config the interfaces
            # and apply iptables (NET_ADMIN); its own netns is still
            # `--network none` — it only owns what we move in.
            "/usr/bin/docker run -d --privileged --network none "
            '--name "$container" "$image" sleep infinity',
            "pid=$(/usr/bin/docker inspect -f '{{.State.Pid}}' \"$container\")",
            "mkdir -p /var/run/netns",
            'ln -sf "/proc/$pid/ns/net" "/var/run/netns/$container"',
            # Move the router's interfaces (transit veth leg + real iface)
            # out of the router netns and into the container (one interface,
            # one netns).
            *move_lines,
            # Enable forwarding in the container netns (it's the router).
            *sysctl_lines,
            # The side's static addressing + masq, applied from outside to
            # the container netns (same code as the router netns path).
            *body.lines,
            # The app's cmd runs INSIDE the container (docker-managed)
            # against the moved-in real interface.
            f'/usr/bin/docker exec "$container" {cmd_tokens} "$dev"',
            'exec /usr/bin/docker wait "$container"',
        ]
    )


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


def _zerotier_wire_script(app: pt.ZerotierApp, netns: str) -> str:
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
    apps: list[KernelApp], router: str, emit: Emitter, ctx: _SideContext
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
                    emit.sh(netns_ops.netns_exec_argv(client_argv, netns), background=True)
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
            # A docker that OWNS the router's interfaces ("container IS the
            # router" mode, dhcpcd-in-docker) carries NO veth addressing in
            # the IR — that structural fact (no `ip`) is how the TS side
            # says "own the interfaces", so this is decided here, not by a
            # TS concept name (2026-08-31).
            if app.ip is None:
                script_path = f"/usr/local/sbin/ovn-kernel-{router}-docker-inject.sh"
                emit.comment(
                    f"# --- kernel app: docker (owns the interfaces) ({container}) "
                    f"on {dev} in {netns} ---"
                )
                if emit.action == "create":
                    emit.append(script_path, _docker_inject_script(app, container, ctx))
                    emit.sh(["/bin/sh", script_path], background=True)
                else:
                    emit.sh(["/usr/bin/docker", "rm", "-f", container])
                    emit.sh(["rm", "-f", script_path])
                    emit.sh(["rm", "-f", f"/var/run/netns/{container}"])
                continue
            # Else: the client mode — one veth injected into the router netns
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
            script_path = f"/usr/local/sbin/ovn-kernel-{router}-docker-wire.sh"
            if app.ip is None or app.routerIp is None:
                raise ValueError(
                    f'kernel app docker "{container}": the veth injection needs an `ip` '
                    "(the container's address on the router's services segment)"
                )
            emit.comment(f"# --- kernel app: docker ({container}) on {dev} in {netns} ---")
            if emit.action == "create":
                emit.append(script_path, _docker_wire_script(app, router, netns, ctx))
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
                        group=(
                            _security_group_by_name(side_node.data.securityGroup, nodes)
                            if side_node.data.securityGroup is not None
                            else None
                        ),
                        ipaddrs=list(side_node.data.ipaddrs or []),
                        routes=list(side_node.data.routes or []),
                        transit_ipaddrs=transit_ipaddrs,
                        transit_routes=transit_routes,
                    ),
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
                emit.sh(linux_net_ops.add_vlan_argv(iface.vlanParent, dev, iface.vlanId))
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
        # The side's static addressing + security-group (masq) rules,
        # applied inside the netns — SAME code as the dhcpcd-in-docker
        # container path (only the target namespace differs).
        group = (
            _security_group_by_name(side_node.data.securityGroup, nodes)
            if side_node.data.securityGroup is not None
            else None
        )
        if side_node.data.securityGroup is not None and group is None:
            raise ValueError(
                f"kernel router {owner.key.name} ({_side(side_node).value}) "
                f'references security group "{side_node.data.securityGroup}" '
                f"but no such security.group node"
            )
        _apply_side_network(
            group,
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
                    group=(
                        _security_group_by_name(side_node.data.securityGroup, nodes)
                        if side_node.data.securityGroup is not None
                        else None
                    ),
                    ipaddrs=list(side_node.data.ipaddrs or []),
                    routes=list(side_node.data.routes or []),
                    transit_ipaddrs=transit_ipaddrs,
                    transit_routes=transit_routes,
                ),
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


def _kernel_router_unit(router: str, script_path: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric kernel router {router}",
            # Bindings first, then the restore pass (which re-applies
            # everything else), then this unit — which creates (and ups)
            # the transit veths.
            "After=network-pre.target ovn-fabric-bindings.service" " ovn-fabric-restore.service",
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
        unit = _kernel_router_unit_name(router)
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
        # Optional: one broken router unit must not stop the rest of the
        # deploy (it is reported and the pass continues). The boot
        # restore pass only enables — systemd starts it after the restore.
        emit.enable_unit(unit, optional=True)
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
def _emit_iface_bindings(
    host_name: str, nodes: list[pt.Model], emit: Emitter, boot: bool = False
) -> None:
    """`boot=True` renders the body of the per-host bindings unit
    (ovn-fabric-bindings.service), which runs BEFORE the kernel
    router/service units. The transit veths do not exist yet at that
    point — their own units create them (and up the root leg themselves,
    see _emit_kernel_router_owner), so the `veth` case here only ensures
    the OVS port row and skips `ip link set` on a device that is not
    there yet."""
    before = len(emit.lines)
    bindings = _host_bindings(nodes).get(host_name, [])
    if emit.action == "create":
        mappings: list[str] = []
        for domain, iface in bindings:
            if iface.kind not in _BINDABLE_IFACE_KINDS:
                emit.comment(
                    f'# unsupported interface kind "{iface.kind}" for domain '
                    f"{domain} on {host_name} — skipped, see deployer/ir_common.py"
                )
                continue
            real_name = _iface_real_name(iface)
            bridge = iface.shortName
            emit.comment(f"# --- interface: {domain} on {host_name} ({real_name}) ---")
            if iface.kind == "vlan":
                emit.sh(linux_net_ops.ensure_vlan_argv(iface.vlanParent, real_name, iface.vlanId))
                emit.sh(linux_net_ops.set_link_up_argv(real_name))
            elif iface.kind == "veth" and not boot:
                # The ROOT-side leg of the kernel router's transit veth —
                # the kernel router block brings the netns-side leg up,
                # but the peer stays DOWN in root until told otherwise
                # (confirmed live, 2026-08-19: `ip l | grep 9adb1d`
                # showed state DOWN until `ip l set dev veth-ovn-... up`),
                # so bring it up before it becomes the bridge's port.
                # At boot the router unit owns it (and creates it), so
                # there is nothing to up here yet.
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
            if iface.kind not in _BINDABLE_IFACE_KINDS:
                continue
            any_bound = True
            real_name = _iface_real_name(iface)
            bridge = iface.shortName
            emit.comment(f"# --- interface: {domain} on {host_name} ({real_name}) ---")
            emit.sh(ovs_ops.del_br_argv(bridge))
            if iface.kind == "vlan":
                emit.sh(linux_net_ops.delete_link_argv(real_name))
        if any_bound:
            emit.sh(ovs_ops.remove_external_id_argv("ovn-bridge-mappings"))
    if len(emit.lines) > before:
        emit.blank()


# ── boot-time interface bindings ──────────────────────────────────────
# _emit_iface_bindings above creates the real segment interfaces (VLAN
# sub-interfaces, the transit veths' root legs) and their OVS
# bridges/ports — but only in the deploy pass. OVS's own rows survive a
# reboot (conf.db); the NETDEVICES do not, so after a reboot every VLAN
# port in every segment bridge read "could not open network device
# eth0.<vlan> (No such device)" and the segments had no uplink
# (confirmed live, 2026-10-20). This unit re-runs the same (idempotent)
# create body at boot, ordered after OVS and before the kernel
# router/service units (which declare After= this unit).
_BINDINGS_SCRIPT = "/usr/local/sbin/ovn-fabric-bindings.sh"
_BINDINGS_UNIT = "ovn-fabric-bindings.service"
_BINDINGS_UNIT_PATH = f"/etc/systemd/system/{_BINDINGS_UNIT}"


def _bindings_script(host_name: str, nodes: list[pt.Model]) -> str:
    body = _ShellBody("create")
    _emit_iface_bindings(host_name, nodes, body, boot=True)
    return "\n".join(
        [
            "#!/bin/sh",
            f"# ovn-fabric host bindings ({host_name}): recreate the real",
            "# segment interfaces (VLAN sub-interfaces) and their OVS",
            "# bridges/ports at boot. OVS keeps its rows across a reboot, the",
            "# netdevices they reference do not. The kernel routers' transit",
            "# veths are created by their own units, which run AFTER this one.",
            "# Idempotent — re-running is always safe.",
            "set -eu",
            *body.lines,
        ]
    ).rstrip("\n")


def _bindings_unit(host_name: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric host bindings ({host_name})",
            # The OVS daemons own the datapath these devices attach to;
            # network-pre.target alone would race them (2026-10-20).
            "After=network-pre.target ovsdb-server.service ovs-vswitchd.service",
            "Wants=network-pre.target ovsdb-server.service ovs-vswitchd.service",
            "",
            "[Service]",
            "Type=oneshot",
            f"ExecStart={_BINDINGS_SCRIPT}",
            "RemainAfterExit=yes",
            "TimeoutStartSec=60",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ]
    )


def _emit_bindings_unit(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """The per-host bindings unit. CREATE installs it (enabled, not
    started — the deploy pass applies the bindings inline right after);
    DELETE removes it BEFORE the bridges are torn down."""
    host_name = host_node.key.host
    bindable = any(
        iface.kind in _BINDABLE_IFACE_KINDS
        for _domain, iface in _host_bindings(nodes).get(host_name, [])
    )
    if not bindable:
        return
    emit.comment(f"# --- host bindings service: {_BINDINGS_UNIT} ---")
    if emit.action == "delete":
        emit.sh(["systemctl", "disable", "--now", _BINDINGS_UNIT], optional=True)
        emit.sh(["rm", "-f", _BINDINGS_SCRIPT])
        emit.sh(["rm", "-f", _BINDINGS_UNIT_PATH])
        emit.sh(["systemctl", "daemon-reload"])
        emit.blank()
        return
    emit.append(_BINDINGS_SCRIPT, _bindings_script(host_name, nodes))
    emit.sh(["chmod", "+x", _BINDINGS_SCRIPT])
    emit.append(_BINDINGS_UNIT_PATH, _bindings_unit(host_name))
    emit.sh(["systemctl", "daemon-reload"])
    emit.sh(["systemctl", "enable", _BINDINGS_UNIT], optional=True)
    emit.blank()


# ── boot-time full restore ────────────────────────────────────────────
# The bindings unit restores today's known gap, but the create pass is
# still the source of truth for everything else (chassis external-ids,
# OVS/OVN settings, kernel routers/workloads). Installing the generated
# program on the host and re-running the host pass at boot makes that
# true on the host too, so a future create-pass step is restored for
# free instead of needing its own unit (2026-10-20).
_RESTORE_SCRIPT = "/usr/local/sbin/ovn-fabric-deployer.py"
_RESTORE_UNIT = "ovn-fabric-restore.service"
_RESTORE_UNIT_PATH = f"/etc/systemd/system/{_RESTORE_UNIT}"


def _restore_unit(host_name: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric boot restore ({host_name})",
            # Bindings first: it recreates the netdevices the host pass
            # would otherwise have to race. The fabric's own units are
            # NOT ordered after this one — the pass itself starts them,
            # and After= here would deadlock (`enable --now` waits for a
            # unit that waits for this one).
            "After=network-pre.target ovsdb-server.service ovs-vswitchd.service"
            " ovn-fabric-bindings.service",
            "Wants=network-pre.target ovsdb-server.service ovs-vswitchd.service",
            "",
            "[Service]",
            "Type=oneshot",
            # A run from stdin has no file to install — skip cleanly
            # instead of failing the boot.
            f"ExecCondition=/usr/bin/test -x {_RESTORE_SCRIPT}",
            # --skip-deps: packages/images/units are the queue's job (its
            # service is already enabled and keeps its own state).
            # --no-start: the fabric units are ordered After= this unit;
            # starting them from inside it would deadlock, so this pass
            # only writes and enables them and systemd starts them next.
            f"ExecStart=/usr/bin/python3 {_RESTORE_SCRIPT} --host {host_name} "
            "--skip-deps --no-start",
            "TimeoutStartSec=300",
            "RemainAfterExit=yes",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ]
    )


def _emit_restore_unit(host_node: pt.InfraHostNode, emit: Emitter) -> None:
    """CREATE installs the running generated program as _RESTORE_SCRIPT
    and enables the unit that re-runs its host pass at boot. DELETE
    disables it FIRST (it must not re-create what the delete pass tears
    down) and removes both files."""
    host_name = host_node.key.host
    emit.comment(f"# --- boot restore service: {_RESTORE_UNIT} ---")
    if emit.action == "delete":
        emit.sh(["systemctl", "disable", "--now", _RESTORE_UNIT], optional=True)
        emit.sh(["rm", "-f", _RESTORE_UNIT_PATH])
        emit.sh(["rm", "-f", _RESTORE_SCRIPT])
        emit.sh(["systemctl", "daemon-reload"])
        emit.blank()
        return
    emit.install_self(_RESTORE_SCRIPT)
    emit.append(_RESTORE_UNIT_PATH, _restore_unit(host_name))
    emit.sh(["systemctl", "daemon-reload"])
    emit.sh(["systemctl", "enable", _RESTORE_UNIT], optional=True)
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
        bridge = iface.shortName
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
}


# ── the install queue ────────────────────────────────────────────────
# Deploying a router can cut the host's own internet access while the
# pass runs, so nothing that needs the network is done inline. The create
# pass QUEUES what the host needs instead — one JSON job file per package,
# docker image and unit under _QUEUE_DIR, each prefixed with the time the
# deployer queued it (Emitter.enqueue) — and installs the consumer of
# that queue (deployer/install_queue.py, copied to the host unchanged)
# with its systemd service. The service polls the directory, works on the
# files in name order — the order they were queued in — and removes a
# job's file once the job is done; see install_queue.py's own header for
# the file format and the retry rules.
_QUEUE_DIR = QUEUE_DIR
_INSTALLER = "/usr/local/sbin/ovn-fabric-install"
_INSTALLER_UNIT = "ovn-fabric-install.service"
_INSTALLER_UNIT_PATH = f"/etc/systemd/system/{_INSTALLER_UNIT}"
# The host OSes whose packages are known (both install with apt).
_APT_OS = ("ubuntu", "debian")

# One queue file: its name WITHOUT the timestamp the deployer puts in
# front of it at runtime (see _queue_name) and the job it holds. A list of
# them is queued in list order.
QueueFile = tuple[str, Job]


def _kernel_router_unit_name(router: str) -> str:
    return f"ovn-kernel-{router}.service"


def _kernel_service_unit_name(name: str) -> str:
    return f"ovn-kernel-service-{name}.service"


def _queue_name(kind: str, name: str) -> str:
    """`<kind>-<name>.json` — what a queue file is called after its
    timestamp. Only there for whoever lists the directory: the consumer
    takes the order from the timestamp and everything else from the
    content."""
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)
    return f"{kind}-{safe}.json"


def _package_jobs(host_node: pt.InfraHostNode) -> tuple[list[QueueFile], list[str]]:
    """One job per ABSTRACT dependency of this host (the IR's
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
    jobs: list[QueueFile] = []
    notes: list[str] = []
    for name in names:
        job: Job
        if name == "zerotier":
            # Not in the distro's own repositories: ZeroTier's installer
            # adds its repository and installs the `zerotier-one` package.
            job = {
                "kind": "package",
                "name": name,
                "packages": ["zerotier-one"],
                "install": ["sh", "-c", "curl -s https://install.zerotier.com | bash"],
                "apt": False,
            }
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
        jobs.append((_queue_name("package", name), job))
    return jobs, notes


def _image_jobs(host_id: str, nodes: list[pt.Model]) -> list[QueueFile]:
    """One job per docker image a unit on this host runs — a kernel
    router's docker apps and the host's workload containers — with the
    Dockerfile to build it from when the IR declares a `build` (else it is
    pulled). The units' own `up` scripts still build/pull on first use;
    queueing the images lets the consumer provide them BEFORE it restarts
    a unit."""
    by_name: dict[str, Job] = {}

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
    return [(_queue_name("image", name), by_name[name]) for name in sorted(by_name)]


def _unit_jobs(host_id: str, nodes: list[pt.Model]) -> list[QueueFile]:
    """One job per systemd unit of the fabric on this host, in the order
    the create pass starts them: a unit that could not come up (no image,
    no tool, no internet) is the consumer's to restart once what it lacked
    is there."""
    units = [_kernel_router_unit_name(o.key.name) for o in _kernel_router_owners(host_id, nodes)]
    units += [
        _kernel_service_unit_name(n.key.name)
        for n in nodes
        if n.kind in ("kernel.app.container", "kernel.app.service") and n.data.host == host_id
    ]
    return [(_queue_name("unit", unit), {"kind": "unit", "unit": unit}) for unit in units]


def _install_queue_consumer() -> str:
    """deployer/install_queue.py's own source — the installed consumer IS
    that tested, type-checked file, not a second implementation in a
    string."""
    source = pathlib.Path(__file__).with_name("install_queue.py").read_text(encoding="utf-8")
    return source.rstrip("\n")


def _install_queue_unit() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=ovn-fabric install queue (packages, docker images, units to restart)",
            # It waits for the internet itself (install_queue.online) —
            # so no network-online.target. After the restore pass, so it
            # never restarts a unit while the pass is rewriting it.
            "After=network-pre.target ovn-fabric-restore.service",
            "Wants=network-pre.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={_INSTALLER}",
            "Restart=on-failure",
            "RestartSec=10",
            # Stopping the service stops the polling loop only: an
            # apt-get/docker command it already started is left to finish
            # (a killed dpkg leaves the package database half-configured).
            "KillMode=process",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ]
    )


def _emit_queue_files(files: list[QueueFile], emit: Emitter) -> None:
    for name, job in files:
        emit.enqueue(_QUEUE_DIR, name, json.dumps(job, indent=2))


def _has_install_queue(host_node: pt.InfraHostNode, nodes: list[pt.Model]) -> bool:
    """A host with no package, no image and no unit of its own has nothing
    to queue."""
    return bool(
        _package_jobs(host_node)[0]
        or _image_jobs(host_node.id, nodes)
        or _unit_jobs(host_node.id, nodes)
    )


def _emit_install_queue(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """CREATE, at the START of the host pass: install the consumer and its
    unit, queue this host's packages, then its images, run the consumer once in
    the foreground (`--once`: what is missing is installed IF the host is
    online; offline it returns at once and the pass goes on with what the
    host has), then (re)start the service, which polls the queue from then
    on. The consumer steps are optional — a host that cannot install
    right now must not stop the deploy; a tool the pass itself needs and
    does not find fails at the command that needs it.
    DELETE: stop and remove it FIRST, queue included, so it cannot restart
    a unit the delete pass is tearing down."""
    if emit.action == "delete":
        emit.comment("# --- install queue: remove ---")
        emit.sh(["systemctl", "disable", "--now", _INSTALLER_UNIT])
        emit.sh(["rm", "-f", _INSTALLER])
        emit.sh(["rm", "-f", _INSTALLER_UNIT_PATH])
        emit.sh(["rm", "-rf", _QUEUE_DIR])
        emit.sh(["systemctl", "daemon-reload"])
        emit.blank()
        return
    packages, notes = _package_jobs(host_node)
    os_data = host_node.data.os
    emit.comment(f"# --- install queue ({os_data.name} {os_data.version}) ---")
    for note in notes:
        emit.comment(f"# {note}")
    emit.append(_INSTALLER, _install_queue_consumer())
    emit.sh(["chmod", "+x", _INSTALLER])
    emit.append(_INSTALLER_UNIT_PATH, _install_queue_unit())
    emit.sh(["systemctl", "daemon-reload"])
    # The queue is THIS deploy's: whatever an earlier one left is dropped.
    emit.sh(["rm", "-rf", _QUEUE_DIR])
    emit.sh(["mkdir", "-p", _QUEUE_DIR])
    _emit_queue_files(packages + _image_jobs(host_node.id, nodes), emit)
    # A consumer an earlier deploy left running is NOT stopped first (a
    # create stops nothing; delete does). Until the restart below it may
    # work on the queue side by side with `--once` — accepted: deploys
    # are rare, and a job tried twice at once is just a failed try.
    emit.sh([_INSTALLER, "--once"], optional=True)
    # `restart`, not `enable --now`: a consumer that is already running
    # must be replaced by the one just written; one that is not is started.
    emit.sh(["systemctl", "enable", _INSTALLER_UNIT], optional=True)
    emit.sh(["systemctl", "restart", _INSTALLER_UNIT], optional=True)
    emit.blank()


def _emit_unit_queue(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """The END of a host's create pass: the fabric's units exist now, so
    queue them — behind the packages and images, which were queued when
    the pass began. The running consumer finds the files on its next poll
    — nothing has to be started — drops the job of every unit that is up
    and restarts one that is not, once its packages and images are in."""
    units = _unit_jobs(host_node.id, nodes)
    if not units:
        return
    emit.comment("# --- install queue: the units just started ---")
    _emit_queue_files(units, emit)
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
            if binding.host == host_name and binding.iface.shortName:
                return binding.iface.shortName
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


def _service_up_lines(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
) -> list[str]:
    """The `up` body: build/run the container, then for EACH NIC (from a
    referencing endpoint) move a veth into its netns, name it after the
    segment, apply that reference's ipaddrs/routes, and bind the root leg
    into the segment's OVS bridge."""
    if node.kind != "kernel.app.container":
        raise NotImplementedError(
            f"'{node.kind}' service '{node.key.name}': rendering this "
            f"workload is not implemented yet"
        )
    cmd_tokens = " ".join(shlex.quote(t) for t in (node.data.cmd or []))
    # The workload's in-memory FILESYSTEM (Service.files -> IR): written on
    # the host and bind-mounted back READ-ONLY, one mount per file — not a
    # directory mount, so whatever else lives in that container directory
    # isn't shadowed. Host state lives under /var/lib/ovn-fabric/<service>/
    # and is removed on `down`.
    file_mounts = [
        (
            f"/var/lib/ovn-fabric/{node.key.name}{f.path}",
            f.path,
            f.content,
        )
        for f in (node.data.files or [])
    ]
    file_lines: list[str] = []
    for host_path, _container_path, content in file_mounts:
        parent = host_path.rsplit("/", 1)[0]
        # content is a string OR its lines (IR-side, for better diffs) —
        # the heredoc body is the joined lines either way.
        body = "\n".join(content) if isinstance(content, list) else content
        file_lines += [
            f"mkdir -p {shlex.quote(parent)}",
            # Delimiter distinct from the outer emitters' heredoc markers.
            f"cat > {shlex.quote(host_path)} << 'OVN_FILE'",
            body,
            "OVN_FILE",
        ]
    mount_args = "".join(f' -v "{host}:{cpath}:ro"' for host, cpath, _ in file_mounts)
    # Environment variables (Service.env -> IR), one -e per entry, keys
    # sorted so the emitted command is stable.
    env_args = "".join(
        f" -e {shlex.quote(f'{k}={v}')}" for k, v in sorted((node.data.env or {}).items())
    )
    # DNS resolver (ServiceRef.resolver, resolved by the generator:
    # attachment > control-plane global > CoreDNS). Docker writes
    # /etc/resolv.conf from --dns; we never bind-mount over it. A container
    # has ONE resolv.conf, so the FIRST NIC that carries a resolver wins
    # (2026-09-19).
    dns_args = ""
    for _, ref, _, _, _ in faces:
        resolver = ref.resolver
        if resolver and resolver.nameservers:
            dns_args = "".join(f" --dns {shlex.quote(ns)}" for ns in resolver.nameservers)
            break
    lines = [
        f'container="ovn-fabric-{node.key.name}"',
        f'image="{node.data.image}"',
        "",
        *file_lines,
        *_docker_image_ensure_lines(node.data.build, node.data.image),
        '/usr/bin/docker rm -f "$container" 2>/dev/null || true',
        # The service IS the container's command — `docker run -d` runs it
        # in the background (no sleep + docker exec dance).
        "/usr/bin/docker run -d --privileged --network none --name "
        f'"$container"{env_args}{mount_args}{dns_args} "$image" {cmd_tokens}'.rstrip(),
        "pid=$(/usr/bin/docker inspect -f '{{.State.Pid}}' \"$container\")",
        "mkdir -p /var/run/netns",
        'ln -sf "/proc/$pid/ns/net" "/var/run/netns/$container"',
    ]
    for i, ref, bridge, dev, nic in faces:
        peer = f"{dev}c"
        # Per-NIC routing table + a source rule: source-based policy
        # routing. Two NICs of the same workload may legitimately reach the
        # SAME destination (e.g. both ControlPlane-Route and Zerotier-route
        # resolve 192.168.129.0/24). In one table the second `ip route add`
        # fails with EEXIST — which under `set -e` aborted the rest of the
        # wiring (hit live 2026-09-16) — and `ip route replace` would
        # instead silently re-home an existing (possibly NAT'd) route. A
        # separate table per NIC, entered by a rule matching that NIC's own
        # source prefix, keeps every path intact and picks the one that
        # belongs to the source address. 1000+ stays clear of the kernel's
        # reserved table ids (and tables are per-netns, so they don't
        # collide across containers).
        table = 1000 + i
        networks = [str(ipaddress.ip_interface(a).network) for a in (ref.ipaddrs or [])]
        # The container-side leg's MAC, resolved by the generator
        # (ServiceRef.mac): the container is recreated on every `up`, and
        # with a random MAC the segment's OVN router would keep a
        # MAC_Binding for the PREVIOUS NIC (2026-10-11). `address` after
        # `peer name` applies to the peer leg. Absent in an older IR ->
        # the kernel's random MAC, as before.
        peer_mac = f" address {shlex.quote(ref.mac)}" if ref.mac else ""
        lines += [
            f"# --- NIC {nic} on {bridge} ---",
            f'ip link add "{dev}" type veth peer name "{peer}"{peer_mac}',
            f'ip link set "{peer}" netns "$container"',
            f'ip netns exec "$container" ip link set "{peer}" name "{nic}"',
            f'ip netns exec "$container" ip link set "{nic}" up',
        ]
        lines += [
            f'ip netns exec "$container" ip addr add {a} dev "{nic}"' for a in (ref.ipaddrs or [])
        ]
        if ref.routes:
            # `ip route`/`ip rule` default to the IPv4 family, so an IPv6
            # destination/source needs an explicit `ip -6` — otherwise the
            # kernel rejects it ("Invalid source address") and `set -e`
            # aborts the rest of the wiring (hit live 2026-09-16).
            for r in ref.routes:
                fam = "-6" if ":" in r.dst else "-4"
                hop = f"via {r.via}" if r.via is not None else f'dev "{nic}"'
                # The GENERATOR resolves the metric (route > attachment >
                # domain > execution order) and writes it onto the route, so
                # the deployer only applies what the IR carries (2026-09-19).
                metric = "" if r.metric is None else f" metric {int(r.metric)}"
                lines.append(
                    f'ip netns exec "$container" ip {fam} route add {r.dst} {hop}'
                    f"{metric} table {table}"
                )
                # A DEFAULT also goes into `main`: the source-based rules
                # above only match once a source is chosen, and the kernel
                # picks that source from `main` — without a default there,
                # locally-originated traffic (dnsmasq→1.1.1.1, coredns→
                # upstream) is simply "Network is unreachable" even when a
                # per-NIC table has a default (confirmed live 2026-09-19).
                # Across NICs the lowest metric wins; source-bound traffic
                # still hits its own table via the rules.
                if r.dst in ("0.0.0.0/0", "::/0"):
                    lines.append(
                        f'ip netns exec "$container" ip {fam} route add {r.dst} {hop}{metric}'
                    )
            lines += [
                f'ip netns exec "$container" ip '
                f'{"-6" if ":" in n else "-4"} rule add from {n} lookup {table}'
                for n in networks
            ]
        lines += [
            f'ip link set "{dev}" up',
            f'/usr/bin/ovs-vsctl add-port "{bridge}" "{dev}"',
        ]
    return lines


def _service_down_lines(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
) -> list[str]:
    """The `down` body: remove the container (which destroys its netns and
    the moved-in NICs), then its root legs and bridge ports."""
    lines = [f'/usr/bin/docker rm -f "ovn-fabric-{node.key.name}" 2>/dev/null || true']
    for _i, _ref, bridge, dev, _nic in faces:
        lines += [
            f'/usr/bin/ovs-vsctl --if-exists del-port "{bridge}" "{dev}"',
            f'ip link delete "{dev}" 2>/dev/null || true',
        ]
    lines.append(f'rm -f "/var/run/netns/ovn-fabric-{node.key.name}"')
    # The host-side filesystem written by `up` (bind-mounted read-only).
    lines.append(f'rm -rf "/var/lib/ovn-fabric/{node.key.name}"')
    return lines


def _service_script(name: str, up_lines: list[str], down_lines: list[str]) -> str:
    """One self-contained per-service script: `up` (run + wire) and `down`
    (stop + unwire). Case labels/bodies at column 0 so nested heredocs (the
    Dockerfile for the image build) keep their delimiters unindented.
    `down` drops `-e` (a stop must not abort on a missing object); `up`
    re-enables it. Same shape as the kernel-router script (2026-09-08)."""
    lines = [
        "#!/bin/sh",
        f"# ovn-fabric kernel.service {name}: 'up' runs the container and",
        "# wires its NICs; 'down' stops/removes it. One systemd unit per",
        "# service; ExecStart=<script> up / ExecStop=<script> down.",
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


def _service_unit(name: str, script_path: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric kernel service {name}",
            # A container's veth attaches to a segment bridge, which the
            # bindings unit and the restore pass (re)create first.
            "After=network-pre.target ovn-fabric-bindings.service" " ovn-fabric-restore.service",
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


def _emit_kernel_services(host_id: str, nodes: list[pt.Model], emit: Emitter) -> None:
    """One systemd unit per kernel.service this host owns: CREATE writes the
    up/down script + unit and `systemctl enable --now` (builds the image,
    runs the container, wires one veth per REFERENCING endpoint); DELETE
    `systemctl disable --now` (runs `down`), then removes the files. Same
    mechanics as the kernel router. Runs AFTER _emit_iface_bindings so the
    segment bridges already exist (2026-09-08)."""
    host_name = host_id.split(":", 1)[1]
    services = [
        n
        for n in nodes
        if n.kind in ("kernel.app.container", "kernel.app.service") and n.data.host == host_id
    ]
    for node in services:
        name = node.key.name
        faces = _service_faces(name, host_name, nodes)
        script_path = f"/usr/local/sbin/ovn-kernel-service-{name}.sh"
        unit = _kernel_service_unit_name(name)
        unit_path = f"/etc/systemd/system/{unit}"
        emit.comment(f"# --- kernel.service: {name} ({len(faces)} NIC(s)) ---")
        if emit.action == "delete":
            emit.sh(["systemctl", "disable", "--now", unit])
            emit.sh(["rm", "-f", script_path])
            emit.sh(["rm", "-f", unit_path])
            emit.sh(["systemctl", "daemon-reload"])
            continue
        emit.append(
            script_path,
            _service_script(name, _service_up_lines(node, faces), _service_down_lines(node, faces)),
        )
        # ExecStart runs the script directly — chmod +x or systemd 203/EXEC.
        emit.sh(["chmod", "+x", script_path])
        emit.append(unit_path, _service_unit(name, script_path))
        emit.sh(["systemctl", "daemon-reload"])
        # Optional: a non-working workload must not block every other
        # operation — the failure is reported and the deploy continues to
        # the next service (2026-09-16). The boot restore pass only
        # enables; systemd starts it after the restore.
        emit.enable_unit(unit, optional=True)


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


def _emit_host(
    host_node: pt.InfraHostNode,
    nodes: list[pt.Model],
    emit: Emitter,
    guard_deps: bool = False,
) -> None:
    host_name = host_node.key.host
    data = host_node.data

    has_queue = _has_install_queue(host_node, nodes)

    if emit.action == "delete":
        # The install queue goes FIRST: its consumer must not restart a
        # unit this pass is about to tear down.
        if has_queue:
            _emit_install_queue(host_node, nodes, emit)
        # Likewise the boot restore unit: it re-runs the create pass.
        _emit_restore_unit(host_node, emit)
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
        # The kernel router's netns is removed FIRST — every device it
        # owns (the transit veth's netns leg, the moved-in WAN vlan)
        # lives inside it and dies with it; only then do the
        # root-namespace bindings (bridges, host-level vlan
        # subinterfaces) get torn down. Deleting the other way round
        # would `ip link delete` a device that's still in the netns
        # (2026-08-18).
        _emit_kernel_router(host_node.id, nodes, emit)
        _emit_kernel_services(host_node.id, nodes, emit)
        # Clear IPFIX BEFORE the segment bridges are torn down
        # (`_emit_iface_bindings` del-br's them) — a clear after would
        # target a bridge that no longer exists.
        _emit_ipfix(host_node, nodes, emit)
        # The boot unit goes before the bridges/interfaces it re-creates.
        _emit_bindings_unit(host_node, nodes, emit)
        _emit_iface_bindings(host_name, nodes, emit)
        return

    # Dependencies FIRST — nothing below (OVS/OVN, netns, services)
    # works without them; no deinstall ever (2026-08-23). They are no
    # longer installed inline: they are QUEUED, the queue's consumer is
    # asked once to install what is missing if the host is online, and
    # its service does the rest in the background (see
    # _emit_install_queue). The Python front-end wraps this in
    # `if not skip_deps:` so the generated deployer can leave a host's
    # dependencies alone altogether (`--skip-deps`); a caller that passes
    # no `guard_deps` gets the commands flat.
    if has_queue:
        if guard_deps:
            emit.begin_block("if not skip_deps:")
        _emit_install_queue(host_node, nodes, emit)
        if guard_deps:
            emit.end_block()
    # Chassis registration RIGHT AFTER the packages, so ovn-controller
    # registers while the heavier steps run (the cluster pass resolves
    # this chassis by the hostname set here).
    _emit_chassis_registration(host_node, nodes, emit)
    # The boot-time bindings unit is written first so the router/service
    # units below can be ordered After= it; the live create pass still
    # applies the same bindings inline (below).
    _emit_bindings_unit(host_node, nodes, emit)
    # Installs the generated program on the host and enables the boot
    # restore unit (which re-runs this host pass, --skip-deps).
    _emit_restore_unit(host_node, emit)
    _emit_kernel_router(host_node.id, nodes, emit)
    # The segment bridges must exist (bound by _emit_iface_bindings) before
    # a container's veth can attach — containers come AFTER.
    _emit_iface_bindings(host_name, nodes, emit)
    # Flow export attaches to those now-existing segment bridges.
    _emit_ipfix(host_node, nodes, emit)
    _emit_kernel_services(host_node.id, nodes, emit)
    # LAST: the units exist now — a router or workload that could not get
    # its image or tool is the queue consumer's to restart once the
    # internet is back (possibly through the very router this pass just
    # deployed).
    if has_queue and _unit_jobs(host_node.id, nodes):
        if guard_deps:
            emit.begin_block("if not skip_deps:")
        _emit_unit_queue(host_node, nodes, emit)
        if guard_deps:
            emit.end_block()


# The per-host body emitter — the one entry point a front-end calls (the
# Python generator hands it a PythonEmitter, the tests a trace emitter).
def _emit_host_script(
    host_node: pt.InfraHostNode,
    nodes: list[pt.Model],
    emit: Emitter,
    guard_deps: bool = False,
) -> None:
    _emit_host(host_node, nodes, emit, guard_deps)
