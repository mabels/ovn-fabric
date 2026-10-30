"""Transports: a message-oriented, full-duplex connection between two
components — like a websocket. `send()` and `recv()` move one whole
frame (one message) each; either side may send at any time. A transport
knows nothing about JSON, parts or kinds.

Addresses pick the transport:

- `mem://<name>`  — in-process (tests, components inside one process)
- `unix://<path>` — a Unix socket, frames length-prefixed (4-byte length)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import struct
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Protocol

MAX_FRAME = 16 * 1024 * 1024
_LENGTH = struct.Struct(">I")


class Closed(ConnectionError):
    """The connection is closed (by either side)."""


class Connection(Protocol):
    peer: str

    async def send(self, frame: bytes) -> None: ...

    async def recv(self) -> bytes: ...

    async def close(self) -> None: ...


OnConnect = Callable[[Connection], Coroutine[Any, Any, None]]

wire_log = logging.getLogger("runtime.wire")


class _DumpingConnection:
    """Logs every frame at debug as it goes over the wire: `>>` sent, `<<` received."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self.peer = conn.peer

    async def send(self, frame: bytes) -> None:
        wire_log.debug(">> %s %s", self.peer, frame.decode(errors="replace"))
        await self._conn.send(frame)

    async def recv(self) -> bytes:
        frame = await self._conn.recv()
        wire_log.debug("<< %s %s", self.peer, frame.decode(errors="replace"))
        return frame

    async def close(self) -> None:
        await self._conn.close()


def dump_wire(conn: Connection) -> Connection:
    """`conn`, with every frame it sends and receives logged to `runtime.wire`."""
    return _DumpingConnection(conn)


# -- mem:// ------------------------------------------------------------------


class _MemConnection:
    def __init__(self, peer: str, inbox: asyncio.Queue[bytes | None]) -> None:
        self.peer = peer
        self._inbox = inbox
        self._other: _MemConnection | None = None
        self._closed = False

    async def send(self, frame: bytes) -> None:
        if self._closed or self._other is None or self._other._closed:
            raise Closed(self.peer)
        await self._other._inbox.put(frame)

    async def recv(self) -> bytes:
        if self._closed:
            raise Closed(self.peer)
        frame = await self._inbox.get()
        if frame is None:
            self._closed = True
            raise Closed(self.peer)
        return frame

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._inbox.put(None)
        if self._other is not None and not self._other._closed:
            await self._other._inbox.put(None)


def mem_pair(name: str = "mem") -> tuple[Connection, Connection]:
    a = _MemConnection(f"{name}:a", asyncio.Queue())
    b = _MemConnection(f"{name}:b", asyncio.Queue())
    a._other, b._other = b, a
    return a, b


_mem_listeners: dict[str, OnConnect] = {}


# -- unix:// -----------------------------------------------------------------


class _StreamConnection:
    def __init__(self, peer: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.peer = peer
        self._reader = reader
        self._writer = writer
        self._send_lock = asyncio.Lock()

    async def send(self, frame: bytes) -> None:
        if len(frame) > MAX_FRAME:
            raise ValueError(f"frame of {len(frame)} bytes exceeds {MAX_FRAME}")
        async with self._send_lock:  # one frame at a time, never interleaved
            if self._writer.is_closing():
                raise Closed(self.peer)
            try:
                self._writer.write(_LENGTH.pack(len(frame)) + frame)
                await self._writer.drain()
            except (ConnectionError, BrokenPipeError) as exc:
                raise Closed(self.peer) from exc

    async def recv(self) -> bytes:
        try:
            (length,) = _LENGTH.unpack(await self._reader.readexactly(_LENGTH.size))
            if length > MAX_FRAME:
                raise Closed(f"{self.peer}: frame of {length} bytes exceeds {MAX_FRAME}")
            return await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            raise Closed(self.peer) from exc

    async def close(self) -> None:
        if self._writer.is_closing():
            return
        self._writer.close()
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await self._writer.wait_closed()


# -- addressing --------------------------------------------------------------


def _split(address: str) -> tuple[str, str]:
    scheme, sep, rest = address.partition("://")
    if not sep or scheme not in ("mem", "unix"):
        raise ValueError(f"unsupported address {address!r} (mem://name, unix:///path)")
    return scheme, rest


async def connect(address: str) -> Connection:
    scheme, rest = _split(address)
    if scheme == "mem":
        on_connect = _mem_listeners.get(rest)
        if on_connect is None:
            raise Closed(f"nobody listens on {address}")
        client, server = mem_pair(rest)
        asyncio.get_running_loop().create_task(on_connect(server))
        return client
    try:
        reader, writer = await asyncio.open_unix_connection(rest)
    except OSError as exc:
        raise Closed(f"cannot connect to {address}: {exc.strerror or exc}") from exc
    return _StreamConnection(address, reader, writer)


@contextlib.asynccontextmanager
async def listen(address: str, on_connect: OnConnect) -> AsyncIterator[None]:
    """Accept connections on `address` while the context is open; each
    one is handed to `on_connect` in its own task."""
    scheme, rest = _split(address)
    if scheme == "mem":
        if rest in _mem_listeners:
            raise OSError(f"{address} is already in use")
        _mem_listeners[rest] = on_connect
        try:
            yield
        finally:
            del _mem_listeners[rest]
        return

    async def accepted(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await on_connect(_StreamConnection(address, reader, writer))

    with contextlib.suppress(FileNotFoundError):
        os.unlink(rest)  # a stale socket from a previous run
    server = await asyncio.start_unix_server(accepted, path=rest)
    os.chmod(rest, 0o600)  # the owner only, until auth exists
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(rest)
