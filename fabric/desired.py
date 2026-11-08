"""The desired RawIR: what a host looks like in RawIR once its IR is
applied — predicted from the plan's primitives by the learned library
(fabric/rawir_learned.py, ADR 0008), without touching the system.

Every primitive's footprint is merged: the nodes it adds, and the field
changes it makes to nodes added by another primitive (a link's address
refs) or not added by any (the global netns' routes — kept apart in
`changes`). Everything a primitive makes is ovn-fabric's: the live nodes
it is compared with are ours only (raw_reconciler/owners.py).

What cannot be predicted is counted, not guessed:

- `not_learned`: primitives primate has no footprint for yet (by class);
- `runtime_values`: primitives with a value only known when the plan
  runs (a captured chassis id) — their nodes are not predicted;
- `not_rawir`: operations that are no RawIR (files, captures, the netns
  link of a container) by step kind.
"""

from __future__ import annotations

import collections
import dataclasses
import enum
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from raw_reconciler.owners import on_links

from .lifecycle import RuntimeRef
from .rawir_learned import VOLATILE, NotLearned, rawir


@dataclasses.dataclass
class Desired:
    nodes: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    # Field changes on nodes no primitive adds: {id: {field: change}}.
    changes: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    not_learned: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    # Why, by `Class.field` (what primate would need a sample for).
    not_learned_why: collections.Counter[str] = dataclasses.field(
        default_factory=collections.Counter
    )
    runtime_values: list[str] = dataclasses.field(default_factory=list)
    not_rawir: collections.Counter[str] = dataclasses.field(default_factory=collections.Counter)
    # A node two primitives add differently: (id, unit that added it second).
    conflicts: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def _has_runtime_value(value: Any) -> bool:
    if isinstance(value, RuntimeRef):
        return True
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return any(_has_runtime_value(getattr(value, f.name)) for f in dataclasses.fields(value))
    if isinstance(value, (tuple, list)):
        return any(_has_runtime_value(item) for item in value)
    return False


def _why(exc: NotLearned) -> str:
    """`Class.field` of a NotLearned ("OVNNbCtlLr.options=…: learned only
    with ()") — without the value; one per learned shape that was tried
    ("RoutePrimitive.via|RoutePrimitive.metric")."""
    reasons = [re.match(r"\w+(\.\w+)?", part.strip()) for part in str(exc).split("; ")]
    return "|".join(dict.fromkeys(m.group(0) for m in reasons if m)) or str(exc)


def _apply_change(fields: dict[str, Any], change: dict[str, Any]) -> None:
    for field, delta in change.items():
        if "added" in delta:
            items = list(fields.get(field) or [])
            items += [item for item in delta["added"] if item not in items]
            fields[field] = [item for item in items if item not in delta["removed"]]
        else:
            fields[field] = delta["to"]


def _merge_change(into: dict[str, Any], change: dict[str, Any]) -> None:
    for field, delta in change.items():
        if "added" in delta and field in into and "added" in into[field]:
            into[field]["added"] += [i for i in delta["added"] if i not in into[field]["added"]]
            into[field]["removed"] += delta["removed"]
        else:
            into[field] = delta


def desired_rawir(operations: Iterable[tuple[str, Any]]) -> Desired:
    """The desired RawIR of a plan's (unit, operation) list."""
    out = Desired()
    pending: list[tuple[str, dict[str, Any]]] = []  # changes, applied once all nodes are in
    for unit, operation in operations:
        if getattr(operation, "step_kind", None) != "Primitive":
            out.not_rawir[getattr(operation, "step_kind", type(operation).__name__)] += 1
            continue
        if _has_runtime_value(operation):
            out.runtime_values.append(f"{unit}: {type(operation).__name__}")
            continue
        try:
            footprint = rawir(operation)
        except NotLearned as exc:
            out.not_learned[type(operation).__name__] += 1
            out.not_learned_why[_why(exc)] += 1
            continue
        for node_id in footprint.get("removed", []):
            out.nodes.pop(node_id, None)
        for node_id, node in footprint["added"].items():
            if node_id in out.nodes and out.nodes[node_id] != node:
                out.conflicts.append((node_id, unit))
                continue
            out.nodes[node_id] = dict(node)
        pending += footprint["changed"].items()
    for node_id, change in pending:
        if node_id in out.nodes:
            _apply_change(out.nodes[node_id], change)
        else:
            _merge_change(out.changes.setdefault(node_id, {}), change)
    return out


