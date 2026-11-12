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
import posixpath
import re
import shlex
from collections.abc import Sequence
from typing import Any, Protocol

from ip_address import IPAddress

from fabric.addresses import Addresses
from fabric.decide import Decide
from fabric.primitives.docker import DockerPrimitive, files_hash
from fabric.primitives.host import HostPrimitive, NetnsPrimitive
from fabric.primitives.net import FirewallRule
from fabric.primitives.ovn import (
    OVNNbCtlAcl,
    OVNNbCtlLr,
    OVNNbCtlLrp,
    OVNNbCtlLs,
    OVNNbCtlLsp,
    OVNNbCtlNat,
    OVNNbCtlRoute,
)
from ladops import linux_net as linux_net_ops
from ladops import netns as netns_ops
from ladops import ovn as ovn_ops
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


def _network_of(addr: str) -> str:
    """`192.168.128.1/24` → `192.168.128.0/24`."""
    parsed = IPAddress.parse(addr)
    if parsed is None:
        raise ValueError(f"not an address: {addr!r}")
    return str(parsed.network().to_string())


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
def unit_name(kind: str, subject: str | None = None) -> str:
    """A unit's name: "<kind>: <subject>", or the kind alone."""
    return f"{kind}: {subject}" if subject else kind


class Emitter:
    label: str

    def __init__(self, action: Action) -> None:
        self.action = action
        self.lines: list[str] = []
        self._locals: set[str] = set()

    def local(self, name: str) -> str:
        """A collision-free local variable derived from an OVN object
        name, so generated code can express ownership:
        `router_home_v2.add_route(...)`."""
        stem = re.sub(r"[^0-9A-Za-z_]", "_", name).strip("_") or "ovn"
        if stem[0].isdigit():
            stem = f"_{stem}"
        if stem in {"lc", "host", "abort_on_error", "job", "ctx"}:
            stem = f"ovn_{stem}"
        candidate = stem
        index = 2
        while candidate in self._locals:
            candidate = f"{stem}_{index}"
            index += 1
        self._locals.add(candidate)
        return candidate

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

    def ovn(
        self,
        primitive: Any,
        *,
        bind: str | None = None,
        parent: str | None = None,
        decide: Decide = Decide.footprint,
    ) -> None:
        """A typed OVN northbound object. `bind` runs it and keeps the
        result in a local variable; `parent` attaches it to an already
        bound object (`router.add_route(...)`). `decide`: who decides in a
        run whether it is applied, skipped or replaced (fabric/decide.py)."""
        raise NotImplementedError

    def assign(self, primitive: Any, var: str) -> None:
        """Bind a primitive to a local without running it yet — used by
        the kernel-router down path, where links are deleted before the
        netns and the netns itself must go last."""
        raise NotImplementedError

    def run_var(self, var: str) -> None:
        raise NotImplementedError

    def netns_symlink(self, container_expr: str, pid_var: str) -> None:
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

    def helper(
        self,
        name: str,
        *args: object,
        undo: str | None = None,
        undo_args: Sequence[object] = (),
    ) -> None:
        """Call the runtime helper `name(job, label, *args)` at this point
        of the body on CREATE, and `undo(job, label, *undo_args)` at the
        mirrored point on DELETE (Python-bodied jobs only). TRANSITIONAL:
        each helper becomes a typed primitive."""
        raise NotImplementedError

    def daemon_reload(self) -> None:
        """`systemctl daemon-reload`, in both directions."""
        raise NotImplementedError

    def append(self, path: str, content: str) -> None:
        raise NotImplementedError

    def install_self(self, path: str) -> None:
        """Copy the running generated program to `path` — what the boot
        restore unit execs (Python: install_self(); the tests' trace
        renders a comment; there is no shell rendering)."""
        raise NotImplementedError

    def start_unit(self, unit: str) -> None:
        """Start a unit without waiting for it (`systemctl start
        --no-block`); its rewind stops it if it still runs."""
        raise NotImplementedError

    def enable_unit(self, unit: str, optional: bool = True) -> None:
        """Enable a unit and start it now — EXCEPT during the boot restore
        pass (`--no-start`), which must only enable: the fabric units are
        ordered After= the restore unit, so starting them from inside it
        would deadlock (`systemctl` waits for a unit that waits for the
        restore to finish)."""
        raise NotImplementedError

    def unit(self, kind: str, subject: str | None = None, *, key: str | None = None) -> Emitter:
        """Open a unit of work and return it: what is declared on the
        returned emitter belongs to it — one logical switch, one router
        port, one kernel app. `kind` is what it is ("router port"),
        `subject` which one ("lrp-x (home)"); `key` the IR node it comes
        from. Only the newest unit takes operations (see fabric/plan.py).

        Here (the generated code) it is a section comment,
        `# --- <name> ---`, and the unit is this same emitter."""
        self.comment(f"# --- {unit_name(kind, subject)} ---")
        return self

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
    router_var: str,
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
    _emit_router_acl(router, router_var, ports, lr, nodes, domain_names, router_unit)
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
def _emit_rule(
    emit: Emitter,
    netns_primitive: NetnsPrimitive | None,
    netns_var: str | None,
    rule: FirewallRule,
) -> None:
    if netns_primitive is not None:
        emit.ovn(rule, parent=netns_var)
        return
    for command in rule.as_primitive().create_cmds:
        emit.sh(list(command.argv))


