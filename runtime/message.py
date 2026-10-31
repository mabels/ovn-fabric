"""Messages: a collection of parts on the wire, one structure inside.

On the wire a message is a list of parts, each a JSON object with its
`kind`:

    [{"kind": "MsgHeader", "id": "…", "reqId": "…", "time": …, "source": "cli"},
     {"kind": "VersionReq"}]

`from_parts()` validates the collection and transposes it into a `Msg`:
its `header` and its one `body`. `to_parts()` is the inverse and always
writes the same order (header parts first), so a message is diffable
and — later — signable. Signing and verifying belong here, at the
transposition, not in the transport or the handlers.

Rules of a valid collection:

- every part is an object with a string `kind`;
- a kind ending in `Header` is a header part; everything else is a body;
- exactly one body, and its kind must be known;
- an unknown header part is ignored (an older runtime accepts a newer
  peer), an unknown body is rejected;
- no kind appears twice;
- `MsgHeader` is required; requests and responses also need its `reqId`.
"""

from __future__ import annotations

import dataclasses
import enum
import json
import keyword
import time
import typing
import uuid
from collections.abc import Mapping
from typing import Any, Generic, Literal, TypeVar

from . import generated as g

Role = Literal["request", "response", "event"]


@dataclasses.dataclass(frozen=True)
class BodySpec:
    cls: type
    role: Role


# Every body kind this process understands, and what it is: the core ones
# here; each service adds its own with register_bodies().
BODIES: dict[str, BodySpec] = {
    "ErrorRes": BodySpec(g.ErrorRes, "response"),
    "LogMsg": BodySpec(g.LogMsg, "event"),
}


def register_bodies(bodies: Mapping[str, BodySpec]) -> None:
    """Make a service's message bodies known (idempotent)."""
    for kind, spec in bodies.items():
        known = BODIES.get(kind)
        if known is not None and known != spec:
            raise ValueError(f"message body {kind} is already registered differently")
        BODIES[kind] = spec


# Header parts this runtime understands (unknown ones are ignored).
HEADERS: dict[str, type] = {
    "MsgHeader": g.MsgHeader,
}

B = TypeVar("B")


class ProtocolError(ValueError):
    """A collection that is not a valid message. `req_id` is set when the
    collection carried one, so the receiver can answer with ErrorRes."""

    def __init__(self, code: str, message: str, req_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.req_id = req_id


@dataclasses.dataclass(frozen=True)
class Msg(Generic[B]):
    header: g.MsgHeader
    body: B

    @property
    def kind(self) -> str:
        kind = getattr(self.body, "kind")
        assert isinstance(kind, str)
        return kind

    @property
    def role(self) -> Role:
        return BODIES[self.kind].role

    @property
    def req_id(self) -> str | None:
        return self.header.reqId


def header(source: str, req_id: str | None = None, parent_req_id: str | None = None) -> g.MsgHeader:
    """A fresh MsgHeader: a new unique id, now."""
    return g.MsgHeader(
        id=uuid.uuid4().hex,
        kind="MsgHeader",
        source=source,
        time=time.time(),
        reqId=req_id,
        parentReqId=parent_req_id,
    )


def new_req_id() -> str:
    return uuid.uuid4().hex


# -- transposition -----------------------------------------------------------


def from_parts(raw: object) -> Msg[Any]:
    """Validate a collection of parts and transpose it into a Msg."""
    if not isinstance(raw, list):
        raise ProtocolError("bad-message", "a message is a list of parts")
    seen: dict[str, Mapping[str, object]] = {}
    for item in raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("kind"), str):
            raise ProtocolError("bad-part", f"a part must be an object with a kind: {item!r}")
        kind = item["kind"]
        assert isinstance(kind, str)
        if kind in seen:
            raise ProtocolError("duplicate-part", f"part {kind} appears twice")
        seen[kind] = item

    req_id = _peek_req_id(seen.get("MsgHeader"))
    bodies = [kind for kind in seen if not kind.endswith("Header")]
    if len(bodies) != 1:
        raise ProtocolError(
            "bad-message", f"a message has exactly one body, got {bodies or 'none'}", req_id
        )
    (body_kind,) = bodies
    spec = BODIES.get(body_kind)
    if spec is None:
        raise ProtocolError("unknown-body", f"unknown message {body_kind}", req_id)
    if "MsgHeader" not in seen:
        raise ProtocolError("missing-part", f"{body_kind} needs a MsgHeader", req_id)

    msg_header = _part(g.MsgHeader, seen["MsgHeader"], req_id)
    assert isinstance(msg_header, g.MsgHeader)
    if spec.role != "event" and msg_header.reqId is None:
        raise ProtocolError("missing-req-id", f"{body_kind} needs a reqId in its MsgHeader")
    return Msg(header=msg_header, body=_part(spec.cls, seen[body_kind], req_id))


