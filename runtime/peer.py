"""A Peer: one connection, as messages.

It runs the connection's one reader loop. Both sides may send at any
time, so a reply is never "the next frame": every incoming message is
routed by its `reqId` —

- to the `Request` this side is waiting on (its logs and progress, then
  the final response), or
- to this side's request handler, when it is a request.

A handler gets the request as a `Call`: everything it emits goes through
that object — nothing is found implicitly (no contextvars), so whatever
works for the request (a helper, a task it starts, a command) is handed
the Call, and a request sent on its behalf names it as parent:
`client.request(body, parent=call.req_id)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from . import generated as g
from . import journal
from .forward import Outbox
from .jobs import JobOutbox
from .message import Msg, ProtocolError, decode, encode, header, new_req_id
from .transport import Closed, Connection

log = logging.getLogger("runtime.peer")


class Call:
    """A request being handled — what its handler gets.

    Everything emitted through it (events, log lines, the lines its
    commands report) goes out tied to the request, in emission order, and
    before the response. `component` is who handles it: a log line carries
    it unless it names its own."""

    def __init__(
        self, peer: Peer, msg: Msg[Any], component: str, outbox: Outbox | JobOutbox
    ) -> None:
        assert msg.req_id is not None
        self.peer = peer
        self.msg = msg
        self.req_id: str = msg.req_id
        self.component = component
        self._outbox = outbox

    @property
    def body(self) -> Any:
        return self.msg.body

    def with_component(self, component: str) -> Call:
        """The same request, handled by `component` (a part of the handler
        with its own name in the logs, such as a unit of work)."""
        return Call(self.peer, self.msg, component, self._outbox)

    async def emit(self, body: object) -> None:
        """An event of the request — after the response, sent on its own."""
        if not self._outbox.put(body):
            await self.peer.send(body, req_id=self.req_id)

    async def log(self, level: str, message: str, component: str | None = None) -> None:
        """A log line of the request, from `component` (default: the Call's)."""
        await self.emit(self._log(level, message, component))

    def report(self, level: str, line: str) -> None:
        """A line about the work (a command and its output; see
        runtime/command.py) — synchronous, so a command's reader can call
        it. After the response it is dropped."""
        self._outbox.put(self._log(level, line, None))

    def _log(self, level: str, message: str, component: str | None) -> g.LogMsg:
        journal.record(
            self.req_id, self.msg.header.parentReqId, component or self.component, level, message
        )
        return g.LogMsg(
            kind="LogMsg",
            level=g.LogLevel(level),
            message=message,
            component=component or self.component,
        )


# A request handler gets the request as a Call and returns the body of the
# final response. An exception becomes an ErrorRes.
OnRequest = Callable[[Call], Awaitable[object]]
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

    async def request(self, body: object, parent: str | None = None) -> Request:
        """Send a request; returns at once with its handle. `parent`: the
        reqId of the request it is sent for (a Call's), if any."""
        req_id = new_req_id()
        request = self._pending[req_id] = Request(req_id)
        msg = Msg(
            header=header(self.source, req_id=req_id, parent_req_id=parent),
            body=body,
        )
        try:
            await self.conn.send(encode(msg))
        except BaseException:
            del self._pending[req_id]
            raise
        return request

    async def ask(
        self, body: object, timeout: float | None = 10.0, parent: str | None = None
    ) -> Msg[Any]:
        """Send a request and return its final response (ErrorRes raises)."""
        request = await self.request(body, parent)
        try:
            final = await request.wait(timeout)
        finally:
            self._pending.pop(request.req_id, None)
        if isinstance(final.body, g.ErrorRes):
            raise RemoteError(final.body)
        return final

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
        outbox = Outbox(self, req_id)
        try:
            if self._on_request is None:
                raise ProtocolError("unknown-request", f"{msg.kind} is not handled here")
            body = await self._on_request(Call(self, msg, self.source, outbox))
            await outbox.close()  # what it emitted first, the response last
            await self.send(body, req_id=req_id)
        except asyncio.CancelledError:
            outbox.abort()
            raise
        except Closed:
            outbox.abort()
            return
        except ProtocolError as exc:
            await outbox.close()
            await self._error(req_id, exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — every failure answers the request
            log.exception("%s: %s failed", self.conn.peer, msg.kind)
            await outbox.close()
            await self._error(req_id, "failed", f"{type(exc).__name__}: {exc}")

    async def _error(self, req_id: str, code: str, message: str) -> None:
        with contextlib.suppress(Closed):
            await self.send(g.ErrorRes(kind="ErrorRes", code=code, message=message), req_id)
