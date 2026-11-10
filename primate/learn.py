"""From observed RawIR snapshots to a template — pure data, no system.

A snapshot is `{node id: node}` (a node a dict of its fields, without the
collection metadata `host`/`time`). For one run of a sample:

    before  RawIR after the sample's setup
    after   … with the primitive applied
    undone  … with the primitive deleted again

`observe()` turns a run into what the primitive did — the nodes it added,
the fields it changed on nodes that were there, the nodes of its own it
took away (a link moved elsewhere), and whether delete undid it — with
every marker value replaced by its parameter's placeholder (`{name}`, or
`{addr|ip}` when the system reports a transform of it).
`template()` merges two runs with different markers: what still differs
between them is volatile (a UUID, an ifindex) and is left out.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping
from typing import Any

Snapshot = dict[str, dict[str, Any]]

# A placeholder: {param} or {param|transform}.
_PLACEHOLDER = re.compile(r"\{[a-z_0-9]+(\|[a-z]+)?\}")

# How the system may report a parameter's value: (name, transform). The
# first one that matches wins; "" is the value as given.
Transform = Callable[[str], Any]


def _ip(value: str) -> str:
    return str(ipaddress.ip_interface(value).ip)


def _len(value: str) -> int:
    return ipaddress.ip_interface(value).network.prefixlen


def _net(value: str) -> str:
    return str(ipaddress.ip_interface(value).network)


def _canonical(value: str) -> str:
    return str(ipaddress.ip_interface(value))


def _broadcast(value: str) -> str:
    return str(ipaddress.ip_interface(value).network.broadcast_address)


def _netaddr(value: str) -> str:
    return str(ipaddress.ip_interface(value).network.network_address)


TRANSFORMS: list[tuple[str, Transform]] = [
    ("", lambda value: value),
    ("lower", str.lower),
    ("upper", str.upper),
    ("ip", _ip),
    ("len", _len),
    ("net", _net),
    ("canonical", _canonical),
    ("broadcast", _broadcast),
    ("netaddr", _netaddr),
]


class Unexplained(ValueError):
    """Two runs differ in a way no parameter and no transform explains."""


def _forms(markers: Mapping[str, Any]) -> list[tuple[Any, str]]:
    """(reported value, placeholder) for every marker and transform that
    applies to it, the longest strings first (so a name inside another
    one is not replaced first)."""
    forms: list[tuple[Any, str]] = []
    for param, value in markers.items():
        if not isinstance(value, str):  # a number: reported as it is
            forms.append((value, "{" + param + "}"))
            continue
        for name, transform in TRANSFORMS:
            try:
                reported = transform(value)
            except ValueError:
                continue
            placeholder = "{" + param + (f"|{name}" if name else "") + "}"
            if all(reported != seen for seen, _ in forms):
                forms.append((reported, placeholder))
    return sorted(forms, key=lambda form: -len(str(form[0])))


def placeholders(value: Any, markers: Mapping[str, Any]) -> Any:
    """`value` with every marker (or a transform of one) replaced by its
    placeholder: a whole value becomes the placeholder, a string that
    contains one (an id, a reference) gets it substituted."""
    forms = _forms(markers)
    return _replace(value, forms)


def _replace(value: Any, forms: list[tuple[Any, str]]) -> Any:
    if isinstance(value, dict):
        return {key: _replace(item, forms) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace(item, forms) for item in value]
    for reported, placeholder in forms:
        if value == reported and type(value) is type(reported):
            return placeholder
    if isinstance(value, str):
        for reported, placeholder in forms:
            # Inside a string (an id, a reference): a marker long enough to
            # be its own — a number too (an ACL's priority in its id).
            text = str(reported) if isinstance(reported, int) else reported
            if isinstance(text, str) and len(text) >= 4 and text in value:
                value = value.replace(text, placeholder)
    return value


def _changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What changed on a node that was there: per field, the list items
    added/removed, or the value before and after."""
    out: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if isinstance(old, list) and isinstance(new, list):
            added = [item for item in new if item not in old]
            removed = [item for item in old if item not in new]
            out[key] = {"added": added, "removed": removed}
        else:
            out[key] = {"from": old, "to": new}
    return out


