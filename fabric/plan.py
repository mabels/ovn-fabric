"""The plan: an IR, on one host, cut into units of work.

The declarations are the deployer's (fabric/emit.py) — the same
emitter calls that generated the old deployer. A PlanEmitter receives them
and, instead of rendering Python code, keeps the lifecycle operations
themselves, in the units the declarations open:

    port_unit = emit.unit("router port", f"{lrp} ({domain})")
    port_unit.ovn(...)

`emit.unit()` returns the unit — an emitter of its own; what is declared
on it belongs to it ("logical switch: backbone", "router port: lrp-x
(home)", "kernel router side: …"). The plan is strict about them:

- every operation is declared on a unit, none on the scope itself;
- only the newest unit takes operations — declaring on an older one
  (after a later unit was opened) is an error: it would run out of order;
- a unit's name is unique in the plan; a unit with nothing to do stays in
  it (and is done at once).

The scopes, in the order the old deployer ran them on create:

    configure   chassis registration, interface bindings, IPFIX
    cluster     the OVN northbound objects (on the central host only)
    routers     each kernel router owned by this host
    workloads   each container that runs on this host

Delete is the same plan rewound (the apply task reverses the units; the
executor the steps of each). The host's abstract dependencies come before
all of it, as their own request (fabric/deps.py), not as a unit.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
from collections.abc import Callable, Sequence
from typing import Any

from protocol import generated as pt

from . import generated as g
from .addresses import Addresses
from .decide import Decide, spec
from .emit import (
    Emitter,
    Ref,
    _emit_cluster_body,
    _emit_host_configure,
    _emit_kernel_router_owner,
    _emit_service_up,
    _kernel_router_owners,
    _netns_name,
    _service_faces,
    unit_name,
)
from .lifecycle import (
    CaptureOperation,
    RuntimeRef,
    SymlinkNetnsOperation,
    WriteFileOperation,
)
from .units import to_step


class PlanError(ValueError):
    """The IR declares something a unit cannot hold (yet)."""


@dataclasses.dataclass
class _Plan:
    """What a scope's emitter and the units opened from it share."""

    scope: str
    units: list[g.Unit] = dataclasses.field(default_factory=list)
    values: dict[str, Any] = dataclasses.field(default_factory=dict)  # `emit.py("x = 1")`
    refs: dict[str, RuntimeRef] = dataclasses.field(default_factory=dict)  # capture -> ref
    # Every unit's operations as the typed objects they were declared as —
    # what a step is serialized from, and what fabric/rawir_learned knows.
    operations: list[tuple[str, Any]] = dataclasses.field(default_factory=list)


class PlanEmitter(Emitter):
    """Collects one scope's declarations as units of lifecycle operations.
    The scope's own emitter only opens units; each unit is a PlanEmitter of
    its own (`unit`), sharing the scope's `plan`."""

    def __init__(self, scope: str, plan: _Plan | None = None, unit: g.Unit | None = None) -> None:
        super().__init__("create")
        self.label = scope
        self.plan = plan if plan is not None else _Plan(scope)
        self._unit = unit

    @property
    def units(self) -> list[g.Unit]:
        return self.plan.units

    # -- units -----------------------------------------------------------------

    def unit(self, kind: str, subject: str | None = None, *, key: str | None = None) -> PlanEmitter:
        unit = g.Unit(name=unit_name(kind, subject), kind=kind, key=key, scope=self.label, steps=[])
        self.plan.units.append(unit)
        return PlanEmitter(self.label, self.plan, unit)

    def comment(self, text: str) -> None:
        return None

    def blank(self) -> None:
        return None

    def _add(self, operation: Any, decide: Decide = Decide.footprint) -> None:
        resolved = self._resolve(operation)
        step = to_step(resolved, spec(decide, resolved))
        if self._unit is None:
            raise PlanError(f"{self.label}: {_what(step)} is declared outside any unit")
        newest = self.plan.units[-1]
        if self._unit is not newest:
            raise PlanError(
                f"{self._unit.name}: {_what(step)} is declared after the unit {newest.name!r} — "
                "give it a unit of its own"
            )
        self._unit.steps.append(step)
        self.plan.operations.append((self._unit.name, resolved))

    # -- what the declarations call ----------------------------------------------

    def ovn(
        self,
        primitive: Any,
        *,
        bind: str | None = None,
        parent: str | None = None,
        decide: Decide = Decide.footprint,
    ) -> None:
        # `bind`/`parent` only shaped the rendered code; the operation is the
        # primitive itself.
        self._add(primitive, decide)

    def append(self, path: str, content: str) -> None:
        self._add(WriteFileOperation(path, content))

    def write_file(
        self, path: str, content: str, mode: int | None, uid: int | None, gid: int | None
    ) -> None:
        """A file with its mode and owner, set before it is in place."""
        self._add(WriteFileOperation(path, content, mode, uid=uid, gid=gid))

    def seed(
        self, path: str, content: str, mode: int | None, uid: int | None, gid: int | None
    ) -> None:
        """A file written only where there is none (WriteFileOperation.keep)."""
        self._add(WriteFileOperation(path, content, mode, keep=True, uid=uid, gid=gid))

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        self.plan.refs[var] = RuntimeRef(var)
        self._add(CaptureOperation(var, self.label, tuple(argv)))  # type: ignore[arg-type]

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        # As the controller's wait_capture: retried, restarting
        # ovn-controller, the value published under CHASSIS_ID.
        self.plan.refs[var] = RuntimeRef(var, "CHASSIS_ID")
        self._add(
            CaptureOperation(
                var,
                self.label,
                tuple(argv),  # type: ignore[arg-type]  # Refs, resolved by _add
                retries=30,
                restart_unit="ovn-controller",
                result_key="CHASSIS_ID",
            )
        )

    def netns_symlink(self, container_expr: str, pid_var: str) -> None:
        container = self._value(container_expr)
        self._add(SymlinkNetnsOperation(container, self.plan.refs[pid_var]))

    def py(self, line: str) -> None:
        # Only `name = <literal>` (a workload's container/image name).
        tree = ast.parse(line.strip())
        statement = tree.body[0] if len(tree.body) == 1 else None
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            raise PlanError(f"{self.label}: cannot plan the code line {line!r}")
        self.plan.values[statement.targets[0].id] = ast.literal_eval(statement.value)

    def helper(
        self, name: str, *args: object, undo: str | None = None, undo_args: Sequence[object] = ()
    ) -> None:
        # A helper of the old generated deployer (zerotier, the docker
        # veth): no typed primitive for it yet.
        raise PlanError(f"{self.label}: {name} cannot be planned as a unit yet")

    def sh(self, argv: Sequence[str | Ref], background: bool = False, optional: Any = None) -> None:
        raise PlanError(f"{self.label}: a bare command {list(argv)!r} has no inverse")

    # -- refs ------------------------------------------------------------------

    def _value(self, expr: str) -> Any:
        """A code expression: a literal, or a name set by `emit.py`."""
        try:
            return ast.literal_eval(expr)
        except (ValueError, SyntaxError):
            if expr in self.plan.values:
                return self.plan.values[expr]
            raise PlanError(f"{self.label}: unknown value {expr!r}") from None

    def _resolve(self, value: Any) -> Any:
        """Replace every Ref: a name set by `emit.py` by its value, a capture
        by its RuntimeRef (filled in when the operation runs)."""
        if isinstance(value, Ref):
            if value.name in self.plan.values:
                return self.plan.values[value.name]
            if value.name in self.plan.refs:
                return self.plan.refs[value.name]
            raise PlanError(f"{self.label}: {value.name} is captured nowhere")
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return dataclasses.replace(
                value,
                **{
                    f.name: self._resolve(getattr(value, f.name))
                    for f in dataclasses.fields(value)
                    if f.init
                },
            )
        if isinstance(value, tuple):
            return tuple(self._resolve(item) for item in value)
        if isinstance(value, list):
            return [self._resolve(item) for item in value]
        if isinstance(value, dict):
            return {key: self._resolve(item) for key, item in value.items()}
        return value


