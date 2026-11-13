"""The containers a host runs, from the IR — the `--spec` of
`python3 -m fabric docker-run`:

    python3 -m fabric.containers py.ir --host net-test-hh-b
    [{"name": "router-zerotier-v2-zerotier", "image": "…", "dockerfile": "…",
      "command": ["sleep", "infinity"], "privileged": true, "network": "none", …}]

Two kinds, as the IR has them:

- a kernel router's docker app that owns its interfaces: kept alive by
  `sleep infinity`, its app is started inside once its legs are injected;
- a workload container (kernel.app.container): its own command and env.

A workload's `files` go along; docker-run writes and mounts them. Not
derived yet: its DNS flags (they depend on its NICs). This lives on
ovn-fabric's side: the runtime never reads the IR.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from fabric.emit import _docker_dockerfile, _kernel_router_owners, _kernel_router_sides
from protocol import generated as pt
from protocol.hydrate import hydrate_nodes


def containers(nodes: list[pt.Model], host: str) -> list[dict[str, Any]]:
    host_id = f"host:{host}"
    out: list[dict[str, Any]] = []
    for owner in _kernel_router_owners(host_id, nodes):
        for side in _kernel_router_sides(owner.key.name, nodes):
            for app in side.data.apps or []:
                if app.kind != "docker" or app.ip is not None:
                    continue  # the client mode (one injected veth) is not covered yet
                name = app.name or f"{owner.key.name}-docker"
                spec: dict[str, Any] = {
                    "name": name,
                    "image": app.image or f"ovn-fabric-{name}",
                    "command": ["sleep", "infinity"],
                    "privileged": True,
                    "network": "none",
                }
                if app.build is not None:
                    spec["dockerfile"] = _docker_dockerfile(app.build)
                if app.volumes:
                    spec["mounts"] = [
                        {"host": v.host, "container": v.container} for v in app.volumes
                    ]
                if app.run:
                    spec["flags"] = list(app.run)
                out.append(spec)
    for node in nodes:
        if node.kind != "kernel.app.container" or node.data.host != host_id:
            continue
        spec = {
            "name": f"ovn-fabric-{node.key.name}",
            "image": node.data.image,
            "privileged": True,
            "network": "none",
        }
        if node.data.build is not None:
            spec["dockerfile"] = _docker_dockerfile(node.data.build)
        if node.data.cmd:
            spec["command"] = list(node.data.cmd)
        if node.data.env:
            spec["env"] = [{"name": k, "value": v} for k, v in sorted(node.data.env.items())]
        if node.data.files:
            spec["files"] = [
                {
                    "path": f.path,
                    "content": (
                        "\n".join(f.content) + "\n" if isinstance(f.content, list) else f.content
                    ),
                }
                for f in node.data.files
            ]
        out.append(spec)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(prog="fabric.containers")
    parser.add_argument("ir", help="the IR (JSON); - for stdin")
    parser.add_argument("--host", required=True)
    args = parser.parse_args()
    raw = json.load(sys.stdin if args.ir == "-" else open(args.ir, encoding="utf-8"))
    nodes = hydrate_nodes(raw)
    hosts = {node.key.host for node in nodes if node.kind == "infra.host"}
    if args.host not in hosts:
        parser.error(f"unknown host {args.host!r}; known hosts: {', '.join(sorted(hosts))}")
    json.dump(containers(nodes, args.host), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
