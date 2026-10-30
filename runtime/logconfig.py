"""Log levels per component.

Every component logs to `runtime.<component>` (the core: `runtime.wire`,
`runtime.peer`) or `fabric.<component>` (the services: `fabric.sysdeps`,
`fabric.docker`, …); a component's level is set on both names. `--log` takes a default
level and/or `component=level` pairs, repeatable and comma-separated:

    --log debug                  everything at debug
    --log wire=debug             only the wire dump
    --log info --log wire=debug  default info, the wire at debug
    --log peer=debug,wire=debug
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def parse(values: Sequence[str]) -> tuple[int, dict[str, int]]:
    """(the default level, {component: level}) from `--log` values."""
    default = logging.INFO
    components: dict[str, int] = {}
    for value in values:
        for item in filter(None, (part.strip() for part in value.split(","))):
            component, sep, name = item.rpartition("=")
            level = LEVELS.get(name.lower())
            if level is None:
                raise ValueError(f"unknown log level {name!r} (one of {', '.join(LEVELS)})")
            if not sep:
                default = level
            elif not component:
                raise ValueError(f"missing component in {item!r}")
            else:
                components[component] = level
    return default, components


# The levels given per component (--log <component>=<level>): also for
# components that are not a logger here — e.g. a unit of work whose lines a
# client receives (fabric's CLI applies them to those lines).
COMPONENTS: dict[str, int] = {}


def configure(values: Sequence[str]) -> None:
    default, components = parse(values)
    COMPONENTS.clear()
    COMPONENTS.update(components)
    logging.basicConfig(level=default, format="%(levelname)s %(name)s: %(message)s")
    for component, level in components.items():
        for package in ("runtime", "fabric"):
            logging.getLogger(f"{package}.{component}").setLevel(level)
