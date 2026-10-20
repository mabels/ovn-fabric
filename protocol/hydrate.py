# protocol/hydrate.py — hand-written, NOT regenerated, but GENERIC: it
# holds no knowledge of any node kind or field. A raw IR JSON node (dict,
# as loaded from generate-ir's output) becomes its typed dataclass by
# walking the type hints of protocol/generated.py — the dataclasses
# `generate-pytypes` builds from src/protocol.ts's ArkType schema. So a
# protocol change is made in ONE place (src/protocol.ts) and this module
# follows it after the next `generate-pytypes`; it never needs editing
# for a new kind or field (2026-10-11 — it used to map every field by
# hand, which silently DROPPED any field the mapping forgot: a docker
# app's `build`, a zerotier app's `routes`).
#
# What the walk understands is exactly what datamodel-codegen emits for
# this schema: dataclasses, Enum, Literal, list[T], dict[str, T], Any,
# unions (`T | None`, `str | list[str]`, a union of dataclasses told
# apart by their Literal fields — e.g. the kernel apps' `kind`) and the
# JSON primitives. An annotation outside that set raises instead of
# passing raw data through untyped.
#
# The walk is STRICT, and every failure is an IRShapeError naming the
# node id and the path: an unknown `kind`, a missing required field, a
# value of the wrong shape, and a key the dataclass does not declare.
# That last one is what catches drift between the generator and the
# protocol (src/ir.ts emitting something src/protocol.ts never
# declared) and a stale deployer bundle receiving newer-shaped IR —
# this data is internal, produced by this project's own generator, not
# adversarial external input, so version skew is what needs catching
# (see ADR 0002, "Type stability across the TypeScript/Python
# boundary").
#
# Every OTHER Python component (deployer/ir_common.py, deployer/
# cli.py) works with these typed nodes exclusively — this module is the
# ONE place a raw dict is still indexed by string key.

from __future__ import annotations

import dataclasses
import enum
import functools
import keyword
import types
import typing
from collections.abc import Mapping, Sequence

from . import generated as pt


class IRShapeError(ValueError):
    """A raw IR node does not match protocol/generated.py's dataclasses."""


_NONE_TYPE = type(None)
_UNION_ORIGINS = (typing.Union, types.UnionType)


@functools.cache
def _hints(cls: type) -> dict[str, typing.Any]:
    # generated.py uses `from __future__ import annotations`, so a
    # dataclass field's `.type` is a string — resolve them once per class.
    return typing.get_type_hints(cls)


def _json_key(field_name: str) -> str:
    # datamodel-codegen appends `_` to a field whose JSON key is a Python
    # keyword (Build.from_ <- "from") — undo exactly that, nothing else.
    if field_name.endswith("_") and keyword.iskeyword(field_name[:-1]):
        return field_name[:-1]
    return field_name


@functools.cache
def _literal_fields(cls: type) -> dict[str, tuple[object, ...]]:
    """JSON key -> allowed values, for every Literal-typed field of a
    dataclass — its discriminators (a node's or an app's `kind`)."""
    return {
        _json_key(name): typing.get_args(hint)
        for name, hint in _hints(cls).items()
        if typing.get_origin(hint) is typing.Literal
    }


def _is_dataclass_type(tp: object) -> typing.TypeGuard[type]:
    # dataclasses.is_dataclass() is also true for an INSTANCE; an
    # annotation is only ever the class.
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _shape_matches(tp: typing.Any, value: object) -> bool:
    """Cheap top-level test used ONLY to pick a union member; the chosen
    member is then built (and fully checked) by _build."""
    if tp is typing.Any:
        return True
    origin = typing.get_origin(tp)
    if origin is list:
        return isinstance(value, list)
    if origin is dict:
        return isinstance(value, dict)
    if origin is typing.Literal:
        return value in typing.get_args(tp)
    if _is_dataclass_type(tp):
        return isinstance(value, dict) and all(
            value.get(key) in allowed for key, allowed in _literal_fields(tp).items()
        )
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        return value in {member.value for member in tp}
    if tp is bool:
        return isinstance(value, bool)
    if tp is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if tp is float:
        return _is_number(value)
    if tp is str:
        return isinstance(value, str)
    return False


def _build_union(members: tuple[object, ...], value: object, path: str) -> object:
    if value is None:
        if _NONE_TYPE in members:
            return None
        raise IRShapeError(f"{path}: null is not allowed here")
    candidates = [m for m in members if m is not _NONE_TYPE]
    if len(candidates) == 1:
        return _build(candidates[0], value, path)
    matches = [m for m in candidates if _shape_matches(m, value)]
    if len(matches) == 1:
        return _build(matches[0], value, path)
    names = ", ".join(getattr(m, "__name__", str(m)) for m in candidates)
    hint = f" (kind={value.get('kind')!r})" if isinstance(value, dict) else ""
    reason = "matches none of" if not matches else "is ambiguous between"
    raise IRShapeError(f"{path}: value{hint} {reason} {names}")


