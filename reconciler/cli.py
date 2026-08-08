# reconciler/cli.py — top-level orchestrator: sweeps the global
# namespace plus every real `ip netns`, calling each --kind reconciler
# once per namespace, merges their IR nodes, emits canonical JSON.
#
# The namespace sweep lives here, in exactly one place, not inside any
# individual reconciler — every net-related reconciler (linux_net,
# iptables, ovn, ovs) takes the namespace as an explicit argument
# (reconcile(scope, netns)) rather than enumerating `ip netns list`
# itself, so this loop is the only place that logic exists. See
# ladops/netns.py — the reconciler package only ever reads through
# ladops, never talks to `ip`/`nft`/`ovn-nbctl`/`ovs-vsctl` directly;
# that's the whole point of the split (ladops/, not reconciler/, is
# what actually knows how to reach the real system, so the deployer can
# reuse the exact same knowledge for writes later).
#
# Extensible on purpose (see docs/adr/0002-intermediate-representation.md,
# "Reconciler/deployer runtime" -> "Reconciler package layout"): adding a
# fourth reconciler later means adding a subpackage with a
# reconcile(scope, netns) -> dict[str, dict] function and one entry in
# _RECONCILERS below — nothing about this file's argument parsing or
# orchestration loop needs to change.

from __future__ import annotations

import argparse
import json
import sys
from typing import Callable

from ladops.netns import list_netns

from .host import hostname as host_hostname
from .host import reconcile as host_reconcile
from .iptables import reconcile as iptables_reconcile
from .linux_net import reconcile as linux_net_reconcile
from .ovn import reconcile as ovn_reconcile
from .ovs import reconcile as ovs_reconcile

ReconcileFn = Callable[[dict, "str | None"], dict[str, dict]]

_RECONCILERS: dict[str, ReconcileFn] = {
    "host": host_reconcile,
    "linux-net": linux_net_reconcile,
    "iptables": iptables_reconcile,
    "ovn": ovn_reconcile,
    "ovs": ovs_reconcile,
}


def _serialize(nodes: dict[str, dict], nice: bool) -> str:
    # A JSON array of nodes, not an object keyed by node id: every node
    # already carries its own "id" field, so a dict wrapper would just
    # repeat that same string twice on the wire for no reason. The dict
    # form (dict[str, dict]) is still what reconcile_all merges through
    # internally — it's what gives cross-kind/cross-namespace "last one
    # wins by matching id" merge semantics O(1) — this only changes the
    # shape at the actual output boundary.
    array = sorted(nodes.values(), key=lambda n: n["id"])
    if nice:
        return json.dumps(array, indent=2)
    return json.dumps(array, separators=(",", ":"))


def reconcile_all(kinds: list[str], scope: dict, *, strict: bool) -> dict[str, dict]:
    # strict=True (an explicit --kind was requested): a stub reconciler
    # raising NotImplementedError is a real failure, propagate it.
    # strict=False (default: every known kind): skip a still-stub kind
    # with a warning instead of losing the real output from every kind
    # that *does* work — confirmed necessary by a real run on the actual
    # router, which hard-crashed on the first stub (iptables) reached.
    nodes: dict[str, dict] = {}
    for netns in [None, *list_netns()]:
        for kind in kinds:
            try:
                nodes.update(_RECONCILERS[kind](scope, netns))
            except NotImplementedError as e:
                if strict:
                    raise
                print(f"warning: skipping --kind {kind}: {e}", file=sys.stderr)
    return nodes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconciler",
        description="Reconcile a router's live state into IR.",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="write the IR JSON here instead of stdout",
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=sorted(_RECONCILERS),
        help="reconciler(s) to run, repeatable (default: all known kinds)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="override the discovered hostname used as every node's base scope "
        "(default: real hostname, discovered by the host reconciler)",
    )
    parser.add_argument(
        "--nice",
        action="store_true",
        help="pretty-print the JSON (2-space indent) instead of compact single-line output",
    )
    args = parser.parse_args(argv)

    # Scope is a structured dict from here all the way down through every
    # reconciler's node-key construction, not a string — so nothing
    # anywhere needs to parse a string to recover which host/namespace a
    # fact belongs to. Hostname comes from the host reconciler, not from
    # cli.py calling socket.gethostname() itself — hostname discovery
    # lives in exactly one place (reconciler/host/reconcile.py).
    scope = {"host": args.host if args.host is not None else host_hostname()}

    kinds = args.kind if args.kind else sorted(_RECONCILERS)
    nodes = reconcile_all(kinds, scope, strict=bool(args.kind))
    serialized = _serialize(nodes, args.nice)

    if args.output:
        with open(args.output, "w") as f:
            f.write(serialized + "\n")
    else:
        sys.stdout.write(serialized + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
