"""The runtime process: listens on an address and serves its services.

Each registered service brings its message bodies, the requests it
answers and optionally a long-running part (see runtime/service.py). The job
tree, the bus and the registry of ADR 0007 come in later steps.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from . import generated as g
from .jobs import Job, JobOutbox, Jobs, follow
from .message import BodySpec, ProtocolError, register_bodies
from .peer import Call, Peer
from .service import Handler, Service
from .transport import Connection, connect, dump_wire, listen, wire_log

_JOB_BODIES = {
    "FollowReq": BodySpec(g.FollowReq, "request"),
    "JobListReq": BodySpec(g.JobListReq, "request"),
    "JobListRes": BodySpec(g.JobListRes, "response"),
}
register_bodies(_JOB_BODIES)  # clients decode these too


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
        # Long requests (Service.detached) run as jobs (ADR 0012 §7).
        self._detached: set[str] = set()
        self._jobs = Jobs()
        register_bodies(_JOB_BODIES)

    def register(self, *services: Service) -> None:
        for service in services:
            taken = sorted(set(service.handlers) & set(self._handlers))
            if taken:
                raise ValueError(f"{service.name}: requests already handled: {', '.join(taken)}")
            register_bodies(service.bodies)
            self._handlers.update(service.handlers)
            self._owners.update(dict.fromkeys(service.handlers, service.name))
            self._detached |= set(service.detached)
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
                # The jobs first: they use the internal connection.
                await self._jobs.stop()
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
        A request sent for a handled one names it: `parent=call.req_id`."""
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

    async def _dispatch(self, call: Call) -> object:
        kind = call.msg.kind
        if kind == "FollowReq":
            return await follow(self._jobs.get(call.body.reqId), call.emit)
        if kind == "JobListReq":
            return g.JobListRes(kind="JobListRes", jobs=self._jobs.infos())
        handler = self._handlers.get(kind)
        if handler is None:
            raise ProtocolError("unknown-request", f"{kind} is not handled here")
        # Handled by the service that answers it: its logs carry its name.
        component = self._owners.get(kind, self.source)
        if kind not in self._detached:
            return await handler(call.with_component(component))
        # A job: the handler runs as the runtime's, writing into the job; this
        # connection is its first follower — closing it ends only the
        # following.
        job = Job(call.req_id, kind)
        detached = Call(call.peer, call.msg, component, JobOutbox(job))
        self._jobs.start(job, lambda: handler(detached))
        await detached.log(
            "info", f"{kind} runs as job {call.req_id} (fabric follow {call.req_id})", "rt"
        )
        return await follow(job, call.emit)
