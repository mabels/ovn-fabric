# ladops/linux_net.py — real List/Add/Delete against a namespace's
# link/addr/route state via `ip -j ...`/`ip ...`. list_*() is used by
# reconciler/linux_net/reconcile.py to build IR nodes (read only); the
# add_*/delete_*() functions exist for the deployer, which needs the
# same "how do I run `ip` inside a given namespace" knowledge to apply a
# diff, not a separately reimplemented copy of it. No update_* — there's
# no atomic "change this fact in place" primitive at this layer either
# (same reasoning as every kind in docs/adr/0002-intermediate-
# representation.md's node-kind tables: del-old + add-new, always);
# composing add+delete into what looks like an update is the deployer's
# call, not something this module decides for it.
#
# add_addr/delete_addr and add_route/delete_route are genuinely per-key
# operations — unlike reconciler/iptables (nft rule handles aren't
# stable identity, so the deployer has to replace a whole table+chain
# atomically, see docs/adr/0002-intermediate-representation.md,
# "Firewall / security-group subschema") — `ip addr add/del <value> dev
# <dev>` and `ip route add/del <prefix> dev <dev> [via <nexthop>]` are
# real, atomic, single-fact operations with no equivalent handle-
# instability problem.

from __future__ import annotations

import json
import shlex
from typing import Any

from . import JsonObject
from .netns import run as run_in_netns


def _run_ip_json(args: list[str], netns: str | None) -> Any:
    out = run_in_netns(["ip", "-j", *args], netns).stdout
    return json.loads(out)


def list_links(netns: str | None) -> list[JsonObject]:
    # peerInOtherNetns: real `ip -j link show` sets `link_netnsid` on a
    # device whose `link` (parent/peer, e.g. a veth's other end, or a
    # VLAN sub-interface's parent NIC) lives in a different namespace —
    # confirmed on the real router for both cases: a moved veth end
    # (`veth-krn-0`) and a moved VLAN sub-interface (`ens18.1280`) both
    # carry it, `lo` never does. This is the closest real, verifiable
    # signal to "was this device added to the namespace from elsewhere"
    # — not `netns exec`-derivable any other way (`ip netns` itself has
    # no membership-listing subcommand at all).
    #
    # Deliberately just a boolean, not a resolved source namespace name:
    # `link_netnsid` is a small integer that's *locally* scoped to
    # whichever namespace you're viewing from (confirmed: `ip netns
    # list-id` run from inside a real ns-uplink-* netns shows only "nsid
    # 0" with no name attached — names are only resolvable from wherever
    # created the /var/run/netns bind mounts). Claiming a specific
    # source namespace from inside a non-root netns would mean trusting
    # an unverified topology assumption (that every cross-netns link
    # here points back to global specifically), not something this data
    # alone proves.
    return [
        {
            "ifname": entry["ifname"],
            "linkType": entry.get("link_type", "unknown"),
            "operstate": entry.get("operstate", "UNKNOWN"),
            "peerInOtherNetns": "link_netnsid" in entry,
        }
        for entry in _run_ip_json(["link", "show"], netns)
    ]


def list_addrs(netns: str | None) -> list[JsonObject]:
    # Deliberately unfiltered — every address including fe80:: link-local
    # ones is returned. Reporting a complete, faithful record of what's
    # real is this module's job; deciding what's relevant (autoconfigured
    # vs. topology-managed) is the caller's.
    out = []
    for entry in _run_ip_json(["addr", "show"], netns):
        ifname = entry["ifname"]
        for a in entry.get("addr_info", []):
            addr = f"{a['local']}/{a['prefixlen']}"
            out.append(
                {"addr": addr, "interface": ifname, "family": "ipv6" if ":" in addr else "ipv4"}
            )
    return out


