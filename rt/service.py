"""A service: what the runtime serves for it.

A service is one file (rt/services/<name>.py) that declares

- the message bodies it speaks (`bodies`, registered on the transposer),
- the requests it answers (`handlers`: body kind → handler), and
- optionally a long-running part (`run`, e.g. a worker draining a queue),
  started with the runtime and cancelled when it stops.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any

from .message import BodySpec, Msg
from .peer import Peer

# Gets the peer the request came from and the request; returns the body of
# the final response. An exception becomes an ErrorRes.
Handler = Callable[[Peer, Msg[Any]], Awaitable[object]]


@dataclasses.dataclass(frozen=True)
class Service:
    name: str
    bodies: Mapping[str, BodySpec]
    handlers: Mapping[str, Handler]
    run: Callable[[], Coroutine[Any, Any, None]] | None = None
