from __future__ import annotations

import dataclasses

from .lifecycle import (
    Command,
    LifecycleController,
    Primitive,
    RawNodeTemplate,
)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlLs:
    """`ovn-nbctl ls-add` + the raw ovn.ls node."""

    name: str

    @property
    def key(self) -> str:
        return f"ovn.ls:{self.name}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.ls",
                    fields={
                        "name": self.name,
                        "portRefs": [],
                        "externalIds": {},
                    },
                ),
            ),
            create=(
                Command(
                    f"ls:{self.name} (create)",
                    ("ovn-nbctl", "ls-add", self.name),
                ),
            ),
            delete=(
                Command(
                    f"ls:{self.name} (delete)",
                    ("ovn-nbctl", "ls-del", self.name),
                ),
            ),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)


@dataclasses.dataclass(frozen=True)
class OVNNbCtlLsp:
    """`ovn-nbctl lsp-add` plus its type/addresses/options setters."""

    switch: str
    name: str
    type: str | None = None
    addresses: tuple[str, ...] = ()
    options: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"ovn.lsp:{self.switch}|{self.name}"

    def as_primitive(self) -> Primitive:
        create: list[Command] = [
            Command(
                f"lsp:{self.switch}/{self.name} (create)",
                ("ovn-nbctl", "lsp-add", self.switch, self.name),
            )
        ]
        if self.type:
            create.append(
                Command(
                    f"lsp:{self.name} (type)",
                    ("ovn-nbctl", "lsp-set-type", self.name, self.type),
                )
            )
        if self.addresses:
            create.append(
                Command(
                    f"lsp:{self.name} (addresses)",
                    (
                        "ovn-nbctl",
                        "lsp-set-addresses",
                        self.name,
                        ",".join(self.addresses),
                    ),
                )
            )
        for key, value in sorted(self.options):
            create.append(
                Command(
                    f"lsp:{self.name} (option {key})",
                    ("ovn-nbctl", "lsp-set-options", self.name, f"{key}={value}"),
                )
            )
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="ovn.lsp",
                    fields={
                        "switchRef": f"ovn.ls:{self.switch}",
                        "name": self.name,
                        "type": self.type or "",
                        "addresses": list(self.addresses),
                        "options": dict(self.options),
                        "externalIds": {},
                    },
                ),
            ),
            create=tuple(create),
            delete=(
                Command(
                    f"lsp:{self.name} (delete)",
                    ("ovn-nbctl", "lsp-del", self.name),
                ),
            ),
        )

    async def run(self, lc: LifecycleController) -> None:
        await self.as_primitive().run(lc)
