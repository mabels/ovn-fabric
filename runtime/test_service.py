from __future__ import annotations

import asyncio
import unittest

from .service import Job, JobError, run_cmd


class ServiceBuilderTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_observes_completion_and_emits_on_stop(self) -> None:
        seen: list[str] = []
        root = Job("root")
        child = root.job("child")

        async def started(job: Job) -> None:
            seen.append("start")

        async def stopped(job: Job) -> None:
            seen.append("stop")

        child.on_start(started)
        child.on_stop(stopped)
        await root.run()
        self.assertEqual(seen, ["start", "stop"])

    async def test_start_and_stop_hooks_receive_the_job(self) -> None:
        seen: list[tuple[str, str]] = []

        async def started(job: Job) -> None:
            seen.append(("start", job.name))

        async def stopped(job: Job) -> None:
            seen.append(("stop", job.name))

        root = Job("root")
        self.assertIs(root.on_start(started), started)
        root.on_stop(stopped)

        await root.start()
        await asyncio.sleep(0)
        self.assertEqual(seen, [("start", "root")])

        await root.stop()
        self.assertEqual(seen, [("start", "root"), ("stop", "root")])

    async def test_job_and_initiate_marker(self) -> None:
        root = Job("root")
        self.assertFalse(root.job("waits").initiate)
        self.assertTrue(root.job("immediate", initiate=True).initiate)

    async def test_child_waits_for_parent_readiness(self) -> None:
        release = asyncio.Event()
        child_started = asyncio.Event()

        async def parent_start(job: Job) -> None:
            await release.wait()

        async def child_start(job: Job) -> None:
            child_started.set()

        root = Job("root")
        parent = root.job("parent")
        parent.on_start(parent_start)
        parent.job("child").on_start(child_start)

        await root.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(child_started.is_set())

        release.set()
        await asyncio.wait_for(child_started.wait(), 1)
        await root.stop()

    async def test_initiate_skips_parent_readiness(self) -> None:
        release = asyncio.Event()
        child_started = asyncio.Event()

        async def parent_start(job: Job) -> None:
            await release.wait()

        async def child_start(job: Job) -> None:
            child_started.set()

        root = Job("root")
        parent = root.job("parent")
        parent.on_start(parent_start)
        parent.job("child", initiate=True).on_start(child_start)

        await root.start()
        await asyncio.wait_for(child_started.wait(), 1)

        release.set()
        await root.stop()

    async def test_queued_event_reaches_a_late_hook(self) -> None:
        got: list[object] = []
        root = Job("root")
        await root.start()
        root.emit({"kind": "dependency", "name": "x"}, queue=True)
        child = root.job("late", initiate=True)

        async def hook(job: Job, event: object) -> None:
            got.append(event)

        child.on_emit(hook)
        await asyncio.sleep(0)
        self.assertEqual(got, [{"kind": "dependency", "name": "x"}])
        await root.stop()

    async def test_non_queued_event_does_not_reach_a_late_hook(self) -> None:
        got: list[object] = []
        root = Job("root")
        await root.start()
        root.emit({"kind": "dependency", "name": "x"})
        child = root.job("late", initiate=True)

        async def hook(job: Job, event: object) -> None:
            got.append(event)

        child.on_emit(hook)
        await asyncio.sleep(0)
        self.assertEqual(got, [])
        await root.stop()

    async def test_emit_reaches_every_registered_job(self) -> None:
        got: list[tuple[str, object]] = []
        root = Job("root")
        deps = root.job("deps")
        child = deps.job("ovn-kernel-one", initiate=True)

        async def record(name: str) -> None:
            pass

        async def on_root(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "dependency-ready":
                got.append(("root", event))

        async def on_deps(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "dependency-ready":
                got.append(("deps", event))

        async def on_child(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "dependency-ready":
                got.append(("child", event))

        root.on_emit(on_root)
        deps.on_emit(on_deps)
        child.on_emit(on_child)

        await root.start()
        deps.emit({"kind": "dependency-ready", "dep": "ovs"})
        await asyncio.sleep(0)

        event = {"kind": "dependency-ready", "dep": "ovs"}
        self.assertEqual(sorted(got), [("child", event), ("deps", event), ("root", event)])
        await root.stop()

    async def test_ctx_is_passed_to_on_emit_hooks(self) -> None:
        got: list[tuple[object, object]] = []
        root = Job("root", ctx={"host": "chassis-1"})
        child = root.job("child")

        async def on_event(job: Job, event: object, ctx: object) -> None:
            got.append((ctx, event))

        child.on_emit(on_event)
        await root.start()
        root.emit({"kind": "dependency", "name": "packages", "state": "ready"})
        await asyncio.sleep(0)
        self.assertEqual(got[0][0], {"host": "chassis-1"})
        await root.stop()

    async def test_run_cmd_timeout_kills_the_process(self) -> None:
        root = Job("root")
        await root.start()
        with self.assertRaises(JobError):
            await root.run_cmd("test", ["sleep", "30"], True, timeout=0.05)
        self.assertEqual(root._processes, {})
        await root.stop()

    async def test_stop_kills_a_running_foreground_command(self) -> None:
        async def start(job: Job) -> None:
            await run_cmd(job, "test", ["sleep", "30"], True)

        root = Job("root")
        root.on_start(start)
        await root.start()
        for _ in range(50):
            if root._processes:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(len(root._processes), 1)
        status = root.status()
        self.assertEqual([proc["cmd"] for proc in status["processes"]], ["sleep 30"])
        pid = status["processes"][0]["pid"]
        self.assertEqual(root.format_status().splitlines()[1], f"  $ [{pid}] sleep 30")
        await root.stop()
        self.assertEqual(root._processes, {})

    async def test_log_bubbles_up_to_the_root(self) -> None:
        got: list[tuple[str, object]] = []
        root = Job("root")
        deps = root.job("deps")
        child = deps.job("ovn-kernel-one", initiate=True)

        async def on_root(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "log":
                got.append((job.name, event))

        root.on_emit(on_root)

        await root.start()
        child.warn("no uplink yet")
        await asyncio.sleep(0)

        self.assertEqual(
            got,
            [
                (
                    "root",
                    {
                        "kind": "log",
                        "level": "warn",
                        "name": "ovn-kernel-one",
                        "message": "no uplink yet",
                    },
                )
            ],
        )
        await root.stop()

    async def test_log_reaches_ancestors_not_the_emitter_or_descendants(self) -> None:
        got: list[str] = []
        root = Job("root")
        parent = root.job("parent", initiate=True)
        child = parent.job("child", initiate=True)

        async def on_parent(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "log":
                got.append("parent")

        async def on_child(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "log":
                got.append("child")

        parent.on_emit(on_parent)
        child.on_emit(on_child)

        await root.start()
        child.info("hello")
        await asyncio.sleep(0)

        self.assertEqual(got, ["parent"])
        await root.stop()

    async def test_log_level_sugar(self) -> None:
        got: list[object] = []
        root = Job("root")

        async def on_root(job: Job, event: object) -> None:
            if isinstance(event, dict) and event.get("kind") == "log":
                got.append(event)

        root.on_emit(on_root)
        job = root.job("child", initiate=True)
        await root.start()
        job.debug("d")
        job.info("i")
        job.warn("w")
        job.error("e")
        await asyncio.sleep(0)
        levels: list[object] = []
        for event in got:
            if isinstance(event, dict):
                levels.append(event["level"])
        self.assertEqual(levels, ["debug", "info", "warn", "error"])
        await root.stop()

    async def test_unknown_log_level_is_an_error(self) -> None:
        root = Job("root")
        with self.assertRaises(ValueError):
            root.log("fatal", "x")  # type: ignore[arg-type]

    async def test_events_are_logged_at_debug(self) -> None:
        root = Job("root")
        root.job("child")
        with self.assertLogs("ovn-fabric.runtime", level="DEBUG") as logs:
            await root.start()
            root.emit({"kind": "dependency-ready", "dep": "configure"})
            await asyncio.sleep(0)
        self.assertTrue(any("emit" in line for line in logs.output))
        await root.stop()

    async def test_emit_before_start_is_an_error(self) -> None:
        with self.assertRaises(RuntimeError):
            Job("root").emit({})

    async def test_a_failing_hook_does_not_block_siblings(self) -> None:
        completed = asyncio.Event()

        async def fails(job: Job) -> None:
            raise RuntimeError("boom")

        async def succeeds(job: Job) -> None:
            completed.set()

        root = Job("root")
        root.job("bad", initiate=True).on_start(fails)
        root.job("good", initiate=True).on_start(succeeds)

        with self.assertLogs("ovn-fabric.runtime", level="ERROR"):
            await root.start()
            await asyncio.wait_for(completed.wait(), 1)
            await asyncio.sleep(0)  # let the failed task's done-callback log
            await root.stop()

    async def test_a_slow_start_does_not_block_start(self) -> None:
        slow_began = asyncio.Event()
        fast_done = asyncio.Event()
        release = asyncio.Event()

        async def slow(job: Job) -> None:
            slow_began.set()
            await release.wait()

        async def fast(job: Job) -> None:
            fast_done.set()

        root = Job("root")
        root.job("slow", initiate=True).on_start(slow)
        root.job("fast", initiate=True).on_start(fast)

        # start() schedules; it must not wait for `slow`.
        await root.start()
        await asyncio.wait_for(slow_began.wait(), 1)
        await asyncio.wait_for(fast_done.wait(), 1)

        release.set()
        await root.stop()

    async def test_stop_cancels_in_flight_hooks(self) -> None:
        cancelled = asyncio.Event()

        async def forever(job: Job) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        root = Job("root")
        root.on_start(forever)

        await root.start()
        await asyncio.sleep(0)
        await root.stop()
        self.assertTrue(cancelled.is_set())

    async def test_a_job_can_stop_itself_without_stopping_the_root(self) -> None:
        child_stopped = asyncio.Event()
        sibling_started = asyncio.Event()

        async def stop_self(job: Job) -> None:
            await job.stop(no_parent=True)

        async def child_stop(job: Job) -> None:
            child_stopped.set()

        async def sibling_start(job: Job) -> None:
            sibling_started.set()

        root = Job("root")
        child = root.job("child", initiate=True)
        child.on_start(stop_self)
        child.on_stop(child_stop)
        root.job("sibling", initiate=True).on_start(sibling_start)

        await root.start()
        await asyncio.wait_for(child_stopped.wait(), 1)
        await asyncio.wait_for(sibling_started.wait(), 1)

        # The root is still running; only the child stopped.
        self.assertFalse(root._stopped)
        await root.stop()

    async def test_start_twice_is_an_error(self) -> None:
        root = Job("root")
        await root.start()
        with self.assertRaises(RuntimeError):
            await root.start()
        await root.stop()

    async def test_stop_is_idempotent(self) -> None:
        calls = 0

        async def stopped(job: Job) -> None:
            nonlocal calls
            calls += 1

        root = Job("root")
        root.on_stop(stopped)
        await root.start()
        await root.stop()
        await root.stop()
        self.assertEqual(calls, 1)

    async def test_run_wakes_up_on_stop(self) -> None:
        root = Job("root")
        running = asyncio.create_task(root.run())
        await asyncio.sleep(0)
        await root.stop()
        await asyncio.wait_for(running, 1)

    async def test_status_reports_the_state_tree(self) -> None:
        release = asyncio.Event()

        async def wait(job: Job) -> None:
            await release.wait()

        root = Job("root")
        parent = root.job("parent")
        parent.on_start(wait)
        parent.job("child")

        self.assertEqual(root.status()["state"], "pending")
        await root.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        status = root.status()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["children"][0]["state"], "starting")
        self.assertEqual(status["children"][0]["children"][0]["state"], "pending")
        self.assertEqual(
            root.format_status().splitlines(),
            ["root: ready", "  parent: starting tasks=2", "    child: pending tasks=1"],
        )

        release.set()
        await root.stop()
        self.assertEqual(
            root.format_status().splitlines(),
            ["root: stopped", "  parent: stopped", "    child: stopped"],
        )
