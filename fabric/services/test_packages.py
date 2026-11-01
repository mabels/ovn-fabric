from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from .. import generated as g
from .abstract_deps import SYSTEM_DEPS
from .packages import MANAGERS, Dnf


class ManagersTest(unittest.TestCase):
    def test_the_managers_are_the_schemas(self) -> None:
        # Every name the IR or a request may carry has an implementation.
        self.assertEqual(set(MANAGERS), {m.value for m in g.PackageManager})


class DnfTest(unittest.TestCase):
    def test_its_commands(self) -> None:
        calls: list[list[str]] = []

        async def run(argv: list[str], **_: Any) -> tuple[bool, str]:
            calls.append(list(argv))
            if argv[0] == "rpm":
                return (True, "1.8.1-2.fc44") if argv[-1] == "jq" else (False, "not installed")
            return True, ""

        async def go() -> tuple[str | None, str | None]:
            dnf = Dnf()
            await dnf.refresh()
            await dnf.install(["jq", "curl"])
            await dnf.upgrade(["jq"])
            return await dnf.version("jq"), await dnf.version("nope")

        with mock.patch("runtime.command.run", run):
            installed, missing = asyncio.run(go())
        self.assertEqual((installed, missing), ("1.8.1-2.fc44", None))
        self.assertEqual(
            calls[:3],
            [
                ["dnf", "-y", "--color=never", "makecache"],
                ["dnf", "-y", "--color=never", "install", "jq", "curl"],
                ["dnf", "-y", "--color=never", "upgrade", "jq"],
            ],
        )
        self.assertTrue(Dnf().permanent("No match for argument: no-such-package"))
        self.assertFalse(Dnf().permanent("Curl error (6): Couldn't resolve host name"))


class TableTest(unittest.TestCase):
    def test_every_abstract_dependency_has_a_column_for_every_manager(self) -> None:
        managers = set(MANAGERS)
        for name, by_manager in SYSTEM_DEPS.items():
            with self.subTest(dep=name):
                self.assertEqual(set(by_manager), managers)


if __name__ == "__main__":
    unittest.main()
