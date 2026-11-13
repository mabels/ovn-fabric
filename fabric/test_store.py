from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from .store import ContentStore, StoreError, digest_of


class ContentStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_name_points_at_content_stored_once(self) -> None:
        store = ContentStore(self.dir, ".bin")
        first = store.put("deployed", b"one", {})
        self.assertEqual((first.status, first.digest), ("created", digest_of(b"one")))
        self.assertEqual(store.put("deployed", b"one", {}).status, "unchanged")
        moved = store.put("deployed", b"two", {})
        self.assertEqual((moved.status, moved.previous), ("replaced", digest_of(b"one")))
        self.assertEqual(store.resolve("deployed"), ("deployed", digest_of(b"two")))

    def test_keep_0_removes_what_no_name_points_to(self) -> None:
        store = ContentStore(self.dir, ".bin")
        store.put("x", b"one", {})
        store.put("x", b"two", {})
        self.assertFalse(store.blob(digest_of(b"one")).exists())

    def test_keep_n_keeps_the_newest_unnamed(self) -> None:
        store = ContentStore(self.dir, ".bin", keep=2)
        for i in range(4):
            store.put("deployed", f"v{i}".encode(), {"storedAt": time.time() + i})
        kept = sorted(p.name for p in self.dir.glob("*.bin"))
        # v3 is named; v2 and v1 are the newest unnamed; v0 is gone
        self.assertEqual(len(kept), 3)
        self.assertFalse(store.blob(digest_of(b"v0")).exists())
        self.assertTrue(store.blob(digest_of(b"v1")).exists())

    def test_point_switches_a_name_to_stored_content(self) -> None:
        store = ContentStore(self.dir, ".bin", keep=10)
        store.put("deployed", b"one", {})
        store.put("deployed", b"two", {})
        self.assertEqual(store.point("deployed", digest_of(b"one")), digest_of(b"two"))
        self.assertEqual(store.resolve("deployed")[1], digest_of(b"one"))
        with self.assertRaises(StoreError):
            store.point("deployed", digest_of(b"never stored"))

    def test_unnamed_content_is_listed_under_its_hash_until_named(self) -> None:
        store = ContentStore(self.dir, ".bin", keep=10)
        put = store.put(None, b"pex", {})
        self.assertEqual(store.refs(), {put.digest: put.digest})
        store.point("deployed", put.digest)
        self.assertEqual(store.refs(), {"deployed": put.digest})

    def test_a_named_hash_cannot_be_deleted_by_its_hash(self) -> None:
        store = ContentStore(self.dir, ".bin")
        put = store.put("deployed", b"pex", {})
        with self.assertRaises(StoreError) as caught:
            store.delete(put.digest)
        self.assertEqual(caught.exception.code, "still-named")


if __name__ == "__main__":
    unittest.main()
