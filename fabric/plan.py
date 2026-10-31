"""The plan: an IR, on one host, cut into units of work.

The declarations are the deployer's (deployer/ir_common.py) — the same
emitter calls that generated the old deployer. A PlanEmitter receives them
and, instead of rendering Python code, keeps the lifecycle operations
themselves, in the units the declarations open: `with emit.unit(kind,
subject):` ("logical switch: backbone", "router port: lrp-x (home)",
"kernel router side: …"). The plan is strict about them:

- every operation is declared inside a unit;
- a unit declared inside another ends the outer one's share — the outer
  one declaring more after it is an error (it would land nowhere sensible);
- a unit's name is unique in the plan; a unit with nothing to do stays in
  it (and is done at once).

The scopes, in the order the old deployer ran them on create:

    configure   chassis registration, interface bindings, IPFIX
    cluster     the OVN northbound objects (on the central host only)
    routers     each kernel router owned by this host
    workloads   each container that runs on this host

Delete is the same plan rewound (the apply task reverses the units; the
executor the steps of each). The host's abstract dependencies come before
all of it, as their own request (deployer/deps.py), not as a unit.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import functools
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from deployer.ir_common import (
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
from deployer.lifecycle import (
    CaptureOperation,
    Command,
    Primitive,
    RuntimeRef,
    SymlinkNetnsOperation,
    WriteFileOperation,
)
from protocol import generated as pt

from . import generated as g
from .units import to_step


class PlanError(ValueError):
    """The IR declares something a unit cannot hold (yet)."""


class PlanEmitter(Emitter):
    """Collects one scope's declarations as units of lifecycle operations."""

    def __init__(self, scope: str) -> None:
        super().__init__("create")
        self.label = scope
        self.units: list[g.Unit] = []
        # The units open now, innermost last; and those an inner unit has
        # ended (they take no more operations).
        self._open: list[g.Unit] = []
        self._ended: set[int] = set()
        self._values: dict[str, Any] = {}  # `emit.py("name = literal")`
        self._refs: dict[str, RuntimeRef] = {}  # capture name -> its RuntimeRef

    # -- units -----------------------------------------------------------------

    @contextlib.contextmanager
    def unit(
        self, kind: str, subject: str | None = None, *, key: str | None = None
    ) -> Iterator[None]:
        unit = g.Unit(name=unit_name(kind, subject), kind=kind, key=key, scope=self.label, steps=[])
        if self._open:
            self._ended.add(id(self._open[-1]))
        self.units.append(unit)
        self._open.append(unit)
        try:
            yield
        finally:
            self._open.pop()

    def comment(self, text: str) -> None:
        return None

    def blank(self) -> None:
        return None

    def _add(self, operation: Any) -> None:
        step = to_step(self._resolve(operation))
        if not self._open:
            raise PlanError(f"{self.label}: {_what(step)} is declared outside any unit")
        unit = self._open[-1]
        if id(unit) in self._ended:
            raise PlanError(
                f"{unit.name}: {_what(step)} is declared after a unit inside it — "
                "give it a unit of its own"
            )
        unit.steps.append(step)

    # -- what the declarations call ----------------------------------------------

    def ovn(self, primitive: Any, *, bind: str | None = None, parent: str | None = None) -> None:
        # `bind`/`parent` only shaped the rendered code; the operation is the
        # primitive itself.
        self._add(primitive)

    def append(self, path: str, content: str) -> None:
        self._add(WriteFileOperation(path, content))

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        self._refs[var] = RuntimeRef(var)
        self._add(CaptureOperation(var, self.label, tuple(argv)))  # type: ignore[arg-type]

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        # As the controller's wait_capture: retried, restarting
        # ovn-controller, the value published under CHASSIS_ID.
        self._refs[var] = RuntimeRef(var, "CHASSIS_ID")
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
        self._add(SymlinkNetnsOperation(container, self._refs[pid_var]))

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
        self._values[statement.targets[0].id] = ast.literal_eval(statement.value)

    def helper(
        self, name: str, *args: object, undo: str | None = None, undo_args: Sequence[object] = ()
    ) -> None:
        translate = _HELPERS.get(name)
        if translate is None:
            raise PlanError(f"{self.label}: {name} cannot be planned as a unit yet")
        self._add(translate(*args))

    def sh(self, argv: Sequence[str | Ref], background: bool = False, optional: Any = None) -> None:
        raise PlanError(f"{self.label}: a bare command {list(argv)!r} has no inverse")

    # -- refs ------------------------------------------------------------------

    def _value(self, expr: str) -> Any:
        """A code expression: a literal, or a name set by `emit.py`."""
        try:
            return ast.literal_eval(expr)
        except (ValueError, SyntaxError):
            if expr in self._values:
                return self._values[expr]
            raise PlanError(f"{self.label}: unknown value {expr!r}") from None

    def _resolve(self, value: Any) -> Any:
        """Replace every Ref: a name set by `emit.py` by its value, a capture
        by its RuntimeRef (filled in when the operation runs)."""
        if isinstance(value, Ref):
            if value.name in self._values:
                return self._values[value.name]
            if value.name in self._refs:
                return self._refs[value.name]
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


