"""The observed RawIR: what the host has now, collected live
(`raw_reconciler`), in the same shape as the desired RawIR
(fabric/desired.py) — by node id, without the collection metadata
(`host`, `time`) that changes every time.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

from raw_reconciler.collect import collect

Nodes = dict[str, dict[str, Any]]

_METADATA = ("host", "time")


def _plain(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def snapshot() -> Nodes:
    """The live RawIR of this host, by node id."""
    nodes: Nodes = {}
    for node in collect().collections:
        plain = _plain(node)
        for key in _METADATA:
            plain.pop(key, None)
        nodes[plain["id"]] = plain
    return nodes
