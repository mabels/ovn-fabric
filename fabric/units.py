"""Units of work as data: their steps are the deployer's lifecycle
operations (deployer/lifecycle.py), serialized.

    to_operation(step) -> the lifecycle operation it describes
    to_step(operation) -> its serialized step

Both directions are 1:1 for the operations a unit may contain — Primitive
(every typed command primitive renders to one), WriteFile, Capture,
SymlinkNetns, and the file-system ones done in Python: Directory, Chmod,
RemovePath.
An argv token is a string or a RuntimeRef (`{ref, key?}`), resolved from
the run's store right before the operation runs, as the controller always
did.
"""

from __future__ import annotations

import json
from typing import Any

from deployer.lifecycle import (
    CaptureOperation,
    Command,
    Operation,
    Primitive,
    RawNodeTemplate,
    RuntimeRef,
    SymlinkNetnsOperation,
    WriteFileOperation,
)
from deployer.primitives_fs import ChmodPrimitive, DirectoryPrimitive, RemovePathPrimitive

from . import generated as g


def _arg(arg: g.Arg) -> Any:
    return arg if isinstance(arg, str) else RuntimeRef(arg.ref, arg.key)


def _to_arg(value: Any) -> g.Arg:
    if isinstance(value, RuntimeRef):
        return g.RefArg(ref=value.name, key=value.key)
    if isinstance(value, str):
        return value
    raise TypeError(f"an argv token must be a string or a RuntimeRef, not {value!r}")


def _encode_ref(value: object) -> object:
    # A raw node's fields may hold a value of the run (a gateway chassis).
    if isinstance(value, RuntimeRef):
        return {"$ref": value.name, "$key": value.key}
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _decode_ref(obj: dict[str, Any]) -> Any:
    return RuntimeRef(obj["$ref"], obj.get("$key")) if "$ref" in obj else obj


def _fields_json(fields: Any) -> str:
    return json.dumps(fields, sort_keys=True, default=_encode_ref)


def _fields(text: str) -> Any:
    return json.loads(text, object_hook=_decode_ref)


def _command(command: g.StepCommand) -> Command:
    return Command(
        command.label,
        tuple(_arg(a) for a in command.argv),
        optional=bool(command.optional),
        timeout=command.timeout,
    )


def _to_command(command: Command) -> g.StepCommand:
    return g.StepCommand(
        label=command.label,
        argv=[_to_arg(a) for a in command.argv],
        optional=command.optional or None,
        timeout=command.timeout,
    )


def to_operation(step: g.UnitStepSpec, host: str = "") -> Operation:
    """The lifecycle operation a serialized step describes (on `host`)."""
    if isinstance(step, g.PrimitiveStep):
        return Primitive(
            key=step.key,
            nodes=tuple(
                RawNodeTemplate(id=n.id, kind=n.kind, fields=_fields(n.fields)) for n in step.nodes
            ),
            create_cmds=tuple(_command(c) for c in step.create),
            delete_cmds=tuple(_command(c) for c in step.delete),
        )
    if isinstance(step, g.WriteFileStep):
        return WriteFileOperation(step.path, step.content, step.mode)
    if isinstance(step, g.CaptureStep):
        return CaptureOperation(
            step.name,
            step.label,
            tuple(_arg(a) for a in step.argv),
            optional=bool(step.optional),
            retries=step.retries or 0,
            restart_unit=step.restartUnit,
            result_key=step.resultKey,
        )
    if isinstance(step, g.SymlinkNetnsStep):
        return SymlinkNetnsOperation(step.container, _arg(step.pid))
    if isinstance(step, g.DirectoryStep):
        return DirectoryPrimitive(host, step.path, owned=bool(step.owned))
    if isinstance(step, g.ChmodStep):
        return ChmodPrimitive(host, step.path, step.mode)
    if isinstance(step, g.RemovePathStep):
        return RemovePathPrimitive(host, step.path, recursive=bool(step.recursive))
    raise TypeError(f"not a unit step: {step!r}")


def to_step(operation: object) -> g.UnitStepSpec:
    """The serialized step for a lifecycle operation a unit may contain."""
    if isinstance(operation, Primitive):
        return g.PrimitiveStep(
            kind="Primitive",
            key=operation.key,
            nodes=[
                g.RawNode(id=n.id, kind=n.kind, fields=_fields_json(n.fields))
                for n in operation.nodes
            ],
            create=[_to_command(c) for c in operation.create_cmds],
            delete=[_to_command(c) for c in operation.delete_cmds],
        )
    if isinstance(operation, WriteFileOperation):
        return g.WriteFileStep(
            kind="WriteFile", path=operation.path, content=operation.content, mode=operation.mode
        )
    if isinstance(operation, CaptureOperation):
        return g.CaptureStep(
            kind="Capture",
            name=operation.name,
            label=operation.label,
            argv=[_to_arg(a) for a in operation.argv],
            optional=operation.optional or None,
            retries=operation.retries or None,
            restartUnit=operation.restart_unit,
            resultKey=operation.result_key,
        )
    if isinstance(operation, SymlinkNetnsOperation):
        return g.SymlinkNetnsStep(
            kind="SymlinkNetns", container=operation.container, pid=_to_arg(operation.pid)
        )
    if isinstance(operation, DirectoryPrimitive):
        return g.DirectoryStep(kind="Directory", path=operation.path, owned=operation.owned or None)
    if isinstance(operation, ChmodPrimitive):
        return g.ChmodStep(kind="Chmod", path=operation.path, mode=operation.mode)
    if isinstance(operation, RemovePathPrimitive):
        return g.RemovePathStep(
            kind="RemovePath", path=operation.path, recursive=operation.recursive or None
        )
    as_primitive = getattr(operation, "as_primitive", None)
    if callable(as_primitive):  # a typed primitive (ovn_nbctl_ls, ovs_port, …)
        return to_step(as_primitive())
    raise TypeError(f"{type(operation).__name__} cannot be part of a unit (not serializable)")
