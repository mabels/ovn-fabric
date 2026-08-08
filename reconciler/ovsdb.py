# reconciler/ovsdb.py — shared OVSDB CLI JSON decoding, used by both
# reconciler/ovn and reconciler/ovs — they're different databases
# (OVN's northbound DB vs. OVS's own), but `ovn-nbctl`/`ovs-vsctl -f
# json list <table>` both speak the same OVSDB wire encoding (RFC 7047):
# scalars appear bare, everything else is a tagged 2-element array —
# ["uuid", "<uuid>"], ["set", [...]], ["map", [[k, v], ...]].
#
# `-f json list <table>` specifically, not `show` (ovn-nbctl's `show`
# ignores --format entirely — confirmed against the real router, it
# prints its normal tree-text output either way) — `list` returns real
# structured data: {"data": [[...row...], ...], "headings": [...]}, the
# column names in `headings` matching each row's positional order, so
# no separate `--columns` request or schema lookup is needed to turn a
# row back into a name-keyed dict.

from __future__ import annotations

import json
from typing import Any

from .netns import run as run_in_netns


def decode(atom: Any) -> Any:
    if isinstance(atom, list) and len(atom) == 2 and atom[0] in ("uuid", "set", "map"):
        tag, value = atom
        if tag == "uuid":
            return value
        if tag == "set":
            return [decode(v) for v in value]
        if tag == "map":
            return {k: decode(v) for k, v in value}
    return atom


def list_table(argv: list[str], table: str, netns: str | None = None) -> list[dict]:
    """Run `<argv> -f json list <table>` and return one plain dict per
    row, keyed by real column name, every OVSDB-tagged value decoded."""
    out = run_in_netns([*argv, "-f", "json", "list", table], netns).stdout
    parsed = json.loads(out)
    headings = parsed["headings"]
    return [{h: decode(v) for h, v in zip(headings, row)} for row in parsed["data"]]
