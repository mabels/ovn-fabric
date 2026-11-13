"""Jobs (ADR 0012 §7): a detached request belongs to the runtime."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from fabric import generated as g
from fabric.services import version

from . import generated as rg
from .peer import Call, Peer
from .runtime import Runtime
from .service import Service
from .transport import Closed, connect


async def _client(address: str) -> tuple[Peer, asyncio.Task[None]]:
    for _ in range(100):  # until it listens
        try:
            conn = await connect(address)
            break
        except Closed:
            await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"{address} never came up")
    peer = Peer(conn, "test")
    return peer, asyncio.create_task(peer.run())


class JobTest(unittest.TestCase):
    def test_a_detached_request_goes_on_and_can_be_followed(self) -> None:
        async def run() -> tuple[list[str], Any, list[rg.JobInfo]]:
            gate = asyncio.Event()

            async def slow(call: Call) -> object:
                await call.log("info", "before the gate")
                await gate.wait()
                await call.log("info", "after the gate")
                return g.VersionRes(kind="VersionRes", runtime="x", protocol=1)

            runtime = Runtime()
            runtime.register(
                Service(
                    name="slow",
                    bodies=version.BODIES,
                    handlers={"VersionReq": slow},
                    detached=frozenset({"VersionReq"}),
                )
            )
            address = f"mem://jobs-{id(runtime)}"
            stop = asyncio.Event()
            server = asyncio.create_task(runtime.serve(address, stop))
            # The first client sends it, sees it start — and goes away.
            first, reader = await _client(address)
            request = await first.request(g.VersionReq(kind="VersionReq"))
            async for _msg in request:
                break
            await first.close()
            await reader
            gate.set()
            # Another follows it: everything it logged, then its answer.
            second, reader2 = await _client(address)
            follow = await second.request(rg.FollowReq(kind="FollowReq", reqId=request.req_id))
            lines = [m.body.message async for m in follow if isinstance(m.body, rg.LogMsg)]
            final = follow.result.body
            jobs = await second.ask(rg.JobListReq(kind="JobListReq"))
            await second.close()
            await reader2
            stop.set()
            await server
            assert isinstance(jobs.body, rg.JobListRes)
            return lines, final, jobs.body.jobs

        lines, final, jobs = asyncio.run(run())
        self.assertIn("before the gate", lines)
        self.assertIn("after the gate", lines)  # it ran on after its sender left
        self.assertIsInstance(final, g.VersionRes)
        self.assertEqual([(j.request, j.state) for j in jobs], [("VersionReq", rg.JobState.done)])

    def test_following_an_unknown_job_is_an_error(self) -> None:
        async def run() -> object:
            runtime = Runtime()
            address = f"mem://jobs-unknown-{id(runtime)}"
            stop = asyncio.Event()
            server = asyncio.create_task(runtime.serve(address, stop))
            client, reader = await _client(address)
            follow = await client.request(rg.FollowReq(kind="FollowReq", reqId="nope"))
            final = await follow.wait(5)
            await client.close()
            await reader
            stop.set()
            await server
            return final.body

        final = asyncio.run(run())
        self.assertIsInstance(final, rg.ErrorRes)
        assert isinstance(final, rg.ErrorRes)
        self.assertEqual(final.code, "not-found")


if __name__ == "__main__":
    unittest.main()
