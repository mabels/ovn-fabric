# ladops/ovs.py — real List/Add/Delete against OVS's own database via
# `ovs-vsctl`.
#
# `ovs-vsctl -f json list <table>`, not `ovs-vsctl show` — same reason
# as ladops/ovn.py: `list` returns real self-describing structured data
# (a "headings" column-name array alongside "data" rows), decoded via
# the shared ladops/ovsdb.py (the same OVSDB wire format ladops/ovn.py
# uses).
#
# external_id write primitives exist for chassis registration
# (ovn-remote/ovn-encap-ip/ovn-encap-type/ovn-cms-options — the real
# consumer is deployer/ir_common.py's per-host script, converting
# infra.host IR nodes) — the concrete need this module's own header
# comment used to say wasn't identified yet.
#
# Never run for real in this session (or anywhere): this project's only
# real router is live production infrastructure. Every write function is
# verified by asserting the exact argv its *_argv() builder produces,
# not by executing it.

from __future__ import annotations

from . import JsonObject
from .netns import run as run_in_netns
from .ovsdb import list_table

_VSCTL = ["ovs-vsctl"]


def _uuids(value: object) -> list[str]:
    # OVSDB decodes a one-element set as a bare scalar, not a list.
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


def list_bridge_ports() -> list[JsonObject]:
    """Every OVS bridge port, joined to its bridge and interface facts."""
    iface_by_uuid = {row["_uuid"]: row for row in list_table(_VSCTL, "Interface")}
    bridge_by_port_uuid: dict[str, str] = {}
    for bridge in list_table(_VSCTL, "Bridge"):
        for port_uuid in _uuids(bridge["ports"]):
            bridge_by_port_uuid[port_uuid] = bridge["name"]
    result: list[JsonObject] = []
    for port in list_table(_VSCTL, "Port"):
        bridge_name = bridge_by_port_uuid.get(port["_uuid"])
        if bridge_name is None:
            continue
        for iface_uuid in _uuids(port["interfaces"]):
            iface = iface_by_uuid.get(iface_uuid)
            if iface is None:
                continue
            result.append(
                {
                    "bridge": bridge_name,
                    "port": port["name"],
                    "name": iface["name"],
                    "type": iface.get("type", ""),
                    "mac": iface.get("mac_in_use", ""),
                    "options": iface.get("options", {}),
                    "external_ids": iface.get("external_ids", {}),
                }
            )
    return result


def list_bridge_mappings() -> dict[str, str]:
    """bridge -> OVN network name from Open_vSwitch.ovn-bridge-mappings."""
    rows = list_table(_VSCTL, "Open_vSwitch")
    if not rows:
        return {}
    raw = rows[0].get("external_ids", {}).get("ovn-bridge-mappings", "")
    mappings: dict[str, str] = {}
    for item in raw.split(","):
        network, sep, bridge = item.partition(":")
        if sep and network and bridge:
            mappings[bridge] = network
    return mappings


def list_interfaces() -> list[JsonObject]:
    return [
        {
            "name": iface["name"],
            "type": iface["type"],
            "adminState": iface["admin_state"],
            "linkState": iface["link_state"],
            "ofport": iface["ofport"],
        }
        for iface in list_table(_VSCTL, "Interface")
    ]


# ── open_vswitch external-ids (chassis registration) ────────────────
# `.` (not a real row-id) is ovs-vsctl's own shorthand for "the one row
# in this table" — real, confirmed idiom, matches every ovs-vsctl
# external-ids write already used in this project's shell generators.


def set_external_id_argv(key: str, value: str) -> list[str]:
    return [*_VSCTL, "set", "open_vswitch", ".", f"external-ids:{key}={value}"]


def remove_external_id_argv(key: str) -> list[str]:
    # No --if-exists equivalent for a map key removal — ovs-vsctl remove
    # on a map column takes just the key (no value needed to identify
    # which pair to drop).
    return [*_VSCTL, "remove", "open_vswitch", ".", "external-ids", key]


