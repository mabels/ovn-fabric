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
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from runtime.message import BodySpec, Msg, ProtocolError, hydrate_part, plain, register_bodies
from runtime.peer import Peer
from runtime.service import Service

from .. import generated as g

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
    return f"sha256:{hashlib.sha256(normal).hexdigest()}"


def _hosts(ir: g.IrCollection) -> list[str]:
    return sorted(node.key.host for node in ir.collection if isinstance(node, g.InfraHostNode))


def _write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class IrStore:
    def __init__(self, state_root: str = STATE_ROOT) -> None:
        self._dir = Path(state_root) / "ir"
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

    async def put(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.IrPutReq)
        if req.name is not None:
            self._check_name(req.name)
        normal = normal_form(req.ir)
        digest = ir_hash(normal)
        name = req.name if req.name is not None else digest
        async with self._lock:
            refs = await asyncio.to_thread(self._read_refs)
            previous = refs.get(name)
            if req.name is None and digest in refs.values():
                # Without a name and already stored: that entry (its hash
                # entry, else its first name), unchanged.
                if digest not in refs:
                    name = min(n for n, d in refs.items() if d == digest)
                previous = digest
            if previous == digest:
                status = g.IrPutStatus.unchanged
            else:
                meta = {
                    "nodes": len(req.ir.collection),
                    "hosts": _hosts(req.ir),
                    "bytes": len(normal),
                    "storedAt": time.time(),
                }
                await asyncio.to_thread(self._store, digest, normal, meta)
                refs[name] = digest
                await asyncio.to_thread(self._write_refs, refs)
                if previous is not None:
                    await asyncio.to_thread(self._collect, previous, refs)
                status = g.IrPutStatus.created if previous is None else g.IrPutStatus.replaced
        was = f", was {previous}" if status is g.IrPutStatus.replaced else ""
        called = "" if name == digest else f" as {name}"
        await peer.log(
            "info", f"{digest}{called}: {status.value} ({len(req.ir.collection)} nodes{was})"
        )
        return g.IrPutRes(
            kind="IrPutRes",
            name=name,
            hash=digest,
            nodes=len(req.ir.collection),
            status=status,
            previous=previous if status is g.IrPutStatus.replaced else None,
        )

    async def list(self, peer: Peer, msg: Msg[Any]) -> object:
        refs = await asyncio.to_thread(self._read_refs)
        infos = [await asyncio.to_thread(self._info, name, digest) for name, digest in refs.items()]
        return g.IrListRes(kind="IrListRes", irs=sorted(infos, key=lambda info: info.name))

    async def get(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.IrGetReq)
        refs = await asyncio.to_thread(self._read_refs)
        if _HASH.match(req.ref):
            digest = req.ref
            name = next((n for n, d in refs.items() if d == digest), "")
        elif req.ref in refs:
            name, digest = req.ref, refs[req.ref]
        else:
            raise ProtocolError("not-found", f"no IR named or hashed {req.ref!r}")
        try:
            raw = await asyncio.to_thread(self._blob(digest).read_bytes)
        except FileNotFoundError:
            raise ProtocolError("not-found", f"no IR with hash {digest}") from None
        ir = hydrate_part(g.IrCollection, {"kind": "IrCollection", "collection": json.loads(raw)})
        info = await asyncio.to_thread(self._info, name, digest)
        return g.IrGetRes(kind="IrGetRes", info=info, ir=ir)

    async def delete(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.IrDeleteReq)
        async with self._lock:
            refs = await asyncio.to_thread(self._read_refs)
            if req.ref not in refs:
                names = sorted(n for n, d in refs.items() if d == req.ref)
                if names:
                    raise ProtocolError(
                        "still-named", f"{req.ref} is named {', '.join(names)}: delete those"
                    )
                raise ProtocolError("not-found", f"no IR named or hashed {req.ref!r}")
            digest = refs.pop(req.ref)
            await asyncio.to_thread(self._write_refs, refs)
            await asyncio.to_thread(self._collect, digest, refs)
        called = "" if req.ref == digest else f" as {req.ref}"
        await peer.log("info", f"{digest}{called}: deleted")
        return g.IrDeleteRes(kind="IrDeleteRes", name=req.ref, hash=digest)

    # -- storage -------------------------------------------------------------

    @staticmethod
    def _check_name(name: str) -> None:
        if not _NAME.match(name):
            raise ProtocolError("bad-request", f"not an IR name: {name!r}")

    def _blob(self, digest: str) -> Path:
        return self._dir / f"{digest.removeprefix('sha256:')}.json"

    def _meta(self, digest: str) -> Path:
        return self._dir / f"{digest.removeprefix('sha256:')}.meta.json"

    def _read_refs(self) -> dict[str, str]:
        try:
            refs = json.loads((self._dir / "refs.json").read_text())
        except FileNotFoundError:
            return {}
        assert isinstance(refs, dict)
        return {str(k): str(v) for k, v in refs.items()}

    def _write_refs(self, refs: dict[str, str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _write_atomically(
            self._dir / "refs.json", json.dumps(refs, sort_keys=True, indent=2).encode()
        )

    def _store(self, digest: str, normal: bytes, meta: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._blob(digest).exists():  # content-addressed: never rewritten
            _write_atomically(self._blob(digest), normal)
            _write_atomically(self._meta(digest), json.dumps(meta).encode())

    def _collect(self, digest: str, refs: dict[str, str]) -> None:
        """Remove content no name points to any more."""
        if digest in refs.values():
            return
        for path in (self._blob(digest), self._meta(digest)):
            path.unlink(missing_ok=True)
        log.info("removed %s: no name points to it", digest)

    def _info(self, name: str, digest: str) -> g.IrInfo:
        meta = json.loads(self._meta(digest).read_text())
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
