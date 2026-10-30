from __future__ import annotations

import dataclasses
import posixpath

from .lifecycle import (
    Command,
    CommandPrimitive,
    Primitive,
    RawNodeTemplate,
)


@dataclasses.dataclass(frozen=True)
class DirectoryPrimitive(CommandPrimitive):
    """A directory.

    Shared (`owned=False`, e.g. /etc/wireguard): CREATE ensures it with
    `mkdir -p`; DELETE removes it and the parents `mkdir -p` made, but
    only while they are empty — whatever else lives there is untouched.

    Owned (`owned=True`, an app's state/volume directory): CREATE fails
    when it already exists; DELETE removes it with its whole content."""

    host: str
    path: str
    owned: bool = False

    @property
    def key(self) -> str:
        return f"fs.dir:{self.path}"

    def as_primitive(self) -> Primitive:
        create: tuple[Command, ...]
        delete: tuple[Command, ...]
        if self.owned:
            parent = posixpath.dirname(self.path.rstrip("/")) or "/"
            create = (
                Command(f"mkdir -p {parent}", ("mkdir", "-p", parent)),
                Command(f"mkdir {self.path}", ("mkdir", self.path)),
            )
            delete = (Command(f"rm -rf {self.path}", ("rm", "-rf", self.path)),)
        else:
            create = (Command(f"mkdir -p {self.path}", ("mkdir", "-p", self.path)),)
            delete = (
                Command(
                    f"rmdir -p {self.path}",
                    ("rmdir", "-p", "--ignore-fail-on-non-empty", self.path),
                ),
            )
        return Primitive(
            key=self.key,
            nodes=(RawNodeTemplate(id=self.key, kind="fs.dir", fields={"path": self.path}),),
            create_cmds=create,
            delete_cmds=delete,
        )


@dataclasses.dataclass(frozen=True)
class ChmodPrimitive(CommandPrimitive):
    host: str
    path: str
    mode: str

    @property
    def key(self) -> str:
        return f"fs.chmod:{self.path}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="fs.chmod",
                    fields={"path": self.path, "mode": self.mode},
                ),
            ),
            create_cmds=(
                Command(f"chmod {self.mode} {self.path}", ("chmod", self.mode, self.path)),
            ),
            delete_cmds=(),
        )


@dataclasses.dataclass(frozen=True)
class RemovePathPrimitive(CommandPrimitive):
    host: str
    path: str
    recursive: bool = False

    @property
    def key(self) -> str:
        return f"fs.path:{self.path}"

    def as_primitive(self) -> Primitive:
        flag = "-rf" if self.recursive else "-f"
        return Primitive(
            key=self.key,
            nodes=(RawNodeTemplate(id=self.key, kind="fs.path", fields={"path": self.path}),),
            create_cmds=(),
            delete_cmds=(Command(f"rm {flag} {self.path}", ("rm", flag, self.path)),),
        )
