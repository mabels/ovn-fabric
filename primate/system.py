"""The real system: apply and delete primitives, collect RawIR.

A primitive is applied by running its own commands — `as_primitive()`'s
create argv, in order; deleted by its delete argv — and nothing else: no
controller, no raw-graph bookkeeping. RawIR comes from
fabric/observed.py — the runtime's own view of the live system.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Sequence
from typing import Any

from fabric import observed
from raw_reconciler.owners import mark_host, ours

from .learn import Snapshot

log = logging.getLogger("primate")


class CommandFailed(RuntimeError):
    pass


def _run(argv: Sequence[str], optional: bool) -> None:
    log.debug("$ %s", " ".join(argv))
    done = subprocess.run(list(argv), capture_output=True, text=True)
    if done.returncode != 0 and not optional:
        raise CommandFailed(f"{' '.join(argv)}: {(done.stderr or done.stdout).strip()}")


def run(argv: Sequence[str]) -> None:
    """One command (restoring what a sample replaced)."""
    _run(argv, False)


def create(primitives: Sequence[Any]) -> None:
    """Each primitive's create commands, the primitives in order."""
    for primitive in primitives:
        for command in primitive.as_primitive().create_cmds:
            _run(command.argv, command.optional)


def delete(primitives: Sequence[Any]) -> None:
    """Each primitive's delete commands, the primitives in reverse order;
    a failing delete only warns (the rest is still removed)."""
    for primitive in reversed(primitives):
        for command in primitive.as_primitive().delete_cmds:
            try:
                _run(command.argv, command.optional)
            except CommandFailed as exc:
                log.warning("%s", exc)


def snapshot() -> Snapshot:
    """The live RawIR, by node id, without the collection metadata — of it
    what is ovn-fabric's: the view the runtime compares against. Not ours:
    what docker, OVS, OVN, the kernel own, and the loopback (the host's)."""
    nodes = observed.snapshot()
    mark_host(nodes, ["lo"])
    return ours(nodes)
