from __future__ import annotations

import unittest

from . import journal


class JournalTest(unittest.TestCase):
    def test_a_line_names_its_request_and_its_parent(self) -> None:
        with self.assertLogs("fabric.journal", "DEBUG") as logs:
            journal.record("r2", "r1", "unit_x", "debug", "$ ip link show")
            journal.record("r1", None, "apply", "info", "two\nlines")
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            [
                "req=r2 parent=r1 debug unit_x: $ ip link show",
                "req=r1 parent=- info apply: two | lines",
            ],
        )

    def test_a_container_s_environment_values_are_masked(self) -> None:
        self.assertEqual(
            journal.masked("$ docker run -d -e TOKEN=abc -e 'B=a b' --name c -v /a:/b img"),
            "$ docker run -d -e TOKEN=*** -e B=*** --name c -v /a:/b img",
        )
        self.assertEqual(journal.masked("-e not at the start"), "-e not at the start")


if __name__ == "__main__":
    unittest.main()