# -- the old deployer's helper lambdas, as plain operations ------------------------


def _attach_legs(container: str, netns: str, dev: str, transit_leg: str | None) -> Primitive:
    """Hand a kernel router's legs to its docker app's container; their
    rewind moves them back, so the router's own delete finds them."""
    legs = [leg for leg in (transit_leg, dev) if leg]
    create: list[Command] = []
    delete: list[Command] = []
    for leg in legs:
        create += [
            Command(
                f"{leg} -> {container}",
                ("ip", "netns", "exec", netns, "ip", "link", "set", leg, "netns", container),
            ),
            Command(
                f"{leg} up", ("ip", "netns", "exec", container, "ip", "link", "set", leg, "up")
            ),
        ]
        delete.append(
            Command(
                f"{leg} -> {netns}",
                ("ip", "netns", "exec", container, "ip", "link", "set", leg, "netns", netns),
                optional=True,
            )
        )
    create += [
        Command(
            "forwarding",
            ("ip", "netns", "exec", container, "sysctl", "-w", "net.ipv4.ip_forward=1"),
        ),
        Command(
            "forwarding",
            ("ip", "netns", "exec", container, "sysctl", "-w", "net.ipv6.conf.all.forwarding=1"),
        ),
    ]
    return Primitive(
        key=f"docker.legs:{container}",
        nodes=(),
        create_cmds=tuple(create),
        delete_cmds=tuple(reversed(delete)),
    )


def _docker_exec(container: str, cmd: Sequence[str], dev: str) -> Primitive:
    """Start the app inside the running container (detached); removing the
    container ends it, so its rewind does nothing."""
    return Primitive(
        key=f"docker.exec:{container}",
        nodes=(),
        create_cmds=(
            Command(f"{container} app", ("/usr/bin/docker", "exec", "-d", container, *cmd, dev)),
        ),
        delete_cmds=(),
    )


_HELPERS: dict[str, Callable[..., Any]] = {
    "_docker_attach_legs": _attach_legs,
    "_docker_exec": _docker_exec,
}


# -- the plan ----------------------------------------------------------------


def _what(step: g.UnitStepSpec) -> str:
    """A step, for an error message."""
    if isinstance(step, g.PrimitiveStep):
        return f"{step.key!r}"
    return f"a {step.kind} step"


def _scope(scope: str, declare: Callable[[Emitter], None]) -> list[g.Unit]:
    emit = PlanEmitter(scope)
    declare(emit)
    return emit.units


def plan_units(nodes: Sequence[pt.Model], host: str) -> list[g.Unit]:
    """The units of work `nodes` (an IR) make on `host`, in create order."""
    all_nodes = list(nodes)
    host_node = next(
        (n for n in all_nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host), None
    )
    if host_node is None:
        raise PlanError(f"{host} is not a host of this IR")
    units = _scope("configure", functools.partial(_emit_host_configure, host_node, all_nodes))
    if host_node.data.ovnRole == pt.OvnRole.central:
        units += _scope("cluster", functools.partial(_emit_cluster_body, all_nodes))
    for owner in _kernel_router_owners(host_node.id, all_nodes):
        name = owner.key.name
        units += _scope(
            name,
            functools.partial(_emit_kernel_router_owner, owner, _netns_name(name), all_nodes),
        )
    for node in all_nodes:
        if node.kind not in ("kernel.app.container", "kernel.app.service"):
            continue
        if node.data.host != host_node.id:
            continue
        name = node.key.name
        faces = _service_faces(name, host, all_nodes)
        units += _scope(name, functools.partial(_emit_service_up, node, faces))
    names: set[str] = set()
    for unit in units:
        if unit.name in names:
            raise PlanError(f"two units are named {unit.name!r}")
        names.add(unit.name)
    return units
