from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import boot


class AllocatorStateTest(unittest.TestCase):
    def test_it_keeps_what_a_run_placed_and_the_boot_id(self) -> None:
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(boot, "boot_id", return_value="b-1"),
        ):
            state = boot.AllocatorState(root)
            self.assertEqual((state.load(), state.remembered()), ({}, {}))
            domains = {"ipnet:ls:core": {"a": {"ipv4": 7}}}
            state.save(domains, "sha256:x")
            self.assertEqual(state.remembered(), domains)
            self.assertEqual(state.load()["bootId"], "b-1")
            # Only the boot id: what was placed stays.
            with mock.patch.object(boot, "boot_id", return_value="b-2"):
                state.save(None)
            self.assertEqual((state.load()["bootId"], state.remembered()), ("b-2", domains))

    def test_an_unreadable_file_is_no_memory(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / boot.STATE_FILE).write_text("{not json")
            self.assertEqual(boot.AllocatorState(root).remembered(), {})


if __name__ == "__main__":
    unittest.main()
