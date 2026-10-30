from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from . import PROTOCOL, __version__
from . import generated as g
from .message import Msg
from .peer import Peer, RemoteError
from .runtime import Runtime
from .services import version
from .transport import Closed, connect, dump_wire, mem_pair


@contextlib.asynccontextmanager
async def _running(address: str, runtime: Runtime) -> AsyncIterator[Peer]:
    """A runtime serving `address`, and a connected client peer."""
    stop = asyncio.Event()
    server = asyncio.create_task(runtime.serve(address, stop))
    for _ in range(100):  # until it listens
        try:
            conn = await connect(address)
            break
        except Closed:
            await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"{address} never came up")
    client = Peer(conn, "test")
    reader = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        await client.close()
        await reader
        stop.set()
        await server


class _Transports:
    """Run every test on each transport: the semantics must be the same."""

    def addresses(self) -> list[str]:
        tmp = tempfile.mkdtemp()
        return ["mem://rt-test", f"unix://{Path(tmp) / 'rt.sock'}"]


class VersionTest(unittest.TestCase, _Transports):
    def run_on_each(self, test: Callable[[Peer, Runtime], Awaitable[None]]) -> None:
        for address in self.addresses():
            with self.subTest(transport=address.split(":", 1)[0]):

                async def run() -> None:
                    runtime = Runtime()
                    runtime.register(version.service())
                    async with _running(address, runtime) as client:
                        await test(client, runtime)

                asyncio.run(run())

    def test_version_round_trip(self) -> None:
        async def test(client: Peer, _runtime: Runtime) -> None:
            res = await client.ask(g.VersionReq(kind="VersionReq"))
            self.assertEqual(res.body, g.VersionRes("VersionRes", PROTOCOL, __version__))

        self.run_on_each(test)

    def test_the_logs_of_a_request_carry_its_req_id(self) -> None:
        async def test(client: Peer, _runtime: Runtime) -> None:
            request = await client.request(g.VersionReq(kind="VersionReq"))
            seen = [msg async for msg in request]
            self.assertEqual([msg.kind for msg in seen], ["LogMsg", "VersionRes"])
            self.assertEqual({msg.req_id for msg in seen}, {request.req_id})
            self.assertIn("version requested by test", seen[0].body.message)

        self.run_on_each(test)

    def test_an_unknown_request_answers_error(self) -> None:
        async def test(client: Peer, runtime: Runtime) -> None:
            runtime._handlers.pop("VersionReq")
            with self.assertRaises(RemoteError) as caught:
                await client.ask(g.VersionReq(kind="VersionReq"))
            self.assertEqual(caught.exception.code, "unknown-request")

        self.run_on_each(test)

    def test_a_failing_handler_answers_error(self) -> None:
        async def test(client: Peer, runtime: Runtime) -> None:
            async def broken(_peer: Peer, _msg: Msg[Any]) -> object:
                raise RuntimeError("boom")

            runtime.handle("VersionReq", broken)
            with self.assertLogs("rt.peer", "ERROR"), self.assertRaises(RemoteError) as caught:
                await client.ask(g.VersionReq(kind="VersionReq"))
            self.assertEqual(caught.exception.code, "failed")
            self.assertIn("boom", str(caught.exception))

        self.run_on_each(test)

    def test_concurrent_requests_get_their_own_answers(self) -> None:
        async def test(client: Peer, runtime: Runtime) -> None:
            release = asyncio.Event()

            async def slow(peer: Peer, msg: Msg[Any]) -> object:
                await peer.log("info", "waiting")
                await release.wait()
                return g.VersionRes(kind="VersionRes", runtime="slow", protocol=0)

            runtime.handle("VersionReq", slow)
            first = await client.request(g.VersionReq(kind="VersionReq"))
            second = await client.request(g.VersionReq(kind="VersionReq"))
            release.set()
            for request in (first, second):
                final = await request.wait(timeout=5)
                self.assertEqual(final.req_id, request.req_id)

        self.run_on_each(test)


class DumpWireTest(unittest.TestCase):
    def test_both_directions_are_logged_as_raw_frames(self) -> None:
        async def run() -> None:
            a, b = mem_pair()

            async def answer(_peer: Peer, _msg: Msg[Any]) -> object:
                return g.VersionRes(kind="VersionRes", runtime="x", protocol=1)

            client = Peer(dump_wire(a), "client")
            server = Peer(b, "server", on_request=answer)
            tasks = [asyncio.create_task(client.run()), asyncio.create_task(server.run())]
            with self.assertLogs("rt.wire", "DEBUG") as logs:
                await client.ask(g.VersionReq(kind="VersionReq"))
            await client.close()
            await asyncio.gather(*tasks)
            sent, received = logs.records
            self.assertTrue(sent.getMessage().startswith(">> mem:a ["))
            self.assertIn('{"kind":"VersionReq"}', sent.getMessage())
            self.assertTrue(received.getMessage().startswith("<< mem:a ["))
            self.assertIn('"kind":"VersionRes"', received.getMessage())

        asyncio.run(run())


