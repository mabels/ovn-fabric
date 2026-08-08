# ladops/ovs.py — real List against OVS's own database via `ovs-vsctl`.
# Read-only for now, matching reconciler/iptables — no concrete
# add_*/delete_* need identified yet, and speculatively designing one
# without a real deployer consumer to validate it against isn't worth
# doing ahead of need.
#
# `ovs-vsctl -f json list <table>`, not `ovs-vsctl show` — same reason
# as ladops/ovn.py: `list` returns real self-describing structured data
# (a "headings" column-name array alongside "data" rows), decoded via
# the shared ladops/ovsdb.py (the same OVSDB wire format ladops/ovn.py
# uses).

from __future__ import annotations

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
