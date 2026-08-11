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
# consumer is deployer/ir_to_shell.py's per-host script, converting
# infra.host IR nodes) — the concrete need this module's own header
# comment used to say wasn't identified yet.
#
# Never run for real in this session (or anywhere): this project's only
# real router is live production infrastructure. Every write function is
# verified by asserting the exact argv its *_argv() builder produces,
# not by executing it.

from __future__ import annotations

from .netns import run as run_in_netns
from .ovsdb import list_table

_VSCTL = ["ovs-vsctl"]


def list_interfaces() -> list[dict]:
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
# deployer/ir_to_shell.py's per-host script) ─────────────────────────
# add-br has no --may-exist here (matches ladops.ovn's own no-may-exist
# rule for adds); del-br uses --if-exists and cascades its own ports —
# same reasoning as ladops.ovn.ls_del_argv.


def add_br_argv(bridge: str) -> list[str]:
    return [*_VSCTL, "add-br", bridge]


def del_br_argv(bridge: str) -> list[str]:
    return [*_VSCTL, "--if-exists", "del-br", bridge]


def set_bridge_fail_mode_argv(bridge: str, mode: str = "standalone") -> list[str]:
    return [*_VSCTL, "set", "bridge", bridge, f"fail-mode={mode}"]


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
