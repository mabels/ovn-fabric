# ladops/ovn.py — real List/Add/Delete against OVN's northbound DB via
# `ovn-nbctl`/`ovn-sbctl`. list_lrps() is used by reconciler/ovn/
# reconcile.py to build IR nodes (read only); every other function here
# exists for the deployer (live execution, add_lrp/delete_lrp today) AND
# for deployer/ir_common.py's emitters (deployer/cli.py -- convert IR
# JSON into the generated Python deployer). Both share the exact same
# *_argv() builders — this module is the ONE place that knows the real
# ovn-nbctl/ovn-sbctl invocation syntax, so the generated script and
# whatever eventually executes for real can never drift apart.
#
# No update_lrp (or update_* for anything else here) — the ADR's own
# node-kind table already calls this out for ovn.lrp specifically
# ("unconditional delete+recreate"): OVN has no atomic "change this
# port's fields in place" primitive either, so composing add+delete into
# what looks like an update is the caller's job, not something this
# module decides for it.
#
# No --may-exist on any *_add*: a caller that doesn't already know
# whether an object exists is the caller's problem to resolve (diff
# against list_lrps()/etc. first) — silently swallowing "already
# exists" here would hide exactly the information a reconciler needs to
# know its own view of live state is stale. Deletes DO use --if-exists
# (delete_lrp's own long-established behavior, kept exactly as-is) —
# "delete something that's already gone" is a genuinely different case
# from "add something that already exists": a delete-everything pass
# naturally revisits objects other passes already removed, and erroring
# on that would make delete non-idempotent for no benefit.
#
# `ovn-nbctl -f json list <table>`, not `ovn-nbctl show` — `show`
# ignores --format entirely (confirmed against the real router: still
# prints its normal tree-text output with -f json passed), while `list`
# returns real self-describing structured data, decoded via the shared
# ladops/ovsdb.py (the same OVSDB wire format ladops/ovs.py uses).
#
# Never run for real in this session (or anywhere): this project's only
# real router is live production infrastructure. Every write function is
# verified by asserting the exact argv its *_argv() builder produces,
# not by executing it.

from __future__ import annotations

from typing import TypeVar

from . import JsonObject
from .netns import run as run_in_netns
from .ovsdb import list_table

_Token = TypeVar("_Token")

_NBCTL = ["ovn-nbctl"]
_SBCTL = ["ovn-sbctl"]


def list_lrps() -> list[JsonObject]:
    """Every real Logical_Router_Port, joined back to its owning
    router's name (the join lives here, not in the reconciler, since
    it's part of "what's really there," not IR shaping)."""
    router_name_by_port_uuid: dict[str, str] = {}
    for router in list_table(_NBCTL, "Logical_Router"):
        for port_uuid in router["ports"]:
            router_name_by_port_uuid[port_uuid] = router["name"]

    lrps = []
    for port in list_table(_NBCTL, "Logical_Router_Port"):
        router_name = router_name_by_port_uuid.get(port["_uuid"])
        if router_name is None:
            continue  # an LRP not (yet) attached to any router — nothing to report it under
        lrps.append(
            {
                "router": router_name,
                "name": port["name"],
                "mac": port["mac"],
                "networks": port["networks"],
                "gatewayChassis": port["gateway_chassis"],
            }
        )
    return lrps


# ── logical router + router port ────────────────────────────────────


def lrp_add_argv(router: str, name: str, mac: str, networks: list[str]) -> list[str]:
    return [*_NBCTL, "lrp-add", router, name, mac, *networks]


def lrp_del_argv(name: str) -> list[str]:
    return [*_NBCTL, "--if-exists", "lrp-del", name]


def lr_nat_add_argv(router: str, nat_type: str, external_ip: str, logical_ip: str) -> list[str]:
    return [*_NBCTL, "lr-nat-add", router, nat_type, external_ip, logical_ip]


