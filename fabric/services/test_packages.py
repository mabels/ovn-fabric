from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from .. import generated as g
from .abstract_deps import SYSTEM_DEPS
from .packages import MANAGERS, Apt, Dnf


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


class DnfRepoTest(unittest.TestCase):
    def test_a_repo_file_is_written(self) -> None:
        repo = g.DepRepo(
            kind="dep.repo",
            id="nfv",
            baseurl="https://example.invalid/nfv",
            gpgkey="https://example.invalid/nfv/key",
        )
        with tempfile.TemporaryDirectory() as root:
            ok, out = asyncio.run(Dnf(repos_root=root).ensure_repos([repo]))
            self.assertEqual((ok, out), (True, ""))
            text = (Path(root) / "nfv.repo").read_text()
        self.assertEqual(
            text,
            "[nfv]\n"
            "name=nfv\n"
            "baseurl=https://example.invalid/nfv\n"
            "enabled=1\n"
            "module_hotfixes=1\n"
            "gpgcheck=1\n"
            "gpgkey=https://example.invalid/nfv/key\n",
        )

    def test_no_key_means_no_gpg_check(self) -> None:
        repo = g.DepRepo(kind="dep.repo", id="nfv", baseurl="https://example.invalid/nfv")
        with tempfile.TemporaryDirectory() as root:
            asyncio.run(Dnf(repos_root=root).ensure_repos([repo]))
            text = (Path(root) / "nfv.repo").read_text()
        self.assertIn("gpgcheck=0\n", text)
        self.assertNotIn("gpgkey=", text)

    def test_a_bad_id_is_refused(self) -> None:
        repo = g.DepRepo(kind="dep.repo", id="../evil", baseurl="https://example.invalid")
        with tempfile.TemporaryDirectory() as root:
            ok, out = asyncio.run(Dnf(repos_root=root).ensure_repos([repo]))
        self.assertFalse(ok)
        self.assertIn("bad repository id", out)

    def test_apt_has_no_repo_implementation(self) -> None:
        repo = g.DepRepo(kind="dep.repo", id="nfv", baseurl="https://example.invalid/nfv")
        ok, out = asyncio.run(Apt().ensure_repos([repo]))
        self.assertFalse(ok)
        self.assertIn("only implemented for dnf", out)


class TableTest(unittest.TestCase):
    def test_every_abstract_dependency_has_a_column_for_every_manager(self) -> None:
        managers = set(MANAGERS)
        for name, by_manager in SYSTEM_DEPS.items():
            with self.subTest(dep=name):
                self.assertEqual(set(by_manager), managers)


if __name__ == "__main__":
    unittest.main()
