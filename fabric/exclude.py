"""Nodes left out of RawIR comparisons: the exclude list (globs over node
ids, `*` any text) and what every host leaves alone (DEFAULT_EXCLUDE).

Apart from fabric/desired.py on purpose: primate (which generates
fabric/rawir_learned.py) uses it, so it must not import the learned
library itself.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Sequence
from typing import Any

# Left alone on every host: what the kernel makes by itself — the loopback
# with its addresses and routes, and every link's IPv6 link-local address
# (from its MAC) with its routes.
DEFAULT_EXCLUDE = (
    "link:*|lo",
    "addr:*|lo|*",
    "route:*|lo|*",
    "addr:*|fe80:*",
    "route:*|ipv6|local|fe80:*",
    "route:*|ipv6|main|fe80::/64|*",
)


def excluded(node_id: Any, patterns: Sequence[str]) -> bool:
    return isinstance(node_id, str) and any(fnmatch.fnmatchcase(node_id, p) for p in patterns)


def without(value: Any, patterns: Sequence[str]) -> Any:
    """`value` without references to excluded nodes (in lists)."""
    if isinstance(value, list):
        return [without(item, patterns) for item in value if not excluded(item, patterns)]
    if isinstance(value, dict):
        return {key: without(item, patterns) for key, item in value.items()}
    return value


def exclude_nodes(
    nodes: dict[str, dict[str, Any]], patterns: Sequence[str]
) -> tuple[dict[str, dict[str, Any]], int]:
    """`nodes` without the excluded ones and without references to them;
    and how many were excluded."""
    kept = {i: without(n, patterns) for i, n in nodes.items() if not excluded(i, patterns)}
    return kept, len(nodes) - len(kept)
