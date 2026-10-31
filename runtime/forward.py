"""The outbox of a handled request: everything its handler emits, in order
— then the response.

The Peer opens one Outbox per request it handles and hands it to the
request's Call (runtime/peer.py): every `call.emit`/`call.log` and every
line the handler's commands report (`call.report`) is queued there, and one
task sends the queue in order. The Peer closes it before it sends the
response: what was queued still goes out, so the response comes last.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from .transport import Closed

if TYPE_CHECKING:
    from .peer import Peer


class Outbox:
    def __init__(self, peer: Peer, req_id: str) -> None:
        self.peer = peer
        self.req_id = req_id
        self._queue: asyncio.Queue[object | None] = asyncio.Queue()
        self._open = True
        self._task = asyncio.get_running_loop().create_task(self._send())

    def put(self, body: object) -> bool:
        """Queue `body`; False once the outbox is closed (then the caller
        sends it itself, or drops it)."""
        if not self._open:
            return False
        self._queue.put_nowait(body)
        return True

    async def close(self) -> None:
        """Send what is queued, then take nothing more."""
        if self._open:
            self._open = False
            self._queue.put_nowait(None)
        await self._task

    def abort(self) -> None:
        """Drop what is queued (the request was cancelled)."""
        self._open = False
        self._task.cancel()

    async def _send(self) -> None:
        while (body := await self._queue.get()) is not None:
            with contextlib.suppress(Closed):
                await self.peer.send(body, req_id=self.req_id)
