# ladops/__init__.py — the one type every ladops reader shares.
from typing import Any, TypeAlias

# One decoded JSON object exactly as a tool printed it (`ip -j`, `nft -j`,
# an ovsdb row) — or a live-state fact/node built from one. Deliberately
# `Any`-valued: this is the LIVE-state side, whose shapes belong to those
# tools, not to this project. The DESIRED-state IR is the opposite — fully
# typed, see protocol/generated.py.
JsonObject: TypeAlias = dict[str, Any]
