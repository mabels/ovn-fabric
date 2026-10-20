# deployer/cli.py — reads an IR JSON file (ovn-fabric's `generate-ir`
# output), hydrates it into protocol/generated.py's typed dataclasses
# (protocol/hydrate.py — a malformed or newer-shaped node fails HERE,
# loudly, before any text is built), and prints the all-in-one generated
# Python deployer (deployer/ir_to_python.py). Pure text output — nothing
# here executes anything; WHAT runs (create/delete, which host, a purge)
# is picked when the generated file itself is run.

from __future__ import annotations

import argparse
import json
import sys

from protocol.hydrate import hydrate_nodes

from .ir_to_python import generate_python_deployer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deployer",
        description="Convert a desired-state IR JSON file into the generated Python deployer.",
    )
    parser.add_argument(
        "ir_path",
        metavar="IR_JSON",
        help="path to an IR JSON file (e.g. `deno run src/cli.ts generate-ir config.ts`'s output), "
        "or - to read from stdin (e.g. piped straight from generate-ir)",
    )
    parser.add_argument(
        "--emit",
        choices=["python"],
        default="python",
        help="the output format — only `python` remains (the all-in-one generated deployer; "
        "pick what it does with --action/--cluster/--host/--ovn-purge when you run it). "
        "Kept so existing `--emit python` invocations keep working",
    )
    args = parser.parse_args(argv)

    if args.ir_path == "-":
        raw_nodes = json.load(sys.stdin)
    else:
        with open(args.ir_path) as f:
            raw_nodes = json.load(f)
    nodes = hydrate_nodes(raw_nodes)

    print(generate_python_deployer(nodes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