def to_parts(msg: Msg[Any]) -> list[dict[str, object]]:
    """The collection for `msg`, in a fixed order: header parts, then the body."""
    return [_dump(msg.header), _dump(msg.body)]


def encode(msg: Msg[Any]) -> bytes:
    return json.dumps(to_parts(msg), separators=(",", ":")).encode()


def decode(frame: bytes) -> Msg[Any]:
    try:
        raw = json.loads(frame)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("bad-json", f"not JSON: {exc}") from exc
    return from_parts(raw)


def hydrate_part(cls: type, raw: object) -> Any:
    """One part (a generated dataclass) from its JSON object, validated."""
    return _part(cls, raw, None)


def _peek_req_id(raw: Mapping[str, object] | None) -> str | None:
    if raw is None:
        return None
    value = raw.get("reqId")
    return value if isinstance(value, str) else None


# -- one part: strict dict <-> generated dataclass ---------------------------


def _part(cls: type, raw: object, req_id: str | None) -> Any:
    if not isinstance(raw, Mapping):
        raise ProtocolError("bad-part", f"{cls.__name__}: expected an object, got {raw!r}", req_id)
    hints = typing.get_type_hints(cls)
    # JSON key -> field (a key that is a Python keyword has a trailing `_`).
    fields = {_json_key(field.name): field for field in dataclasses.fields(cls)}
    unknown = set(raw) - set(fields)
    if unknown:
        raise ProtocolError(
            "bad-part", f"{cls.__name__}: unknown field(s) {sorted(unknown)}", req_id
        )
    values: dict[str, object] = {}
    for key, field in fields.items():
        if key not in raw:
            if field.default is dataclasses.MISSING:
                raise ProtocolError("bad-part", f"{cls.__name__}: missing {key}", req_id)
            continue
        values[field.name] = _value(hints[field.name], raw[key], f"{cls.__name__}.{key}", req_id)
    return cls(**values)


def _json_key(field_name: str) -> str:
    # datamodel-codegen names a field `from_` for the JSON key `from`.
    stem = field_name[:-1]
    return stem if field_name.endswith("_") and keyword.iskeyword(stem) else field_name


def _value(tp: Any, value: object, path: str, req_id: str | None) -> object:
    origin = typing.get_origin(tp)
    if origin is typing.Union or isinstance(tp, type(int | None)):
        members = typing.get_args(tp)
        if value is None and type(None) in members:
            return None
        for member in members:
            if member is type(None):
                continue
            try:
                return _value(member, value, path, req_id)
            except ProtocolError:
                continue
        raise ProtocolError("bad-part", f"{path}: unexpected {value!r}", req_id)
    if origin is list:
        (item,) = typing.get_args(tp)
        if not isinstance(value, list):
            raise ProtocolError("bad-part", f"{path}: expected a list, got {value!r}", req_id)
        return [_value(item, entry, f"{path}[{i}]", req_id) for i, entry in enumerate(value)]
    if origin is dict:
        key_type, item_type = typing.get_args(tp)
        if not isinstance(value, Mapping) or not all(isinstance(k, str) for k in value):
            raise ProtocolError("bad-part", f"{path}: expected an object, got {value!r}", req_id)
        return {k: _value(item_type, v, f"{path}.{k}", req_id) for k, v in value.items()}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        return _part(tp, value, req_id)
    if origin is Literal:
        if value in typing.get_args(tp):
            return value
        raise ProtocolError("bad-part", f"{path}: expected one of {typing.get_args(tp)}", req_id)
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        try:
            return tp(value)
        except ValueError:
            raise ProtocolError("bad-part", f"{path}: unexpected {value!r}", req_id) from None
    if tp is bool:
        ok = isinstance(value, bool)
    elif tp is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif tp is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif tp is str:
        ok = isinstance(value, str)
    else:
        raise TypeError(f"{path}: unsupported field type {tp!r} in a generated module")
    if not ok:
        raise ProtocolError("bad-part", f"{path}: expected {tp.__name__}, got {value!r}", req_id)
    return float(value) if tp is float else value  # type: ignore[arg-type]


def plain(part: object) -> object:
    """A part (or any value of one) as JSON-able data — what it is on the
    wire: absent optionals left out, keyword fields under their JSON key."""
    return _plain(part)


def _dump(part: object) -> dict[str, object]:
    out = _plain(part)
    assert isinstance(out, dict)
    return out


def _plain(value: object) -> object:
    # A dataclass becomes an object — `kind` first (when it has one), then
    # the fields in declaration order, absent optionals left out.
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, object] = {}
        if hasattr(value, "kind"):
            out["kind"] = getattr(value, "kind")
        for field in dataclasses.fields(value):
            item = getattr(value, field.name)
            if field.name != "kind" and item is not None:
                out[_json_key(field.name)] = _plain(item)
        return out
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, enum.Enum):
        return value.value
    return value
