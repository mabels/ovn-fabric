from __future__ import annotations

import asyncio
from typing import Any

DEPENDENCY_TIMEOUT = 180.0


class DepWaiter:
    """Deployer-side dependency waiter, installed as a job's ctx. The
    runtime only knows ctx and on_emit; the job's on_emit filter feeds
    every event to waitfor(), which fires the action once all deps are
    seen."""

    def __init__(self, dependencies: list[dict[str, Any]]) -> None:
        self._dependencies = {self._key(dep) for dep in dependencies}
        self._seen: set[tuple[Any, ...]] = set()
        self._fired = False

    @staticmethod
    def _key(dep: object) -> tuple[Any, ...]:
        if isinstance(dep, dict):
            kind = dep.get("kind")
            if kind == "abstract":
                return ("abstract", dep.get("name"))
            if kind == "docker-image":
                return ("docker-image", dep.get("image"))
            if kind in ("apt-dep", "apt-pkg"):
                return ("apt-dep", dep.get("package") or dep.get("name"))
            if kind in ("sys-cmd-dep", "sys-cmd"):
                return ("sys-cmd-dep", dep.get("name"))
        return ("unknown", repr(dep))

    def names(self) -> list[str]:
        return sorted(":".join(str(part) for part in key) for key in self._dependencies)

    def missing(self) -> list[str]:
        return sorted(
            ":".join(str(part) for part in key) for key in self._dependencies - self._seen
        )

    async def waitfor(self, event: object, action: Any) -> None:
        if self._fired:
            return
        if self._dependencies <= self._seen:
            self._fired = True
            await action()
            return
        if isinstance(event, dict):
            kind = event.get("kind")
            key: tuple[Any, ...] | None = None
            if kind == "dependency" and isinstance(event.get("dep"), dict):
                key = self._key(event["dep"])
            elif kind == "apt-dep":
                key = ("apt-dep", event.get("package"))
            elif kind == "sys-cmd-dep":
                key = ("sys-cmd-dep", event.get("name"))
            elif kind == "abstract-dep":
                key = ("abstract", event.get("name"))
            if key is not None and key in self._dependencies:
                self._seen.add(key)
        if not self._dependencies or self._dependencies <= self._seen:
            self._fired = True
            await action()


def register(
    job: Any,
    dependencies: list[dict[str, Any]],
    action: Any,
    keep_alive: bool = False,
    timeout: float = DEPENDENCY_TIMEOUT,
) -> None:
    waiter = DepWaiter(dependencies)
    job.ctx = waiter
    job.hold()

    async def on_event(job: Any, event: object, ctx: object = None) -> None:
        async def run() -> None:
            if waiter.names():
                job.info(f"dependencies fulfilled: {', '.join(waiter.names())}")
            try:
                await action(job)
            except BaseException:
                job.release()
                raise
            if keep_alive:
                job.hold()
            else:
                job.release()

        await waiter.waitfor(event, run)

    job.on_emit(on_event)

    async def dependency_timeout() -> None:
        if timeout <= 0:
            return
        await asyncio.sleep(timeout)
        if not waiter._fired and not job._stopped:
            # Not fatal: an image pull/build may legitimately take longer.
            # Say what is still missing; the job keeps waiting.
            job.warn(f"still waiting after {timeout:g}s for: {', '.join(waiter.missing())}")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    asyncio.create_task(dependency_timeout())


def package_waiters(host: str, bidirectional_deps: dict[str, Any]) -> dict[str, DepWaiter]:
    waiters: dict[str, DepWaiter] = {}
    for name, entry in bidirectional_deps.get(host, {}).items():
        if entry["abstract"].get("kind") != "abstract":
            continue
        waiters[name] = DepWaiter(entry["concrete"])
    return waiters
