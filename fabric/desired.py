"""The desired RawIR: what a host looks like in RawIR once its IR is
applied — predicted from the plan's primitives by the learned library
(fabric/rawir_learned.py, ADR 0008), without touching the system.

Every primitive's footprint is merged: the nodes it adds, and the field
changes it makes to nodes added by another primitive (a link's address
refs) or not added by any (the global netns' routes — kept apart in
`changes`). Then the exclude list is applied: matching node ids
(globs, `*` any text) are dropped, and so are references to them. The
loopback is always excluded (DEFAULT_EXCLUDE).

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
from collections.abc import Iterable, Sequence
from typing import Any

from .exclude import DEFAULT_EXCLUDE, exclude_nodes, excluded, without
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
    excluded: int = 0


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


def desired_rawir(operations: Iterable[tuple[str, Any]], exclude: Sequence[str] = ()) -> Desired:
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
    _exclude(out, [*DEFAULT_EXCLUDE, *exclude])
    return out


def _exclude(out: Desired, patterns: Sequence[str]) -> None:
    out.nodes, out.excluded = exclude_nodes(out.nodes, patterns)
    out.changes = {
        i: without(c, patterns) for i, c in out.changes.items() if not excluded(i, patterns)
    }


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
    read = "read"  # a capture: reads, changes nothing — always safe to run
    unknown = "unknown"  # not learned, a run-time value, or no RawIR (files, …)


def step_state(
    operation: Any, live: dict[str, dict[str, Any]], exclude: Sequence[str]
) -> tuple[StepState, dict[str, list[str]]]:
    """The step's state, and — when present — the fields where its
    predicted nodes differ from the live ones. Nodes the exclude list
    matches are no part of the footprint (the kernel's link-locals, …)."""
    kind = getattr(operation, "step_kind", None)
    if kind == "Capture":
        return StepState.read, {}
    if kind != "Primitive" or _has_runtime_value(operation):
        return StepState.unknown, {}
    try:
        footprint = rawir(operation)
    except NotLearned:
        return StepState.unknown, {}
    added = {i: n for i, n in footprint["added"].items() if not excluded(i, exclude)}
    if not added:
        return _changes_state(footprint["changed"], live, exclude), {}
    there = [i for i in added if i in live]
    if not there:
        return StepState.missing, {}
    if len(there) < len(added):
        return StepState.partial, {}
    return StepState.present, compare(added, {i: live[i] for i in added}).differ


def _changes_state(
    changed: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]], exclude: Sequence[str]
) -> StepState:
    """A step that only changes fields of nodes others add (a bridge's
    mapping): present when every change is live, missing when none is.
    Without changes nothing can be told."""
    done = [
        node_id in live
        and all(_has(live[node_id].get(field), delta) for field, delta in change.items())
        for node_id, change in changed.items()
        if not excluded(node_id, exclude)
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


def missing_nodes(
    operation: Any, live: dict[str, dict[str, Any]], exclude: Sequence[str]
) -> list[str]:
    """The nodes a learned step would add that are not live (what makes a
    step partly there)."""
    try:
        added = rawir(operation)["added"]
    except NotLearned:
        return []
    return sorted(i for i in added if i not in live and not excluded(i, exclude))


@dataclasses.dataclass(frozen=True)
class Choice:
    """What run does with one unit: `run` it (skipping the steps `skip`),
    or leave it alone — `why`, in words, either way."""

    run: bool
    skip: list[int]
    why: str


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
        return Choice(False, [], "a step is partly there")
    if not learned:
        if reads and not unknown:
            return Choice(True, [], "only reads")
        return Choice(False, [], "no learned step to tell whether it is there")
    if not present:
        return Choice(True, [], "not there: all of it")
    if len(present) == len(learned):
        if reads:
            return Choice(True, [i for i in every if i not in reads], "there: only its reads")
        return Choice(False, [], "there")
    if unknown:
        return Choice(False, [], "partly there, with steps that cannot be checked")
    return Choice(True, present, "partly there: the missing steps")