_FAMILY = {pt.Family.ipv4: "ipv4", pt.Family.ipv6: "ipv6"}


def _emit_security_group_rules(
    group: pt.SecurityGroupNode,
    netns: str,
    dev: str,
    emit: Emitter,
    netns_primitive: NetnsPrimitive | None = None,
    netns_var: str | None = None,
) -> None:
    group_unit = emit.unit("security group", f"{group.key.name} in {netns} on {dev}")
    # Builds the rules (its host only names their commands).
    rules = netns_primitive or NetnsPrimitive(host="", name=netns)
    # Every filter rule is stateful: allow replies first, per family for
    # every direction the group actually uses.
    stateful = sorted(
        {
            (_FAMILY[rule.family], rule.direction.value)
            for rule in group.data.rules
            if rule.kind == "filter"
        }
    )
    for family, direction in stateful:
        _emit_rule(
            group_unit, netns_primitive, netns_var, rules.add_stateful_accept(family, direction)
        )
    for rule in group.data.rules:
        if rule.kind == "masq":
            _emit_rule(
                group_unit, netns_primitive, netns_var, rules.add_masq(_FAMILY[rule.family], dev)
            )
            continue
        if rule.kind != "filter":
            raise ValueError(
                f"{group.key.name}: rule kind {rule.kind!r} is not a kernel "
                "rule; a kernel reference may not carry it"
            )
        _emit_rule(
            group_unit,
            netns_primitive,
            netns_var,
            rules.add_filter(
                _FAMILY[rule.family],
                rule.direction.value,
                rule.action.value,
                sources=rule.sources or (),
                exclude_sources=rule.excludeSources or (),
                destinations=rule.destinations or (),
                protocol=rule.protocol.value if rule.protocol is not None else None,
                ports=[(port.from_, port.to) for port in rule.ports or ()],
            ),
        )
    default = group.data.default
    if default is not None:
        for direction in ("input", "output", "forward"):
            action = getattr(default, direction, None)
            if action is not None:
                _emit_rule(
                    group_unit,
                    netns_primitive,
                    netns_var,
                    rules.add_default(direction, action.value),
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
    addrs: Addresses,
    netns: str,
    dev: str,
    emit: Emitter,
    netns_primitive: NetnsPrimitive | None = None,
    netns_var: str | None = None,
) -> None:
    """Apply a kernel router side's static addressing (addresses + routes)
    and security-group rules (masq) to a target namespace — the router's
    netns OR a docker container's netns (the dhcpcd-in-docker path). The
    ONLY difference between the two is the namespace name, so both go
    through this one function (2026-08-31)."""
    for addr in ipaddrs or []:
        if netns_primitive is not None:
            emit.ovn(netns_primitive.add_addr(dev, addr), parent=netns_var)
        else:
            argv = linux_net_ops.add_addr_argv(addr, dev)
            emit.sh(netns_ops.netns_exec_argv(argv, netns))
    for route in routes or []:
        via = addrs.ref(route.via) if route.via is not None else None
        if netns_primitive is not None:
            emit.ovn(
                netns_primitive.add_route(route.dst, dev, via, route.metric),
                parent=netns_var,
            )
        else:
            argv = linux_net_ops.add_route_argv(route.dst, dev, via, route.metric)
            emit.sh(netns_ops.netns_exec_argv(argv, netns))
    for group in groups:
        _emit_security_group_rules(group, netns, dev, emit, netns_primitive, netns_var)


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
    netns and the side's real interface `dev`."""

    netns: str
    dev: str
    addrs: Addresses


