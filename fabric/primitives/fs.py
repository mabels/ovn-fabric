"""File-system objects — done in Python, not by running mkdir/rm/chmod.

Each is a lifecycle operation of its own, like WriteFileOperation; CREATE
and DELETE also record or drop its node in the raw graph.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..lifecycle import LifecycleAction, RawNodeTemplate

if TYPE_CHECKING:
    from ..lifecycle import LifecycleController


@dataclasses.dataclass(frozen=True)
class DirectoryPrimitive:
    """A directory. CREATE ensures it with its parents (one already there
    is fine).

    Shared (`owned=False`, e.g. /etc/wireguard): DELETE removes it and its
    parents, but only while they are empty — whatever else lives there is
    untouched.

    Owned (`owned=True`, an app's state/volume directory): DELETE removes it
    with its whole content."""

    # Its kind as a unit step (fabric/units.py; the schema's `kind`).
    step_kind: ClassVar[str] = "Directory"

    host: str
    path: str
    owned: bool = False

    @property
    def key(self) -> str:
        return f"fs.dir:{self.path}"

    async def create(self, lc: LifecycleController) -> None:
        await asyncio.to_thread(os.makedirs, self.path, exist_ok=True)
        lc.graph.upsert(
            self.key, (RawNodeTemplate(id=self.key, kind="fs.dir", fields={"path": self.path}),)
        )

    async def delete(self, lc: LifecycleController) -> None:
        if self.owned:
            await asyncio.to_thread(shutil.rmtree, self.path, True)
        else:
            await asyncio.to_thread(_remove_empty_upwards, self.path)
        lc.graph.remove(self.key)

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return [f"mkdir -p {self.path}"]
        if self.owned:
            return [f"rm -rf {self.path}"]
        return [f"rmdir -p {self.path}"]


@dataclasses.dataclass(frozen=True)
class ChmodPrimitive:
    """The mode of a file (octal, e.g. "600"); DELETE leaves it."""

    # Its kind as a unit step (fabric/units.py; the schema's `kind`).
    step_kind: ClassVar[str] = "Chmod"

    host: str
    path: str
    mode: str

    @property
    def key(self) -> str:
        return f"fs.chmod:{self.path}"

    async def create(self, lc: LifecycleController) -> None:
        await asyncio.to_thread(os.chmod, self.path, int(self.mode, 8))
        lc.graph.upsert(
            self.key,
            (
                RawNodeTemplate(
                    id=self.key, kind="fs.chmod", fields={"path": self.path, "mode": self.mode}
                ),
            ),
        )

    async def delete(self, lc: LifecycleController) -> None:
        lc.graph.remove(self.key)

    def describe(self, action: LifecycleAction) -> list[str]:
        return [f"chmod {self.mode} {self.path}"] if action is LifecycleAction.CREATE else []


@dataclasses.dataclass(frozen=True)
class RemovePathPrimitive:
    """A path that only goes away: CREATE does nothing, DELETE removes it
    (a whole tree with `recursive`). Gone already is fine."""

    # Its kind as a unit step (fabric/units.py; the schema's `kind`).
    step_kind: ClassVar[str] = "RemovePath"

    host: str
    path: str
    recursive: bool = False

    @property
    def key(self) -> str:
        return f"fs.path:{self.path}"

    async def create(self, lc: LifecycleController) -> None:
        return None

    async def delete(self, lc: LifecycleController) -> None:
        await asyncio.to_thread(_remove, Path(self.path), self.recursive)
        lc.graph.remove(self.key)

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return []
        return [f"rm {'-rf' if self.recursive else '-f'} {self.path}"]


def _remove_empty_upwards(path: str) -> None:
    """`rmdir -p --ignore-fail-on-non-empty`: the directory, then its
    parents, as long as each is empty."""
    try:
        os.removedirs(path)
    except OSError:
        pass


def _remove(path: Path, recursive: bool) -> None:
    if recursive and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