@dataclasses.dataclass
class Comparison:
    """Desired against observed RawIR, by node id."""

    applied: list[str] = dataclasses.field(default_factory=list)  # in both, alike
    to_apply: list[str] = dataclasses.field(default_factory=list)  # only desired
    only_observed: list[str] = dataclasses.field(default_factory=list)  # a removal, or foreign
    differ: dict[str, list[str]] = dataclasses.field(default_factory=dict)  # id -> fields


def compare(desired: dict[str, dict[str, Any]], observed: dict[str, dict[str, Any]]) -> Comparison:
    """Which desired nodes are there and alike, which are missing, which
    observed ones no primitive wants — and which fields differ. Fields the
    system fills in differently every time (VOLATILE) are not compared."""
    volatile = {field for fields in VOLATILE.values() for field in fields}
    volatile |= {f"iptables.rule.{field}" for field in ("handle", "nftHandle")}  # its place
    out = Comparison()
    for node_id in sorted(set(desired) | set(observed)):
        want, have = desired.get(node_id), observed.get(node_id)
        if have is None:
            out.to_apply.append(node_id)
        elif want is None:
            out.only_observed.append(node_id)
        else:
            kind = want.get("kind", "?")
            fields = sorted(
                field
                for field in set(want) | set(have)
                if f"{kind}.{field}" not in volatile and want.get(field) != have.get(field)
            )
            if fields:
                out.differ[node_id] = fields
            else:
                out.applied.append(node_id)
    return out


class NodeState(enum.Enum):
    """What run knows about a node of the reconciled graph."""

    observed = "observed"  # live, and nothing desired matches it (yet)
    present = "present"  # desired, and already live
    applied = "applied"  # desired, and created by this run
    missing = "missing"  # desired, and its create failed


@dataclasses.dataclass
class Reconciled:
    """The reconciled graph: the live RawIR as run found it, updated by
    what run did — every node with its state. A node run creates is
    inserted as its primitive's footprint predicts it, and the footprint's
    field changes land on the nodes they touch, so the graph stays the
    host's RawIR without collecting it again."""

    nodes: dict[str, dict[str, Any]]
    state: dict[str, NodeState]

    @classmethod
    def of(cls, live: dict[str, dict[str, Any]], desired: dict[str, dict[str, Any]]) -> Reconciled:
        """From the live RawIR: what the desired RawIR also has is present,
        the rest observed."""
        state = {
            node_id: NodeState.present if node_id in desired else NodeState.observed
            for node_id in live
        }
        return cls({node_id: dict(node) for node_id, node in live.items()}, state)

    def applied(self, operation: Any) -> None:
        """`operation` was created: its footprint goes into the graph."""
        footprint = rawir(operation)
        for node_id in footprint.get("removed", []):
            self.nodes.pop(node_id, None)
            self.state.pop(node_id, None)
        for node_id, node in footprint["added"].items():
            self.nodes[node_id] = dict(node)
            self.state[node_id] = NodeState.applied
        for node_id, change in footprint["changed"].items():
            if node_id in self.nodes:
                _apply_change(self.nodes[node_id], change)

    def failed(self, operation: Any) -> None:
        """`operation`'s create failed: its nodes are missing."""
        for node_id in rawir(operation)["added"]:
            if self.state.get(node_id) is not NodeState.present:
                self.state[node_id] = NodeState.missing

    def counts(self) -> collections.Counter[str]:
        return collections.Counter(state.value for state in self.state.values())


class StepState(enum.Enum):
    """What the live RawIR says about one step of a unit."""

    present = "present"  # learned: every node it adds is live
    missing = "missing"  # learned: none of its nodes is live
    partial = "partial"  # learned: some of its nodes are live
    read = "read"  # safe to run every time: a capture, a file, no commands at all
    unknown = "unknown"  # not learned, a run-time value, or no RawIR (files, …)


