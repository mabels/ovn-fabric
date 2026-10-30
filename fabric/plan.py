"""The plan: an IR, on one host, cut into units of work.

The declarations are the deployer's (deployer/ir_common.py) — the same
emitter calls that generated the old deployer. A PlanEmitter receives them
and, instead of rendering Python code, keeps the lifecycle operations
themselves, cut into units along the declarations' sections: every
`# --- <kind>: <name> ---` comment opens a unit ("logical switch:
backbone", "router port: lrp-x (home)", "kernel router side: …"); the
operations up to the next one belong to it. Operations before the first
section of a scope go to a unit named for that scope (the chassis
registration, a kernel router's netns, a workload's container).

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
import dataclasses
import functools
import re
from collections.abc import Callable, Sequence
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

_SECTION = re.compile(r"^# --- (.*) ---$")
# A section's kind: what comes before ": " ("router port: …"), else before
# " (" ("bridge (…)"), else the whole text.
_KIND = re.compile(r"^([^:(]+?)(?::| \(|$)")


class PlanError(ValueError):
    """The IR declares something a unit cannot hold (yet)."""


class PlanEmitter(Emitter):
    """Collects one scope's declarations as units of lifecycle operations."""

    def __init__(self, scope: str, first_unit: str) -> None:
        super().__init__("create")
        self.label = scope
        self.units: list[g.Unit] = []
        self._first_unit = first_unit
        self._values: dict[str, Any] = {}  # `emit.py("name = literal")`
        self._refs: dict[str, RuntimeRef] = {}  # capture name -> its RuntimeRef

    # -- sections --------------------------------------------------------------

    def comment(self, text: str) -> None:
        match = _SECTION.match(text.strip())
        if match is None:
            return  # a plain comment
        name = match.group(1)
        kind_match = _KIND.match(name)
        kind = kind_match.group(1).strip() if kind_match else name
        self.units.append(g.Unit(name=name, kind=kind, steps=[]))

    def blank(self) -> None:
        return None

    def _add(self, operation: Any) -> None:
        if not self.units:
            kind = self._first_unit.split(":", 1)[0]
            self.units.append(g.Unit(name=self._first_unit, kind=kind, steps=[]))
        self.units[-1].steps.append(to_step(self._resolve(operation)))

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


def _scope(scope: str, first_unit: str, declare: Callable[[Emitter], None]) -> list[g.Unit]:
    emit = PlanEmitter(scope, first_unit)
    declare(emit)
    for unit in emit.units:
        unit.scope = scope
    return [unit for unit in emit.units if unit.steps]


def plan_units(nodes: Sequence[pt.Model], host: str) -> list[g.Unit]:
    """The units of work `nodes` (an IR) make on `host`, in create order."""
    all_nodes = list(nodes)
    host_node = next(
        (n for n in all_nodes if isinstance(n, pt.InfraHostNode) and n.key.host == host), None
    )
    if host_node is None:
        raise PlanError(f"{host} is not a host of this IR")
    units = _scope(
        "configure",
        f"chassis registration: {host}",
        functools.partial(_emit_host_configure, host_node, all_nodes),
    )
    if host_node.data.ovnRole == pt.OvnRole.central:
        units += _scope("cluster", "cluster", functools.partial(_emit_cluster_body, all_nodes))
    for owner in _kernel_router_owners(host_node.id, all_nodes):
        name = owner.key.name
        units += _scope(
            name,
            f"kernel router: {name}",
            functools.partial(_emit_kernel_router_owner, owner, _netns_name(name), all_nodes),
        )
    for node in all_nodes:
        if node.kind not in ("kernel.app.container", "kernel.app.service"):
            continue
        if node.data.host != host_node.id:
            continue
        name = node.key.name
        faces = _service_faces(name, host, all_nodes)
        units += _scope(name, f"workload: {name}", functools.partial(_emit_service_up, node, faces))
    return units
