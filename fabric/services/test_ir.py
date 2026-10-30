from __future__ import annotations

import asyncio
import contextlib
import copy
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from runtime.message import hydrate_part
from runtime.peer import Peer, RemoteError
from runtime.runtime import Runtime
from runtime.test_message import IR_SAMPLE
from runtime.transport import connect

from .. import generated as g
from .ir import IrStore, ir_hash, normal_form

CREATED, UNCHANGED, REPLACED = (
    g.IrPutStatus.created,
    g.IrPutStatus.unchanged,
    g.IrPutStatus.replaced,
)


def _ir(nodes: list[dict[str, Any]]) -> g.IrCollection:
    ir = hydrate_part(g.IrCollection, {"kind": "IrCollection", "collection": nodes})
    assert isinstance(ir, g.IrCollection)
    return ir


@contextlib.asynccontextmanager
async def _client(root: str) -> AsyncIterator[Peer]:
    runtime = Runtime()
    runtime.register(IrStore(root).service())
    stop = asyncio.Event()
    address = f"mem://ir-{id(runtime)}"
    server = asyncio.create_task(runtime.serve(address, stop))
    await asyncio.sleep(0)
    client = Peer(await connect(address), "test")
    reader = asyncio.create_task(client.run())
    try:
        yield client
    finally:
        await client.close()
        await reader
        stop.set()
        await server


def _put(name: str | None, nodes: list[dict[str, Any]]) -> g.IrPutReq:
    return g.IrPutReq(kind="IrPutReq", name=name, ir=_ir(nodes))


class NormalFormTest(unittest.TestCase):
    def test_order_format_and_absent_vs_null_do_not_change_the_hash(self) -> None:
        host, dns = copy.deepcopy(IR_SAMPLE)
        reordered = [dns, host]
        with_nulls = copy.deepcopy([host, dns])
        with_nulls[1]["data"]["deps"] = None  # an optional field as null
        a = ir_hash(normal_form(_ir([host, dns])))
        self.assertEqual(ir_hash(normal_form(_ir(reordered))), a)
        self.assertEqual(ir_hash(normal_form(_ir(with_nulls))), a)

    def test_an_inner_lists_order_does_change_it(self) -> None:
        host, dns = copy.deepcopy(IR_SAMPLE)
        swapped = copy.deepcopy(dns)
        swapped["data"]["cmd"] = list(reversed(dns["data"]["cmd"]))  # arguments: ordered
        self.assertNotEqual(
            ir_hash(normal_form(_ir([host, dns]))), ir_hash(normal_form(_ir([host, swapped])))
        )