def lr_nat_del_argv(router: str, nat_type: str, external_ip: str | None = None) -> list[str]:
    argv = [*_NBCTL, "lr-nat-del", router, nat_type]
    if external_ip is not None:
        argv.append(external_ip)
    return argv


def lr_add_argv(router: str) -> list[str]:
    return [*_NBCTL, "lr-add", router]


# One Logical_Router.options key — same one-call-per-key shape as
# lrp_set_ipv6_ra_config_argv below. key/value come pre-resolved from the
# router's own ovn.lr IR node (src/ir.ts's routerToIR): OVN's own key
# name and an already-stringified value; this only knows the invocation
# shape. Idempotent (`set` on a map key). Today the one key is
# mac_binding_age_threshold: OVN's default (0) keeps a learned
# MAC_Binding FOREVER and never re-probes it, so a neighbour that came
# back with a different MAC is black-holed until it happens to ARP/ND the
# router itself (confirmed live 2026-10-11).
def lr_set_option_argv(router: str, key: str, value: str) -> list[str]:
    return [*_NBCTL, "set", "logical_router", router, f"options:{key}={value}"]


def lr_del_argv(router: str) -> list[str]:
    # Cascades: deleting a Logical_Router deletes its own
    # Logical_Router_Ports too (confirmed against ovn-nbctl's real
    # northbound schema — LRPs are strongly referenced from their
    # owning router) — no separate lrp_del_argv() call needed per port.
    return [*_NBCTL, "--if-exists", "lr-del", router]


def acl_add_argv(switch: str, direction: str, priority: int, match: str, action: str) -> list[str]:
    return [*_NBCTL, "acl-add", switch, direction, str(priority), match, action]


def acl_del_argv(
    switch: str,
    direction: str,
    priority: int | None = None,
    match: str | None = None,
) -> list[str]:
    argv = [*_NBCTL, "acl-del", switch, direction]
    if priority is not None:
        argv.append(str(priority))
    if match is not None:
        argv.append(match)
    return argv


def lr_route_add_argv(router: str, prefix: str, nexthop: str) -> list[str]:
    # No separate lr_route_del_argv/delete needed: lr_del_argv above
    # already cascades a router's own static routes away with it — a
    # route only ever needs deleting on its own when the ROUTE changes
    # but the router doesn't. A duplicate add FAILS; the create pass is
    # desired-state, not a silent merge.
    return [*_NBCTL, "lr-route-add", router, prefix, nexthop]


def lrp_set_gateway_chassis_argv(
    lrp: str, chassis: _Token, priority: int = 100
) -> list[str | _Token]:
    # `chassis` is whatever token the caller's argv carries for it: a plain
    # string here, or the deployer's runtime variable reference (the
    # chassis id only exists on the target host — see
    # sb_chassis_id_by_hostname_argv). It is passed through untouched.
    return [*_NBCTL, "lrp-set-gateway-chassis", lrp, chassis, str(priority)]


def lrp_set_redirect_chassis_argv(lrp: str) -> list[str]:
    return [*_NBCTL, "set", "logical_router_port", lrp, "options:reside-on-redirect-chassis=true"]


def sb_chassis_id_by_hostname_argv(hostname: str) -> list[str]:
    """The `Chassis.name` (the OVS `system-id`) of the chassis whose
    reported `hostname` equals `hostname` — resolved AT RUNTIME, because
    the system-id is generated on the host and can't be baked into the
    generated script. `lrp-set-gateway-chassis` matches against
    `Chassis.name`, never the hostname (confirmed live 2026-09-19), so
    the cluster pass must translate the IR's host name through this
    lookup first. `--bare --columns=name find` prints just the value(s),
    one per line; the deployer trims and uses the first."""
    return [*_SBCTL, "--bare", "--columns=name", "find", "Chassis", f"hostname={hostname}"]


