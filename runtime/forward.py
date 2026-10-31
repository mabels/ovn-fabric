"""The outbox of a dispatched request: everything its handler emits, in
order — then the response.

`outbox(peer)` opens a peer.Outbox for the request being handled: while it
is open, every `peer.emit`/`peer.log` of the handler (and of the tasks it
starts) is queued there, and so is every command it runs (`command.report`:
the command line at info, its output at debug, as LogMsg from the current
component). One task sends the queue in order; on exit it is drained, so
the response, sent after, comes last. The runtime wraps every handler in
one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from . import command
from . import generated as g
from .peer import Outbox, Peer, current_component, current_outbox, current_req
from .transport import Closed


@contextlib.asynccontextmanager
async def outbox(peer: Peer) -> AsyncIterator[None]:
    box = Outbox(peer)

    async def send() -> None:
        while (item := await box.queue.get()) is not None:
            body, req_id = item
            with contextlib.suppress(Closed):
                await peer.send(body, req_id=req_id)

    def report(level: str, line: str) -> None:
        body = g.LogMsg(
            kind="LogMsg",
            level=g.LogLevel(level),
            message=line,
            component=current_component.get(),
        )
        if box.open:
            box.queue.put_nowait((body, current_req.get()))

    task = asyncio.get_running_loop().create_task(send())
    token = current_outbox.set(box)
    try:
        with command.reporting(report):
            yield
    finally:
        current_outbox.reset(token)
        box.open = False
        box.queue.put_nowait(None)  # what was emitted still goes out
        await task
