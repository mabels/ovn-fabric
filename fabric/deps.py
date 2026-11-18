"""A host's abstract dependencies, from the IR — what `fabric ir-apply`
asks abstract_deps for, and the `--spec` of `python3 -m fabric.deps`:

    python3 -m fabric.deps py.ir --host net-test-hh-b
    [{"name": "ovn"}, {"name": "ovn-central"}, {"name": "ovs"}, …]

Only abstract names: the runtime translates them for the host's distro
(fabric/hostos.py), unless the IR's `dependencyOverrides` replace that
name's packages. `ovn` is ovn-controller on every OVN host; the central
also gets `ovn-central`. This lives on ovn-fabric's side: the runtime
never reads the IR itself.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from protocol import generated as pt
from protocol.hydrate import hydrate_nodes

# The live RawIR collector needs nft on every host; this is runtime
# knowledge, so it is added here rather than generated into the IR.
CORE_DEPS = ("nftables",)


def abstract_deps(host_node: pt.InfraHostNode) -> list[dict[str, Any]]:
    """[{name}] — the host's dependencies, in the IR's order."""
    out: list[dict[str, Any]] = []
    names = dict.fromkeys([*(host_node.data.abstractDependencies or []), *CORE_DEPS])
    for name in names:
        out.append({"name": name})
        if name == "ovn" and host_node.data.ovnRole == pt.OvnRole.central:
            out.append({"name": "ovn-central"})  # the NB/SB DBs and northd too
    return out


def main() -> int:
    parser = argparse.ArgumentParser(prog="fabric.deps")
    parser.add_argument("ir", help="the IR (JSON); - for stdin")
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    raw = json.load(sys.stdin if args.ir == "-" else open(args.ir, encoding="utf-8"))
    hosts = {n.key.host: n for n in hydrate_nodes(raw) if isinstance(n, pt.InfraHostNode)}
    if args.host not in hosts:
        parser.error(f"unknown host {args.host!r}; known hosts: {', '.join(sorted(hosts))}")
    deps = abstract_deps(hosts[args.host])
    json.dump(deps, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
