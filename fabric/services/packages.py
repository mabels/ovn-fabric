"""The package managers the runtime installs dependencies with — one
concrete implementation per kind:

    Apt   Debian, Ubuntu   dpkg-query / apt-get
    Dnf   Fedora           rpm / dnf

Which one a host uses is the resolved target's choice (the distro ->
apt/dnf translation in fabric/hostos.py), never detected: every
dependency request names it, and MANAGERS maps the name to the
implementation.

The resolver (sysdeps.py) only calls a manager's methods; what differs
between them — the commands, and which failures no retry can fix — is
here. Every method reports its commands to `report` (the waiting
requests).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar, Protocol

from runtime import command
from runtime.command import Report

log = logging.getLogger("fabric.sysdeps")


class Repo(Protocol):
    id: str
    baseurl: str
    gpgkey: str | None


class PackageManager(Protocol):
    # Its kind: "apt", "dnf" — the column of abstract_deps' table.
    name: ClassVar[str]

    async def ensure_repos(
        self, repos: Sequence[Repo], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        """Enable these repositories; they must be present before any install."""

    async def version(self, package: str, *, report: Report = command.nowhere) -> str | None:
        """The installed version, or None when it is not installed."""

    async def refresh(self, *, report: Report = command.nowhere) -> tuple[bool, str]:
        """Bring the package index up to date."""

    async def install(
        self, packages: Sequence[str], *, report: Report = command.nowhere
    ) -> tuple[bool, str]: ...

    async def upgrade(
        self, packages: Sequence[str], *, report: Report = command.nowhere
    ) -> tuple[bool, str]: ...

    def permanent(self, output: str) -> bool:
        """The failure is one no retry can fix (an unknown package)."""


class Apt:
    """Debian's apt/dpkg."""

    name: ClassVar[str] = "apt"

    _ENV = {
        "DEBIAN_FRONTEND": "noninteractive",
        "APT_LISTCHANGES_FRONTEND": "none",
        "LC_ALL": "C",
        "TERM": "dumb",
    }
    _OPTIONS = (
        "-o",
        "Dpkg::Use-Pty=0",
        "-o",
        "Dpkg::Progress-Fancy=0",
        # Wait for another apt/dpkg on the host instead of failing.
        "-o",
        "DPkg::Lock::Timeout=600",
    )
    _PERMANENT = (
        "Unable to locate package",
        "has no installation candidate",
        "is not available, but is referred to",
    )

    async def ensure_repos(
        self, repos: Sequence[Repo], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        if not repos:
            return True, ""
        return False, "dep.repo is only implemented for dnf"

    async def version(self, package: str, *, report: Report = command.nowhere) -> str | None:
        ok, out = await self._run(["dpkg-query", "-W", "-f=${Status} ${Version}", package], report)
        if not ok or not out.startswith("install ok installed"):
            return None
        return out.rsplit(" ", 1)[-1].strip()

    async def refresh(self, *, report: Report = command.nowhere) -> tuple[bool, str]:
        return await self._run(["apt-get", *self._OPTIONS, "update"], report)

    async def install(
        self, packages: Sequence[str], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        return await self._run(["apt-get", *self._OPTIONS, "install", "-y", *packages], report)

    async def upgrade(
        self, packages: Sequence[str], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        return await self._run(
            ["apt-get", *self._OPTIONS, "install", "--only-upgrade", "-y", *packages], report
        )

    def permanent(self, output: str) -> bool:
        return any(marker in output for marker in self._PERMANENT)

    async def _run(self, argv: list[str], report: Report) -> tuple[bool, str]:
        return await command.run(argv, log=log, env=self._ENV, report=report)


class Dnf:
    """Fedora's dnf/rpm."""

    name: ClassVar[str] = "dnf"

    _ENV = {"LC_ALL": "C", "TERM": "dumb"}
    _OPTIONS = ("-y", "--color=never")
    _PERMANENT = ("No match for argument",)
    _REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    def __init__(self, repos_root: str = "/etc/yum.repos.d") -> None:
        self._repos_root = Path(repos_root)

    async def ensure_repos(
        self, repos: Sequence[Repo], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        for repo in repos:
            if not self._REPO_ID.match(repo.id):
                return False, f"bad repository id {repo.id!r}"
            if not repo.baseurl.strip():
                return False, f"repository {repo.id!r} has no baseurl"
        try:
            changed = await asyncio.to_thread(self._write_repos, repos)
        except OSError as exc:
            return False, f"cannot write repositories: {exc}"
        for repo in repos:
            state = "written" if repo.id in changed else "unchanged"
            report("info", f"repository {repo.id}: {state}")
        return True, ""

    def _write_repos(self, repos: Sequence[Repo]) -> set[str]:
        self._repos_root.mkdir(parents=True, exist_ok=True)
        changed: set[str] = set()
        for repo in repos:
            path = self._repos_root / f"{repo.id}.repo"
            text = self._repo_text(repo)
            if path.exists() and path.read_text(encoding="utf-8") == text:
                continue
            path.write_text(text, encoding="utf-8")
            changed.add(repo.id)
        return changed

    @staticmethod
    def _repo_text(repo: Repo) -> str:
        lines = [
            f"[{repo.id}]",
            f"name={repo.id}",
            f"baseurl={repo.baseurl}",
            "enabled=1",
            "module_hotfixes=1",
        ]
        if repo.gpgkey:
            lines += ["gpgcheck=1", f"gpgkey={repo.gpgkey}"]
        else:
            lines += ["gpgcheck=0"]
        return "\n".join(lines) + "\n"

    async def version(self, package: str, *, report: Report = command.nowhere) -> str | None:
        # `rpm -q` fails (exit 1) for a package that is not installed.
        ok, out = await self._run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", package], report)
        return out.strip() if ok and out.strip() else None

    async def refresh(self, *, report: Report = command.nowhere) -> tuple[bool, str]:
        return await self._run(["dnf", *self._OPTIONS, "makecache"], report)

    async def install(
        self, packages: Sequence[str], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        return await self._run(["dnf", *self._OPTIONS, "install", *packages], report)

    async def upgrade(
        self, packages: Sequence[str], *, report: Report = command.nowhere
    ) -> tuple[bool, str]:
        return await self._run(["dnf", *self._OPTIONS, "upgrade", *packages], report)

    def permanent(self, output: str) -> bool:
        return any(marker in output for marker in self._PERMANENT)

    async def _run(self, argv: list[str], report: Report) -> tuple[bool, str]:
        return await command.run(argv, log=log, env=self._ENV, report=report)


# The package manager's name (the IR's / a request's `packageManager`) -> its
# implementation.
MANAGERS: dict[str, PackageManager] = {Apt.name: Apt(), Dnf.name: Dnf()}
