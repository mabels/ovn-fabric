"""The docker images a host runs, from the IR — the `--spec` of
`python3 -m fabric docker-prepare`:

    python3 -m fabric.images py.ir --host net-test-hh-b
    [{"image": "busybox"}, {"image": "ovn-fabric-…", "dockerfile": "FROM …"}]

Each entry is built from its Dockerfile when the IR declares a `build`,
else pulled. This lives on ovn-fabric's side: the runtime never
reads the IR.
"""

from __future__ import annotations

import argparse
import json
import sys

from fabric.emit import _image_jobs
from protocol.hydrate import hydrate_nodes


def images(nodes: list[object], host: str) -> list[dict[str, str]]:
    host_id = f"host:{host}"
    out: list[dict[str, str]] = []
    for job in _image_jobs(host_id, nodes):  # type: ignore[arg-type]
        entry = {"image": job["image"]}
        if job["dockerfile"] is not None:
            entry["dockerfile"] = job["dockerfile"]
        out.append(entry)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(prog="fabric.images")
    parser.add_argument("ir", help="the IR (JSON); - for stdin")
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    raw = json.load(sys.stdin if args.ir == "-" else open(args.ir, encoding="utf-8"))
    nodes = hydrate_nodes(raw)
    hosts = {node.key.host for node in nodes if node.kind == "infra.host"}
    if args.host not in hosts:
        parser.error(f"unknown host {args.host!r}; known hosts: {', '.join(sorted(hosts))}")
    json.dump(images(list(nodes), args.host), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