# Steps that are safe to run again and again: a capture reads; a file is
# written over, a directory made if missing, a mode set again.
_REPEATABLE = ("Capture", "WriteFile", "Directory", "Chmod")


def step_state(
    operation: Any,
    live: dict[str, dict[str, Any]],
    consumed: Mapping[str, bool] | None = None,
) -> tuple[StepState, dict[str, list[str]]]:
    """The step's state, and — when present — the fields where its
    predicted nodes differ from the live ones. A predicted node is looked
    up by its id, whoever the live node belongs to now.

    `consumed`: nodes a later step takes away (a link it moves into a
    container) — and whether that step has happened. A node it took, or
    one on it (an address on the moved link), is not where this step put
    it any more: it counts as there when the later step happened."""
    kind = getattr(operation, "step_kind", None)
    if kind in _REPEATABLE or _no_commands(operation):
        return StepState.read, {}
    if kind != "Primitive" or _has_runtime_value(operation):
        return StepState.unknown, {}
    try:
        footprint = rawir(operation)
    except NotLearned:
        return StepState.unknown, {}
    added = footprint["added"]
    if not added:
        return _changes_state(footprint["changed"], live), {}
    taken = {i for i, n in added.items() if _taken(i, n, consumed or {})}
    there = [i for i in added if i in live or i in taken]
    if not there:
        return StepState.missing, {}
    if len(there) < len(added):
        return StepState.partial, {}
    checked = {i: n for i, n in added.items() if i not in taken}
    return StepState.present, compare(checked, {i: live[i] for i in checked}).differ


def _no_commands(operation: Any) -> bool:
    """A primitive that runs nothing (the root netns, a container's netns
    only referred to): nothing to check, nothing to do — like a read."""
    as_primitive = getattr(operation, "as_primitive", None)
    if as_primitive is None or _has_runtime_value(operation):
        return False
    primitive = as_primitive()
    return not primitive.create_cmds and not primitive.delete_cmds


def _taken(node_id: str, node: dict[str, Any], consumed: Mapping[str, bool]) -> bool:
    """Taken away by a later step that happened: the node itself, or a
    node on one (its linkRef/devRef)."""
    refs = [node_id, *(value for value in node.values() if isinstance(value, str))]
    return any(consumed.get(ref, False) for ref in refs)


def consumed_later(
    units: Sequence[Sequence[Any]], live: dict[str, dict[str, Any]]
) -> list[dict[str, bool]]:
    """Per unit (in plan order, by its steps): the nodes steps of LATER
    units take away, and whether that step happened (its own nodes are
    live)."""
    out: list[dict[str, bool]] = []
    later: dict[str, bool] = {}
    for steps in reversed(units):
        out.append(dict(later))
        for step in steps:
            if getattr(step, "step_kind", None) != "Primitive" or _has_runtime_value(step):
                continue
            try:
                footprint = rawir(step)
            except NotLearned:
                continue
            if not footprint.get("removed"):
                continue
            own = list(footprint["added"])
            happened = bool(own) and all(i in live for i in own)
            for node_id in footprint["removed"]:
                later[node_id] = later.get(node_id, False) or happened
    return list(reversed(out))


def _changes_state(
    changed: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]]
) -> StepState:
    """A step that only changes fields of nodes others add (a bridge's
    mapping): present when every change is live, missing when none is.
    Without changes nothing can be told."""
    done = [
        node_id in live
        and all(_has(live[node_id].get(field), delta) for field, delta in change.items())
        for node_id, change in changed.items()
    ]
    if not done:
        return StepState.unknown
    if all(done):
        return StepState.present
    return StepState.partial if any(done) else StepState.missing


