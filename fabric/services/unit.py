"""The unit executor: runs one unit of work.

    UnitExecReq { unit, action: create|delete, inputs?, host?, dryRun?, skip?, decide? }
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

import dataclasses
import json
import logging
import re
import shlex
import socket
from typing import cast

from runtime import command
from runtime.message import BodySpec, register_bodies
from runtime.peer import Call
from runtime.service import Service

from .. import generated as g
from ..context import Ctx, Options
from ..decide import STARTS_AFRESH, Decided, Decision, Replaced, decide, undo
from ..lifecycle import LifecycleAction, LifecycleController, Primitive
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
    and reported to the request the unit runs for (`call`)."""

    def __init__(self, call: Call) -> None:
        self.call = call

    async def run_cmd(
        self, label: str, cmd: list[str], abort_on_error: bool, /, *, timeout: float | None = None
    ) -> bool:
        ok, out = await command.run(
            cmd, log=log, level="debug", timeout=timeout, report=self.call.report
        )
        if not ok:
            tail = "\n".join(out.strip().splitlines()[-3:])
            message = f"{label}: {shlex.join(cmd)} failed: {tail}".rstrip(": ")
            if abort_on_error:
                raise UnitFailed(message)
            self.warn(message)  # not fatal (a delete): reported, the rest goes on
        return ok

    async def capture_cmd(
        self, label: str, cmd: list[str], abort_on_error: bool, /, *, timeout: float | None = None
    ) -> str:
        stdout: list[str] = []  # only stdout is the captured value
        stderr: list[str] = []  # why it failed — and in the unit's debug output

        def on_err(line: str) -> None:
            stderr.append(line)
            self.call.report("debug", line)

        ok, out = await command.run(
            cmd,
            log=log,
            on_line=stdout.append,
            on_err=on_err,
            level="debug",
            timeout=timeout,
            report=self.call.report,
        )
        if not ok:
            if abort_on_error:
                lines = out.strip().splitlines()
                why = stderr[-3:] if stderr else lines[-3:]
                if lines and lines[-1].startswith(command.TIMED_OUT) and lines[-1] not in why:
                    why.append(lines[-1])
                tail = "\n".join(why)
                raise UnitFailed(f"{label}: {shlex.join(cmd)} failed: {tail}".rstrip(": "))
            return ""
        return "\n".join(stdout).strip()

    def info(self, message: str, /) -> None:
        # The controller's per-operation lines: details of the unit — debug.
        log.debug("%s", message)
        self.call.report("debug", message)

    def warn(self, message: str, /) -> None:
        log.warning("%s", message)
        self.call.report("warn", message)


class UnitExecutor:
    def __init__(self, host: str | None = None) -> None:
        self.host = host or socket.gethostname()

    def service(self) -> Service:
        return Service(name="unit", bodies=BODIES, handlers={"UnitExecReq": self.execute})

    async def execute(self, call: Call) -> object:
        req = call.body
        assert isinstance(req, g.UnitExecReq)
        unit = req.unit
        options = Options(dry_run=bool(req.dryRun))
        if req.commandTimeout is not None:
            options = dataclasses.replace(options, command_timeout=req.commandTimeout)
        ctx = Ctx(options)
        for value in req.inputs or []:
            ctx.store[value.name] = json.loads(value.json)
        # The unit is the sender of everything it logs and runs, as one token
        # ("NIC_management-v2_on_br-d0868885") — greppable, and a client can
        # pick its level: --log NIC_management-v2_on_br-d0868885=debug.
        call = call.with_component(component_name(unit.name))
        lc = LifecycleController(
            ctx,
            CommandJob(call),
            LifecycleAction(req.action.value),
            req.host or self.host,
            name=unit.name,
        )
        skip = set(req.skip or [])
        decided = Decided()
        decisions: list[g.StepDecision] = []
        afresh = False  # a step started afresh what the later ones build on
        started: Primitive | None = None  # the container it started
        for index, step in enumerate(unit.steps):
            if index in skip and not afresh:
                continue  # already there (apply found it in the live RawIR)
            operation = to_operation(step, lc.graph.host)
            # Only a primitive step decides (the kind tags the union).
            primitive = step if step.kind == "Primitive" else None
            spec = primitive.decide if req.decide and primitive is not None else None
            if primitive is not None and spec is not None:
                # Its own live state decides (fabric/decide.py) — before any
                # step of the unit runs.
                decision = await decide(spec, decided, lc)
                decided.by_key[primitive.key] = decision
                decisions.append(g.StepDecision(index=index, decision=g.Decision(decision.value)))
                await call.log("info", f"{primitive.key}: {decision.value}")
                if spec.kind in STARTS_AFRESH and decision is not Decision.skip:
                    afresh = True
                    started = cast(Primitive, operation)
                if decision is Decision.skip:
                    continue
                if decision is Decision.replace:
                    # A Primitive step loads as a Primitive (fabric/units.py).
                    operation = Replaced(cast(Primitive, operation))
            await lc.run(operation)
        await call.log("info", f"{req.action.value} ({len(unit.steps)} steps)")

        state, message = g.IrApplyOutcome.done, None
        if req.dryRun:
            for line in lc.plan_lines():
                await call.log("info", line)
        else:
            try:
                await lc.execute()  # create forward (strict), delete rewound (best effort)
            except Exception as exc:  # noqa: BLE001 — a failed unit is an answer, not an error
                state, message = g.IrApplyOutcome.failed, str(exc)
                log.warning("%s: %s", unit.name, message)
                await call.log("warn", message)
                # Half set up: the next run must start it afresh, not find
                # it running and leave it.
                if started is not None:
                    await undo(started, lc)
        return g.UnitExecRes(
            kind="UnitExecRes",
            unit=unit.name,
            state=state,
            message=message,
            outputs=[
                g.UnitValue(name=name, json=json.dumps(value))
                for name, value in sorted(ctx.store.items())
            ],
            decisions=decisions or None,
        )


def service(host: str | None = None) -> Service:
    return UnitExecutor(host).service()