def set_external_id(key: str, value: str) -> None:
    run_in_netns(set_external_id_argv(key, value), None)


def remove_external_id(key: str) -> None:
    run_in_netns(remove_external_id_argv(key), None)


# ── bridges + ports (real-world binding for ovn.ls collision domains —
# deployer/ir_common.py's per-host script) ─────────────────────────
# Both adds FAIL if the object already exists: the create pass is a
# desired-state pass, not a silent merge.
# "make it so" (README's idempotency guarantee), and an existing bridge/
# port is the expected state on every run after the first — without the
# flag `ovs-vsctl add-br` aborts the whole pass (confirmed live
# 2026-09-19). del-br/del-port use --if-exists and cascade, same
# reasoning as ladops.ovn.ls_del_argv.


def add_br_argv(bridge: str) -> list[str]:
    return [*_VSCTL, "add-br", bridge]


def del_br_argv(bridge: str) -> list[str]:
    return [*_VSCTL, "--if-exists", "del-br", bridge]


def set_bridge_fail_mode_argv(bridge: str, mode: str = "standalone") -> list[str]:
    return [*_VSCTL, "set", "bridge", bridge, f"fail-mode={mode}"]


def set_bridge_datapath_argv(bridge: str, datapath_type: str) -> list[str]:
    """`system` (kernel) is OVS's default; `netdev` is the userspace
    datapath DPDK/AF_XDP/vhost-user ports require."""
    return [*_VSCTL, "set", "bridge", bridge, f"datapath_type={datapath_type}"]


def set_port_tag_argv(port: str, tag: int) -> list[str]:
    """OVS access-port VLAN tag — the netdev datapath's replacement for a
    kernel `<parent>.<vlan>` sub-interface."""
    return [*_VSCTL, "set", "port", port, f"tag={tag}"]


def set_port_trunks_argv(port: str, vlan_ids: list[int]) -> list[str]:
    """Mark a port as a TRUNK for `vlan_ids`. An AF_XDP parent carrying
    several VLANs must be a trunk: without `trunks` OVS treats the port
    as untagged (VLAN 0), and every tagged frame from the wire is
    dropped before it can reach the `tag=<vid>` access legs (confirmed
    live, 2026-10-20 — `trunks: []` on `ovnxdp`, nothing forwarded)."""
    return [
        *_VSCTL,
        "set",
        "port",
        port,
        "trunks=" + ",".join(str(vid) for vid in vlan_ids),
    ]


def add_afxdp_port_argv(
    bridge: str,
    port: str,
    mode: str,
    n_rxq: int,
) -> list[str]:
    """An AF_XDP port on a netdev bridge. `mode` is one of OVS's own
    options:xdp-mode values (`best-effort`, `native-with-zerocopy`,
    `native`, `generic`); `n_rxq` its queue count."""
    return [
        *_VSCTL,
        "add-port",
        bridge,
        port,
        "--",
        "set",
        "Interface",
        port,
        "type=afxdp",
        f"options:xdp-mode={mode}",
        f"options:n_rxq={n_rxq}",
    ]


def add_port_argv(bridge: str, port: str) -> list[str]:
    return [*_VSCTL, "add-port", bridge, port]


def del_port_argv(bridge: str, port: str) -> list[str]:
    return [*_VSCTL, "--if-exists", "del-port", bridge, port]


def add_br(bridge: str) -> None:
    run_in_netns(add_br_argv(bridge), None)


def del_br(bridge: str) -> None:
    run_in_netns(del_br_argv(bridge), None)


def set_bridge_fail_mode(bridge: str, mode: str = "standalone") -> None:
    run_in_netns(set_bridge_fail_mode_argv(bridge, mode), None)


def add_port(bridge: str, port: str) -> None:
    run_in_netns(add_port_argv(bridge, port), None)


def del_port(bridge: str, port: str) -> None:
    run_in_netns(del_port_argv(bridge, port), None)