def list_routes(netns: str | None, family: str) -> list[JsonObject]:
    # Family comes from which command produced the entry, not from
    # sniffing the value for ":" — the destination is literally the bare
    # string "default" for BOTH families (confirmed against a real
    # router's `ip route show`/`ip -6 route show`), so string-sniffing
    # silently mislabels every v6 default route as v4.
    args = ["-6", "route", "show"] if family == "-6" else ["route", "show"]
    out = []
    for r in _run_ip_json(args, netns):
        dev = r.get("dev", "unknown")
        out.append(
            {
                "prefix": r["dst"],
                "dev": dev,
                "nexthop": r.get("gateway") or dev,
                "family": "ipv6" if family == "-6" else "ipv4",
            }
        )
    return out


def add_addr_argv(addr: str, dev: str) -> list[str]:
    return ["ip", "addr", "add", addr, "dev", dev]


def delete_addr_argv(addr: str, dev: str) -> list[str]:
    return ["ip", "addr", "del", addr, "dev", dev]


def add_route_argv(
    prefix: str, dev: str, nexthop: str | None, metric: float | None = None
) -> list[str]:
    args = ["ip", "route", "add", prefix, "dev", dev]
    if nexthop and nexthop != dev:
        args += ["via", nexthop]
    if metric is not None:
        # `metric` only decides between EQUAL-prefix routes: the workload's
        # default-egress preference (lower wins). Connected routes are more
        # specific, so they always win regardless (2026-09-19).
        args += ["metric", str(int(metric))]
    return args


def delete_route_argv(prefix: str, dev: str) -> list[str]:
    return ["ip", "route", "del", prefix, "dev", dev]


def add_addr(addr: str, dev: str, netns: str | None) -> None:
    run_in_netns(add_addr_argv(addr, dev), netns)


def delete_addr(addr: str, dev: str, netns: str | None) -> None:
    run_in_netns(delete_addr_argv(addr, dev), netns)


def add_route(prefix: str, dev: str, nexthop: str | None, netns: str | None) -> None:
    run_in_netns(add_route_argv(prefix, dev, nexthop), netns)


def delete_route(prefix: str, dev: str, netns: str | None) -> None:
    run_in_netns(delete_route_argv(prefix, dev), netns)


# ── VLAN sub-interfaces (real-world binding for ovn.ls collision ────
# domains — deployer/ir_common.py's per-host script) ───────────────
# Argv builders exposed separately from their run_in_netns wrappers,
# same reasoning as ladops/ovn.py: the generator needs the exact argv
# to print, not to execute.


def add_vlan_argv(parent: str, name: str, vlan_id: int) -> list[str]:
    return ["ip", "link", "add", "link", parent, "name", name, "type", "vlan", "id", str(vlan_id)]


def ensure_vlan_argv(parent: str, name: str, vlan_id: int) -> list[str]:
    """Create the VLAN subinterface only if it's missing — `ip link add`
    has no `--may-exist`, so a host-level VLAN that survived from a
    previous run would abort the whole create pass (confirmed live
    2026-09-19). The guard rides inside one `sh -c` (the same idiom as
    the zerotier installer and set_sysctl_file_argv) so it stays a single
    argv operation: the Python deployer shlex-splits it back to `sh -c
    '<script>'` and the shell runs the `||`."""
    script = (
        f"ip link show {shlex.quote(name)} >/dev/null 2>&1 || "
        f"ip link add link {shlex.quote(parent)} name {shlex.quote(name)} "
        f"type vlan id {vlan_id}"
    )
    return ["sh", "-c", script]


def ensure_veth_argv(name: str, peer: str) -> list[str]:
    """Create a veth pair only if the `name` side is missing — `ip link
    add` has no --may-exist, same reasoning as ensure_vlan_argv above.
    Used for the per-segment legs between the XDP trunk bridge and the
    segment bridge (deployer/ir_common.py's _emit_xdp_leg)."""
    script = (
        f"ip link show {shlex.quote(name)} >/dev/null 2>&1 || "
        f"ip link add {shlex.quote(name)} type veth peer name {shlex.quote(peer)}"
    )
    return ["sh", "-c", script]


