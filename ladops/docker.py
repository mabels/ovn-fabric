from __future__ import annotations

import json
import subprocess
from typing import Any


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


# The label a container declares its natural key with (ADR 0010).
NATURAL_KEY_LABEL = "ovn-fabric.natural-key"


def list_containers() -> list[dict[str, Any]]:
    ids = _run(["docker", "ps", "-q"]).split()
    if not ids:
        return []
    rows = json.loads(_run(["docker", "inspect", *ids]))
    out: list[dict[str, Any]] = []
    for row in rows:
        state = row.get("State", {})
        netns = row.get("NetworkSettings", {})
        out.append(
            {
                "containerId": row["Id"],
                "name": row["Name"].lstrip("/"),
                "image": row.get("Config", {}).get("Image", ""),
                "pid": int(state.get("Pid") or 0),
                "state": state.get("Status", "unknown"),
                "netnsRef": f"netns:docker:{row['Name'].lstrip('/')}",
                "networks": sorted(netns.get("Networks", {}).keys()),
                "labels": row.get("Config", {}).get("Labels") or {},
            }
        )
    return out


def list_networks() -> list[dict[str, Any]]:
    """Docker's networks: the bridge interface each one has on the host
    (`docker0`, `br-<id>`, or its `bridge.name` option) and its subnets.
    Empty when docker is not there."""
    try:
        ids = _run(["docker", "network", "ls", "-q"]).split()
        rows = json.loads(_run(["docker", "network", "inspect", *ids])) if ids else []
    except (OSError, subprocess.CalledProcessError):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("Driver") != "bridge":
            continue  # host/none/macvlan…: no bridge of its own
        options = row.get("Options") or {}
        bridge = options.get("com.docker.network.bridge.name") or f"br-{row['Id'][:12]}"
        configs = (row.get("IPAM") or {}).get("Config") or []
        out.append(
            {
                "name": row.get("Name", ""),
                "bridge": bridge,
                "subnets": [c["Subnet"] for c in configs if c.get("Subnet")],
            }
        )
    return out
