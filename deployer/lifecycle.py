from __future__ import annotations

import dataclasses
import enum
import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

DEFAULT_ROOT = Path("/var/lib/ovn-fabric")
SNAPSHOT_DIR = "snapshots"
LATEST_LINK = "rawIR.snapshot"


class LifecycleAction(enum.Enum):
    CREATE = "create"
    DELETE = "delete"
    RUN = "run"


class Runnable(Protocol):
    async def run(self, lc: "LifecycleController") -> None: ...


class JobLike(Protocol):
    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
    ) -> bool: ...

    async def capture_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
    ) -> str: ...


@dataclasses.dataclass(frozen=True)
class Command:
    label: str
    argv: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class RawNodeTemplate:
    id: str
    kind: str
    fields: dict[str, Any]


RuntimeProbe = Callable[["LifecycleController"], Awaitable[Mapping[str, Any] | None]]


@dataclasses.dataclass(frozen=True)
class Primitive:
    key: str
    nodes: tuple[RawNodeTemplate, ...]
    create: tuple[Command, ...]
    delete: tuple[Command, ...]
    exists: Command | None = None
    probe: RuntimeProbe | None = None

    async def run(self, lc: "LifecycleController") -> None:
        if lc.action is LifecycleAction.CREATE:
            if self.exists is not None and await lc.run_cmd(
                self.exists.label, self.exists.argv, False
            ):
                runtime = await self.probe(lc) if self.probe is not None else None
                lc.graph.upsert(self.key, self.nodes, runtime)
                return
            for command in self.create:
                await lc.run_cmd(command.label, command.argv, True)
            runtime = await self.probe(lc) if self.probe is not None else None
            lc.graph.upsert(self.key, self.nodes, runtime)
        elif lc.action is LifecycleAction.DELETE:
            for command in self.delete:
                await lc.run_cmd(command.label, command.argv, False)
            lc.graph.remove(self.key)
        else:
            raise RuntimeError("a Primitive is create/delete only; use RUN primitives")


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
    """The only writer of both commands and their raw reflection."""

    def __init__(
        self,
        job: JobLike,
        action: LifecycleAction,
        host: str,
        *,
        root: Path = DEFAULT_ROOT,
    ) -> None:
        self.job = job
        self.action = action
        self.graph = RawGraphBuilder(host)
        self.root = root

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
    ) -> bool:
        return await self.job.run_cmd(label, argv, abort_on_error, background)

    async def capture_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
    ) -> str:
        return await self.job.capture_cmd(label, argv, abort_on_error)

    async def run(self, primitive: Runnable) -> None:
        await primitive.run(self)

    async def finish(self) -> Path:
        return self.graph.write(self.root)