def lrp_set_ipv6_ra_config_argv(lrp: str, key: str, value: str) -> list[str]:
    # One call per key, same as generate-ovn.ts's own emitSegmentBackboneJoin
    # (address_mode/send_periodic set in separate ovn-nbctl calls, not one
    # combined smap literal) — key/value themselves come pre-resolved from
    # src/ir.ts's resolveIpv6RaConfigs, this only knows the invocation shape.
    return [*_NBCTL, "set", "logical_router_port", lrp, f"ipv6_ra_configs:{key}={value}"]


def add_lrp(router: str, name: str, mac: str, networks: list[str]) -> None:
    run_in_netns(lrp_add_argv(router, name, mac, networks), None)


def delete_lrp(name: str) -> None:
    run_in_netns(lrp_del_argv(name), None)


# ── logical switch + switch-side router port ────────────────────────


def ls_add_argv(name: str) -> list[str]:
    return [*_NBCTL, "ls-add", name]


def ls_del_argv(name: str) -> list[str]:
    # Cascades: deleting a Logical_Switch deletes every Logical_Switch_
    # Port it owns too, including a router-type peer port bound via
    # lsp_add_router_argv() below — no separate lsp_del_argv() call
    # needed for those.
    return [*_NBCTL, "--if-exists", "ls-del", name]


def lsp_add_router_argv(switch: str, lsp: str, lrp: str) -> list[list[str]]:
    """A router-type LSP needs four ovn-nbctl calls, always together —
    bundled into one list here so a caller (the deployer's emitters, a
    future live deployer) can't accidentally emit one without the
    others and leave a half-bound port."""
    return [
        [*_NBCTL, "lsp-add", switch, lsp],
        [*_NBCTL, "lsp-set-type", lsp, "router"],
        [*_NBCTL, "lsp-set-addresses", lsp, "router"],
        [*_NBCTL, "lsp-set-options", lsp, f"router-port={lrp}"],
    ]


def lsp_add_localnet_argv(switch: str, lsp: str, network_name: str) -> list[list[str]]:
    """The other half of "bridge a collision domain into the real
    world": a localnet-type LSP binds a logical switch to whichever
    real OVS bridge ovn-bridge-mappings names `network_name` for on a
    given chassis (see ladops.ovs.add_br_argv/set_external_id_argv,
    deployer/ir_common.py's host-script side) — same bundling
    reasoning as lsp_add_router_argv above."""
    return [
        [*_NBCTL, "lsp-add", switch, lsp],
        [*_NBCTL, "lsp-set-type", lsp, "localnet"],
        [*_NBCTL, "lsp-set-addresses", lsp, "unknown"],
        [*_NBCTL, "lsp-set-options", lsp, f"network_name={network_name}"],
    ]


def lsp_del_argv(lsp: str) -> list[str]:
    # Not needed after ls_del_argv (which already cascades every LSP a
    # switch owns) — only useful for removing ONE port from a switch
    # that otherwise stays. Kept for that case, and for symmetry with
    # lrp_del_argv.
    return [*_NBCTL, "--if-exists", "lsp-del", lsp]


# ── NB/SB DB connection management (central chassis only) ───────────
# `ovn-nbctl`/`ovn-sbctl set-connection`/`del-connection` — real
# ovn-*ctl subcommands (mirrors ovs-vsctl's own get-manager/del-manager
# pattern), confirmed live against the 3-container multi-chassis test
# (ADR 0003): the central chassis exposes ptcp:6641 (NB)/ptcp:6642 (SB)
# so every other chassis's ovn-controller can reach the shared DBs.


def nb_set_connection_argv(target: str = "ptcp:6641:0.0.0.0") -> list[str]:
    return [*_NBCTL, "set-connection", target]


def sb_set_connection_argv(target: str = "ptcp:6642:0.0.0.0") -> list[str]:
    return [*_SBCTL, "set-connection", target]


def nb_del_connection_argv() -> list[str]:
    return [*_NBCTL, "del-connection"]


def sb_del_connection_argv() -> list[str]:
    return [*_SBCTL, "del-connection"]
