from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import tempfile
import unittest
from typing import Any

from runtime import logconfig
from runtime.message import Msg
from runtime.peer import Peer
from runtime.runtime import Runtime
from runtime.service import Service
from runtime.test_message import IR_SAMPLE

from . import __main__ as cli
from . import generated as g
from .services import abstract_deps, unit
from .services.apply import Apply
from .services.ir import IrStore
from .services.test_apply import HOST, PLAN, Fakes
from .services.test_ir import _put


class FailingUnits(Fakes):
    """Every unit logs a debug line; the second one fails."""

    async def unit(self, peer: Peer, msg: Msg[Any]) -> object:
        await peer.log("debug", f"$ doing {msg.body.unit.name}")
        if msg.body.unit.name == PLAN[1]:
            self.fail = PLAN[1]
        return await super().unit(peer, msg)


def _run_cli(debug: bool, components: dict[str, int] | None = None) -> str:
    async def run() -> str:
        fakes = FailingUnits()
        runtime = Runtime()
        runtime.register(
            IrStore(tempfile.mkdtemp()).service(),
            Apply(runtime.client, HOST).service(),
            Service(
                name="abstract_deps",
                bodies=abstract_deps.BODIES,
                handlers={"AbstractDepsReq": fakes.deps},
            ),
            Service(name="unit", bodies=unit.BODIES, handlers={"UnitExecReq": fakes.unit}),
        )
        stop = asyncio.Event()
        address = f"mem://cli-{id(runtime)}"
        server = asyncio.create_task(runtime.serve(address, stop))
        await asyncio.sleep(0)
        await cli._ask(address, _put("draft", IR_SAMPLE))
        req = g.IrApplyReq(kind="IrApplyReq", ref="draft", action=g.ApplyAction.create)
        err = io.StringIO()
        handler = logging.StreamHandler(err)
        handler.setFormatter(logging.Formatter("%(levelname)s %(component)s: %(message)s"))
        cli.remote_log.handlers[:] = [handler]
        cli.remote_log.propagate = False
        cli.remote_log.setLevel(logging.DEBUG if debug else logging.INFO)
        logconfig.COMPONENTS.clear()
        logconfig.COMPONENTS.update(components or {})
        with contextlib.redirect_stderr(err):
            await cli._ask(address, req)
        stop.set()
        await server
        return err.getvalue()

    return asyncio.run(run())


class FailedUnitDebugTest(unittest.TestCase):
    def tearDown(self) -> None:
        cli.remote_log.handlers.clear()
        cli.remote_log.setLevel(logging.NOTSET)
        logconfig.COMPONENTS.clear()

    def test_the_debug_of_a_failed_unit_is_shown_and_only_its(self) -> None:
        out = _run_cli(debug=False)
        self.assertIn(f"WARNING cli: --- what the failed unit {PLAN[1]} did ---", out)
        self.assertIn(f"DEBUG unit: $ doing {PLAN[1]}", out)
        self.assertNotIn(f"$ doing {PLAN[0]}", out)  # it succeeded: its debug stays hidden
        # Everything in the one format, the client's own lines as `cli`.
        self.assertIn(f"INFO cli: {PLAN[0]}: done", out)
        self.assertIn(f"WARNING cli: {PLAN[1]}: failed — boom", out)

    def test_a_component_can_have_its_own_level(self) -> None:
        out = _run_cli(debug=False, components={"unit": logging.DEBUG})
        self.assertIn(f"DEBUG unit: $ doing {PLAN[0]}", out)  # live: its level is debug
        self.assertNotIn("--- what the failed unit", out)  # nothing hidden, nothing to show

    def test_with_debug_shown_live_nothing_is_repeated(self) -> None:
        out = _run_cli(debug=True)
        self.assertNotIn("--- what the failed unit", out)
        self.assertEqual(out.count(f"$ doing {PLAN[1]}"), 1)
        self.assertIn(f"$ doing {PLAN[0]}", out)


if __name__ == "__main__":
    unittest.main()