def _build_dataclass(cls: type, value: object, path: str) -> object:
    if not isinstance(value, dict):
        raise IRShapeError(
            f"{path}: expected an object for {cls.__name__}, got {type(value).__name__}"
        )
    hints = _hints(cls)
    kwargs: dict[str, object] = {}
    known: set[str] = set()
    for field in dataclasses.fields(cls):
        key = _json_key(field.name)
        known.add(key)
        if key in value:
            kwargs[field.name] = _build(hints[field.name], value[key], f"{path}.{key}")
        elif field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            raise IRShapeError(f"{path}.{key}: required field is missing ({cls.__name__})")
    unknown = sorted(set(value) - known)
    if unknown:
        raise IRShapeError(
            f"{path}: {cls.__name__} has no field(s) {', '.join(unknown)} — the IR carries "
            "something protocol/generated.py does not declare. Declare it in "
            "src/protocol.ts and re-run generate-pytypes, or this deployer bundle is "
            "older than the IR it was given."
        )
    return cls(**kwargs)


def _build(tp: typing.Any, value: object, path: str) -> object:
    if tp is typing.Any:
        return value
    origin = typing.get_origin(tp)
    if origin in _UNION_ORIGINS:
        return _build_union(typing.get_args(tp), value, path)
    if origin is typing.Literal:
        if value not in typing.get_args(tp):
            raise IRShapeError(
                f"{path}: expected one of {list(typing.get_args(tp))}, got {value!r}"
            )
        return value
    if origin is list:
        if not isinstance(value, list):
            raise IRShapeError(f"{path}: expected a list, got {type(value).__name__}")
        (item,) = typing.get_args(tp)
        return [_build(item, v, f"{path}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise IRShapeError(f"{path}: expected an object, got {type(value).__name__}")
        _, item = typing.get_args(tp)
        return {k: _build(item, v, f"{path}.{k}") for k, v in value.items()}
    if _is_dataclass_type(tp):
        return _build_dataclass(tp, value, path)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        try:
            return tp(value)
        except ValueError:
            allowed = [member.value for member in tp]
            raise IRShapeError(f"{path}: expected one of {allowed}, got {value!r}") from None
    if tp is bool:
        if not isinstance(value, bool):
            raise IRShapeError(f"{path}: expected a boolean, got {value!r}")
        return value
    if tp is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise IRShapeError(f"{path}: expected an integer, got {value!r}")
        return value
    if tp is float:
        # A JSON `number`: 200 and 200.0 are both fine.
        if not _is_number(value):
            raise IRShapeError(f"{path}: expected a number, got {value!r}")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise IRShapeError(f"{path}: expected a string, got {value!r}")
        return value
    raise IRShapeError(
        f"{path}: protocol/hydrate.py does not understand the annotation {tp!r} — "
        "datamodel-codegen emitted a shape this walker has no rule for yet"
    )


@functools.cache
def _node_classes() -> dict[str, type]:
    """`kind` -> node dataclass, read off generated.Model (the union of
    every node) — each member's own `kind: Literal[...]` is its key."""
    by_kind: dict[str, type] = {}
    for cls in typing.get_args(pt.Model):
        for kind in _literal_fields(cls)["kind"]:
            if not isinstance(kind, str):
                raise IRShapeError(f"{cls.__name__}: a node kind must be a string, got {kind!r}")
            if kind in by_kind:
                raise IRShapeError(
                    f"node kind {kind!r} is declared by both {by_kind[kind].__name__} "
                    f"and {cls.__name__} in protocol/generated.py"
                )
            by_kind[kind] = cls
    return by_kind


def hydrate_node(raw: Mapping[str, object]) -> pt.Model:
    kind = raw.get("kind")
    cls = _node_classes().get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise IRShapeError(
            f"unknown IR node kind {kind!r} (id={raw.get('id')!r}) — protocol/generated.py "
            "has no matching dataclass for it. Either this IR JSON is from a newer "
            "ovn-fabric than this deployer bundle knows about, or generate-pytypes "
            "(deno run -A src/cli.ts generate-pytypes) needs to be re-run for a "
            "genuinely new kind."
        )
    # `cls` is one of pt.Model's own members (_node_classes), so what the
    # walk builds from it IS a pt.Model — the cast only tells the checker
    # what the reflection above already guarantees.
    return typing.cast(pt.Model, _build_dataclass(cls, raw, str(raw.get("id") or f"<{kind}>")))


_Shape = typing.TypeVar("_Shape")


def hydrate_as(cls: type[_Shape], raw: object, path: str = "<value>") -> _Shape:
    """Any ONE protocol shape (a generated dataclass) from its raw JSON
    form, under the same strict rules as a whole node — for a caller that
    holds a fragment rather than a node (test fixtures, mostly)."""
    if not _is_dataclass_type(cls):
        raise IRShapeError(f"{path}: {cls!r} is not a protocol dataclass")
    return typing.cast(_Shape, _build_dataclass(cls, raw, path))


def hydrate_nodes(raw_nodes: Sequence[Mapping[str, object]]) -> list[pt.Model]:
    return [hydrate_node(n) for n in raw_nodes]
