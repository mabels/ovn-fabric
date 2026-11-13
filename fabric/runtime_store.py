"""Deploying the runtime (ADR 0012 §1): a store of PEX files by hash on the
host, and the two halves of `deploy`.

    /var/lib/ovn-fabric/runtime/<hex>.pex        a runtime, by content
    /var/lib/ovn-fabric/runtime/<hex>.meta.json  {bytes, storedAt}
    /var/lib/ovn-fabric/runtime/refs.json        {"deployed": …, "previous": …}
    /usr/local/lib/ovn-fabric/fabric.pex         → the deployed one (a link)
    /usr/local/bin/fabric                        the CLI on the host: runs it

`install` runs ON the host, as the PEX being deployed (or the deployed one,
to switch back with a ref): it stores itself, points `deployed` at it, links
the unit's executable to it, restarts the service and waits until it answers
`version` with that hash — else it goes back to what ran before.

`deploy` runs where a PEX is built (ADR 0012 §0: a PEX that is not a host's
deployed one can only deploy): it copies itself over and runs `install`
there over ssh. The 10 most recent runtimes no name points to are kept.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import sys
import time
from functools import cache
from pathlib import Path

from .store import ContentStore, StoreError

STATE_ROOT = "/var/lib/ovn-fabric"
LINK = Path("/usr/local/lib/ovn-fabric/fabric.pex")
UNIT = Path("/etc/systemd/system/ovn-fabric.service")
SOCKET = "/run/ovn-fabric.sock"
PEX_ROOT = "/var/cache/ovn-fabric/pex"
KEEP = 10
COMMAND = Path("/usr/local/bin/fabric")
_MARK = "# ovn-fabric: the deployed runtime's CLI"


def store(state_root: str = STATE_ROOT) -> ContentStore:
    return ContentStore(Path(state_root) / "runtime", ".pex", keep=KEEP)


def own_pex() -> Path | None:
    """The PEX file this process runs from; None from a source checkout."""
    path = os.environ.get("PEX")
    return Path(path) if path and Path(path).is_file() else None


@cache
def own_hash() -> str | None:
    pex = own_pex()
    if pex is None:
        return None
    return f"sha256:{hashlib.sha256(pex.read_bytes()).hexdigest()}"


def unit_text(state_root: str = STATE_ROOT) -> str:
    """The one service (ADR 0007): the deployed runtime, through its link."""
    return "\n".join(
        [
            "[Unit]",
            "Description=ovn-fabric runtime (the deployed fabric.pex)",
            "Wants=network-online.target",
            "After=network-online.target",
            "",
            "[Service]",
            f"Environment=PEX_ROOT={PEX_ROOT}",
            f"ExecStart=/usr/bin/env python3 {LINK} --socket {SOCKET} serve "
            f"--state-root {state_root} --files-root {state_root}/containers",
            "Restart=on-failure",
            "RestartSec=5",
            "# More grace than the runtime gives the containers it stops (10s each).",
            "TimeoutStopSec=60",
            "KillMode=mixed",
            "# A run logs every line to the journal: thousands, in seconds.",
            "LogRateLimitIntervalSec=30s",
            "LogRateLimitBurst=100000",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def command_text() -> str:
    """`fabric …` on the host (ADR 0012 §0): the deployed PEX, talking to
    the runtime's socket, with the unit's PEX_ROOT (no second unpacking)."""
    return "\n".join(
        [
            "#!/bin/sh",
            _MARK,
            f'PEX_ROOT={PEX_ROOT} exec python3 {LINK} --socket {SOCKET} "$@"',
            "",
        ]
    )


def _write_command() -> None:
    """Install `fabric` into /usr/local/bin — never over one that is not ours."""
    if COMMAND.exists() and _MARK not in COMMAND.read_text(errors="replace"):
        print(f"warning: {COMMAND} is not ovn-fabric's: left as it is", file=sys.stderr)
        return
    if COMMAND.exists() and COMMAND.read_text() == command_text():
        return
    COMMAND.parent.mkdir(parents=True, exist_ok=True)
    tmp = COMMAND.with_name(f".{COMMAND.name}.tmp")
    tmp.write_text(command_text())
    tmp.chmod(0o755)
    os.replace(tmp, COMMAND)


def _link(target: Path) -> None:
    LINK.parent.mkdir(parents=True, exist_ok=True)
    tmp = LINK.with_name(f".{LINK.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    os.replace(tmp, LINK)


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True)


def _answers(digest: str, timeout: float) -> bool:
    """The service answers `version` with `digest` within `timeout`."""
    deadline = time.monotonic() + timeout
    env = {**os.environ, "PEX_ROOT": PEX_ROOT}
    env.pop("PEX", None)
    while time.monotonic() < deadline:
        out = subprocess.run(
            ["python3", str(LINK), "--socket", SOCKET, "version"],
            capture_output=True,
            text=True,
            env=env,
        )
        if out.returncode == 0 and digest in out.stdout:
            return True
        time.sleep(1)
    return False


def _switch(runtimes: ContentStore, digest: str, state_root: str) -> None:
    _link(runtimes.blob(digest))
    _write_command()
    if not UNIT.exists() or UNIT.read_text() != unit_text(state_root):
        UNIT.write_text(unit_text(state_root))
    _systemctl("daemon-reload")
    _systemctl("enable", "--quiet", "ovn-fabric.service")
    _systemctl("restart", "ovn-fabric.service")


def install(ref: str | None, state_root: str = STATE_ROOT, timeout: float = 60) -> int:
    """On the host: make this PEX (or the stored `ref`) the deployed runtime."""
    runtimes = store(state_root)
    if ref is not None:
        try:
            _name, digest = runtimes.resolve(ref)
        except StoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        pex = own_pex()
        if pex is None:
            print("error: deploy a PEX — this runs from a source checkout", file=sys.stderr)
            return 2
        data = pex.read_bytes()
        digest = runtimes.put(None, data, {"bytes": len(data)}).digest
    before = runtimes.refs().get("deployed")
    if before == digest and _answers(digest, 2):
        _write_command()
        print(f"{digest}: already deployed and running")
        return 0
    runtimes.point("deployed", digest)
    if before is not None and before != digest:
        runtimes.point("previous", before)
    _switch(runtimes, digest, state_root)
    if _answers(digest, timeout):
        print(f"{digest}: deployed" + (f" (was {before})" if before else ""))
        return 0
    print(f"error: {digest} does not answer — going back", file=sys.stderr)
    if before is not None and before != digest:
        runtimes.point("deployed", before)
        runtimes.point("previous", digest)
        _switch(runtimes, before, state_root)
    return 1


def listing(state_root: str = STATE_ROOT) -> list[str]:
    """`<name or -> <hash> <bytes> <stored at>` for every stored runtime."""
    runtimes = store(state_root)
    names: dict[str, list[str]] = {}
    for name, digest in runtimes.entries():
        names.setdefault(digest, []).append(name)
    lines = []
    for path in sorted(runtimes.dir.glob("*.pex")):
        digest = f"sha256:{path.stem}"
        meta = runtimes.meta(digest)
        at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(meta["storedAt"])))
        label = ",".join(n for n in names.get(digest, []) if n != digest) or "-"
        lines.append(f"{label:18} {digest} {meta['bytes']:>10} {at}")
    return lines


def deploy(target: str, pex: Path | None, ref: str | None) -> int:
    """Where a PEX is built: put it onto `target` and install it there — or,
    with `ref`, switch the host to a runtime it already stores."""
    remote_env = f"PEX_ROOT={PEX_ROOT}"
    if ref is not None:
        command = f"{remote_env} python3 {LINK} runtime-install --ref {shlex.quote(ref)}"
        return subprocess.run(["ssh", target, command]).returncode
    if pex is None:
        print("error: no PEX to deploy (build one with `bundle`, or --pex)", file=sys.stderr)
        return 2
    digest = hashlib.sha256(pex.read_bytes()).hexdigest()
    remote = f"/tmp/ovn-fabric-{digest[:16]}.pex"
    if subprocess.run(["scp", "-q", str(pex), f"{target}:{remote}"]).returncode != 0:
        return 1
    command = (
        f"{remote_env} python3 {remote} runtime-install; status=$?; rm -f {remote}; exit $status"
    )
    return subprocess.run(["ssh", target, command]).returncode
