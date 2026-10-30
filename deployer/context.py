from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path("/var/lib/ovn-fabric")


@dataclasses.dataclass(frozen=True)
class Options:
    """Everything the operator passed on the command line, parsed once
    by `main()` and never mutated afterwards."""

    dry_run: bool = False
    timeout: float = 180.0
    no_start: bool = False
    verbose: bool = False
    state_dir: Path = DEFAULT_ROOT


@dataclasses.dataclass
class Ctx:
    """The one object every generated function and every lifecycle
    controller receives: the options of this run plus the run-wide
    information store. The store holds values produced at runtime
    (captures such as a chassis id or a container PID) so that later
    primitives — in any controller of this run — can read them."""

    opts: Options = dataclasses.field(default_factory=Options)
    store: dict[str, Any] = dataclasses.field(default_factory=dict)
