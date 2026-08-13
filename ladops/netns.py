# ladops/netns.py — shared network-namespace plumbing, used by every
# net-related ladops module (linux_net, iptables, ovn, ovs — anything
# whose facts vary by which netns you're looking from), not owned by
# any single one of them. ladops is the one place that knows *how* to
# talk to the real system — both reconciler/<kind>/reconcile.py (read
# only) and the deployer (read + write) call into it rather than each
# reimplementing "how do I run this inside a given namespace."
#
# Each net-related ladops function takes the namespace as an explicit
# argument rather than looping over `ip netns list` internally —
# reconciler/cli.py's orchestration loop owns the "call every kind once
# per namespace" sweep in exactly one place, and calls this module's
# list_netns()/netns_scope() to do it, so every caller stays consistent
# without each reimplementing the same enumeration and scope-formatting
# logic.
#
# Scope hierarchy: host scope is always the base (e.g. {"host": "mam-hh-
# ovn"}), and each namespace is an explicit sub-scope of it —
# netns_scope() adds "netns": "*global*" for the root namespace,
# "netns": "ns-uplink-voda-avm" etc. for each real one — "*global*" is
# the literal name requested for the root namespace, not left blank/
# implicit.
#
# Scope is a structured dict, not a string, all the way from
# reconciler/cli.py down through every reconciler's node-key
# construction — so that no caller anywhere ever needs to parse a
# string to recover which host/namespace a fact belongs to. scope_id()
# is the one place a flat string gets derived from it, for building each
# node's `id` (today's opaque, single-string key format).

from __future__ import annotations

import subprocess

GLOBAL_NETNS = "*global*"


def list_netns() -> list[str]:
    # Real output is one namespace per line, e.g. "ns-uplink-voda-avm
    # (id: 0)" — the id suffix is display-only, so only the first
    # whitespace-separated token is the actual namespace name `ip netns
    # exec` expects.
    out = subprocess.run(
        ["ip", "netns", "list"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def netns_scope(base_scope: dict, netns: str | None) -> dict:
    return {**base_scope, "netns": netns if netns is not None else GLOBAL_NETNS}


def scope_id(scope: dict) -> str:
    """The flat "host:X|netns:Y" string form of a structured scope —
    used only for building a node's `id`, never for reading attributes
    back out of (that's what the structured scope/key dicts are for)."""
    parts = [f"host:{scope['host']}"]
    if "netns" in scope:
        parts.append(f"netns:{scope['netns']}")
    return "|".join(parts)


def netns_exec_argv(argv: list[str], netns: str | None) -> list[str]:
    """Wrap argv to run inside `netns` if given (via `ip netns exec`),
    unwrapped otherwise — the SAME wrapping run() applies below, exposed
    separately so a generator (deployer/ir_to_shell.py) can print the
    exact argv without executing it, same reasoning as add_netns_argv/
    delete_netns_argv above."""
    return argv if netns is None else ["ip", "netns", "exec", netns, *argv]


def run(argv: list[str], netns: str | None) -> subprocess.CompletedProcess:
    """Run argv, inside `netns` if given (via `ip netns exec`), in the
    root/global namespace otherwise. Not `ip`-specific — iptables-save,
    ovs-vsctl etc. are all netns-sensitive commands the same way."""
    cmd = netns_exec_argv(argv, netns)
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def add_netns_argv(name: str) -> list[str]:
    """`ip netns add` — the mount-based naming convention every
    list_netns()/net.netns fact already relies on. Argv exposed
    separately from add_netns() below, same reasoning as ladops/ovn.py:
    deployer/ir_to_shell.py (the generator) needs the exact argv to
    print, not to execute. Not netns-relative — `ip netns add/delete`
    manage /var/run/netns, reachable the same way regardless of which
    namespace you happen to be running from — so this always runs in
    the global context, unlike add_if_to_netns (linux_net.py)."""
    return ["ip", "netns", "add", name]


def add_netns(name: str) -> None:
    run(add_netns_argv(name), None)


def delete_netns_argv(name: str) -> list[str]:
    """`ip netns delete` — see add_netns_argv above for why this is
    exposed separately from delete_netns() below."""
    return ["ip", "netns", "delete", name]


def delete_netns(name: str) -> None:
    run(delete_netns_argv(name), None)