# A real, addressable stand-in for a KernelRouterSide's own device — see
# that type's own doc comment (types.ts): no real iface binding yet
# ("iface mappings into the namespace" is its own next step), so a
# dummy netdev is what addresses/routes actually attach to for now.
def add_dummy_argv(name: str) -> list[str]:
    return ["ip", "link", "add", name, "type", "dummy"]


# A KernelRouter's own transit veth pair (types.ts's InterfaceKind "veth"
# variant) — `name` is the ROOT-side leg (later attached to the transit
# domain's OVS bridge), `peer` the leg moved into the kernel router's
# netns by deployer/ir_common.py's _emit_kernel_router_create. Run from
# the root namespace (2026-08-18).
#
# `peer_mac` pins the PEER leg's MAC (`address` after `peer name` applies
# to the peer, not to `name`). The generator derives it from the leg's
# own address (src/addressing.ts's macFromAddrsOrName) so it is the SAME
# on every recreation: an OVN logical router never ages or re-probes a
# learned MAC_Binding, so a leg that came back with a new random MAC kept
# receiving frames addressed to the old one, which the kernel drops as
# "other host" (confirmed live 2026-10-11). None keeps the kernel's
# random MAC (an IR written before the generator resolved one).
def add_veth_argv(name: str, peer: str, peer_mac: str | None = None) -> list[str]:
    argv = ["ip", "link", "add", name, "type", "veth", "peer", "name", peer]
    if peer_mac is not None:
        argv += ["address", peer_mac]
    return argv


def set_link_up_argv(name: str) -> list[str]:
    return ["ip", "link", "set", name, "up"]


# Set a sysctl knob in place — used to enable IPv4/IPv6 forwarding inside
# a KernelRouter's netns (deployer/ir_common.py's _emit_kernel_router_
# create): a kernel router IS a router, and Linux won't forward between
# its two sides until `net.ipv4.ip_forward`/`net.ipv6.conf.all.forwarding`
# say so (2026-08-19).
def set_sysctl_argv(key: str, value: str) -> list[str]:
    return ["sysctl", "-w", f"{key}={value}"]


def set_sysctl_file_argv(path: str, value: str) -> list[str]:
    """Write a sysctl value by writing the proc file DIRECTLY — sysctl's
    own key parsing splits on EVERY dot (and slash), so an interface name
    like `eth0.2280` can never be a sysctl key component (the dotted key
    `net.ipv6.conf.eth0.2280.accept_ra` becomes the path
    `.../eth0/2280/accept_ra`). Writing `/proc/sys/net/ipv6/conf/eth0.2280/
    accept_ra` keeps the dotted name intact (2026-08-30)."""
    return ["sh", "-c", f"echo {value} > {path}"]


def delete_link_argv(name: str) -> list[str]:
    return ["ip", "link", "delete", name]


def add_vlan(parent: str, name: str, vlan_id: int, netns: str | None = None) -> None:
    run_in_netns(add_vlan_argv(parent, name, vlan_id), netns)


def set_link_up(name: str, netns: str | None = None) -> None:
    run_in_netns(set_link_up_argv(name), netns)


def delete_link(name: str, netns: str | None = None) -> None:
    run_in_netns(delete_link_argv(name), netns)


def add_if_to_netns_argv(dev: str, netns: str) -> list[str]:
    return ["ip", "link", "set", dev, "netns", netns]


def add_if_to_netns(dev: str, netns: str) -> None:
    """Move `dev` (currently in the global/root namespace) into `netns`
    — `ip link set <dev> netns <netns>`, run from global, since a device
    is only visible to `ip link set` from whichever namespace it's
    currently in."""
    run_in_netns(add_if_to_netns_argv(dev, netns), None)


def delete_if_to_netns(dev: str, netns: str) -> None:
    """The inverse of add_if_to_netns: move `dev` back out of `netns`,
    into the global/root namespace. Must run from inside `netns` itself
    (same "only visible from its current namespace" reasoning), target
    is `netns 1` — PID 1's namespace, the standard idiom for "the root/
    global namespace" when a bare name for it isn't otherwise reachable
    from inside another namespace."""
    run_in_netns(["ip", "link", "set", dev, "netns", "1"], netns)
