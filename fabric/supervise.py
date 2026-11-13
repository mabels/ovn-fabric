"""Keeping the applied IR's containers alive (ADR 0012 §4, §6).

A container of the applied IR that is not running — it died, it was
stopped or removed by hand, docker restarted without it — is brought back
by the runtime: it runs the deployed IR again (no cleanup), which leaves
alone what is there and makes the container anew with its netns link,
veths and NIC addresses. That decision is taken only while no apply runs:
a run that replaces a container stops it on purpose.

One that keeps dying is not started in a loop: the wait before the next
try doubles (BASE, 2·BASE, … up to MAX); once it has run HEALTHY seconds
its count starts over.

This module is the bookkeeping only — what is due, what was tried; the
apply service (fabric/services/apply.py) watches docker and runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import subprocess
from collections.abc import Callable, Iterable, Mapping

log = logging.getLogger("fabric.supervise")

BASE = 5.0
MAX = 300.0
HEALTHY = 600.0
# How often docker is looked at without an event; how long after an event.
POLL = 30.0
SETTLE = 3.0


@dataclasses.dataclass
class Restarts:
    count: int = 0
    last: float | None = None
    next_at: float = 0.0


class Supervisor:
    def __init__(self) -> None:
        self.wanted: dict[str, str] = {}  # container -> the unit that starts it
        self.restarts: dict[str, Restarts] = {}

    def want(self, containers: Mapping[str, str]) -> None:
        """The applied IR's containers (none: nothing is kept alive)."""
        self.wanted = dict(containers)
        for name in list(self.restarts):
            if name not in self.wanted:
                del self.restarts[name]

    def down(self, states: Mapping[str, str]) -> list[str]:
        """The wanted containers not running ('missing' when docker has none)."""
        return sorted(n for n in self.wanted if states.get(n, "missing") != "running")

    def due(self, states: Mapping[str, str], now: float) -> list[str]:
        """Those down whose wait is over."""
        return [n for n in self.down(states) if self.restarts.get(n, Restarts()).next_at <= now]

    def started(self, names: Iterable[str], now: float) -> None:
        for name in names:
            r = self.restarts.setdefault(name, Restarts())
            r.count += 1
            r.last = now
            r.next_at = now + min(MAX, BASE * 2 ** (r.count - 1))

    def healthy(self, states: Mapping[str, str], now: float) -> None:
        """A container that has run HEALTHY seconds since its last start
        starts over: its next death is tried again at once."""
        for name, r in self.restarts.items():
            if states.get(name) == "running" and r.last is not None and now - r.last >= HEALTHY:
                r.count, r.next_at = 0, 0.0


def docker_states() -> dict[str, str] | None:
    """docker's containers: name -> state; None when docker does not answer."""
    out = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    states: dict[str, str] = {}
    for line in out.stdout.splitlines():
        name, _, state = line.partition("\t")
        if name:
            states[name] = state.strip()
    return states


async def watch_docker(wake: Callable[[], None]) -> None:
    """Call `wake` whenever a container dies, stops or is removed (docker
    events); started again when the stream ends (docker restarted)."""
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "events",
                "--filter",
                "type=container",
                "--filter",
                "event=die",
                "--filter",
                "event=stop",
                "--filter",
                "event=destroy",
                "--format",
                "{{.Actor.Attributes.name}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert proc.stdout is not None
            async for _line in proc.stdout:
                wake()
            await proc.wait()
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            log.debug("docker events: %s", exc)
        finally:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        wake()  # docker went away and may be back: look
        await asyncio.sleep(BASE)
