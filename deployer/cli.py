# deployer/cli.py — reads an IR JSON file (ovn-fabric's `generate-ir`
# output, or the reconciler's own live-state output), hydrates it into
# protocol/generated.py's typed dataclasses (protocol/hydrate.py — a
# malformed or newer-shaped node fails HERE, loudly, before any script
# text is built), and prints shell script text via ir_to_shell.
# build_scripts(). Pure text output — see ir_to_shell.py's header
# comment for why this never executes anything.

from __future__ import annotations

import argparse
import json
import sys

from protocol.hydrate import hydrate_nodes

from .ir_to_shell import build_scripts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deployer",
        description="Convert a desired-state IR JSON file into shell scripts.",
    )
    parser.add_argument(
        "ir_path",
        metavar="IR_JSON",
        help="path to an IR JSON file (e.g. `deno run src/cli.ts generate-ir config.ts`'s output)",
    )
    parser.add_argument(
        "--action",
        choices=["create", "delete"],
        default="create",
        help="create (default): plain adds. delete: --if-exists removes, cascading per ladops/ovn.py.",
    )
    args = parser.parse_args(argv)

    with open(args.ir_path) as f:
        raw_nodes = json.load(f)
    nodes = hydrate_nodes(raw_nodes)

    cluster_script, host_scripts = build_scripts(nodes, args.action)

    print("# ===== cluster (run once, on the central chassis) =====")
    print(cluster_script)
    for host_name, script in host_scripts.items():
        print(f"# ===== host: {host_name} =====")
        print(script)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
