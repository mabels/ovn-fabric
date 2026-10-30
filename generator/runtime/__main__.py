"""`python3 -m runtime`: run the root service.

The CLI and the systemd service will both invoke this; the empty tree just
waits until the process is stopped.
"""

from __future__ import annotations

import asyncio
import sys

from .service import Job


async def _run() -> None:
    root = Job("ovn-fabric")
    try:
        await root.run()
    finally:
        await root.stop()


def main() -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
