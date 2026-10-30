from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import os
import shlex
import shutil
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .context import DEFAULT_ROOT, Ctx

SNAPSHOT_DIR = "snapshots"
LATEST_LINK = "rawIR.snapshot"


class LifecycleAction(enum.Enum):
    CREATE = "create"
    DELETE = "delete"
    # The lazy phase of the same declarations: only the operations queued
    # with `lazy=True` (image pulls/builds), run once by the lazy-create
    # unit that CREATE starts, and removed only by an explicit delete.
    LAZY_CREATE = "lazy-create"
    LAZY_DELETE = "lazy-delete"
    RUN = "run"

    @property
    def creating(self) -> bool:
        return self in (LifecycleAction.CREATE, LifecycleAction.LAZY_CREATE)

    @property
    def lazy(self) -> bool:
        return self in (LifecycleAction.LAZY_CREATE, LifecycleAction.LAZY_DELETE)

    @property
    def direction(self) -> LifecycleAction:
        """CREATE or DELETE: what an operation's create/delete/describe
        sees, whichever phase runs it."""
        return LifecycleAction.CREATE if self.creating else LifecycleAction.DELETE


class Operation(Protocol):
    """What the LifecycleController queues. The controller picks the
    method: `create` on CREATE (queue forward), `delete` on DELETE (queue
    reversed). An operation never looks at the action itself.

    `create` must fail when the object already exists. `delete` is best
    effort: the controller logs its failures and keeps going."""

    async def create(self, lc: LifecycleController) -> None: ...

    async def delete(self, lc: LifecycleController) -> None: ...

    def describe(self, action: LifecycleAction) -> list[str]: ...


class JobLike(Protocol):
    """The part of runtime.service.Job a controller uses."""

    async def run_cmd(self, label: str, cmd: list[str], abort_on_error: bool, /) -> bool: ...

    async def capture_cmd(self, label: str, cmd: list[str], abort_on_error: bool, /) -> str: ...

    def info(self, message: str, /) -> None: ...

    def warn(self, message: str, /) -> None: ...


@dataclasses.dataclass(frozen=True)
class RuntimeRef:
    """A value produced by a queued capture (chassis system-id, docker
    PID, ...), read from the run-wide store (`ctx.store`) just before the
    consuming operation runs. A value nobody produced resolves to None.

    `key` selects one field when the capture publishes a result object,
    e.g. `{"CHASSIS_ID": "..."}`. `ref["CHASSIS_ID"]` builds that
    keyed reference."""

    name: str
    key: str | None = None

    def __getitem__(self, key: str) -> RuntimeRef:
        return RuntimeRef(self.name, key)


@dataclasses.dataclass(frozen=True)
class Command:
    label: str
    argv: tuple[str, ...]
    optional: bool = False


@dataclasses.dataclass(frozen=True)
class RawNodeTemplate:
    id: str
    kind: str
    fields: dict[str, Any]


RuntimeProbe = Callable[["LifecycleController"], Awaitable[Mapping[str, Any] | None]]


def _resolve(value: Any, store: Mapping[str, Any]) -> Any:
    if isinstance(value, RuntimeRef):
        result = store.get(value.name)
        if value.key is None:
            return result
        if isinstance(result, Mapping):
            return result.get(value.key)
        return None
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        changes = {
            field.name: _resolve(getattr(value, field.name), store)
            for field in dataclasses.fields(value)
        }
        return dataclasses.replace(value, **changes)
    if isinstance(value, Mapping):
        return {key: _resolve(item, store) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_resolve(item, store) for item in value)
    if isinstance(value, list):
        return [_resolve(item, store) for item in value]
    return value


def _display(value: Any) -> str:
    if isinstance(value, RuntimeRef):
        return f"<{value.name}>" if value.key is None else f"<{value.name}[{value.key}]>"
    if value is None:
        return ""
    return str(value)


def _argv_line(argv: Sequence[Any]) -> str:
    return shlex.join(_display(token) for token in argv)


def _with_newline(content: str) -> str:
    return content + ("" if content.endswith("\n") else "\n")


