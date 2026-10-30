"""A host's abstract dependencies and their system dependencies, from the
IR — the `--spec` of `python3 -m fabric abstract-deps`:

    python3 -m deployer.deps py.ir --host net-test-hh-b
    [{"name": "ovn", "systemDeps": ["ovn-central", "ovn-host"]}, …]

The mapping is the deployer's own (_PACKAGE_BY_DEP via _package_jobs,
`ovn` by the host's OVN role). A dependency installed by a command rather
than a package is left out (with a note on stderr): the runtime's sysdeps
only installs apt packages so far. This lives on ovn-fabric's side: the
runtime (rt) never reads the IR.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from protocol import generated as pt
from protocol.hydrate import hydrate_nodes

from .ir_common import _dependency_resolution, _package_jobs


def abstract_deps(host_node: pt.InfraHostNode) -> tuple[list[dict[str, Any]], list[str]]:
    """([{name, systemDeps}], notes)."""
    _jobs, notes = _package_jobs(host_node)
    out: list[dict[str, Any]] = []
    for name, resolution in _dependency_resolution(host_node).items():
        if resolution["kind"] != "apt":
            notes.append(f"{name}: installed by a command, not a package — left out")
            continue
        out.append({"name": name, "systemDeps": list(resolution["packages"])})
    return out, notes


def main() -> int:
    parser = argparse.ArgumentParser(prog="deployer.deps")
    parser.add_argument("ir", help="the IR (JSON); - for stdin")
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    raw = json.load(sys.stdin if args.ir == "-" else open(args.ir, encoding="utf-8"))
    hosts = {n.key.host: n for n in hydrate_nodes(raw) if n.kind == "infra.host"}
    if args.host not in hosts:
        parser.error(f"unknown host {args.host!r}; known hosts: {', '.join(sorted(hosts))}")
    deps, notes = abstract_deps(hosts[args.host])
    for note in notes:
        print(f"note: {note}", file=sys.stderr)
    json.dump(deps, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
