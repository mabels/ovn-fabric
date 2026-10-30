from __future__ import annotations

import logging
import unittest

from .logconfig import parse


class ParseTest(unittest.TestCase):
    def test_default_and_components(self) -> None:
        self.assertEqual(parse([]), (logging.INFO, {}))
        self.assertEqual(parse(["debug"]), (logging.DEBUG, {}))
        self.assertEqual(parse(["wire=debug"]), (logging.INFO, {"wire": logging.DEBUG}))
        self.assertEqual(
            parse(["warn", "peer=debug, wire=DEBUG"]),
            (logging.WARNING, {"peer": logging.DEBUG, "wire": logging.DEBUG}),
        )

    def test_the_last_value_wins(self) -> None:
        self.assertEqual(parse(["wire=info", "wire=debug"])[1], {"wire": logging.DEBUG})

    def test_invalid_values(self) -> None:
        for value in ("loud", "wire=loud", "=debug"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse([value])


if __name__ == "__main__":
    unittest.main()
