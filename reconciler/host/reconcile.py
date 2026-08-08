# reconciler/host/reconcile.py — this host's own identity: hostname
# (which every other reconciler's scope is derived from — cli.py calls
# `hostname()` directly rather than reaching for socket.gethostname()
# itself, so hostname discovery lives in exactly one place), plus
# `uname -a` and a capture timestamp with timezone as descriptive data.
#
# `uname -a`: shelled out for real, not reconstructed from `platform.
# uname()`'s structured fields — the point is byte-identical output to
# what a human gets running the command themselves, useful for support/
# debugging ("does this match what I see"), not just equivalent info.
#
# The timestamp, by contrast, uses stdlib `datetime` directly rather
# than shelling out to `date` — Python's own `datetime.now().
# astimezone()` gets the local time WITH the system's UTC offset
# natively, no subprocess needed for something the standard library
# already does reliably.

from __future__ import annotations

import socket
import subprocess
from datetime import datetime


def hostname() -> str:
    return socket.gethostname()


def _uname_a() -> str:
    return subprocess.run(
        ["uname", "-a"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _reconciled_at() -> str:
    return datetime.now().astimezone().isoformat()


def reconcile(scope: dict, netns: str | None = None) -> dict[str, dict]:
    # Host identity isn't namespace-scoped — there's exactly one of it
    # per host, not one per netns. Takes the same (scope, netns) shape
    # as every other reconciler so cli.py's orchestration loop can call
    # all of them uniformly, but only produces nodes for the netns=None
    # (global) pass; every other namespace contributes nothing here.
    if netns is not None:
        return {}
    # name comes from scope["host"], not a second hostname() call here —
    # cli.py already resolved it once (real discovery, or a --host
    # override), and this node has to agree with every other node's
    # scope, not silently re-discover the real hostname even when the
    # caller asked to override it.
    name = scope["host"]
    key = {"host": name}
    id_ = f"host:{name}"
    return {
        id_: {
            "id": id_,
            "kind": "infra.host",
            "key": key,
            "data": {
                "name": name,
                "unameA": _uname_a(),
                "reconciledAt": _reconciled_at(),
            },
        }
    }