class StoreTest(unittest.TestCase):
    def test_put_list_get_delete(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            async with _client(root) as client:
                put = (await client.ask(_put("draft", IR_SAMPLE), timeout=5)).body
                self.assertEqual((put.status, put.nodes), (CREATED, 2))
                listed = (await client.ask(g.IrListReq(kind="IrListReq"), timeout=5)).body
                (info,) = listed.irs
                self.assertEqual(
                    (info.name, info.hash, info.hosts), ("draft", put.hash, ["net-test-hh-b"])
                )
                for ref in ("draft", put.hash):
                    got = (await client.ask(g.IrGetReq(kind="IrGetReq", ref=ref), timeout=5)).body
                    self.assertEqual(normal_form(got.ir), normal_form(_ir(IR_SAMPLE)))
                    self.assertEqual(got.info.name, "draft")
                deleted = (
                    await client.ask(g.IrDeleteReq(kind="IrDeleteReq", ref="draft"), timeout=5)
                ).body
                self.assertEqual(deleted.hash, put.hash)
                listed = (await client.ask(g.IrListReq(kind="IrListReq"), timeout=5)).body
                self.assertEqual(listed.irs, [])
            # its content went with its last name
            self.assertEqual(sorted(p.name for p in (Path(root) / "ir").iterdir()), ["refs.json"])

        asyncio.run(run())

    def test_the_same_content_is_unchanged_and_new_content_replaces(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            host, dns = copy.deepcopy(IR_SAMPLE)
            async with _client(root) as client:
                first = (await client.ask(_put("draft", [host, dns]), timeout=5)).body
                again = (await client.ask(_put("draft", [dns, host]), timeout=5)).body
                self.assertEqual((again.status, again.hash), (UNCHANGED, first.hash))
                dns["data"]["image"] = "other"
                changed = (await client.ask(_put("draft", [host, dns]), timeout=5)).body
                self.assertEqual((changed.status, changed.previous), (REPLACED, first.hash))
            blobs = [p.name for p in (Path(root) / "ir").glob("*.json") if ".meta" not in p.name]
            self.assertEqual(sorted(blobs), sorted(["refs.json", f"{changed.hash[7:]}.json"]))

        asyncio.run(run())

    def test_content_shared_by_two_names_stays_until_the_last_goes(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            async with _client(root) as client:
                a = (await client.ask(_put("a", IR_SAMPLE), timeout=5)).body
                await client.ask(_put("b", IR_SAMPLE), timeout=5)
                await client.ask(g.IrDeleteReq(kind="IrDeleteReq", ref="a"), timeout=5)
                got = (await client.ask(g.IrGetReq(kind="IrGetReq", ref=a.hash), timeout=5)).body
                self.assertEqual(got.info.name, "b")

        asyncio.run(run())

    def test_without_a_name_the_hash_is_its_identity(self) -> None:
        async def run() -> None:
            root = tempfile.mkdtemp()
            async with _client(root) as client:
                put = (await client.ask(_put(None, IR_SAMPLE), timeout=5)).body
                self.assertEqual((put.name, put.status), (put.hash, CREATED))
                again = (await client.ask(_put(None, list(reversed(IR_SAMPLE))), timeout=5)).body
                self.assertEqual((again.name, again.status), (put.hash, UNCHANGED))
                listed = (await client.ask(g.IrListReq(kind="IrListReq"), timeout=5)).body
                self.assertEqual([i.name for i in listed.irs], [put.hash])
                got = (await client.ask(g.IrGetReq(kind="IrGetReq", ref=put.hash), timeout=5)).body
                self.assertEqual(got.info.name, put.hash)
                deleted = (
                    await client.ask(g.IrDeleteReq(kind="IrDeleteReq", ref=put.hash), timeout=5)
                ).body
                self.assertEqual(deleted.hash, put.hash)
                listed = (await client.ask(g.IrListReq(kind="IrListReq"), timeout=5)).body
                self.assertEqual(listed.irs, [])

        asyncio.run(run())

    def test_unnamed_content_that_has_a_name_is_not_stored_twice(self) -> None:
        async def run() -> None:
            async with _client(tempfile.mkdtemp()) as client:
                named = (await client.ask(_put("draft", IR_SAMPLE), timeout=5)).body
                unnamed = (await client.ask(_put(None, IR_SAMPLE), timeout=5)).body
                self.assertEqual((unnamed.name, unnamed.status), ("draft", UNCHANGED))
                listed = (await client.ask(g.IrListReq(kind="IrListReq"), timeout=5)).body
                self.assertEqual([i.name for i in listed.irs], ["draft"])
                # Deleting by hash is refused while a name points to it.
                with self.assertRaises(RemoteError) as caught:
                    await client.ask(g.IrDeleteReq(kind="IrDeleteReq", ref=named.hash), timeout=5)
                self.assertEqual(caught.exception.code, "still-named")
                self.assertIn("draft", str(caught.exception))

        asyncio.run(run())

    def test_errors(self) -> None:
        async def run() -> None:
            async with _client(tempfile.mkdtemp()) as client:
                for req, code in (
                    (g.IrGetReq(kind="IrGetReq", ref="nope"), "not-found"),
                    (g.IrDeleteReq(kind="IrDeleteReq", ref="nope"), "not-found"),
                    (_put("../escape", IR_SAMPLE), "bad-request"),
                ):
                    with self.subTest(req=req.kind), self.assertRaises(RemoteError) as caught:
                        await client.ask(req, timeout=5)
                    self.assertEqual(caught.exception.code, code)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
