"""VersionReq → VersionRes: the runtime's and the wire protocol's version."""

from __future__ import annotations

from typing import Any

from runtime import PROTOCOL, __version__
from runtime.message import BodySpec, Msg, register_bodies
from runtime.peer import Peer
from runtime.service import Service

from .. import generated as g

BODIES = {
    "VersionReq": BodySpec(g.VersionReq, "request"),
    "VersionRes": BodySpec(g.VersionRes, "response"),
}
register_bodies(BODIES)  # clients decode these too


async def version(peer: Peer, msg: Msg[Any]) -> object:
    await peer.log("info", f"version requested by {msg.header.source}")
    return g.VersionRes(kind="VersionRes", runtime=__version__, protocol=PROTOCOL)


def service() -> Service:
    return Service(name="version", bodies=BODIES, handlers={"VersionReq": version})
