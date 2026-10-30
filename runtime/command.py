"""Running a system command — the one way every service does it.

Each command is logged on the caller's logger: the command line (`$ …`) at
info, every output line as it comes (`| …`; with `on_err`, stderr lines as
`! …`) and the exit (`exit N after Xs`) at debug.

The command is also *reported* — to whoever is waiting on the work: the
runtime sets a reporter for each request it dispatches (see `reporting`),
so the client sees every command its request starts (info) and, unless the
caller takes the output itself (`on_line`/`on_err`), its output (debug).

Without `on_err`, stdout and stderr are merged in order; with it, stderr
goes there instead. The full output is returned for the caller's own use
(e.g. the tail of a failure).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import os
import shlex
import time
from collections.abc import Callable, Iterator, Mapping, Sequence

LineSink = Callable[[str], None]
# (level, line): where commands and their output are reported.
Report = Callable[[str, str], None]

_report: contextvars.ContextVar[Report | None] = contextvars.ContextVar("report", default=None)


@contextlib.contextmanager
def reporting(report: Report | None) -> Iterator[None]:
    """Report the commands run inside (in this task and the tasks it starts)
    to `report`."""
    token = _report.set(report)
    try:
        yield
    finally:
        _report.reset(token)


def report(level: str, line: str) -> None:
    """Report a line to whoever waits on the current work (if anyone)."""
    sink = _report.get()
    if sink is not None:
        sink(level, line)


async def run(
    argv: Sequence[str],
    *,
    log: logging.Logger,
    env: Mapping[str, str] | None = None,
    on_line: LineSink | None = None,
    on_err: LineSink | None = None,
    level: str = "info",
) -> tuple[bool, str]:
    """Run `argv`; (exited 0, all its output). `level` is the command line's
    (logged and reported); its output is always debug."""
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
    try:
        await asyncio.gather(*readers)
        code = await proc.wait()
    except asyncio.CancelledError:  # never leave the process behind
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        log.debug("cancelled: %s", argv[0])
        raise
    log.debug("exit %d after %.1fs: %s", code, time.monotonic() - started, argv[0])
    return code == 0, "\n".join(lines)