def _write_new(path: Path, content: str, mode: int | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # "x": an existing file is a pre-existing object, and CREATE fails.
    with path.open("x", encoding="utf-8") as out:
        out.write(_with_newline(content))
    if mode is not None:
        path.chmod(mode)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


@dataclasses.dataclass(frozen=True)
class Primitive:
    """The generic command-backed object: CREATE runs `create_cmds`,
    DELETE runs every one of `delete_cmds` (each failure only warns).
    `nodes` are its raw-graph reflection; `probe` is kept for the coming
    object graph and is not run today."""

    key: str
    nodes: tuple[RawNodeTemplate, ...]
    create_cmds: tuple[Command, ...]
    delete_cmds: tuple[Command, ...]
    probe: RuntimeProbe | None = None

    async def create(self, lc: LifecycleController) -> None:
        for command in self.create_cmds:
            await lc.run_cmd(command.label, command.argv, optional=command.optional)
        lc.graph.upsert(self.key, self.nodes)

    async def delete(self, lc: LifecycleController) -> None:
        for command in self.delete_cmds:
            await lc.run_cmd(command.label, command.argv, optional=command.optional)
        lc.graph.remove(self.key)

    def describe(self, action: LifecycleAction) -> list[str]:
        commands = self.create_cmds if action is LifecycleAction.CREATE else self.delete_cmds
        return [_argv_line(command.argv) for command in commands]


class CommandPrimitive:
    """Base of every typed primitive that renders to one `Primitive`
    (`as_primitive()`): its create/delete/describe are that Primitive's."""

    def as_primitive(self) -> Primitive:
        raise NotImplementedError

    async def create(self, lc: LifecycleController) -> None:
        await self.as_primitive().create(lc)

    async def delete(self, lc: LifecycleController) -> None:
        await self.as_primitive().delete(lc)

    def describe(self, action: LifecycleAction) -> list[str]:
        return self.as_primitive().describe(action)


@dataclasses.dataclass(frozen=True)
class InstallSelfOperation:
    """Copy THIS deployer to `path`, so the fabric service can re-run it
    at boot. Inside a PEX the executable the operator invoked
    (sys.argv[0]) is the real file."""

    path: str

    async def create(self, lc: LifecycleController) -> None:
        src = sys.argv[0] if sys.argv else ""
        if not src or not os.path.isfile(src):
            lc.warn(f"cannot install {self.path}: run the deployer from a file, not stdin")
            return
        target = Path(self.path)
        if os.path.abspath(src) == os.path.abspath(target):
            return
        if os.path.lexists(target):
            raise FileExistsError(f"install_self: {target} already exists")
        await asyncio.to_thread(_install_copy, src, target)

    async def delete(self, lc: LifecycleController) -> None:
        await asyncio.to_thread(_unlink, Path(self.path))

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return [f"install_self {self.path}"]
        return [f"rm -f {self.path}"]


def _install_copy(src: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    target.chmod(0o755)


@dataclasses.dataclass(frozen=True)
class DaemonReloadOperation:
    """`systemctl daemon-reload` — after units are written (CREATE) and,
    reversed, after they are removed (DELETE)."""

    async def create(self, lc: LifecycleController) -> None:
        await self._reload(lc)

    async def delete(self, lc: LifecycleController) -> None:
        await self._reload(lc)

    async def _reload(self, lc: LifecycleController) -> None:
        await lc.run_cmd("systemctl daemon-reload", ["systemctl", "daemon-reload"], optional=True)

    def describe(self, action: LifecycleAction) -> list[str]:
        return ["systemctl daemon-reload"]


@dataclasses.dataclass(frozen=True)
class SystemdUnitOperation:
    unit: str
    start: bool = True
    restart: bool = False

    async def create(self, lc: LifecycleController) -> None:
        for argv in self._create_argvs():
            await lc.run_cmd(shlex.join(argv), argv, optional=True)

    async def delete(self, lc: LifecycleController) -> None:
        argv = ["systemctl", "disable", "--now", self.unit]
        await lc.run_cmd(shlex.join(argv), argv, optional=True)

    def _create_argvs(self) -> list[list[str]]:
        if not self.start:
            return [["systemctl", "enable", self.unit]]
        argvs = [["systemctl", "enable", "--now", self.unit]]
        if self.restart:
            argvs.append(["systemctl", "restart", self.unit])
        return argvs

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return [shlex.join(argv) for argv in self._create_argvs()]
        return [f"systemctl disable --now {self.unit}"]


@dataclasses.dataclass(frozen=True)
class SystemdStartOperation:
    """Start a (not enabled) unit without waiting for it: CREATE queues
    `systemctl start --no-block`; DELETE stops it if it is still running."""

    unit: str
    start: bool = True

    async def create(self, lc: LifecycleController) -> None:
        if self.start:
            argv = ["systemctl", "start", "--no-block", self.unit]
            await lc.run_cmd(shlex.join(argv), argv)

    async def delete(self, lc: LifecycleController) -> None:
        argv = ["systemctl", "stop", self.unit]
        await lc.run_cmd(shlex.join(argv), argv, optional=True)

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return [f"systemctl start --no-block {self.unit}"] if self.start else []
        return [f"systemctl stop {self.unit}"]


@dataclasses.dataclass(frozen=True)
class WriteFileOperation:
    """A file this deployer owns: CREATE writes it (and fails when it is
    already there), DELETE removes it."""

    path: str
    content: str
    mode: int | None = None

    async def create(self, lc: LifecycleController) -> None:
        await asyncio.to_thread(_write_new, Path(self.path), self.content, self.mode)

    async def delete(self, lc: LifecycleController) -> None:
        await asyncio.to_thread(_unlink, Path(self.path))

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return [f"write_file {self.path} ({len(self.content.splitlines())} lines)"]
        return [f"rm -f {self.path}"]


@dataclasses.dataclass(frozen=True)
class SymlinkNetnsOperation:
    """`/var/run/netns/<container>` -> `/proc/<pid>/ns/net`."""

    container: str
    pid: Any

    @property
    def path(self) -> Path:
        return Path("/var/run/netns") / self.container

    async def create(self, lc: LifecycleController) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.symlink_to(Path(f"/proc/{self.pid}/ns/net"))

    async def delete(self, lc: LifecycleController) -> None:
        _unlink(self.path)

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            return [f"ln -s /proc/{_display(self.pid)}/ns/net {self.path}"]
        return [f"rm -f {self.path}"]


@dataclasses.dataclass(frozen=True)
class CaptureOperation:
    """A queued capture. CREATE stores the value under `name` in the
    run-wide store; DELETE does nothing (the value is only needed to
    build create commands)."""

    name: str
    label: str
    argv: tuple[str, ...]
    optional: bool = False
    retries: int = 0
    restart_unit: str | None = None
    result_key: str | None = None

    async def create(self, lc: LifecycleController) -> None:
        for attempt in range(self.retries + 1):
            value = await lc.capture_cmd(self.label, self.argv, optional=self.optional)
            if value:
                lc.ctx.store[self.name] = (
                    {self.result_key: value} if self.result_key is not None else value
                )
                return
            if attempt < self.retries:
                if self.restart_unit is not None:
                    await lc.run_cmd(
                        self.label,
                        ["systemctl", "restart", self.restart_unit],
                        optional=True,
                    )
                await asyncio.sleep(1)
        lc.ctx.store[self.name] = None

    async def delete(self, lc: LifecycleController) -> None:
        return None

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is not LifecycleAction.CREATE:
            return []
        return [f"capture {self.name}: {_argv_line(self.argv)}"]


StepFn = Callable[["LifecycleController"], Awaitable[None]]


class Step:
    """TRANSITIONAL: a generated helper call (docker attach, zerotier,
    image build, ...) queued in its place, so it runs in order with the
    primitives around it: `Step(label, create=..., delete=...)`. Each one
    is replaced by a typed primitive."""

    def __init__(
        self, label: str, *, create: StepFn | None = None, delete: StepFn | None = None
    ) -> None:
        # Stored privately: `create`/`delete` are the Operation methods.
        self.label = label
        self._create = create
        self._delete = delete

    async def create(self, lc: LifecycleController) -> None:
        if self._create is not None:
            await self._create(lc)

    async def delete(self, lc: LifecycleController) -> None:
        if self._delete is not None:
            await self._delete(lc)

    def describe(self, action: LifecycleAction) -> list[str]:
        fn = self._create if action is LifecycleAction.CREATE else self._delete
        return [] if fn is None else [f"step {self.label}"]


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name)) for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _refs(node: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in node.items():
        if key.endswith("Ref") and isinstance(value, str):
            out.append(value)
        elif key.endswith("Refs") and isinstance(value, list):
            out.extend(item for item in value if isinstance(item, str))
    return out


class RawGraphBuilder:
    def __init__(self, host: str) -> None:
        self.host = host
        self.nodes: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        key: str,
        templates: Sequence[RawNodeTemplate],
        runtime: Mapping[str, Any] | None = None,
    ) -> None:
        for template in templates:
            node = {
                "id": template.id,
                "kind": template.kind,
                "host": self.host,
                **template.fields,
            }
            if runtime is not None:
                node["runtime"] = dict(runtime)
            self.nodes[template.id] = node

    def remove(self, key: str) -> None:
        self.nodes.pop(key, None)

    def validate(self) -> None:
        for node in self.nodes.values():
            for ref in _refs(node):
                if ref not in self.nodes:
                    raise ValueError(f"raw graph: {node['id']} references missing {ref!r}")

    def snapshot(self) -> dict[str, Any]:
        self.validate()
        return {
            "host": self.host,
            "time": datetime.now(timezone.utc).isoformat(),
            "collections": [self.nodes[key] for key in sorted(self.nodes)],
        }

    def write(
        self,
        root: Path = DEFAULT_ROOT,
        now: datetime | None = None,
    ) -> Path:
        directory = root / SNAPSHOT_DIR
        directory.mkdir(parents=True, exist_ok=True)
        stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S.%fZ")
        target = directory / f"{stamp}-rawIR.snapshot"
        tmp = target.with_suffix(".snapshot.tmp")
        tmp.write_text(
            json.dumps(
                _jsonable(self.snapshot()),
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n"
        )
        os.replace(tmp, target)
        target.chmod(0o644)

        link = root / LATEST_LINK
        link_tmp = link.with_name(link.name + ".tmp")
        if link_tmp.exists() or link_tmp.is_symlink():
            link_tmp.unlink()
        link_tmp.symlink_to(Path(SNAPSHOT_DIR) / target.name)
        os.replace(link_tmp, link)
        return target


class LifecycleController:
    """Owns the order of execution and the error policy of one queue.

    Generated code only declares: `await lc.run(op)` queues `op`, and
    `await lc.execute()` runs the queue — forward with each op's `create`
    on CREATE, reversed with each op's `delete` on DELETE. CREATE aborts
    on the first failure; DELETE logs every failure and keeps going.
    With `ctx.opts.dry_run` nothing runs: the queue is described.

    Bulk scope operations — replacing a NAT set per external IP, pruning
    an ACL set per switch/direction, deleting drifted objects — belong
    here, driven by the plan. They are deliberately not modelled as
    object primitives: they have no single semantic key, no raw node, and
    no object inverse.
    """

    def __init__(
        self,
        ctx: Ctx,
        job: JobLike,
        action: LifecycleAction,
        host: str,
        *,
        name: str | None = None,
    ) -> None:
        if action is LifecycleAction.RUN:
            raise ValueError("a LifecycleController runs CREATE/DELETE or their lazy phase")
        self.ctx = ctx
        self.job = job
        self.action = action
        self.graph = RawGraphBuilder(host)
        self.name = name or host
        self.queue: list[Operation] = []
        # Parallel to `queue`: was the operation queued with `lazy=True`?
        self.lazy: list[bool] = []

    @property
    def dry_run(self) -> bool:
        return self.ctx.opts.dry_run

    def info(self, message: str) -> None:
        self.job.info(message)

    def warn(self, message: str) -> None:
        self.job.warn(message)

    # -- what operations call while they execute -------------------------

    async def run_cmd(self, label: str, argv: Sequence[str], *, optional: bool = False) -> bool:
        # The error policy: only a non-optional command on CREATE aborts.
        abort = self.action.creating and not optional
        return await self.job.run_cmd(label, list(argv), abort)

    async def capture_cmd(self, label: str, argv: Sequence[str], *, optional: bool = False) -> str:
        abort = self.action.creating and not optional
        return await self.job.capture_cmd(label, list(argv), abort)

    # -- what generated code calls: declaration only ---------------------

    async def run(self, operation: Operation, *, lazy: bool = False) -> None:
        """Queue an operation; nothing executes until `execute()`. A
        `lazy` operation runs only in the lazy phase (LAZY_CREATE /
        LAZY_DELETE), every other one only in CREATE / DELETE."""
        self.queue.append(operation)
        self.lazy.append(lazy)

    def capture(
        self,
        name: str,
        label: str,
        argv: Sequence[str],
        *,
        optional: bool = False,
    ) -> RuntimeRef:
        self.queue.append(CaptureOperation(name, label, tuple(argv), optional))
        self.lazy.append(False)
        return RuntimeRef(name)

    def wait_capture(
        self,
        name: str,
        label: str,
        argv: Sequence[str],
        retries: int = 30,
        restart_unit: str | None = "ovn-controller",
        result_key: str | None = None,
    ) -> RuntimeRef:
        self.queue.append(
            CaptureOperation(
                name,
                label,
                tuple(argv),
                False,
                retries,
                restart_unit,
                result_key,
            )
        )
        self.lazy.append(False)
        return RuntimeRef(name, result_key)

    def result(self, name: str) -> Any:
        """The published result object of a capture, e.g.
        `{"CHASSIS_ID": "..."}`."""
        return self.ctx.store.get(name)

    # -- execution -------------------------------------------------------

    def _ordered(self) -> list[Operation]:
        # This phase's operations: forward when creating, rewound when
        # deleting.
        operations = [
            operation
            for operation, lazy in zip(self.queue, self.lazy, strict=True)
            if lazy is self.action.lazy
        ]
        if not self.action.creating:
            operations.reverse()
        return operations

    def _operation_name(self, operation: Operation) -> str:
        key = getattr(operation, "key", None)
        if isinstance(key, str):
            return f"{type(operation).__name__}[{key}]"
        return type(operation).__name__

    def plan_lines(self) -> list[str]:
        """The commands a real run would execute, in order. Captures are
        shown as placeholders; dry-run never resolves them."""
        out: list[str] = []
        for operation in self._ordered():
            prefix = f"{self.name} --- {self._operation_name(operation)} --- "
            # One line per operation at least, so a CREATE and a DELETE
            # listing of the same queue mirror each other line for line.
            lines = operation.describe(self.action.direction) or ["(nothing to run)"]
            out.extend(prefix + line for line in lines)
        return out

    def write_plan(self, path: Path | None = None) -> Path:
        target = path or (
            self.ctx.opts.state_dir / "queues" / f"{self.graph.host}-{self.action.value}.txt"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(self.plan_lines()) + "\n")
        target.chmod(0o644)
        return target

    def queue_dump(self) -> list[dict[str, Any]]:
        """A JSON-friendly view of the pending operations, in queue
        order. DELETE walks this list in reverse."""
        out: list[dict[str, Any]] = []
        for item in self.queue:
            entry: dict[str, Any] = {"type": type(item).__name__}
            if isinstance(item, Primitive):
                entry.update(
                    {
                        "key": item.key,
                        "create": [list(command.argv) for command in item.create_cmds],
                        "delete": [list(command.argv) for command in item.delete_cmds],
                        "nodes": [node.id for node in item.nodes],
                    }
                )
            else:
                entry["repr"] = repr(item)
            out.append(entry)
        return out

    def write_queue(self, path: Path | None = None) -> Path:
        target = path or (
            self.ctx.opts.state_dir / "queues" / f"{self.graph.host}-{self.action.value}.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"host": self.graph.host, "queue": self.queue_dump()},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        target.chmod(0o644)
        self.warn(f"queue dumped to {target}")
        return target

    async def execute(self) -> None:
        """Run the queue: forward on CREATE, reversed on DELETE."""
        if self.dry_run:
            for line in self.plan_lines():
                print(line)
            return
        for operation in self._ordered():
            resolved = _resolve(operation, self.ctx.store)
            described = operation.describe(self.action.direction)
            if described:
                self.info(f"{self.action.value} {self._operation_name(operation)}: {described[0]}")
            if self.action.creating:
                await resolved.create(self)
                continue
            try:
                await resolved.delete(self)
            except Exception as exc:  # noqa: BLE001 — DELETE is best effort
                self.warn(f"{self.name}: {self._operation_name(operation)}: {exc}")
