#!/usr/bin/env python3
"""Small runtime verification example.

Run:

    python3 runtime/verification-example.py          # real timings (5 s sleeps)
    python3 runtime/verification-example.py --fast   # short sleeps

It mirrors:

  mainJob1: 5 x (emit preSleep -> sleep -> emit postSleep)
  mainJob1.job("childJob1", start: sleep 86400)
  mainJob1.on_emit(postSleep -> mainJob1.job("programmicJob{i}", sleep (i+1)*3))
  childJob1.on_emit(runtime.stop + 5 postSleeps -> stop(mainJob1))
  statusJob: print root.format_status() every 3 s

Every event is broadcast to all registered jobs, including the emitter; the
runtime emits start/ready/stop lifecycle events itself.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

# Allow `python3 runtime/verification-example.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from runtime.service import Job  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="use short sleeps (test mode)")
    parser.add_argument("--iterations", type=int, default=5)
    args = parser.parse_args()

    delay = 0.2 if args.fast else 5.0
    child_delay = 30.0 if args.fast else 86400.0
    factor = 0.3 if args.fast else 3.0
    status_every = 0.3 if args.fast else 3.0

    root = Job("root")
    main_job = root.job("mainJob1", initiate=True)
    child_job = main_job.job("childJob1", initiate=True)
    seen = {"pre": 0, "post": 0}

    async def on_log(job: Job, event: object) -> None:
        # Log events travel UP to the root; everything else is broadcast.
        if isinstance(event, dict) and event.get("kind") == "log":
            print(
                f"[{event.get('level', 'info')}] {event.get('name')}: {event.get('message')}",
                flush=True,
            )

    root.on_emit(on_log)

    async def child_start(job: Job) -> None:
        print("childJob1: sleeping (the runtime will kill this on stop)", flush=True)
        await job.run_cmd("childJob1", ["sleep", f"{child_delay:g}"], False)

    async def child_emit(job: Job, event: object) -> None:
        if not isinstance(event, dict):
            return
        if event.get("kind") == "runtime" and event.get("state") == "stop":
            print(f"childJob1: saw runtime.stop (posts={seen['post']})", flush=True)
            if seen["post"] == args.iterations:
                print(
                    "childJob1: runtime.stop after all postSleeps -> stop(mainJob1)",
                    flush=True,
                )
                await main_job.stop()

    child_job.on_start(child_start)
    child_job.on_emit(child_emit)

    def configure_programmatic(i: int) -> Any:
        async def start(job: Job) -> None:
            print(f"programmicJob{i}: start", flush=True)
            await job.run_cmd(f"programmicJob{i}", ["sleep", f"{(i + 1) * factor:g}"], False)

        async def stop(job: Job) -> None:
            print(f"programmicJob{i}: on_stop", flush=True)

        def configure(job: Job) -> None:
            job.on_start(start)
            job.on_stop(stop)

        return configure

    async def main_start(job: Job) -> None:
        for i in range(1, args.iterations + 1):
            job.emit({"kind": "mainJob1", "state": "preSleep", "i": i})
            print(f"mainJob1: preSleep {i} -> sleep {delay:g}s", flush=True)
            await job.run_cmd("mainJob1", ["sleep", f"{delay:g}"], False)
            job.emit({"kind": "mainJob1", "state": "postSleep", "i": i})
            print(f"mainJob1: postSleep {i}", flush=True)

    async def main_emit(job: Job, event: object) -> None:
        if not isinstance(event, dict):
            return
        if event.get("kind") == "mainJob1" and event.get("state") == "preSleep":
            seen["pre"] += 1
        if event.get("kind") == "mainJob1" and event.get("state") == "postSleep":
            seen["post"] += 1
            i = int(event["i"])
            # Dynamic job: Job.job() on a live tree builds and starts it now.
            main_job.job(
                f"programmicJob{i}",
                initiate=True,
                configure=configure_programmatic(i),
            )

    main_job.on_start(main_start)
    main_job.on_emit(main_emit)

    async def print_status(job: Job) -> None:
        # Loops until the root stops and cancels it.
        while True:
            await asyncio.sleep(status_every)
            print(f"--- status ---\n{root.format_status()}", flush=True)

    root.job("statusJob", initiate=True).on_start(print_status)

    await root.run()
    print(f"--- final status ---\n{root.format_status()}", flush=True)
    print("verification example finished", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
