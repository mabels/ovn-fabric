"""What the runtime remembers across a reboot (ADR 0012 §6).

    /var/lib/ovn-fabric/allocator.json
        {"bootId": …, "ir": …, "applied": …,
         "domains": {net id: {owner: {family: n}}}}

`domains` is the allocator's memory: every number the last create or run
placed, read back as its own precedence level (ADR 0011 §3: pinned > live >
remembered > new) — after a reboot the live view lacks what only the kernel
held (a workload NIC's address), and a replay puts it back on its number.
Written by the runtime only; not a second topology.

`applied` is the IR the last create or run applied on this host — none
after a delete: the runtime keeps containers alive only while it is the
deployed one (fabric/supervise.py).

`bootId` is the kernel's boot id when it was written: the runtime compares
it at start — another one means the host rebooted and the deployed IR is
replayed; the same one means only the runtime restarted (a deploy, a crash)
and everything is still there.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("fabric.boot")

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
STATE_FILE = "allocator.json"

# What ran the deployed IR at boot before the runtime did it itself
# (fabric/plan.py `_emit_boot`, gone): removed once, where it is still there.
OLD_ONESHOT = Path("/etc/systemd/system/ovn-fabric-reconcile.service")
OLD_SCRIPT = Path("/usr/local/lib/ovn-fabric/reconcile-on-boot")


def boot_id() -> str | None:
    try:
        return BOOT_ID.read_text().strip() or None
    except OSError:
        return None


class AllocatorState:
    def __init__(self, state_root: str | Path) -> None:
        self.path = Path(state_root) / STATE_FILE

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("%s unreadable, ignored: %s", self.path, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def remembered(self) -> dict[str, dict[str, dict[str, int]]]:
        domains = self.load().get("domains")
        return domains if isinstance(domains, dict) else {}

    def save(
        self, domains: dict[str, dict[str, dict[str, int]]] | None, ir: str | None = None
    ) -> None:
        """Write the boot id now and, given, what the run placed; with no
        `domains`, only the boot id changes."""
        data = self.load()
        data["bootId"] = boot_id()
        if domains is not None:
            data["domains"] = domains
            data["ir"] = ir
            data["applied"] = ir
        self._write(data)

    def applied(self) -> str | None:
        value = self.load().get("applied")
        return value if isinstance(value, str) else None

    def deleted(self) -> None:
        """A delete ran: nothing is applied (what was placed is remembered)."""
        data = self.load()
        data["applied"] = None
        self._write(data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, self.path)


def remove_old_oneshot() -> bool:
    """Disable and remove the old boot oneshot; True when it was there."""
    if not OLD_ONESHOT.exists() and not OLD_SCRIPT.exists():
        return False
    subprocess.run(
        ["systemctl", "disable", "--quiet", OLD_ONESHOT.name],
        check=False,
        capture_output=True,
    )
    OLD_ONESHOT.unlink(missing_ok=True)
    OLD_SCRIPT.unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
    return True
