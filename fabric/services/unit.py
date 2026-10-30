"""The unit executor: runs one unit of work.

    UnitExecReq { unit, action: create|delete, inputs?, host?, dryRun? }
      → LogMsg …    each operation and every command it runs (with output)
      → UnitExecRes { unit, state, message?, outputs }

Logging: the unit is the sender (the component) of everything it logs and
runs — its start at info ("<unit name>: create (N steps)"), the operations
and every command, with their output, at debug.

A unit is data (fabric/units.py): its steps become the deployer's
lifecycle operations again and run on a LifecycleController — create
forward and strict (the first failure ends the unit), delete rewound and
best effort (failures are reported, the rest still runs). The executor
keeps no state between units: values captured by earlier units come as
`inputs` (the run's store, for the steps' refs), and every value known
afterwards goes back as `outputs`. Which units run, and in which order, is
the caller's (the apply task's) business.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import socket
from typing import Any

from deployer.context import Ctx, Options
from deployer.lifecycle import LifecycleAction, LifecycleController
from runtime import command
from runtime.message import BodySpec, Msg, register_bodies
from runtime.peer import Peer, current_component
from runtime.service import Service

from .. import generated as g
from ..units import to_operation

log = logging.getLogger("fabric.unit")

BODIES = {
    "UnitExecReq": BodySpec(g.UnitExecReq, "request"),
    "UnitExecRes": BodySpec(g.UnitExecRes, "response"),
}
register_bodies(BODIES)  # clients decode these too


def component_name(unit_name: str) -> str:
    """A unit's name as one token: every run of characters other than
    letters, digits, `.` and `-` becomes `_`."""
    return re.sub(r"[^A-Za-z0-9.-]+", "_", unit_name).strip("_")


class UnitFailed(RuntimeError):
    """A command of a create failed: the unit stops."""


class CommandJob:
    """The JobLike a LifecycleController runs its commands through: the
    runtime's command runner, so every command (and its output) is logged
    and reported to the requester."""

    async def run_cmd(self, label: str, cmd: list[str], abort_on_error: bool, /) -> bool:
        ok, out = await command.run(cmd, log=log, level="debug")
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-3:])
            message = f"{label}: {shlex.join(cmd)} failed: {tail}".rstrip(": ")
            if abort_on_error:
                raise UnitFailed(message)
            self.warn(message)  # not fatal (a delete): reported, the rest goes on
        return ok

    async def capture_cmd(self, label: str, cmd: list[str], abort_on_error: bool, /) -> str:
        stdout: list[str] = []  # only stdout is the captured value
        ok, out = await command.run(
            cmd, log=log, on_line=stdout.append, on_err=lambda _: None, level="debug"
        )
        if not ok:
            if abort_on_error:
                raise UnitFailed(f"{label}: {shlex.join(cmd)} failed: {out.strip()[-200:]}")
            return ""
        return "\n".join(stdout).strip()

    def info(self, message: str, /) -> None:
        # The controller's per-operation lines: details of the unit — debug.
        log.debug("%s", message)
        command.report("debug", message)

    def warn(self, message: str, /) -> None:
        log.warning("%s", message)
        command.report("warn", message)


class UnitExecutor:
    def __init__(self, host: str | None = None) -> None:
        self.host = host or socket.gethostname()

    def service(self) -> Service:
        return Service(name="unit", bodies=BODIES, handlers={"UnitExecReq": self.execute})

    async def execute(self, peer: Peer, msg: Msg[Any]) -> object:
        req = msg.body
        assert isinstance(req, g.UnitExecReq)
        unit = req.unit
        ctx = Ctx(Options(dry_run=bool(req.dryRun)))
        for value in req.inputs or []:
            ctx.store[value.name] = json.loads(value.json)
        lc = LifecycleController(
            ctx,
            CommandJob(),
            LifecycleAction(req.action.value),
            req.host or self.host,
            name=unit.name,
        )
        for step in unit.steps:
            await lc.run(to_operation(step, lc.graph.host))
        # The unit is the sender of everything it logs and runs from here,
        # as one token ("NIC_management-v2_on_br-d0868885") — greppable, and
        # a client can pick its level: --log NIC_management-v2_on_br-d0868885=debug.
        current_component.set(component_name(unit.name))
        await peer.log("info", f"{req.action.value} ({len(unit.steps)} steps)")

        state, message = g.IrApplyOutcome.done, None
        if req.dryRun:
            for line in lc.plan_lines():
                await peer.log("info", line)
        else:
            try:
                await lc.execute()  # create forward (strict), delete rewound (best effort)
            except Exception as exc:  # noqa: BLE001 — a failed unit is an answer, not an error
                state, message = g.IrApplyOutcome.failed, str(exc)
                log.warning("%s: %s", unit.name, message)
                await peer.log("warn", message)
        return g.UnitExecRes(
            kind="UnitExecRes",
            unit=unit.name,
            state=state,
            message=message,
            outputs=[
                g.UnitValue(name=name, json=json.dumps(value))
                for name, value in sorted(ctx.store.items())
            ],
        )


def service(host: str | None = None) -> Service:
    return UnitExecutor(host).service()
