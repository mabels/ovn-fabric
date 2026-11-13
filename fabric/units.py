"""Units of work as data: their steps are the deployer's lifecycle
operations (fabric/lifecycle.py), serialized.

    to_operation(step) -> the lifecycle operation it describes
    to_step(operation) -> its serialized step

Both directions are 1:1 for the operations a unit may contain, one entry
per step kind in `_KINDS` — Primitive (every typed command primitive
renders to one), WriteFile, Capture, SymlinkNetns, and the file-system
ones done in Python: Directory, Chmod, RemovePath. An operation names its
kind itself (`step_kind`); nothing is dispatched on Python types.
An argv token is a string or a RuntimeRef (`{ref, key?}`), resolved from
the run's store right before the operation runs, as the controller always
did.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from typing import Any

from . import generated as g
from .lifecycle import (
    CaptureOperation,
    Command,
    CommandPrimitive,
    Operation,
    Primitive,
    RawNodeTemplate,
    RuntimeRef,
    SymlinkNetnsOperation,
    WriteFileOperation,
)
from .primitives.fs import ChmodPrimitive, DirectoryPrimitive, RemovePathPrimitive


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


# -- one entry per step kind: load (step -> operation) and dump (operation ->
# step) side by side. An operation names its kind (`step_kind`, the
# schema's `kind`); a typed command primitive is a "Primitive" step.


def _load_primitive(step: g.PrimitiveStep, host: str) -> Operation:
    return Primitive(
        key=step.key,
        nodes=tuple(
            RawNodeTemplate(id=n.id, kind=n.kind, fields=_fields(n.fields)) for n in step.nodes
        ),
        create_cmds=tuple(_command(c) for c in step.create),
        delete_cmds=tuple(_command(c) for c in step.delete),
    )


def _dump_primitive(
    operation: Primitive | CommandPrimitive, decide: g.StepDecide | None = None
) -> g.PrimitiveStep:
    primitive = operation.as_primitive()
    return g.PrimitiveStep(
        decide=decide,
        kind="Primitive",
        key=primitive.key,
        nodes=[
            g.RawNode(id=n.id, kind=n.kind, fields=_fields_json(n.fields)) for n in primitive.nodes
        ],
        create=[_to_command(c) for c in primitive.create_cmds],
        delete=[_to_command(c) for c in primitive.delete_cmds],
    )


def _load_write_file(step: g.WriteFileStep, host: str) -> Operation:
    return WriteFileOperation(
        step.path, step.content, step.mode, bool(step.keep), step.uid, step.gid
    )


def _dump_write_file(operation: WriteFileOperation) -> g.WriteFileStep:
    return g.WriteFileStep(
        kind="WriteFile",
        path=operation.path,
        content=operation.content,
        mode=operation.mode,
        keep=operation.keep or None,
        uid=operation.uid,
        gid=operation.gid,
    )


def _load_capture(step: g.CaptureStep, host: str) -> Operation:
    return CaptureOperation(
        step.name,
        step.label,
        tuple(_arg(a) for a in step.argv),
        optional=bool(step.optional),
        retries=step.retries or 0,
        restart_unit=step.restartUnit,
        result_key=step.resultKey,
    )


def _dump_capture(operation: CaptureOperation) -> g.CaptureStep:
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


def _load_symlink_netns(step: g.SymlinkNetnsStep, host: str) -> Operation:
    return SymlinkNetnsOperation(step.container, _arg(step.pid))


def _dump_symlink_netns(operation: SymlinkNetnsOperation) -> g.SymlinkNetnsStep:
    return g.SymlinkNetnsStep(
        kind="SymlinkNetns", container=operation.container, pid=_to_arg(operation.pid)
    )


def _load_directory(step: g.DirectoryStep, host: str) -> Operation:
    return DirectoryPrimitive(host, step.path, owned=bool(step.owned))


def _dump_directory(operation: DirectoryPrimitive) -> g.DirectoryStep:
    return g.DirectoryStep(kind="Directory", path=operation.path, owned=operation.owned or None)


def _load_chmod(step: g.ChmodStep, host: str) -> Operation:
    return ChmodPrimitive(host, step.path, step.mode)


def _dump_chmod(operation: ChmodPrimitive) -> g.ChmodStep:
    return g.ChmodStep(kind="Chmod", path=operation.path, mode=operation.mode)


def _load_remove_path(step: g.RemovePathStep, host: str) -> Operation:
    return RemovePathPrimitive(host, step.path, recursive=bool(step.recursive))


def _dump_remove_path(operation: RemovePathPrimitive) -> g.RemovePathStep:
    return g.RemovePathStep(
        kind="RemovePath", path=operation.path, recursive=operation.recursive or None
    )


@dataclasses.dataclass(frozen=True)
class _Kind:
    load: Callable[[Any, str], Operation]  # (its step, host) -> the operation
    dump: Callable[[Any], g.UnitStepSpec]  # the operation -> its step


_KINDS: dict[str, _Kind] = {
    "Primitive": _Kind(_load_primitive, _dump_primitive),
    "WriteFile": _Kind(_load_write_file, _dump_write_file),
    "Capture": _Kind(_load_capture, _dump_capture),
    "SymlinkNetns": _Kind(_load_symlink_netns, _dump_symlink_netns),
    "Directory": _Kind(_load_directory, _dump_directory),
    "Chmod": _Kind(_load_chmod, _dump_chmod),
    "RemovePath": _Kind(_load_remove_path, _dump_remove_path),
}


def to_operation(step: g.UnitStepSpec, host: str = "") -> Operation:
    """The lifecycle operation a serialized step describes (on `host`)."""
    kind = _KINDS.get(step.kind)
    if kind is None:
        raise TypeError(f"not a unit step kind: {step.kind!r}")
    return kind.load(step, host)


def to_step(operation: object, decide: g.StepDecide | None = None) -> g.UnitStepSpec:
    """The serialized step for a lifecycle operation a unit may contain;
    `decide`: who decides it in a run (fabric/decide.py) — a primitive's
    only."""
    step_kind = getattr(operation, "step_kind", None)
    kind = _KINDS.get(step_kind) if isinstance(step_kind, str) else None
    if kind is None:
        raise TypeError(f"{type(operation).__name__} cannot be part of a unit (no step kind)")
    if decide is None:
        return kind.dump(operation)
    if step_kind != "Primitive":
        raise TypeError(f"a {step_kind} step decides nothing (only a primitive does)")
    return _dump_primitive(operation, decide)  # type: ignore[arg-type]