def observe(
    before: Snapshot, after: Snapshot, undone: Snapshot, markers: Mapping[str, Any]
) -> dict[str, Any]:
    """What one run of a primitive did, in placeholders."""
    added = {
        placeholders(node_id, markers): placeholders(after[node_id], markers)
        for node_id in sorted(set(after) - set(before))
    }
    # Nodes of its own (a marker in the id) that it takes away: a link it
    # moves to another namespace is gone from the first one.
    removed = sorted(
        key
        for node_id in set(before) - set(after)
        if _PLACEHOLDER.search(key := placeholders(node_id, markers))
    )
    changed = {}
    for node_id in sorted(set(after) & set(before)):
        if before[node_id] == after[node_id]:
            continue
        key = placeholders(node_id, markers)
        change = placeholders(_changes(before[node_id], after[node_id]), markers)
        # The primitive's own change involves one of its markers; anything
        # else changed meanwhile on a live system (OVN updating its rows).
        if _PLACEHOLDER.search(key) or _PLACEHOLDER.search(repr(change)):
            changed[key] = change
    # What delete did not undo — of the primitive's own (involving a marker;
    # anything else changed meanwhile on the live system).
    leftover = sorted(
        node_id
        for node_id in set(before) | set(undone)
        if before.get(node_id) != undone.get(node_id)
        and (
            _PLACEHOLDER.search(placeholders(node_id, markers))
            or _PLACEHOLDER.search(
                repr(
                    placeholders(
                        _changes(before.get(node_id, {}), undone.get(node_id, {})), markers
                    )
                )
            )
        )
    )
    return {"added": added, "changed": changed, "removed": removed, "leftover": leftover}


def template(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """One template from two observations with different markers: what is
    the same in both is the template; a field that differs is volatile.
    Nodes that are not in both (by their placeholder id) are unexplained."""
    first, second = dict(first), dict(second)
    for part in ("added", "changed"):
        only = set(first[part]) ^ set(second[part])
        # In one run only, without a marker: the live system changing
        # meanwhile (a router coming up), not what the sample did.
        noise = {
            node_id
            for node_id in only
            if not _PLACEHOLDER.search(node_id)
            and not _PLACEHOLDER.search(repr((first[part] | second[part])[node_id]))
        }
        if only - noise:
            raise Unexplained(f"{part}: not in both runs: {sorted(only - noise)}")
        first[part] = {i: n for i, n in first[part].items() if i not in noise}
        second[part] = {i: n for i, n in second[part].items() if i not in noise}
    volatile: set[str] = set()
    added: dict[str, Any] = {}
    for node_id, node in first["added"].items():
        other = second["added"][node_id]
        fields: dict[str, Any] = {}
        for key in sorted(set(node) | set(other)):
            if node.get(key) == other.get(key):
                fields[key] = node.get(key)
            else:
                volatile.add(f"{node.get('kind', '?')}.{key}")
        added[node_id] = fields
    changed = {
        node_id: change
        for node_id, change in first["changed"].items()
        if change == second["changed"][node_id]
    }
    for node_id in set(first["changed"]) - set(changed):
        volatile.add(f"{node_id}: changed fields")
    removed_first, removed_second = first.get("removed", []), second.get("removed", [])
    if removed_first != removed_second:
        differ = sorted(set(removed_first) ^ set(removed_second))
        raise Unexplained(f"removed: not in both runs: {differ}")
    return {
        "added": added,
        "changed": changed,
        "removed": removed_first,
        "volatile": sorted(volatile),
        "leftover": sorted(set(first["leftover"]) | set(second["leftover"])),
    }


def no_trace(free: Mapping[str, Any], learned: Mapping[str, Any]) -> None:
    """A `free` field must leave no trace in what was learned: none of its
    (longer) values shows up in the footprint. Unset (None), it has none."""
    footprint = repr((learned["added"], learned["changed"]))
    for field, value in free.items():
        parts = value if isinstance(value, tuple) else (value,)
        for part in parts:
            if part is None:
                continue
            text = str(part)
            if len(text) >= 4 and text in footprint:
                raise Unexplained(f"free field {field}: {text!r} shows up in RawIR")


def pass_on(maps: Mapping[str, tuple[str, Any]], learned: dict[str, Any]) -> None:
    """A `maps` field: the node's field holds the parameter, as a mapping."""
    for field, (node_field, _value) in maps.items():
        for node in learned["added"].values():
            if node_field in node:
                node[node_field] = "{" + field + "|map}"
