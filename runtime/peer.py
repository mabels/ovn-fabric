"""A Peer: one connection, as messages.

It runs the connection's one reader loop. Both sides may send at any
time, so a reply is never "the next frame": every incoming message is
routed by its `reqId` —

- to the `Request` this side is waiting on (its logs and progress, then
  the final response), or
- to this side's request handler, when it is a request.

While a handler runs, the request's `reqId` is the *current request*
(a contextvar): every `log()` made inside it carries that reqId, and a
request sent from inside it carries it as `parentReqId` — without the
handler passing ids around.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from . import generated as g
from .message import Msg, ProtocolError, decode, encode, header, new_req_id
from .transport import Closed, Connection

log = logging.getLogger("runtime.peer")

# The reqId of the request being handled in this task, if any.
current_req: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_req", default=None
)

# Who is handling it — the service the runtime dispatched it to; a log
# line carries it as its component.
current_component: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_component", default=None
)


class Outbox:
    """Everything a handler emits for its request, in emission order: its
    events, its logs and the commands it reports. The runtime opens one per
    dispatched request (runtime/forward.py) and drains it before the
    response, so nothing overtakes anything — the response last."""

    def __init__(self, peer: Peer) -> None:
        self.peer = peer
        self.queue: asyncio.Queue[tuple[object, str | None] | None] = asyncio.Queue()
        self.open = True


current_outbox: contextvars.ContextVar[Outbox | None] = contextvars.ContextVar(
    "current_outbox", default=None
)

# A request handler gets the peer and the request, and returns the body of
# the final response. An exception becomes an ErrorRes.
OnRequest = Callable[["Peer", Msg[Any]], Awaitable[object]]
OnEvent = Callable[["Peer", Msg[Any]], Awaitable[None]]


class RemoteError(RuntimeError):
    """The other side answered a request with ErrorRes."""

    def __init__(self, error: g.ErrorRes) -> None:
        super().__init__(f"{error.code}: {error.message}")
        self.code = error.code
        self.message = error.message


class Request:
    """A request this side sent: iterate it for everything carrying its
    reqId (logs, progress …) until the final response; then `result`."""

    def __init__(self, req_id: str) -> None:
        self.req_id = req_id
        self._queue: asyncio.Queue[Msg[Any] | BaseException] = asyncio.Queue()
        self._final: Msg[Any] | None = None
        self._done = asyncio.Event()

    @property
    def result(self) -> Msg[Any]:
        if self._final is None:
            raise RuntimeError(f"request {self.req_id} has no response yet")
        return self._final

    async def __aiter__(self) -> AsyncIterator[Msg[Any]]:
        while not self._done.is_set() or not self._queue.empty():
            item = await self._queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    async def wait(self, timeout: float | None = None) -> Msg[Any]:
        """The final response, ignoring everything in between."""
        await asyncio.wait_for(self._done.wait(), timeout)
        if self._final is None:
            while not self._queue.empty():
                item = self._queue.get_nowait()
                if isinstance(item, BaseException):
                    raise item
            raise Closed(f"request {self.req_id}: no response")
        return self._final

    def _deliver(self, msg: Msg[Any]) -> None:
        self._queue.put_nowait(msg)
        if msg.role == "response":
            self._final = msg
            self._done.set()

    def _fail(self, exc: BaseException) -> None:
        if not self._done.is_set():
            self._queue.put_nowait(exc)
            self._done.set()


class Peer:
    def __init__(
        self,
        conn: Connection,
        source: str,
        on_request: OnRequest | None = None,
        on_event: OnEvent | None = None,
    ) -> None:
        self.conn = conn
        self.source = source
        self._on_request = on_request
        self._on_event = on_event
        self._pending: dict[str, Request] = {}
        self._handlers: set[asyncio.Task[None]] = set()

    # -- sending -------------------------------------------------------------

    async def send(self, body: object, req_id: str | None = None) -> None:
        msg = Msg(header=header(self.source, req_id=req_id), body=body)
        await self.conn.send(encode(msg))

    async def request(self, body: object) -> Request:
        """Send a request; returns at once with its handle."""
        req_id = new_req_id()
        request = self._pending[req_id] = Request(req_id)
        msg = Msg(
            header=header(self.source, req_id=req_id, parent_req_id=current_req.get()),
            body=body,
        )
        try:
            await self.conn.send(encode(msg))
        except BaseException:
            del self._pending[req_id]
            raise
        return request

    async def ask(self, body: object, timeout: float | None = 10.0) -> Msg[Any]:
        """Send a request and return its final response (ErrorRes raises)."""
        request = await self.request(body)
        try:
            final = await request.wait(timeout)
        finally:
            self._pending.pop(request.req_id, None)
        if isinstance(final.body, g.ErrorRes):
            raise RemoteError(final.body)
        return final

    async def emit(self, body: object) -> None:
        """An event, tied to the request being handled (if any) — in order
        with everything else the handler emits."""
        outbox = current_outbox.get()
        if outbox is not None and outbox.peer is self and outbox.open:
            outbox.queue.put_nowait((body, current_req.get()))
            return
        await self.send(body, req_id=current_req.get())

    async def log(self, level: str, message: str, component: str | None = None) -> None:
        """A log line, tied to the request being handled (if any), from
        `component` — by default the service handling it."""
        await self.emit(
            g.LogMsg(
                kind="LogMsg",
                level=g.LogLevel(level),
                message=message,
                component=component or current_component.get(),
            )
        )

    async def close(self) -> None:
        await self.conn.close()

    # -- receiving -----------------------------------------------------------

    async def run(self) -> None:
        """The reader loop; returns when the connection closes."""
        try:
            while True:
                try:
                    frame = await self.conn.recv()
                except Closed:
                    return
                await self._receive(frame)
        finally:
            for task in self._handlers:
                task.cancel()
            for task in list(self._handlers):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for request in self._pending.values():
                request._fail(Closed(f"{self.conn.peer}: connection closed"))
            self._pending.clear()

    async def _receive(self, frame: bytes) -> None:
        try:
            msg = decode(frame)
        except ProtocolError as exc:
            log.warning("%s: invalid message: %s", self.conn.peer, exc)
            if exc.req_id is None:
                return
            mine = self._pending.pop(exc.req_id, None)
            if mine is not None:
                mine._fail(exc)  # an invalid answer to our own request
            else:
                await self._error(exc.req_id, exc.code, str(exc))
            return
        if msg.role == "request":
            self._spawn(msg)
            return
        request = self._pending.get(msg.req_id) if msg.req_id is not None else None
        if request is not None:
            request._deliver(msg)
            if msg.role == "response":
                self._pending.pop(request.req_id, None)
        elif self._on_event is not None:
            await self._on_event(self, msg)

    def _spawn(self, msg: Msg[Any]) -> None:
        task = asyncio.get_running_loop().create_task(self._handle(msg))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _handle(self, msg: Msg[Any]) -> None:
        req_id = msg.req_id
        assert req_id is not None
        current_req.set(req_id)  # this task's own context
        try:
            if self._on_request is None:
                raise ProtocolError("unknown-request", f"{msg.kind} is not handled here")
            body = await self._on_request(self, msg)
            await self.send(body, req_id=req_id)
        except asyncio.CancelledError:
            raise
        except Closed:
            return
        except ProtocolError as exc:
            await self._error(req_id, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — every failure answers the request
            log.exception("%s: %s failed", self.conn.peer, msg.kind)
            await self._error(req_id, "failed", f"{type(exc).__name__}: {exc}")

    async def _error(self, req_id: str, code: str, message: str) -> None:
        with contextlib.suppress(Closed):
            await self.send(g.ErrorRes(kind="ErrorRes", code=code, message=message), req_id)
