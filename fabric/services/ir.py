"""The IR manager: stores IRs under names, by content.

    IrPutReq { name, ir }    → IrPutRes { name, hash, nodes, status, previous? }
    IrListReq {}             → IrListRes { irs: [IrInfo] }
    IrGetReq { ref }         → IrGetRes { info, ir }      ref: a name or a hash
    IrDeleteReq { ref }      → IrDeleteRes { name, hash }   ref: a name or a hash

An IR is stored once per content, under its hash; a name points to one
hash (like a branch to a commit). Without a name the hash is its identity:
it is listed under its hash. Putting the same content again changes
nothing (`unchanged` — without a name also when the content already has
one); different content moves the name (`replaced`, with the `previous`
hash). Deleting by hash removes an IR stored without a name, and is
refused while names still point to it. Content nothing refers to any more
is removed — no history is kept.

The hash is sha256 over the IR's normal form, so every equivalent input
gets the same one: its typed form (absent and null are the same), the
nodes sorted by id, object keys sorted, no whitespace, UTF-8. Lists inside
a node keep their order — some are ordered by meaning (a command's
arguments, routes, rules). The normal form is also what is stored.

Storage, under <state root>/ir/:
    <hex>.json         the normal form of an IR
    <hex>.meta.json    {nodes, hosts, bytes, storedAt}
    refs.json          {name: hash}  (an IR without a name: {hash: hash})
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from runtime.message import BodySpec, ProtocolError, hydrate_part, plain, register_bodies
from runtime.peer import Call
from runtime.service import Service

from .. import generated as g
from ..store import ContentStore, StoreError, digest_of

log = logging.getLogger("fabric.ir")

BODIES = {
    "IrPutReq": BodySpec(g.IrPutReq, "request"),
    "IrPutRes": BodySpec(g.IrPutRes, "response"),
    "IrListReq": BodySpec(g.IrListReq, "request"),
    "IrListRes": BodySpec(g.IrListRes, "response"),
    "IrGetReq": BodySpec(g.IrGetReq, "request"),
    "IrGetRes": BodySpec(g.IrGetRes, "response"),
    "IrDeleteReq": BodySpec(g.IrDeleteReq, "request"),
    "IrDeleteRes": BodySpec(g.IrDeleteRes, "response"),
}
register_bodies(BODIES)  # clients decode these too

STATE_ROOT = "/var/lib/ovn-fabric"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_HASH = re.compile(r"^sha256:([0-9a-f]{64})$")


def normal_form(ir: g.IrCollection) -> bytes:
    """The IR's normal form: what is hashed and stored."""
    nodes = plain(ir.collection)
    assert isinstance(nodes, list)
    nodes.sort(key=lambda node: str(node["id"]))
    return json.dumps(nodes, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def ir_hash(normal: bytes) -> str:
    return digest_of(normal)


def _hosts(ir: g.IrCollection) -> list[str]:
    return sorted(node.key.host for node in ir.collection if isinstance(node, g.InfraHostNode))


class IrStore:
    def __init__(self, state_root: str = STATE_ROOT) -> None:
        self._store = ContentStore(Path(state_root) / "ir", ".json")
        self._lock = asyncio.Lock()  # one change at a time

    def service(self) -> Service:
        return Service(
            name="ir",
            bodies=BODIES,
            handlers={
                "IrPutReq": self.put,
                "IrListReq": self.list,
                "IrGetReq": self.get,
                "IrDeleteReq": self.delete,
            },
        )

    # -- requests ------------------------------------------------------------

    async def put(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.IrPutReq)
        normal = normal_form(req.ir)
        meta = {"nodes": len(req.ir.collection), "hosts": _hosts(req.ir), "bytes": len(normal)}
        async with self._lock:
            try:
                put = await asyncio.to_thread(self._store.put, req.name, normal, meta)
            except StoreError as exc:
                raise ProtocolError("bad-request", f"not an IR name: {req.name!r}") from exc
        status = g.IrPutStatus(put.status)
        was = f", was {put.previous}" if status is g.IrPutStatus.replaced else ""
        called = "" if put.name == put.digest else f" as {put.name}"
        await call.log(
            "info", f"{put.digest}{called}: {status.value} ({len(req.ir.collection)} nodes{was})"
        )
        return g.IrPutRes(
            kind="IrPutRes",
            name=put.name,
            hash=put.digest,
            nodes=len(req.ir.collection),
            status=status,
            previous=put.previous if status is g.IrPutStatus.replaced else None,
        )

    async def list(self, call: Call) -> object:
        entries = await asyncio.to_thread(self._store.entries)
        infos = [await asyncio.to_thread(self._info, name, digest) for name, digest in entries]
        return g.IrListRes(kind="IrListRes", irs=sorted(infos, key=lambda info: info.name))

    async def get(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.IrGetReq)
        try:
            name, digest = await asyncio.to_thread(self._store.resolve, req.ref)
            raw = await asyncio.to_thread(self._store.blob(digest).read_bytes)
        except (StoreError, FileNotFoundError):
            raise ProtocolError("not-found", f"no IR named or hashed {req.ref!r}") from None
        ir = hydrate_part(g.IrCollection, {"kind": "IrCollection", "collection": json.loads(raw)})
        info = await asyncio.to_thread(self._info, name, digest)
        return g.IrGetRes(kind="IrGetRes", info=info, ir=ir)

    async def delete(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.IrDeleteReq)
        async with self._lock:
            try:
                digest = await asyncio.to_thread(self._store.delete, req.ref)
            except StoreError as exc:
                raise ProtocolError(exc.code, str(exc).replace("nothing", "no IR")) from None
        called = "" if req.ref == digest else f" as {req.ref}"
        await call.log("info", f"{digest}{called}: deleted")
        return g.IrDeleteRes(kind="IrDeleteRes", name=req.ref, hash=digest)

    # -- storage -------------------------------------------------------------

    def _info(self, name: str, digest: str) -> g.IrInfo:
        meta = self._store.meta(digest)
        return g.IrInfo(
            name=name,
            hash=digest,
            nodes=int(meta["nodes"]),
            hosts=list(meta["hosts"]),
            bytes=int(meta["bytes"]),
            storedAt=float(meta["storedAt"]),
        )


def service(state_root: str = STATE_ROOT) -> Service:
    return IrStore(state_root).service()
