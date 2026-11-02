"""Every primitive there is: the dataclasses in `fabric.primitives`
that name a step kind (`step_kind`) — a typed command primitive
(`CommandPrimitive`, kind "Primitive") or one done in Python (`Directory`,
`Chmod`, `RemovePath`). Its dataclass fields are its parameters.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import pkgutil
import typing
from typing import Any

import fabric.primitives
from fabric.lifecycle import CommandPrimitive

# Classes that carry a step kind but are no object of their own: the host
# builder, and the base of the named iptables rules.
_BUILDERS = ("HostPrimitive", "FirewallRule")


@dataclasses.dataclass(frozen=True)
class Param:
    name: str
    type: Any
    required: bool


@dataclasses.dataclass(frozen=True)
class Found:
    module: str
    name: str
    cls: type
    params: tuple[Param, ...]

    @property
    def commands(self) -> bool:
        """Runs commands (create/delete argv) — else it is done in Python."""
        return issubclass(self.cls, CommandPrimitive)


def primitives() -> list[Found]:
    found: list[Found] = []
    for info in pkgutil.iter_modules(fabric.primitives.__path__):
        if info.name.startswith("test_"):
            continue
        module = importlib.import_module(f"fabric.primitives.{info.name}")
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or name in _BUILDERS:
                continue
            if not dataclasses.is_dataclass(cls) or not hasattr(cls, "step_kind"):
                continue
            hints = typing.get_type_hints(cls)
            params = tuple(
                Param(
                    field.name,
                    hints[field.name],
                    field.default is dataclasses.MISSING
                    and field.default_factory is dataclasses.MISSING,
                )
                for field in dataclasses.fields(cls)
            )
            found.append(Found(info.name, name, cls, params))
    return sorted(found, key=lambda f: (f.module, f.name))
