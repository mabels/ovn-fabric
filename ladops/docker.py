from __future__ import annotations

import json
import subprocess
from typing import Any


def _run(args: list[str]) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


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
            }
        )
    return out