# -- the plan ----------------------------------------------------------------


def _what(step: g.UnitStepSpec) -> str:
    """A step, for an error message."""
    if isinstance(step, g.PrimitiveStep):
        return f"{step.key!r}"
    return f"a {step.kind} step"


def _scope(scope: str, declare: Callable[[Emitter], None]) -> _Plan:
    emit = PlanEmitter(scope)
    declare(emit)
    return emit.plan


def plan_units(
    nodes: Sequence[pt.Model], host: str, addrs: Addresses | None = None
) -> list[g.Unit]:
    """The units of work `nodes` (an IR) make on `host`, in create order.
    `addrs`: the IR's addresses placed against the live view (run); without
    it, placed against an empty one (create, ADR 0011 §3)."""
    return [unit for plan in _plans(nodes, host, addrs) for unit in plan.units]


def plan_operations(
    nodes: Sequence[pt.Model], host: str, addrs: Addresses | None = None
) -> list[tuple[str, Any]]:
    """The same plan as (unit name, operation) — the typed objects (an
    AddrPrimitive, an OVSBridge, …) its units are serialized from."""
    return [entry for plan in _plans(nodes, host, addrs) for entry in plan.operations]


def _plans(nodes: Sequence[pt.Model], host: str, addrs: Addresses | None) -> list[_Plan]:
    all_nodes = list(nodes)
    # Every address the IR leaves to the runtime, placed (ADR 0011 §3b):
    # one metadata object, handed to everything that emits an address.
    if addrs is None:
        addrs = Addresses.resolve(all_nodes)
    host_node = next(
        (n for n in all_nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host), None
    )
    if host_node is None:
        raise PlanError(f"{host} is not a host of this IR")
    plans = [_scope("configure", functools.partial(_emit_host_configure, host_node, all_nodes))]
    if host_node.data.ovnRole == pt.OvnRole.central:
        plans.append(_scope("cluster", functools.partial(_emit_cluster_body, all_nodes, addrs)))
    for owner in _kernel_router_owners(host_node.id, all_nodes):
        name = owner.key.name
        plans.append(
            _scope(
                name,
                functools.partial(
                    _emit_kernel_router_owner, owner, _netns_name(name), all_nodes, addrs
                ),
            )
        )
    for node in all_nodes:
        if node.kind not in ("kernel.app.container", "kernel.app.service"):
            continue
        if node.data.host != host_node.id:
            continue
        name = node.key.name
        faces = _service_faces(name, host, all_nodes)
        plans.append(_scope(name, functools.partial(_emit_service_up, node, faces, addrs)))
    # No boot unit: the runtime replays the deployed IR itself after a
    # reboot (ADR 0012 §6, fabric/boot.py).
    names: set[str] = set()
    for unit in (unit for plan in plans for unit in plan.units):
        if unit.name in names:
            raise PlanError(f"two units are named {unit.name!r}")
        names.add(unit.name)
    return plans