class PeerTest(unittest.TestCase):
    def test_a_push_between_request_and_response_does_not_confuse_the_reply(self) -> None:
        async def run() -> None:
            a, b = mem_pair()
            events: list[Msg[Any]] = []

            async def on_event(_peer: Peer, msg: Msg[Any]) -> None:
                events.append(msg)

            async def answer(peer: Peer, msg: Msg[Any]) -> object:
                # An unrelated push first (no reqId), then the response.
                await peer.send(g.LogMsg(kind="LogMsg", level=g.LogLevel.info, message="push"))
                return g.VersionRes(kind="VersionRes", runtime="x", protocol=1)

            client = Peer(a, "client", on_event=on_event)
            server = Peer(b, "server", on_request=answer)
            tasks = [asyncio.create_task(client.run()), asyncio.create_task(server.run())]
            res = await client.ask(g.VersionReq(kind="VersionReq"))
            self.assertEqual(res.body.runtime, "x")
            self.assertEqual([msg.body.message for msg in events], ["push"])
            await client.close()
            await asyncio.gather(*tasks)

        asyncio.run(run())

    def test_a_request_without_answer_times_out(self) -> None:
        async def run() -> None:
            a, b = mem_pair()

            async def never(_peer: Peer, _msg: Msg[Any]) -> object:
                await asyncio.Event().wait()
                raise AssertionError

            client, server = Peer(a, "client"), Peer(b, "server", on_request=never)
            tasks = [asyncio.create_task(client.run()), asyncio.create_task(server.run())]
            with self.assertRaises(TimeoutError):
                await client.ask(g.VersionReq(kind="VersionReq"), timeout=0.05)
            await client.close()
            await asyncio.gather(*tasks)

        asyncio.run(run())

    def test_closing_fails_a_pending_request(self) -> None:
        async def run() -> None:
            a, b = mem_pair()
            client = Peer(a, "client")
            reader = asyncio.create_task(client.run())
            request = await client.request(g.VersionReq(kind="VersionReq"))
            await b.close()  # the other side goes away without answering
            await reader
            with self.assertRaises(Closed):
                await request.wait(timeout=1)

        asyncio.run(run())

    def test_an_invalid_request_is_answered_with_its_req_id(self) -> None:
        async def run() -> None:
            a, b = mem_pair()
            server = Peer(b, "server", on_request=_unreachable)
            task = asyncio.create_task(server.run())
            with self.assertLogs("rt.peer", "WARNING"):
                await a.send(
                    b'[{"kind":"MsgHeader","id":"m","time":1,"source":"x","reqId":"r7"},'
                    b'{"kind":"LaunchReq"}]'
                )
                raw = await a.recv()
            await a.close()
            await task
            self.assertIn(b'"reqId":"r7"', raw)
            self.assertIn(b'"code":"unknown-body"', raw)

        asyncio.run(run())

    def test_a_request_from_a_handler_carries_the_parent_req_id(self) -> None:
        async def run() -> None:
            a, b = mem_pair()
            seen: list[Msg[Any]] = []

            async def inner(_peer: Peer, msg: Msg[Any]) -> object:
                seen.append(msg)
                return g.VersionRes(kind="VersionRes", runtime="inner", protocol=1)

            async def outer(peer: Peer, _msg: Msg[Any]) -> object:
                return (await peer.ask(g.VersionReq(kind="VersionReq"))).body

            # Both sides answer: the client's handler is the nested one.
            client = Peer(a, "client", on_request=inner)
            server = Peer(b, "server", on_request=outer)
            tasks = [asyncio.create_task(client.run()), asyncio.create_task(server.run())]
            res = await client.ask(g.VersionReq(kind="VersionReq"))
            self.assertEqual(res.body.runtime, "inner")
            self.assertEqual(seen[0].header.parentReqId, res.req_id)
            await client.close()
            await asyncio.gather(*tasks)

        asyncio.run(run())


async def _unreachable(_peer: Peer, _msg: Msg[Any]) -> object:
    raise AssertionError("must not be called")


if __name__ == "__main__":
    unittest.main()
