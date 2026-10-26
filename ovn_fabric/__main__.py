from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TOOL = "ovn-fabric"
PACKAGES = (
    "ovn_fabric",
    "deployer",
    "reconciler",
    "raw_reconciler",
    "ladops",
    "protocol",
    "runtime",
)


def _source_root() -> Path:
    # Source checkout: ovn_fabric/__main__.py -> repo root.
    return Path(__file__).resolve().parent.parent


def _in_bundle() -> bool:
    # In a pex the modules live inside the archive; there is no source tree
    # to hand to `pex` for a rebuild.
    return (
        ".pex" in str(Path(__file__).resolve()) or not (_source_root() / "pyproject.toml").is_file()
    )


def _deployer(argv: list[str]) -> int:
    from deployer.cli import main

    return main(argv)


def _reconciler(argv: list[str]) -> int:
    from reconciler.cli import main

    return main(argv)


def _raw(argv: list[str]) -> int:
    from raw_reconciler.cli import main

    return main(argv)


def _ensure_pex(root: Path) -> str:
    found = shutil.which("pex")
    if found is not None:
        return found

    # Keep PEX as a build-time tool: install it into the repo's existing
    # build venv instead of requiring a global pip install.
    venv = root / ".venv-build"
    python = venv / "bin/python"
    pex = venv / "bin/pex"
    if pex.is_file():
        return str(pex)
    if not python.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "pex",
        ],
        check=True,
    )
    if not pex.is_file():
        raise RuntimeError(f"pex installed but {pex} is missing")
    return str(pex)


def _build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog=f"{TOOL} build",
        description="Bundle the deployer + reconciler into one executable PEX.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=("output path, or - for stdout (default: dist/ovn-fabric, or " "stdout when piped)"),
    )
    parser.add_argument(
        "--python-shebang",
        default="/usr/bin/env python3",
        help="shebang for the generated executable",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="store zip entries uncompressed (PEX --no-compress)",
    )
    args = parser.parse_args(argv)

    if _in_bundle():
        parser.error("build must run from the source checkout, not from the bundled command")

    root = _source_root()
    pex = _ensure_pex(root)

    stream = args.output == "-" or (args.output is None and not sys.stdout.isatty())
    temp_output: Path | None = None
    if stream:
        # The output file must NOT exist: pex appends its bootstrap to an
        # existing path in a way that corrupts the zipapp header.
        temp_dir = Path(tempfile.mkdtemp(prefix="ovn-fabric-pex-"))
        temp_output = temp_dir / "ovn-fabric"
        output = temp_output
    else:
        output = Path(args.output or "dist/ovn-fabric")
        if not output.is_absolute():
            output = root / output
        output.parent.mkdir(parents=True, exist_ok=True)

    # PEX source directories are ROOTS whose contents land at the archive
    # root; passing package dirs directly would collide their __init__.py.
    # Stage the package directories under one root instead.
    staging = Path(tempfile.mkdtemp(prefix="ovn-fabric-pex-"))
    try:
        for package in PACKAGES:
            shutil.copytree(
                root / package,
                staging / package,
                ignore=shutil.ignore_patterns("__pycache__", "test_*.py"),
            )
        cmd = [
            pex,
            "-D",
            str(staging),
            "-m",
            "ovn_fabric.__main__:main",
            "-o",
            str(output),
            "--python-shebang",
            args.python_shebang,
        ]
        if args.no_compress:
            cmd.append("--no-compress")
        subprocess.run(
            cmd,
            check=True,
            # pex prints the output path on stdout; keep stdout clean for
            # the streamed artifact.
            stdout=sys.stderr if stream else None,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if stream:
        assert temp_output is not None
        try:
            sys.stdout.buffer.write(temp_output.read_bytes())
            sys.stdout.buffer.flush()
        finally:
            shutil.rmtree(temp_output.parent, ignore_errors=True)
    else:
        output.chmod(0o755)
        print(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description=(
            "One entrypoint for the generated deployer, the live reconciler, " "and the PEX build."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("deployer", help="generate the Python deployer from IR (deployer.cli)")
    sub.add_parser("reconciler", help="read live host state and emit IR (reconciler.cli)")
    sub.add_parser("raw", help="read live host state as the raw graph (raw_reconciler)")
    sub.add_parser("build", help="build the single-file PEX")

    if not argv or argv[0] in ("-h", "--help"):
        parser.print_help()
        return 0 if argv else 2

    command, rest = argv[0], argv[1:]
    if command == "deployer":
        return _deployer(rest)
    if command == "reconciler":
        return _reconciler(rest)
    if command == "raw":
        return _raw(rest)
    if command == "build":
        return _build(rest)

    parser.error(f"unknown command {command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
