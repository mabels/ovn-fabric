"""A content store: blobs by their sha256, names pointing at hashes.

    <dir>/<hex><suffix>     a blob, by content — never rewritten
    <dir>/<hex>.meta.json   what the store's user records about it
    <dir>/refs.json         {name: hash}  (a blob without a name: {hash: hash})

A name points to one hash (like a branch to a commit). Putting the same
content under a name again changes nothing; other content moves the name.
What no name points to any more is removed — at once (`keep=0`, the IR
store), or once more than `keep` such blobs pile up, the oldest first (the
runtime store keeps the last 10 to go back to, ADR 0012).

Used by the IR store (fabric/services/ir.py: IRs, by their normal form) and
the runtime store (fabric/runtime_store.py: deployed PEX files).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("fabric.store")

HASH = re.compile(r"^sha256:([0-9a-f]{64})$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


class StoreError(ValueError):
    """`code` says which: not-found, still-named, bad-name."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclasses.dataclass(frozen=True)
class Put:
    """What a put did: `status` created | replaced | unchanged."""

    name: str
    digest: str
    status: str
    previous: str | None


class ContentStore:
    def __init__(self, directory: Path, suffix: str, keep: int = 0) -> None:
        self.dir = directory
        self._suffix = suffix
        self._keep = keep

    # -- names ---------------------------------------------------------------

    def refs(self) -> dict[str, str]:
        try:
            refs = json.loads((self.dir / "refs.json").read_text())
        except FileNotFoundError:
            return {}
        assert isinstance(refs, dict)
        return {str(k): str(v) for k, v in refs.items()}

    def _write_refs(self, refs: dict[str, str]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        write_atomically(
            self.dir / "refs.json", json.dumps(refs, sort_keys=True, indent=2).encode()
        )

    def resolve(self, ref: str) -> tuple[str, str]:
        """(name, hash) of `ref` — a name, or a stored hash (its first name,
        or "" when it has none)."""
        refs = self.refs()
        if HASH.match(ref):
            if not self.blob(ref).exists():
                raise StoreError("not-found", f"nothing stored with hash {ref}")
            return next((n for n, d in sorted(refs.items()) if d == ref), ""), ref
        if ref in refs:
            return ref, refs[ref]
        raise StoreError("not-found", f"nothing named or hashed {ref!r}")

    # -- blobs ---------------------------------------------------------------

    def blob(self, digest: str) -> Path:
        return self.dir / f"{digest.removeprefix('sha256:')}{self._suffix}"

    def meta(self, digest: str) -> dict[str, Any]:
        meta = json.loads((self.dir / f"{digest.removeprefix('sha256:')}.meta.json").read_text())
        assert isinstance(meta, dict)
        return meta

    def put(self, name: str | None, data: bytes, meta: dict[str, Any]) -> Put:
        """Store `data` (once per content) and point `name` at it — without a
        name, the content is listed under its hash."""
        if name is not None and not NAME.match(name):
            raise StoreError("bad-name", f"not a name: {name!r}")
        digest = digest_of(data)
        refs = self.refs()
        key = name if name is not None else digest
        previous = refs.get(key)
        if name is None and digest in refs.values():
            # Without a name and already stored: that entry, unchanged.
            if digest not in refs:
                key = min(n for n, d in refs.items() if d == digest)
            previous = digest
        if previous == digest:
            return Put(key, digest, "unchanged", None)
        self._store(digest, data, meta)
        refs[key] = digest
        self._write_refs(refs)
        if previous is not None:
            self._collect(refs)
        return Put(key, digest, "created" if previous is None else "replaced", previous)

    def point(self, name: str, digest: str) -> str | None:
        """Point `name` at an already stored hash; the hash it pointed to."""
        if not NAME.match(name):
            raise StoreError("bad-name", f"not a name: {name!r}")
        if not self.blob(digest).exists():
            raise StoreError("not-found", f"nothing stored with hash {digest}")
        refs = self.refs()
        previous = refs.get(name)
        refs[name] = digest
        refs.pop(digest, None)  # named now: no longer listed under its hash
        self._write_refs(refs)
        self._collect(refs)
        return previous

    def delete(self, ref: str) -> str:
        """Remove the name `ref` (or the unnamed entry of a hash); its hash."""
        refs = self.refs()
        if ref not in refs:
            names = sorted(n for n, d in refs.items() if d == ref)
            if names:
                raise StoreError("still-named", f"{ref} is named {', '.join(names)}: delete those")
            raise StoreError("not-found", f"nothing named or hashed {ref!r}")
        digest = refs.pop(ref)
        self._write_refs(refs)
        self._collect(refs)
        return digest

    def entries(self) -> list[tuple[str, str]]:
        """(name, hash) of every name, sorted by name."""
        return sorted(self.refs().items())

    def _store(self, digest: str, data: bytes, meta: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.blob(digest).exists():  # content-addressed: never rewritten
            write_atomically(self.blob(digest), data)
            write_atomically(
                self.dir / f"{digest.removeprefix('sha256:')}.meta.json",
                json.dumps({**meta, "storedAt": meta.get("storedAt", time.time())}).encode(),
            )

    def _collect(self, refs: dict[str, str]) -> None:
        """Remove blobs no name points to, keeping the newest `keep` of them."""
        named = set(refs.values())
        loose = []
        for path in self.dir.glob(f"*{self._suffix}"):
            digest = f"sha256:{path.name.removesuffix(self._suffix)}"
            if HASH.match(digest) and digest not in named:
                loose.append((self._stored_at(digest), digest))
        loose.sort(reverse=True)
        for _at, digest in loose[self._keep :]:
            self.blob(digest).unlink(missing_ok=True)
            (self.dir / f"{digest.removeprefix('sha256:')}.meta.json").unlink(missing_ok=True)
            log.info("removed %s: no name points to it", digest)

    def _stored_at(self, digest: str) -> float:
        try:
            return float(self.meta(digest).get("storedAt", 0.0))
        except FileNotFoundError:
            return 0.0
