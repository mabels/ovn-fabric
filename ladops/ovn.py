# ladops/ovn.py — real List/Add/Delete against OVN's northbound DB via
# `ovn-nbctl`. list_lrps() is used by reconciler/ovn/reconcile.py to
# build IR nodes (read only); add_lrp/delete_lrp exist for the deployer.
# No update_lrp — the ADR's own node-kind table already calls this out
# for ovn.lrp specifically ("unconditional delete+recreate", matching
# `emitIdempotentLrpAdd`'s existing behavior on the generator side): OVN
# has no atomic "change this port's fields in place" primitive either,
# so composing add+delete into what looks like an update is the
# deployer's call, not something this module decides for it.
#
# `ovn-nbctl -f json list <table>`, not `ovn-nbctl show` — `show`
# ignores --format entirely (confirmed against the real router: still
# prints its normal tree-text output with -f json passed), while `list`
# returns real self-describing structured data, decoded via the shared
# ladops/ovsdb.py (the same OVSDB wire format ladops/ovs.py uses).
#
# Never run for real in this session (or anywhere): this project's only
# real router is live production infrastructure. add_lrp/delete_lrp are
# verified by asserting the exact argv built for `ovn-nbctl`, not by
# executing it.

from __future__ import annotations

from .netns import run as run_in_netns
from .ovsdb import list_table

_NBCTL = ["ovn-nbctl"]


def list_lrps() -> list[dict]:
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


def add_lrp(router: str, name: str, mac: str, networks: list[str]) -> None:
    run_in_netns([*_NBCTL, "lrp-add", router, name, mac, *networks], None)


def delete_lrp(name: str) -> None:
    run_in_netns([*_NBCTL, "--if-exists", "lrp-del", name], None)
