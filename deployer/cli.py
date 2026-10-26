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
        choices=["python", "pex"],
        default="pex",
        help="output format; both now produce one PEX executable. `python` is "
        "kept as a legacy alias so existing pipelines keep working",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="PEX output path (or - for stdout); default dist/ovn-fabric-deployer, "
        "or stdout when piped",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="store PEX zip entries uncompressed",
    )
    args = parser.parse_args(argv)

    if args.ir_path == "-":
        raw_nodes = json.load(sys.stdin)
    else:
        with open(args.ir_path) as f:
            raw_nodes = json.load(f)
    nodes = hydrate_nodes(raw_nodes)
    source = generate_python_deployer(nodes)

    import shutil
    import tempfile
    from pathlib import Path

    from ovn_fabric.bundle import build_pex, source_root

    root = source_root()
    stream = args.output == "-" or (args.output is None and not sys.stdout.isatty())
    temp_dir: Path | None = None
    if stream:
        temp_dir = Path(tempfile.mkdtemp(prefix="ovn-fabric-deployer-"))
        output = temp_dir / "ovn-fabric-deployer"
    else:
        output = Path(args.output or "dist/ovn-fabric-deployer")
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)

    build_pex(
        output,
        "generated_deployer:main",
        extra_files={"generated_deployer.py": source},
        no_compress=args.no_compress,
    )
    if stream:
        assert temp_dir is not None
        try:
            sys.stdout.buffer.write(output.read_bytes())
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            return 0
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
