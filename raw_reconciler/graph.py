from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

from protocol import raw_generated as raw
from protocol.hydrate import hydrate_as


class RawGraphError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class RawGraph:
    """Hydrated raw snapshot plus its reference indexes.

    `nodes` are the generated ArkType dataclasses; every `*Ref`/`*Refs`
    field is guaranteed to resolve after construction.
    """

    host: str
    time: str
    nodes: dict[str, raw.RawCollection]
    outgoing: dict[str, tuple[str, ...]]
    incoming: dict[str, tuple[str, ...]]

    def get(self, node_id: str) -> raw.RawCollection:
        try:
            return self.nodes[node_id]
        except KeyError as e:
            raise RawGraphError(f"unknown raw node id {node_id!r}") from e

    def by_kind(self, kind: str) -> list[raw.RawCollection]:
        return [node for node in self.nodes.values() if node.kind == kind]

    def netns(self, name: str) -> raw.NetnsCollection:
        node = self.get(f"netns:{name}")
        if not isinstance(node, raw.NetnsCollection):
            raise RawGraphError(f"{name!r} is not a netns node")
        return node

    def links_of(self, netns: raw.NetnsCollection | str) -> list[raw.LinkCollection]:
        ns = self.netns(netns) if isinstance(netns, str) else netns
        return [
            node
            for node in (self.get(ref) for ref in ns.linkRefs)
            if isinstance(node, raw.LinkCollection)
        ]

    def routes_of(self, netns: raw.NetnsCollection | str) -> list[raw.RouteCollection]:
        ns = self.netns(netns) if isinstance(netns, str) else netns
        return [
            node
            for node in (self.get(ref) for ref in ns.routeRefs)
            if isinstance(node, raw.RouteCollection)
        ]

    def rules_of(self, netns: raw.NetnsCollection | str) -> list[raw.IptablesRuleCollection]:
        ns = self.netns(netns) if isinstance(netns, str) else netns
        return [
            node
            for node in (self.get(ref) for ref in ns.ruleRefs)
            if isinstance(node, raw.IptablesRuleCollection)
        ]

    def ports_of(self, bridge: raw.OvsBridgeCollection | str) -> list[raw.OvsPortCollection]:
        node = self.get(bridge) if isinstance(bridge, str) else bridge
        if not isinstance(node, raw.OvsBridgeCollection):
            raise RawGraphError(f"{bridge!r} is not an OVS bridge")
        return [
            port
            for port in (self.get(ref) for ref in node.portRefs)
            if isinstance(port, raw.OvsPortCollection)
        ]


def _ref_fields(node: raw.RawCollection) -> dict[str, tuple[str, ...]]:
    """Every reference field of a node, normalized to a tuple of ids."""
    out: dict[str, tuple[str, ...]] = {}
    for field in dataclasses.fields(node):
        value = getattr(node, field.name)
        if field.name.endswith("Refs") and isinstance(value, list):
            out[field.name] = tuple(item for item in value if isinstance(item, str))
        elif field.name.endswith("Ref") and isinstance(value, str):
            out[field.name] = (value,)
    return out


def graph_from_snapshot(payload: Mapping[str, Any] | str | bytes) -> RawGraph:
    """Rebuild a runtime graph from the serialized RawSnapshot JSON.

    Hydration is strict (unknown fields/kinds fail), then every reference
    is resolved against the node map. The returned graph exposes forward
    and reverse adjacency plus typed convenience lookups.
    """
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    if not isinstance(payload, Mapping):
        raise RawGraphError("raw snapshot must be a JSON object")
    snapshot = hydrate_as(raw.RawSnapshot, dict(payload))

    nodes = {node.id: node for node in snapshot.collections}
    if len(nodes) != len(snapshot.collections):
        raise RawGraphError("duplicate raw node id in snapshot")

    outgoing: dict[str, tuple[str, ...]] = {}
    incoming_sets: dict[str, set[str]] = {node_id: set() for node_id in nodes}
    for node_id, node in nodes.items():
        refs = _ref_fields(node)
        all_refs: list[str] = []
        for field, ids in refs.items():
            for ref in ids:
                if ref not in nodes:
                    raise RawGraphError(
                        f"{node_id}: field {field!r} points at missing node {ref!r}"
                    )
                all_refs.append(ref)
                incoming_sets[ref].add(node_id)
        outgoing[node_id] = tuple(all_refs)

    incoming = {node_id: tuple(sorted(refs)) for node_id, refs in incoming_sets.items()}
    return RawGraph(
        host=snapshot.host,
        time=snapshot.time,
        nodes=nodes,
        outgoing=outgoing,
        incoming=incoming,
    )
