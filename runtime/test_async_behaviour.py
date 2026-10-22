"""The runtime's concurrency semantics: who waits for whom, what gets
cancelled when, and in which order things happen.

Waits are driven by asyncio.Events wherever possible; TIMEOUT only turns a
hang into a failure."""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable

from .service import Job, StopHook

TIMEOUT = 2


def runtime_state(event: object) -> str | None:
    if isinstance(event, dict) and event.get("kind") == "runtime":
        return str(event["state"])
    return None


async def ticks(n: int = 10) -> None:
    """Let every runnable task take a few steps."""
    for _ in range(n):
        await asyncio.sleep(0)


async def forever(job: Job) -> None:
    await asyncio.Event().wait()


class StartTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_dynamic_job_starts_at_once_and_run_stops_it_when_done(self) -> None:
        order: list[str] = []
        dyn_stopped = asyncio.Event()

        async def dyn_start(job: Job) -> None:
            order.append("dyn start")

        async def dyn_stop(job: Job) -> None:
            order.append("dyn stop")
            dyn_stopped.set()

        async def spawner(job: Job) -> None:
            def configure(dyn: Job) -> None:
                dyn.on_start(dyn_start)
                dyn.on_stop(dyn_stop)

            job.job("dyn", initiate=True, configure=configure)
            # The spawner keeps running; run() must stop the finished
            # dynamic leaf on its own.
            await asyncio.wait_for(dyn_stopped.wait(), TIMEOUT)
            order.append("spawner done")

        root = Job("root")
        root.job("spawner", initiate=True).on_start(spawner)
        await asyncio.wait_for(root.run(), TIMEOUT)

        self.assertEqual(order, ["dyn start", "dyn stop", "spawner done"])

    async def test_a_child_added_to_a_pending_parent_starts_once(self) -> None:
        release = asyncio.Event()
        starts = 0

        async def gate_start(job: Job) -> None:
            await release.wait()

        async def count(job: Job) -> None:
            nonlocal starts
            starts += 1

        root = Job("root")
        gate = root.job("gate")
        gate.on_start(gate_start)
        late = gate.job("late")

        await root.start()
        await ticks()
        self.assertEqual(late.state, "pending")
        late.job("added").on_start(count)

        release.set()
        await ticks()
        self.assertEqual(starts, 1)
        await root.stop()

    async def test_a_job_stopped_while_pending_never_starts(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()

        async def gate_start(job: Job) -> None:
            await release.wait()

        async def late_start(job: Job) -> None:
            started.set()

        root = Job("root")
        gate = root.job("gate")
        gate.on_start(gate_start)
        late = gate.job("late")
        late.on_start(late_start)

        await root.start()
        await ticks()
        await late.stop(no_parent=True)
        release.set()
        await ticks()

        self.assertFalse(started.is_set())
        self.assertEqual(late.state, "stopped")
        await root.stop()


class EventTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle_events_are_broadcast_in_order(self) -> None:
        seen: list[tuple[str, str]] = []
        child_ready = asyncio.Event()
        release = asyncio.Event()

        async def record(job: Job, event: object) -> None:
            state = runtime_state(event)
            if state is not None and isinstance(event, dict):
                seen.append((event["job"], state))
                if event["job"] == "child" and state == "ready":
                    child_ready.set()

        async def child_start(job: Job) -> None:
            await release.wait()

        root = Job("root")
        root.on_emit(record)
        root.job("child").on_start(child_start)

        await root.start()
        await ticks()
        self.assertNotIn(("child", "ready"), seen)
        release.set()
        await asyncio.wait_for(child_ready.wait(), TIMEOUT)
        await root.stop()
        await ticks()

        child = [state for job, state in seen if job == "child"]
        self.assertEqual(child, ["start", "ready", "stop"])
        # Without initiate=True the child starts only after the root is ready.
        self.assertLess(seen.index(("root", "ready")), seen.index(("child", "start")))

    async def test_stopping_the_emitter_does_not_cancel_a_receivers_hook(self) -> None:
        release = asyncio.Event()
        handled = asyncio.Event()

        async def on_emitter_stop(job: Job, event: object) -> None:
            if runtime_state(event) == "stop" and isinstance(event, dict):
                if event["job"] == "emitter":
                    await release.wait()
                    handled.set()

        root = Job("root")
        emitter = root.job("emitter", initiate=True)
        root.job("receiver", initiate=True).on_emit(on_emitter_stop)

        await root.start()
        await emitter.stop(no_parent=True)
        # The receiver's hook outlives the emitter's stop.
        release.set()
        await asyncio.wait_for(handled.wait(), TIMEOUT)
        await root.stop()


class StopTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_runs_own_hooks_first_then_children_last_first(self) -> None:
        order: list[str] = []

        def recorder(name: str) -> StopHook:
            async def stopped(job: Job) -> None:
                order.append(name)

            return stopped

        root = Job("root")
        root.on_stop(recorder("root"))
        a = root.job("a")
        a.on_stop(recorder("a"))
        a.job("a1").on_stop(recorder("a1"))
        root.job("b").on_stop(recorder("b"))
        root.job("c").on_stop(recorder("c"))

        await root.start()
        await root.stop()

        self.assertEqual(order, ["root", "c", "b", "a", "a1"])

    async def test_processes_are_killed_before_the_stop_hooks_run(self) -> None:
        running = asyncio.Event()
        in_stop_hook: list[object] = []

        async def start(job: Job) -> None:
            running.set()
            await job.run_cmd("test", ["sleep", "30"], False)

        async def stop(job: Job) -> None:
            in_stop_hook.append(job.state)
            in_stop_hook.append(job.status()["processes"])

        root = Job("root")
        root.on_start(start)
        root.on_stop(stop)

        await root.start()
        await asyncio.wait_for(running.wait(), TIMEOUT)
        while not root.status()["processes"]:
            await asyncio.sleep(0.01)
        await root.stop()

        self.assertEqual(in_stop_hook, ["stopping", []])
        self.assertEqual(root.state, "stopped")

    async def test_a_failing_stop_hook_does_not_block_the_children(self) -> None:
        child_stopped = asyncio.Event()

        async def fails(job: Job) -> None:
            raise RuntimeError("boom")

        async def child_stop(job: Job) -> None:
            child_stopped.set()

        root = Job("root")
        root.on_stop(fails)
        root.job("child").on_stop(child_stop)

        await root.start()
        with self.assertLogs("ovn-fabric.runtime", level="ERROR"):
            await root.stop()
        self.assertTrue(child_stopped.is_set())

    async def test_a_hook_stopping_its_job_stops_the_whole_tree(self) -> None:
        sibling_cancelled = asyncio.Event()

        async def stop_me(job: Job) -> None:
            await job.stop()

        async def sibling(job: Job) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

        root = Job("root")
        root.job("quitter", initiate=True).on_start(stop_me)
        root.job("sibling", initiate=True).on_start(sibling)

        await asyncio.wait_for(root.run(), TIMEOUT)

        self.assertTrue(sibling_cancelled.is_set())
        self.assertEqual(root.state, "stopped")


class RunTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_keeps_a_parent_whose_start_hook_is_still_running(self) -> None:
        release = asyncio.Event()
        child_stopped = asyncio.Event()
        parent_finished = asyncio.Event()

        async def parent_start(job: Job) -> None:
            await release.wait()
            parent_finished.set()

        async def child_stop(job: Job) -> None:
            child_stopped.set()

        root = Job("root")
        parent = root.job("parent", initiate=True)
        parent.on_start(parent_start)
        parent.job("child", initiate=True).on_stop(child_stop)

        running = asyncio.create_task(root.run())
        await asyncio.wait_for(child_stopped.wait(), TIMEOUT)
        await ticks()
        # Its only child is done, but the parent itself is not.
        self.assertFalse(running.done())

        release.set()
        await asyncio.wait_for(running, TIMEOUT)
        self.assertTrue(parent_finished.is_set())

    async def test_run_waits_for_a_stop_started_from_a_hook(self) -> None:
        stopped: list[str] = []

        def slow_stop(name: str) -> StopHook:
            async def stop(job: Job) -> None:
                await asyncio.sleep(0.01)
                stopped.append(name)

            return stop

        async def on_trigger_ready(job: Job, event: object) -> None:
            # Runs in its own task, like childJob1 in the verification example.
            if runtime_state(event) == "ready" and isinstance(event, dict):
                if event["job"] == "trigger":
                    await work.stop()

        root = Job("root")
        root.on_emit(on_trigger_ready)
        work = root.job("work", initiate=True)
        for name in ("w1", "w2", "w3"):
            kid = work.job(name, initiate=True)
            kid.on_start(forever)
            kid.on_stop(slow_stop(name))
        root.job("trigger", initiate=True)

        await asyncio.wait_for(root.run(), TIMEOUT)

        # Every stop hook finished before run() returned.
        self.assertEqual(stopped, ["w3", "w2", "w1"])
        self.assertEqual({job["state"] for job in root.status()["children"]}, {"stopped"})


class CommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_capture_cmd_returns_stdout(self) -> None:
        got: list[str] = []

        async def start(job: Job) -> None:
            got.append(await job.capture_cmd("test", ["echo", "hello"]))

        root = Job("root")
        root.on_start(start)
        await asyncio.wait_for(root.run(), TIMEOUT)
        self.assertEqual(got, ["hello"])

    async def test_a_failed_command_without_abort_warns_and_continues(self) -> None:
        warnings: list[object] = []
        after = asyncio.Event()

        async def on_log(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("level") == "warn":
                warnings.append(event["message"])

        async def start(job: Job) -> None:
            await job.run_cmd("test", ["false"], False)
            after.set()

        root = Job("root")
        root.on_emit(on_log)
        root.job("child", initiate=True).on_start(start)
        await asyncio.wait_for(root.run(), TIMEOUT)

        self.assertTrue(after.is_set())
        self.assertEqual(warnings, ["failed in test: false"])


class VerificationScenarioTest(unittest.IsolatedAsyncioTestCase):
    """verification-example.py, shrunk: a looping job spawns dynamic jobs,
    a watcher stops the loop once it has seen every iteration."""

    async def test_scenario(self) -> None:
        iterations = 3
        posts = 0
        dyn_stopped: list[str] = []

        root = Job("root")
        main_job = root.job("main", initiate=True)
        watcher = main_job.job("watcher", initiate=True)
        watcher.on_start(forever)

        def configure(i: int) -> Callable[[Job], None]:
            async def start(job: Job) -> None:
                # The last one finishes on its own; the others run forever.
                if i < iterations:
                    await asyncio.Event().wait()

            async def stop(job: Job) -> None:
                dyn_stopped.append(job.name)

            def apply(job: Job) -> None:
                job.on_start(start)
                job.on_stop(stop)

            return apply

        async def main_start(job: Job) -> None:
            for i in range(1, iterations + 1):
                job.emit({"kind": "main", "state": "post", "i": i})
                await asyncio.sleep(0.01)
            await asyncio.Event().wait()

        async def main_emit(job: Job, event: object) -> None:
            nonlocal posts
            if isinstance(event, dict) and event.get("state") == "post":
                posts += 1
                i = int(event["i"])
                main_job.job(f"dyn{i}", initiate=True, configure=configure(i))

        async def watcher_emit(job: Job, event: object) -> None:
            # The last dynamic job finishing is the trigger.
            if runtime_state(event) == "stop" and posts == iterations:
                await main_job.stop()

        main_job.on_start(main_start)
        main_job.on_emit(main_emit)
        watcher.on_emit(watcher_emit)

        await asyncio.wait_for(root.run(), TIMEOUT)

        self.assertEqual(posts, iterations)
        self.assertEqual(sorted(dyn_stopped), ["dyn1", "dyn2", "dyn3"])
        self.assertEqual(
            root.format_status().splitlines(),
            [
                "root: stopped",
                "  main: stopped initiate",
                "    watcher: stopped initiate",
                "    dyn1: stopped initiate",
                "    dyn2: stopped initiate",
                "    dyn3: stopped initiate",
            ],
        )
