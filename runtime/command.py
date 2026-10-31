"""Running a system command — the one way every service does it.

Each command is logged on the caller's logger: the command line (`$ …`) at
info, every output line as it comes (`| …`; with `on_err`, stderr lines as
`! …`) and the exit (`exit N after Xs`) at debug.

The command is also *reported* to `report`, if given — whoever waits on the
work (a request's `call.report`, runtime/peer.py): every command line
(info) and, unless the caller takes the output itself (`on_line`/`on_err`),
its output (debug).

Without `on_err`, stdout and stderr are merged in order; with it, stderr
goes there instead. The full output is returned for the caller's own use
(e.g. the tail of a failure).

A command runs in its own process group: on `timeout` or cancellation the
whole group is killed, so nothing it started is left behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shlex
import signal
import time
from collections.abc import Callable, Mapping, Sequence

LineSink = Callable[[str], None]
# The last output line of a command that ran too long: "timed out after 5s: …".
TIMED_OUT = "timed out after"
# (level, line): where commands and their output are reported.
Report = Callable[[str, str], None]


def nowhere(level: str, line: str) -> None:
    """A Report that drops everything (no one waits on the work)."""
    return None


async def run(
    argv: Sequence[str],
    *,
    log: logging.Logger,
    env: Mapping[str, str] | None = None,
    on_line: LineSink | None = None,
    on_err: LineSink | None = None,
    level: str = "info",
    timeout: float | None = None,
    report: Report = nowhere,
) -> tuple[bool, str]:
    """Run `argv`; (exited 0, all its output). `level` is the command line's
    (logged and reported); its output is always debug. A command still
    running after `timeout` seconds is killed and has failed."""
    command = shlex.join(argv)
    log.log(logging.DEBUG if level == "debug" else logging.INFO, "$ %s", command)
    report(level, f"$ {command}")
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if on_err is not None else asyncio.subprocess.STDOUT,
            env={**os.environ, **env} if env is not None else None,
            start_new_session=True,  # its own process group: killed as a whole
        )
    except OSError as exc:
        message = f"cannot run {argv[0]}: {exc.strerror or exc}"
        log.info("%s", message)
        report("warn", message)
        return False, message
    lines: list[str] = []

    async def read(stream: asyncio.StreamReader, mark: str, sink: LineSink | None) -> None:
        while chunk := await stream.readline():
            # apt/dpkg redraw progress with '\r': split on it too.
            for line in chunk.decode(errors="replace").replace("\r", "\n").splitlines():
                if not line.strip():
                    continue
                lines.append(line)
                log.debug("%s %s", mark, line)
                if sink is not None:
                    sink(line)
                else:
                    report("debug", line)

    assert proc.stdout is not None
    readers = [read(proc.stdout, "|", on_line)]
    if proc.stderr is not None:
        readers.append(read(proc.stderr, "!", on_err))

    async def finish() -> int:
        await asyncio.gather(*readers)
        return await proc.wait()

    try:
        code = await asyncio.wait_for(finish(), timeout)
    except TimeoutError:
        await _kill(proc)
        message = f"{TIMED_OUT} {timeout:g}s: {command}"
        log.warning("%s", message)
        report("warn", message)
        lines.append(message)
        return False, "\n".join(lines)
    except asyncio.CancelledError:  # never leave the process behind
        await _kill(proc)
        log.debug("cancelled: %s", argv[0])
        raise
    log.debug("exit %d after %.1fs: %s", code, time.monotonic() - started, argv[0])
    return code == 0, "\n".join(lines)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the process and everything it started (its process group)."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
