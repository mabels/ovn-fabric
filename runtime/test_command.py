from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import unittest

from .command import reporting, run


class RunTest(unittest.TestCase):
    def test_logs_the_command_every_line_and_the_exit(self) -> None:
        lines: list[str] = []
        script = "import sys; print('one', flush=True); print('two', file=sys.stderr); sys.exit(3)"
        with self.assertLogs("runtime.test", "DEBUG") as logs:
            ok, out = asyncio.run(
                run([sys.executable, "-c", script], log=_log(), on_line=lines.append)
            )
        self.assertFalse(ok)
        self.assertEqual(out, "one\ntwo")
        self.assertEqual(lines, ["one", "two"])
        messages = [record.getMessage() for record in logs.records]
        self.assertTrue(messages[0].startswith("$ "))
        self.assertEqual(messages[1:3], ["| one", "| two"])
        self.assertTrue(messages[3].startswith("exit 3 after"))

    def test_carriage_return_progress_becomes_lines(self) -> None:
        lines: list[str] = []
        script = "import sys; sys.stdout.write('10%\\r50%\\r100%\\n')"
        asyncio.run(run([sys.executable, "-c", script], log=_log(), on_line=lines.append))
        self.assertEqual(lines, ["10%", "50%", "100%"])

    def test_stderr_apart_when_asked(self) -> None:
        out: list[str] = []
        err: list[str] = []
        script = "import sys; print('o', flush=True); print('e', file=sys.stderr)"
        ok, _ = asyncio.run(
            run([sys.executable, "-c", script], log=_log(), on_line=out.append, on_err=err.append)
        )
        self.assertTrue(ok)
        self.assertEqual((out, err), (["o"], ["e"]))

    def test_cancelling_kills_the_process(self) -> None:
        async def go() -> None:
            pids: list[str] = []
            script = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
            task = asyncio.create_task(
                run([sys.executable, "-c", script], log=_log(), on_line=pids.append)
            )
            while not pids:
                await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pids[0]), 0)

        asyncio.run(go())

    def test_the_command_and_its_output_are_reported(self) -> None:
        reported: list[tuple[str, str]] = []
        taken: list[str] = []
        script = "print('out')"
        with reporting(lambda level, line: reported.append((level, line))):
            asyncio.run(run([sys.executable, "-c", script], log=_log()))
            asyncio.run(run([sys.executable, "-c", script], log=_log(), on_line=taken.append))
        command = f"$ {shlex.join([sys.executable, '-c', script])}"
        # The output the caller takes itself is not reported again.
        self.assertEqual(reported, [("info", command), ("debug", "out"), ("info", command)])
        self.assertEqual(taken, ["out"])

    def test_a_missing_program(self) -> None:
        ok, out = asyncio.run(run(["/nonexistent/apt-get"], log=_log()))
        self.assertFalse(ok)
        self.assertIn("cannot run /nonexistent/apt-get", out)


def _log() -> logging.Logger:
    return logging.getLogger("runtime.test")


if __name__ == "__main__":
    unittest.main()
