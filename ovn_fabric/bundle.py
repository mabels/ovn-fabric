from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PACKAGES = (
    "ovn_fabric",
    "deployer",
    "reconciler",
    "raw_reconciler",
    "ladops",
    "protocol",
    "runtime",
)


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent


def ensure_pex(root: Path) -> str:
    found = shutil.which("pex")
    if found is not None:
        return found
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


def build_pex(
    output: Path,
    entrypoint: str,
    *,
    extra_files: dict[str, str] | None = None,
    packages: tuple[str, ...] = PACKAGES,
    python_shebang: str = "/usr/bin/env python3",
    no_compress: bool = False,
) -> None:
    root = source_root()
    if not (root / "pyproject.toml").is_file():
        raise RuntimeError("build must run from the source checkout")
    pex = ensure_pex(root)
    staging = Path(tempfile.mkdtemp(prefix="ovn-fabric-pex-"))
    try:
        for package in packages:
            shutil.copytree(
                root / package,
                staging / package,
                ignore=shutil.ignore_patterns("__pycache__", "test_*.py"),
            )
        for relpath, content in (extra_files or {}).items():
            path = staging / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        cmd = [
            pex,
            "-D",
            str(staging),
            "-m",
            entrypoint,
            "-o",
            str(output),
            "--python-shebang",
            python_shebang,
        ]
        if no_compress:
            cmd.append("--no-compress")
        subprocess.run(cmd, check=True, stdout=sys.stderr if output.name else None)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    output.chmod(0o755)