def _emit_kernel_app_rules(
    apps: list[KernelApp],
    router: str,
    host: HostPrimitive,
    netns_var: str,
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
                app_unit = emit.unit("kernel app", f"dhcp-client (dhclient) on {dev} in {netns}")
                app_unit.ovn(host.make_dir(posixpath.dirname(lease)))  # the lease's directory
                app_unit.ovn(host.netns(netns).dhclient(dev, lease), parent=netns_var)
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
            app_unit = emit.unit("kernel app", f"docker ({container}) on {dev} in {netns}")
            veth = app.vethName or ("ve-" + hashlib.md5(container.encode()).hexdigest()[:8])
            container_ip = ctx.addrs.one(app.ip)
            router_ip = ctx.addrs.one(app.routerIp)
            gateway = router_ip.split("/")[0]
            image = app.image or f"ovn-fabric-{container}"
            app_unit.helper(
                "_docker_veth_up",
                container,
                image,
                _docker_dockerfile(app.build) if app.build else None,
                list(app.cmd or []),
                netns,
                veth,
                router_ip,
                container_ip,
                gateway,
                # Stopping a docker container is done THE HARD WAY — an
                # explicit `docker rm -f` via the daemon (SIGTERM through
                # the router's stop is not a reliable container shutdown).
                undo="_docker_down",
                undo_args=(container,),
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
            conf = "\n".join(_wg_conf_lines(app))
            app_unit = emit.unit("kernel app", f"wireguard ({app.ifaceName}) in {netns}")
            app_unit.ovn(host.make_dir("/etc/wireguard"))
            app_unit.append(conf_path, conf)
            app_unit.ovn(host.chmod(conf_path, "600"))
            netns_primitive = host.netns(netns)
            # Up with the config it should have: run leaves it; with another
            # one it brings it down and up again (fabric/decide.py).
            app_unit.ovn(
                netns_primitive.wireguard(app.ifaceName, conf_path, conf),
                parent=netns_var,
                decide=Decide.wireguard,
            )
            for family in _masq_families(list(app.securityGroups), nodes):
                app_unit.ovn(
                    netns_primitive.add_masq(family, app.ifaceName),
                    parent=netns_var,
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
            app_unit = emit.unit("kernel app", f"zerotier in {netns}")
            masq = _masq_families(list(app.securityGroups), nodes)
            routes = [route.dst for route in (app.routes or [])]
            # The daemon is the job's tracked foreground child; the runtime
            # kills it on stop, and its rules/routes go with the netns.
            app_unit.helper("_zerotier_up", netns, app.instanceDir, app.networkId, masq, routes)
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
    owner: pt.KernelRouterNode,
    netns: str,
    nodes: list[pt.Model],
    addrs: Addresses,
    emit: Emitter,
) -> None:
    """One kernel router's declaration: its `up` creates it, its `down`
    is the same declaration rewound (the netns' moved-in links are deleted
    from inside the netns before the netns itself). See the module doc
    block above for the netns/side semantics."""
    host = HostPrimitive(owner.data.host.removeprefix("host:"))
    kernel_var = emit.local("kernel")
    netns_var = emit.local(netns)
    netns_unit = emit.unit("kernel router", owner.key.name, key=owner.id)
    netns_unit.ovn(host.rootNs(), bind=kernel_var)
    # A router whose container owns its interfaces has no netns of its own:
    # the container's is it. All of it is one unit: a restart of the
    # container makes everything in its netns again.
    owning = _owning_app(owner, nodes)
    docker = None
    if owning is None:
        kernel_netns = host.netns(netns)
        netns_unit.ovn(kernel_netns, bind=netns_var)
    else:
        docker = _start_owning_container(owning[0], owning[1], host, netns_unit)
        netns = owning[1]
        kernel_netns = docker.get_netns()
        netns_unit.ovn(kernel_netns, bind=netns_var)
        # A sysctl is set every run: no undo, the same value again.
        for key in ("net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"):
            netns_unit.ovn(
                kernel_netns.set_sysctl(key, "1"), parent=netns_var, decide=Decide.always
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
        side_unit = (
            netns_unit
            if docker is not None
            else emit.unit("kernel router side", f"{owner.key.name} ({_side(side_node).value})")
        )
        if iface is not None:
            if iface.kind == "vlan":
                side_unit.ovn(
                    kernel_netns.add_link(
                        dev,
                        "vlan",
                        parent=iface.vlanParent.name,
                        vlan_id=iface.vlanId,
                    ),
                    parent=netns_var,
                )
            elif iface.kind == "veth":
                # `mac` (src/ir.ts's kernelRouterSideToIR) pins the
                # netns-side leg's MAC so the OVN router's MAC_Binding
                # for this side stays valid across a down/up.
                root_leg = f"veth-ovn-{iface.shortName}"
                # The bridge itself belongs to the host's configure
                # declaration (_emit_iface_bindings); the router only
                # plugs its root leg into it.
                bridge = _veth_root_bridge(root_leg, nodes)
                side_unit.ovn(
                    kernel_netns.add_link(
                        dev,
                        "veth",
                        mac=iface.mac or addrs.mac(side_node.data.ipaddrs, iface.ifaceName),
                        root_leg=root_leg,
                        bridge=bridge,
                    ),
                    parent=netns_var,
                )
            elif iface.kind == "physical":
                side_unit.ovn(kernel_netns.add_link(dev, "physical"), parent=netns_var)
        else:
            side_unit.ovn(kernel_netns.add_link(dev, "dummy"), parent=netns_var)
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
            side_unit.ovn(
                kernel_netns.set_sysctl_file(f"/proc/sys/net/ipv6/conf/{dev}/accept_ra", "2"),
                parent=netns_var,
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
            addrs.list(side_node.data.ipaddrs),
            list(side_node.data.routes or []),
            addrs,
            netns,
            dev,
            side_unit,
            kernel_netns,
            netns_var,
        )
        # The side's apps (right side in practice) run inside the
        # netns on this interface — started after the interface is up
        # and bound, so the DHCP client can immediately acquire a
        # lease on a ready device.
        apps = [a for a in side_node.data.apps or [] if owning is None or a is not owning[0]]
        if apps:
            _emit_kernel_app_rules(
                apps,
                owner.key.name,
                host,
                netns_var,
                side_unit,
                _SideContext(
                    netns=netns,
                    dev=dev,
                    addrs=addrs,
                ),
                nodes,
            )
    # Its app starts once its interfaces are there, and with its container.
    # A device the app makes itself is wired from here, not in its image:
    # its masq needs only the name; a route needs the device.
    if docker is not None and owning is not None:
        app = owning[0]
        if app.tunnel is not None:
            for family in _masq_families(list(app.securityGroups or []), nodes):
                netns_unit.ovn(kernel_netns.add_masq(family, app.tunnel.iface), parent=netns_var)
        netns_unit.ovn(
            docker.exec_detached([*(app.cmd or ["sh"]), owning[2]]),
            decide=Decide.docker_exec,
        )
        if app.tunnel is not None:
            netns_unit.ovn(
                kernel_netns.wait_link_up(app.tunnel.iface),
                parent=netns_var,
                decide=Decide.always,
            )
            for dst in app.tunnel.routes:
                netns_unit.ovn(kernel_netns.add_route(dst, app.tunnel.iface), parent=netns_var)


def _owning_app(
    owner: pt.KernelRouterNode, nodes: list[pt.Model]
) -> tuple[pt.DockerApp, str, str] | None:
    """The docker app that OWNS the router's interfaces ("the container IS the
    router", dhcpcd-in-docker): (app, its container, its side's device). It
    carries NO veth addressing in the IR — that structural fact (no `ip`) is
    how the TS side says so, not a concept name (2026-08-31)."""
    for side_node in _kernel_router_sides(owner.key.name, nodes):
        for app in side_node.data.apps or []:
            if app.kind == "docker" and app.ip is None:
                iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
                if iface is None:
                    dev = _dummy_name(_side(side_node))
                elif iface.kind == "veth":
                    dev = f"veth-krn-{iface.shortName}"
                else:
                    dev = _iface_real_name(iface)
                return app, app.name or f"{owner.key.name}-docker", dev
    return None


def _start_owning_container(
    app: pt.DockerApp, container: str, host: HostPrimitive, unit: Emitter
) -> DockerPrimitive:
    """Its image, state and container, up to its named netns: the router's
    interfaces are made right there (the same way as a workload's NICs)."""
    image = app.image or f"ovn-fabric-{container}"
    volumes = [
        {"host": volume.host, "container": volume.container} for volume in (app.volumes or [])
    ]
    identity = (
        {"public": app.identity.public, "secret": app.identity.secret} if app.identity else None
    )
    dockerfile = _docker_dockerfile(app.build) if app.build else None
    _emit_image(host, image, dockerfile, f"/var/lib/ovn-fabric/build/{container}", unit)
    # The app owns its volume directories: DELETE removes them with whatever
    # the app wrote into them.
    for volume in volumes:
        unit.ovn(host.state_dir(volume["host"]))
    if identity is not None:
        state_dir = next(
            (
                volume["host"]
                for volume in volumes
                if volume["container"] == "/var/lib/zerotier-one"
            ),
            None,
        )
        if state_dir is not None:
            public = f"{state_dir}/identity.public"
            secret = f"{state_dir}/identity.secret"
            unit.append(public, identity["public"])
            unit.append(secret, identity["secret"])
            unit.ovn(host.chmod(public, "644"))
            unit.ovn(host.chmod(secret, "600"))
    state_files = list(app.stateFiles or [])
    for state_file in state_files:
        directory = posixpath.dirname(state_file.path)
        if directory not in {volume["host"] for volume in volumes}:
            unit.ovn(host.state_dir(directory))
        if state_file.content is not None:
            unit.append(state_file.path, state_file.content)
            if state_file.mode is not None:
                unit.ovn(host.chmod(state_file.path, state_file.mode))
        else:
            # The app writes into it: not written over, whatever a run finds.
            unit.ovn(host.touch(state_file.path), decide=Decide.always)
    docker = host.docker(
        container,
        image,
        command=("sleep", "infinity"),
        privileged=True,
        volumes=tuple((volume["host"], volume["container"]) for volume in volumes),
        run_flags=tuple(app.run or ()),
        # Its natural key: the node address, the public part of its identity.
        natural_key=f"zt:{identity['public'].split(':')[0]}" if identity else "",
        # A changed identity or state file needs a restart: the daemon read it.
        files=files_hash(
            *(identity.values() if identity else ()),
            *(f"{f.path}={f.content}" for f in state_files),
        ),
    )
    # Running from its image with what it should run with: run leaves it;
    # else it is restarted, and the unit's later steps run again
    # (fabric/decide.py).
    unit.ovn(docker, bind=unit.local(container), decide=Decide.docker_container)
    pid_var = unit.local(f"{container}-pid")
    unit.capture(pid_var, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", container])
    unit.netns_symlink(repr(container), pid_var)
    return docker


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


def _emit_xdp_leg(
    host: HostPrimitive,
    kernel_var: str,
    segment_bridge: str,
    vlan_id: int | None,
    emit: Emitter,
) -> None:
    suffix = _short_suffix(segment_bridge)
    trunk_leg = f"veth-xdp-{suffix}"
    seg_leg = f"veth-seg-{suffix}"
    emit.ovn(
        host.kernel.add_if(trunk_leg, kind="veth", peer=seg_leg, up=True),
        parent=kernel_var,
    )
    emit.ovn(host.ovs_port(_XDP_TRUNK_BRIDGE, trunk_leg, tag=vlan_id))
    emit.ovn(host.ovs_port(segment_bridge, seg_leg))


def _emit_xdp_leg_delete(
    host: HostPrimitive,
    root_var: str,
    segment_bridge: str,
    emit: Emitter,
) -> None:
    suffix = _short_suffix(segment_bridge)
    trunk_leg = f"veth-xdp-{suffix}"
    seg_leg = f"veth-seg-{suffix}"
    emit.ovn(host.ovs_port(segment_bridge, seg_leg))
    emit.ovn(host.ovs_port(_XDP_TRUNK_BRIDGE, trunk_leg))
    # Deleting one end removes the pair.
    emit.ovn(
        host.rootNs().add_if(trunk_leg, kind="veth", peer=seg_leg),
        parent=root_var,
    )
    emit.ovn(host.ovs_bridge(segment_bridge))


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
    host = HostPrimitive(host_name)
    # The root namespace is the parent of every host-local link node.
    kernel_var = emit.local("kernel")
    root_unit = emit.unit("root namespace", host_name)
    root_unit.ovn(host.rootNs(), bind=kernel_var)
    mappings: list[str] = []
    xdp_ports = _xdp_ports(all_bindings)
    xdp_trunks = _xdp_trunks(all_bindings)
    if xdp_ports:
        trunk_unit = emit.unit("XDP trunk", "userspace datapath, one port for all segments")
        trunk_unit.ovn(host.ovs_bridge(_XDP_TRUNK_BRIDGE, datapath="netdev"))
        for name, port in xdp_ports.items():
            trunk_unit.ovn(
                host.ovs_afxdp_port(
                    _XDP_TRUNK_BRIDGE,
                    name,
                    port.mode.value,
                    port.nRxq,
                    trunks=sorted(set(xdp_trunks.get(name, []))),
                )
            )
        trunk_unit.blank()
    # The IR carries one node per OVS bridge with all of its ports
    # (`ovn.ls.interfaces` or `bridge.ports`). Group by bridge name and
    # emit ensure-bridge ONCE, then only the ports.
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
        bridge_unit = emit.unit("bridge", f"{bridge} on {host_name}")
        bridge_unit.ovn(host.ovs_bridge(bridge))
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
            iface_unit = bridge_unit.unit("interface", f"{domain} ({label})")
            if iface.kind == "vlan" and not is_xdp:
                iface_unit.ovn(
                    host.rootNs().add_vlan(
                        iface.vlanParent.name,
                        _iface_real_name(iface),
                        iface.vlanId,
                    ),
                    parent=kernel_var,
                )
            # A transit veth's ROOT leg is created AND upped by its own
            # kernel-router job (_emit_kernel_router_owner). This block
            # must not touch it (2026-10-20, boot-restore ordering).
            if is_xdp:
                vlan_id = iface.vlanId if iface.kind == "vlan" else None
                _emit_xdp_leg(host, kernel_var, bridge, vlan_id, iface_unit)
            elif iface.kind == "veth":
                pass
            else:
                iface_unit.ovn(host.ovs_port(bridge, _iface_real_name(iface)))
            if domain in ovn_domains:
                mappings.append(f"{_network_name(domain)}:{bridge}")
    if mappings:
        # Host-wide (one `ovn-bridge-mappings` for all bridges), so its own
        # unit — after every bridge it names exists.
        mappings_unit = emit.unit("bridge mappings", host_name)
        mappings_unit.ovn(host.ovs_mapping(tuple(mappings)))
    if len(emit.lines) > before:
        emit.blank()


# ── the one host service ──────────────────────────────────────────────
# One systemd service per host; there are no per-router/service units.
# For now it runs the lazy phase once (image pulls/builds, in the
# background of CREATE); later it will run the job tree (`--action run`).
_RESTORE_SCRIPT = "/usr/local/sbin/ovn-fabric-deployer.py"
_FABRIC_UNIT = "ovn-fabric.service"
_FABRIC_UNIT_PATH = f"/etc/systemd/system/{_FABRIC_UNIT}"


def _fabric_unit(host_name: str) -> str:
    return "\n".join(
        [
            "[Unit]",
            f"Description=ovn-fabric ({host_name}): pull and build images",
            "Wants=network-online.target",
            "After=network-online.target docker.service",
            "",
            "[Service]",
            # Runs to completion once — a pull or build takes as long as
            # it takes. Not enabled: CREATE starts it, a reboot finds the
            # images there.
            "Type=oneshot",
            "RemainAfterExit=yes",
            "TimeoutStartSec=infinity",
            f"ExecStart={_RESTORE_SCRIPT} --action lazy-create --host {host_name}",
        ]
    )


def _emit_image(
    host: HostPrimitive, image: str, dockerfile: str | None, build_dir: str, emit: Emitter
) -> None:
    """A container's image: built from its Dockerfile (context dir,
    Dockerfile, build) or pulled from the registry."""
    if dockerfile is None:
        # Pulled while run decides: its container compares with it.
        emit.ovn(host.docker_pull(image), decide=Decide.docker_pull)
        return
    dockerfile_path = f"{build_dir}/Dockerfile"
    emit.ovn(host.make_dir(build_dir))
    emit.append(dockerfile_path, dockerfile)
    # Built anew only when its Dockerfile changed (its label).
    spec = hashlib.sha256(dockerfile.encode()).hexdigest()
    emit.ovn(host.docker_image(image, dockerfile_path, build_dir, spec), decide=Decide.docker_image)


def _emit_fabric_unit(host_node: pt.InfraHostNode, emit: Emitter) -> None:
    """Install the generated program and the ONE service (for now: the
    lazy phase; _emit_fabric_start starts it). Rewound (DELETE) this
    removes the unit and the program. A daemon-reload on each side of the
    unit file keeps systemd's view right in both directions."""
    host_name = host_node.key.host
    service_unit = emit.unit("fabric service", _FABRIC_UNIT)
    service_unit.install_self(_RESTORE_SCRIPT)
    service_unit.daemon_reload()
    service_unit.append(_FABRIC_UNIT_PATH, _fabric_unit(host_name))
    service_unit.daemon_reload()
    service_unit.blank()


def _emit_fabric_start(host_node: pt.InfraHostNode, emit: Emitter) -> None:
    """The host's last scope: start the service in the background, once
    every scope's synchronous part exists — CREATE does not wait for the
    lazy phase. Rewound (first on DELETE), this stops it if it still runs."""
    emit.start_unit(_FABRIC_UNIT)


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
    """One IPFIX sampler per sampled bridge on this host (DELETE clears
    it). A bridge whose leg opted out gets none. No-op when the host has
    no `ipfix` config or no bindable segment bridges."""
    ipfix = host_node.data.ipfix
    if ipfix is None:
        return
    flags = _ipfix_bridge_flags(host_node.key.host, nodes)
    if not flags:
        return
    host = HostPrimitive(host_node.key.host)
    ipfix_unit = emit.unit("flow export", "IPFIX")
    for bridge in sorted(flags):
        if flags[bridge]:
            ipfix_unit.ovn(host.ovs_ipfix(bridge, _ipfix_enable_script(bridge, ipfix)))
    ipfix_unit.blank()


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
    addrs: Addresses,
    emit: Emitter,
) -> None:
    """A workload: its image, then its container, NICs and addresses."""
    workload_unit = emit.unit("workload", node.key.name, key=node.id)
    _emit_service_body(node, faces, addrs, workload_unit)


def _emit_service_body(
    node: pt.KernelAppContainerNode | pt.KernelAppServiceNode,
    faces: list[Face],
    addrs: Addresses,
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
    host = HostPrimitive(emit.label.split(":", 1)[0])
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
        emit.ovn(host.make_dir(parent))
        emit.append(host_path, body)
    _emit_image(
        host,
        node.data.image or "",
        _docker_dockerfile(node.data.build) if node.data.build is not None else None,
        f"/var/lib/ovn-fabric/build/{name}",
        emit,
    )
    # The service IS the container's command — `docker run -d` runs it
    # in the background (no sleep + docker exec dance).
    env = tuple(sorted((node.data.env or {}).items()))
    mounts = tuple(
        (host_path, f"{container_path}:ro") for host_path, container_path, _ in file_mounts
    )
    # DNS resolver (ServiceRef.resolver, resolved by the generator:
    # attachment > control-plane global > CoreDNS). Docker writes
    # /etc/resolv.conf from --dns; we never bind-mount over it. A container
    # has ONE resolv.conf, so the FIRST NIC that carries a resolver wins.
    run_flags: list[str] = []
    for _i, ref, _bridge, _dev, _nic in faces:
        resolver = ref.resolver
        if resolver and resolver.nameservers:
            for server in resolver.nameservers:
                run_flags += ["--dns", addrs.ref(server)]
            break
    docker = host.docker(
        container,
        node.data.image or "",
        command=tuple(node.data.cmd or ()),
        privileged=True,
        environment=env,
        volumes=mounts,
        run_flags=tuple(run_flags),
        # A changed file needs a restart: the service read it.
        files=files_hash(
            *(
                "\n".join(content) if isinstance(content, list) else content
                for _path, _container_path, content in file_mounts
            )
        ),
    )
    docker_var = emit.local(container)
    emit.ovn(docker, bind=docker_var, decide=Decide.docker_container)
    # The capture name is per container: the store is shared by every
    # controller of the run, and services run concurrently.
    pid_var = emit.local(f"{container}-pid")
    emit.capture(pid_var, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", Ref("container")])
    emit.ovn(host.make_dir("/var/run/netns"))
    # `container` is a Python local; the PID is a queued capture ref.
    emit.netns_symlink("container", pid_var)
    container_ns = docker.get_netns()
    container_ns_var = emit.local(f"{container}-netns")
    emit.ovn(container_ns, bind=container_ns_var)
    for i, ref, bridge, dev, nic in faces:
        peer = f"{dev}c"
        # Per-NIC routing table + a source rule: source-based policy
        # routing. Two NICs of the same workload may legitimately reach the
        # SAME destination; a separate table per NIC, entered by a rule
        # matching that NIC's own source prefix, keeps every path intact.
        table = 1000 + i
        nic_addrs = addrs.list(ref.ipaddrs)
        networks = [_network_of(a) for a in nic_addrs]
        nic_unit = emit.unit("NIC", f"{nic} on {bridge}")
        nic_unit.ovn(
            container_ns.add_link(
                peer,
                "veth",
                rename_to=nic,
                mac=ref.mac or addrs.mac(ref.ipaddrs, f"{ref.service}|{nic}"),
                root_leg=dev,
                bridge=bridge,
            ),
            parent=container_ns_var,
        )
        for address in nic_addrs:
            nic_unit.ovn(
                container_ns.add_addr(nic, address),
                parent=container_ns_var,
            )
        if ref.routes:
            for route in ref.routes:
                family = 6 if ":" in route.dst else 4
                nic_unit.ovn(
                    container_ns.add_route(
                        route.dst,
                        nic,
                        via=addrs.ref(route.via) if route.via is not None else None,
                        metric=route.metric,
                        table=table,
                        family=family,
                    ),
                    parent=container_ns_var,
                )
                if route.dst in ("0.0.0.0/0", "::/0"):
                    nic_unit.ovn(
                        container_ns.add_route(
                            route.dst,
                            nic,
                            via=addrs.ref(route.via) if route.via is not None else None,
                            metric=route.metric,
                            family=family,
                        ),
                        parent=container_ns_var,
                    )
            for network in networks:
                family = 6 if ":" in network else 4
                nic_unit.ovn(
                    container_ns.add_ip_rule(network, table, family),
                    parent=container_ns_var,
                )


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
    host = HostPrimitive(host_name)
    if data.ovnRole == pt.OvnRole.central:
        emit.comment("# central: expose the shared NB/SB DBs to every other chassis")
        emit.ovn(host.ovn_nb_connection())
        emit.ovn(host.ovn_sb_connection())
        emit.ovn(host.ovs_external_id("ovn-remote", "unix:/var/run/ovn/ovnsb_db.sock"))
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
        emit.ovn(host.ovs_external_id("ovn-remote", f"tcp:{central.data.encapIp}:6642"))

    # The topology's own host name (NOT the auto FQDN OVS reports): the
    # cluster pass looks this chassis up by it to get the system-id, the
    # only identifier `lrp-set-gateway-chassis` matches (2026-09-19).
    emit.ovn(host.ovs_external_id("hostname", host_name))

    if data.encapIp is not None:
        emit.ovn(host.ovs_external_id("ovn-encap-type", "geneve"))
        emit.ovn(host.ovs_external_id("ovn-encap-ip", data.encapIp))

    if host_name in _gateway_eligible_hosts(nodes):
        emit.ovn(host.ovs_external_id("ovn-cms-options", "enable-chassis-as-gw"))


def _emit_host_configure(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """The idempotent part of the host pass: chassis registration,
    interface bindings and IPFIX. The job tree's configure job."""
    host_name = host_node.key.host
    registration_unit = emit.unit("chassis registration", host_name, key=host_node.id)
    _emit_chassis_registration(host_node, nodes, registration_unit)
    _emit_iface_bindings(host_name, nodes, emit)
    _emit_ipfix(host_node, nodes, emit)


def _emit_host(
    host_node: pt.InfraHostNode,
    nodes: list[pt.Model],
    emit: Emitter,
) -> None:
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