def _has(value: Any, delta: dict[str, Any]) -> bool:
    """`value` (a live field) already shows the change `delta`: the items
    it adds are there and the ones it removes are not, or it has the value
    it sets — a mapping at least its entries (others may be there too)."""
    if "added" in delta:
        items = value or []
        return all(i in items for i in delta["added"]) and not any(
            i in items for i in delta["removed"]
        )
    target = delta["to"]
    if isinstance(target, dict) and isinstance(value, dict):
        return all(value.get(key) == item for key, item in target.items())
    return bool(value == target)


def missing_nodes(operation: Any, live: dict[str, dict[str, Any]]) -> list[str]:
    """The nodes a learned step would add that are not live (what makes a
    step partly there)."""
    try:
        added = rawir(operation)["added"]
    except NotLearned:
        return []
    return sorted(i for i in added if i not in live)


def _wireguard_owns(step: Any, live: dict[str, dict[str, Any]]) -> set[str]:
    """What `wg-quick up` makes follows from its config, not its fields:
    the link with what is on it, and its nft table."""
    owned = on_links(live, {f"link:{step.netns}|{step.iface}"})
    table = f"wg-quick-{step.iface}"
    return owned | {
        node_id
        for node_id, node in live.items()
        if node["kind"] == "iptables.rule"
        and node.get("netnsRef") == f"netns:{step.netns}"
        and node.get("table") == table
    }


# What a step makes that no footprint can say: class name -> the live nodes.
_OWNS: dict[str, Callable[[Any, dict[str, dict[str, Any]]], set[str]]] = {
    "WireguardPrimitive": _wireguard_owns,
}


def crossed_out(
    steps: Iterable[Any], live: dict[str, dict[str, Any]]
) -> tuple[set[str], list[str]]:
    """The cross-out of the initial graph: the node ids the plan's steps
    (their values resolved) fulfil — and the classes of the steps that could
    not be translated into RawIR."""
    fulfilled: set[str] = set()
    untranslated: set[str] = set()
    for step in steps:
        name = type(step).__name__
        if getattr(step, "step_kind", None) != "Primitive" or _no_commands(step):
            continue
        if getattr(step, "no_rawir", False):
            continue
        if name in _OWNS:
            fulfilled |= _OWNS[name](step, live)
            continue
        try:
            fulfilled.update(rawir(step)["added"])
        except NotLearned:
            untranslated.add(name)
    return fulfilled, sorted(untranslated)


@dataclasses.dataclass(frozen=True)
class Choice:
    """What run does with one unit: `run` it (skipping the steps `skip`),
    or leave it alone — `why`, in words, either way. `unchecked`: left
    alone because run cannot tell whether it is there."""

    run: bool
    skip: list[int]
    why: str
    unchecked: bool = False


def choose(states: Sequence[StepState]) -> Choice:
    """run's choice for a unit, from its steps' states. The learned steps
    tell whether the unit is there:

    - none of them there (the unit is gone, e.g. deleted): run all of it —
      the steps that cannot be checked too, they belong to it;
    - all of them there: run only its captures (later units need their
      values) — nothing, when it has none;
    - some there: run the missing ones — only when every other step can
      be checked; otherwise leave the unit alone (it cannot be told what
      its other steps would hit).

    Without a learned step nothing can be told: such a unit is left alone,
    unless it only reads."""
    learned = [i for i, s in enumerate(states) if s not in (StepState.read, StepState.unknown)]
    reads = [i for i, s in enumerate(states) if s is StepState.read]
    unknown = [i for i, s in enumerate(states) if s is StepState.unknown]
    present = [i for i in learned if states[i] is StepState.present]
    every = range(len(states))
    if any(states[i] is StepState.partial for i in learned):
        return Choice(False, [], "a step is partly there", unchecked=True)
    if not learned:
        if reads and not unknown:
            return Choice(True, [], "only reads")
        return Choice(False, [], "no learned step to tell whether it is there", unchecked=True)
    if not present:
        return Choice(True, [], "not there: all of it")
    if len(present) == len(learned):
        if reads:
            return Choice(True, [i for i in every if i not in reads], "there: only its reads")
        return Choice(False, [], "there")
    if unknown:
        return Choice(False, [], "partly there, with steps that cannot be checked", unchecked=True)
    return Choice(True, present, "partly there: the missing steps")
