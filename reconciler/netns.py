# reconciler/netns.py — shared network-namespace plumbing, used by
# every net-related reconciler (linux_net, and eventually iptables/ovn/
# ovs — anything whose facts vary by which netns you're looking from),
# not owned by any single one of them.
#
# Each net-related reconciler's reconcile(scope, netns) takes the
# namespace as an explicit argument rather than looping over `ip netns
# list` internally — cli.py's orchestration loop (reconciler/cli.py)
# owns the "call every kind once per namespace" sweep in exactly one
# place, and calls this module's list_netns()/netns_scope() to do it,
# so every reconciler stays consistent without each reimplementing the
# same enumeration and scope-formatting logic.
#
# Scope hierarchy: host scope is always the base (e.g. "host:mam-hh-
# ovn"), and each namespace is an explicit sub-scope of it:
# "{host}|netns:*global*" for the root namespace, "{host}|netns:ns-
# uplink-voda-avm" etc. for each real one — "*global*" is the literal
# name requested for the root namespace, not left blank/implicit.

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


def netns_scope(base_scope: str, netns: str | None) -> str:
    return f"{base_scope}|netns:{netns if netns is not None else GLOBAL_NETNS}"


def run(argv: list[str], netns: str | None) -> subprocess.CompletedProcess:
    """Run argv, inside `netns` if given (via `ip netns exec`), in the
    root/global namespace otherwise. Not `ip`-specific — iptables-save,
    ovs-vsctl etc. are all netns-sensitive commands the same way."""
    cmd = argv if netns is None else ["ip", "netns", "exec", netns, *argv]
    return subprocess.run(cmd, check=True, capture_output=True, text=True)
