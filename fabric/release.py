"""Release before claim (ADR 0010): the leftovers that hold a single
resource the plan claims.

A rename leaves the old owner of a single resource (a VLAN, a NIC, a veth)
as a leftover that still holds it, and the new owner's step fails: a VLAN
can be the port of one bridge only. Both sides are matched by the natural
key (raw_reconciler/natural_key.py), never by name:

- **claimed**: a natural key of a node some step's footprint adds;
- **held**: the live node with that key, and what attaches it directly —
  the nodes pointing at it (`linkRef`, `devRef`, …; an OVS interface holds
  the link of its name), and an OVS interface's port. Never further up: a
  bridge holds more than one resource.

A held node that is ours and no step fulfils is released: removed before the
plan applies (step 3). This module only finds them.
"""

from __future__ import annotations

import collections
import dataclasses
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ladops.linux_net import key_hash
from raw_reconciler.natural_key import all_keys

from .rawir_learned import NotLearned, rawir

Graph = Mapping[str, Mapping[str, Any]]

_GLOBAL = "*global*"


@dataclasses.dataclass(frozen=True)
class Release:
    """A leftover to remove before the plan applies, and why."""

    node_id: str
    key: str  # the natural key it holds
    claimed_by: str  # the planned node that claims it


def _wireguard(step: Any) -> tuple[str, str] | None:
    """A WireGuard step's identity: the hash of the private key in its
    config — as the collector hashes the live one."""
    for line in str(step.conf).splitlines():
        name, _, value = line.partition("=")
        if name.strip() == "PrivateKey" and value.strip():
            return f"wg:{key_hash(value)}", f"link:{step.netns}|{step.iface}"
    return None


def _container(step: Any) -> tuple[str, str] | None:
    """A container's declared natural key (a ZeroTier instance)."""
    return (step.natural_key, f"docker.container:{step.name}") if step.natural_key else None


# Keys a footprint cannot carry (a secret's hash, a label): from the step.
PLANNED: dict[str, Callable[[Any], tuple[str, str] | None]] = {
    "WireguardPrimitive": _wireguard,
    "DockerPrimitive": _container,
}


def claims(steps: Iterable[Any], live: Graph) -> dict[str, str]:
    """The natural keys the plan's steps claim: key → the planned node id.
    A planned node's key may need another planned node (a router port's
    switch is its planned peer port's): keys are taken over live and
    planned."""
    out: dict[str, str] = {}
    planned_nodes: dict[str, Mapping[str, Any]] = {}
    for step in steps:
        planned = PLANNED.get(type(step).__name__)
        if planned is not None and (claim := planned(step)) is not None:
            out.setdefault(claim[0], claim[1])
        if getattr(step, "step_kind", None) != "Primitive":
            continue
        try:
            added = rawir(step)["added"]
        except (NotLearned, TypeError):
            continue
        planned_nodes.update(added)
    graph = {**live, **planned_nodes}
    for node_id, node in planned_nodes.items():
        if node.get("kind") == "ovn.lsp":
            continue  # only says where a router port is
        for key in all_keys(node, graph):
            out.setdefault(key, node_id)
    return out


def _holders_index(live: Graph) -> dict[str, set[str]]:
    """Who points at whom: node id → the ids of the nodes referring to it."""
    index: dict[str, set[str]] = collections.defaultdict(set)
    for node_id, node in live.items():
        collection = node.get("collection")
        fields: Mapping[str, Any] = collection if isinstance(collection, Mapping) else node
        for field, value in fields.items():
            if field == "id" or not (field.endswith("Ref") or field.endswith("Refs")):
                continue
            for target in value if isinstance(value, list) else [value]:
                if isinstance(target, str) and target in live:
                    index[target].add(node_id)
        # An OVS interface holds the root netns link of its name.
        if node.get("kind") == "ovs.iface" and fields.get("name"):
            link = f"link:{_GLOBAL}|{fields['name']}"
            if link in live:
                index[link].add(node_id)
    return index


# What contains many things — never released early for one of them.
_CONTAINERS = ("netns", "ovs.bridge", "ovn.lr", "ovn.ls")


def _held(node_id: str, live: Graph, index: Mapping[str, set[str]]) -> list[str]:
    """The node and what attaches it directly: the nodes pointing at it —
    and for an OVS interface, the port carrying it. Not further up: a
    bridge, or what holds the bridge, holds more than this one resource;
    it stays an ordinary leftover for the end."""
    out = [node_id]
    for holder in sorted(index.get(node_id, ())):
        if live.get(holder, {}).get("kind") in _CONTAINERS:
            continue
        out.append(holder)
        if live.get(holder, {}).get("kind") == "ovs.iface":
            out.extend(sorted(index.get(holder, ())))
    return list(dict.fromkeys(out))


def find(steps: Iterable[Any], live: Graph, leftovers: Iterable[str]) -> list[Release]:
    """The leftovers holding a single resource the plan claims."""
    # A container is released by its natural key even though the cleanup
    # never takes it (cleanup.NEVER_CLEANED): it is a leftover here.
    left = set(leftovers) | {
        node_id for node_id, node in live.items() if node.get("kind") == "docker.container"
    }
    claimed = claims(steps, live)
    if not claimed:
        return []
    index = _holders_index(live)
    out: list[Release] = []
    seen: set[str] = set()
    for node_id, key in (
        (node_id, key) for node_id, node in live.items() for key in all_keys(node, live)
    ):
        planned = claimed.get(key)
        if planned is None:
            continue
        for held in _held(node_id, live, index):
            # The planned node itself is where it belongs: never released.
            if held in seen or held == planned:
                continue
            # A port the plan wants too (same id) is still held when the
            # bridge carrying it is a leftover: it is on the old bridge.
            on_old_bridge = live.get(held, {}).get("kind") == "ovs.port" and any(
                bridge in left for bridge in index.get(held, ())
            )
            if held in left or on_old_bridge:
                seen.add(held)
                out.append(Release(held, key, planned))
    return out
