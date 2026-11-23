# fabric/emit.py — the emitters behind the runtime's plan (fabric/plan.py):
# the desired-state IR (ovn-fabric's src/ir.ts toIR(), emitted as JSON by
# `deno run src/cli.ts generate-ir`, then hydrated into protocol/
# generated.py's typed dataclasses by protocol/hydrate.py) in, OPERATIONS
# out. Every _emit_* function builds through an `Emitter` sink (see
# below); fabric/plan.py's PlanEmitter turns what it receives into the
# units of work the runtime runs (ADR 0007). Until 2026-11 this module
# was deployer/ir_common.py, shared with the generated deployer and its
# shell rendering — both gone (tag legacy-pre-adr-0007).
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
#
# Split by topic into this package (each module imports only the ones
# before it): emitter, lookups, security_groups, cluster, images,
# container, kernel_router, bindings, ipfix, service, host.

from __future__ import annotations

from .cluster import _emit_cluster_body
from .emitter import Emitter, Ref, unit_name
from .host import _emit_host_configure
from .images import _docker_dockerfile, _image_jobs
from .kernel_router import _emit_kernel_router_owner
from .lookups import _kernel_router_owners, _kernel_router_sides, _netns_name
from .security_groups import _selectors
from .service import _emit_service_up, _service_faces

__all__ = [
    "Emitter",
    "Ref",
    "_docker_dockerfile",
    "_emit_cluster_body",
    "_emit_host_configure",
    "_emit_kernel_router_owner",
    "_emit_service_up",
    "_image_jobs",
    "_kernel_router_owners",
    "_kernel_router_sides",
    "_netns_name",
    "_selectors",
    "_service_faces",
    "unit_name",
]
