"""primate — learns what each primitive does to RawIR, on this system.

    python3 -m primate list                    every primitive there is
    python3 -m primate learn [--sample NAME …] > rawir_module.py

`learn` applies the samples (samples.py) on this system — as root, on a
disposable test system only — and prints the generated library: one
function per primitive class, and `rawir(primitive)` (module.py).
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import socket
import sys
import typing
from typing import Any

from . import discover, learn, module, system
from .samples import SAMPLES, Sample, build, marker_kinds, markers

log = logging.getLogger("primate")


def _annotation(tp: Any) -> str:
    return tp.__name__ if isinstance(tp, type) else str(tp).replace("typing.", "")


def _observe(sample: Sample, run: int, host: str) -> dict[str, Any]:
    """One run: setup, collect, apply, collect, delete, collect, teardown."""
    values = markers(sample, run)
    setup, primitive = build(sample, host, values)
    log.info("%s run %d: %s", sample.name, run, values)
    restore = sample.keep() if sample.keep is not None else None
    try:
        system.create(setup)
        try:
            before = system.snapshot()
            system.create([primitive])
            try:
                after = system.snapshot()
            finally:
                system.delete([primitive])
            undone = system.snapshot()
        finally:
            system.delete(setup)
    finally:
        if restore is not None:
            restore()
    return learn.observe(before, after, undone, values)


def _learn(samples: list[Sample], host: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sample in samples:
        hints = typing.get_type_hints(sample.primitive)
        head = {
            "class": sample.primitive.__name__,
            "params": {
                f.name: _annotation(hints[f.name]) for f in dataclasses.fields(sample.primitive)
            },
            "fixed": dict(sample.fixed),
            "shapes": dict(sample.shape),
            "pairs": {field: list(keys) for field, keys in sample.pairs.items()},
            "kinds": marker_kinds(sample),
            "each": dict(sample.each),
        }
        try:
            first = _observe(sample, 0, host)
            second = _observe(sample, 1, host)
            out[sample.name] = {**head, **learn.template(first, second)}
            for node_id in out[sample.name]["leftover"]:
                log.warning("%s: delete left %s behind", sample.name, node_id)
        except (learn.Unexplained, system.CommandFailed) as exc:
            log.error("%s: %s", sample.name, exc)
            out[sample.name] = {**head, "error": str(exc)}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="primate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every primitive and its parameters")
    learn_cmd = commands.add_parser("learn", help="learn the samples here; print the library")
    learn_cmd.add_argument(
        "--sample",
        action="append",
        choices=[sample.name for sample in SAMPLES],
        help="only these samples (repeatable); default all",
    )
    learn_cmd.add_argument("-v", "--verbose", action="store_true", help="show every command")
    args = parser.parse_args(argv)
    if args.command == "list":
        for found in discover.primitives():
            params = ", ".join(
                f"{p.name}{'' if p.required else '?'}: {_annotation(p.type)}" for p in found.params
            )
            how = "commands" if found.commands else "python  "
            print(f"{found.module:18} {found.name:22} {how}  {params}")
        return 0
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    host = socket.gethostname()
    chosen = [s for s in SAMPLES if not args.sample or s.name in args.sample]
    result = _learn(chosen, host)
    sys.stdout.write(module.render(result, f"learned on {host}"))
    return 1 if any("error" in r or r.get("leftover") for r in result.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
