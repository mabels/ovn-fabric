"""The verbose record of what a request did: every line it logs, at every
level, whatever `--log` says and whatever its client shows. The service's
output goes to journald, which also keeps it within bounds.

    journalctl -u ovn-fabric --grep <req>

A line names its request and the one it was sent on behalf of, so a run's id
finds its units too. What journald stores stays on disk: the values of a
`docker run -e KEY=value` are masked here; a command's output is not.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("fabric.journal")
log.setLevel(logging.DEBUG)

# `-e KEY=value` as shlex.join writes it: quoted when the value needs it.
_ENV = re.compile(r"(\s-e\s+)(?:'([A-Za-z_]\w*)=[^']*'|([A-Za-z_]\w*)=\S*)")


def masked(message: str) -> str:
    return _ENV.sub(lambda m: f"{m.group(1)}{m.group(2) or m.group(3)}=***", message)


def record(req_id: str, parent: str | None, component: str, level: str, message: str) -> None:
    """One line; a line of its own however many the message has."""
    text = masked(message).replace("\n", " | ")
    log.debug("req=%s parent=%s %s %s: %s", req_id, parent or "-", level, component, text)
