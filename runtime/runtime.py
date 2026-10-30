"""The runtime process: listens on an address and serves its services.

Each registered service brings its message bodies, the requests it
answers and optionally a long-running part (see runtime/service.py). The job
tree, the bus and the registry of ADR 0007 come in later steps.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from .forward import outbox
from .message import Msg, ProtocolError, register_bodies
from .peer import Peer, current_component
from .service import Handler, Service
from .transport import Connection, connect, dump_wire, listen, wire_log


class Runtime:
    def __init__(self, source: str = "rt") -> None:
        self.source = source
        self._services: list[Service] = []
        self._handlers: dict[str, Handler] = {}
        # Which service answers a request kind: its component name.
        self._owners: dict[str, str] = {}
        self._peers: set[Peer] = set()
        # Services ask each other through this in-process address, with the
        # same protocol as any outside client.
        self._internal = f"mem://rt-internal-{id(self)}"
        self._client: Peer | None = None
        self._client_reader: asyncio.Task[None] | None = None
        self._client_lock = asyncio.Lock()

    def register(self, *services: Service) -> None:
        for service in services:
            taken = sorted(set(service.handlers) & set(self._handlers))
            if taken:
                raise ValueError(f"{service.name}: requests already handled: {', '.join(taken)}")
            register_bodies(service.bodies)
            self._handlers.update(service.handlers)
            self._owners.update(dict.fromkeys(service.handlers, service.name))
            self._services.append(service)

    def handle(self, kind: str, handler: Handler) -> None:
        """Answer requests whose body is `kind` (replaces a service's handler)."""
        self._handlers[kind] = handler

    async def serve(self, address: str, stop: asyncio.Event) -> None:
        """Run the services and serve `address` until `stop` is set; then
        close every connection and stop the services."""
        async with asyncio.TaskGroup() as tg:
            running = [
                tg.create_task(service.run(), name=service.name)
                for service in self._services
                if service.run is not None
            ]
            async with listen(self._internal, self._connected), listen(address, self._connected):
                await stop.wait()
                if self._client is not None:
                    await self._client.close()
                for peer in list(self._peers):
                    await peer.close()
            if self._client_reader is not None:
                await self._client_reader
            for task in running:
                task.cancel()

    async def client(self) -> Peer:
        """The services' own connection to this runtime (while it serves).
        A request sent from inside a handler carries the handled request as
        its parentReqId."""
        async with self._client_lock:
            if self._client is None:
                client = Peer(await connect(self._internal), f"{self.source}:internal")
                self._client_reader = asyncio.get_running_loop().create_task(client.run())
                self._client = client
            return self._client

    async def _connected(self, conn: Connection) -> None:
        if wire_log.isEnabledFor(logging.DEBUG):  # --log wire=debug
            conn = dump_wire(conn)
        peer = Peer(conn, self.source, on_request=self._dispatch)
        self._peers.add(peer)
        try:
            await peer.run()
        finally:
            self._peers.discard(peer)
            with contextlib.suppress(Exception):
                await conn.close()

    async def _dispatch(self, peer: Peer, msg: Msg[Any]) -> object:
        handler = self._handlers.get(msg.kind)
        if handler is None:
            raise ProtocolError("unknown-request", f"{msg.kind} is not handled here")
        # This handler task's own context: its logs (and the tasks it starts)
        # carry the service's name, and every command it runs is reported
        # to the requester.
        current_component.set(self._owners.get(msg.kind, self.source))
        async with outbox(peer):
            return await handler(peer, msg)
