from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Sequence

from ..lifecycle import (
    Command,
    CommandPrimitive,
    Primitive,
    RawNodeTemplate,
)
from .host import NetnsPrimitive

# A build or pull may take long (a slow link, a big image) — but not forever.
IMAGE_TIMEOUT = 1800.0

# The label a container or a built image carries: the hash of what it was
# made from (fabric/decide.py compares it, a run restarts or rebuilds).
from ladops.docker import NATURAL_KEY_LABEL  # noqa: E402

SPEC_LABEL = "ovn-fabric.spec"


def _hash(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def files_hash(*contents: str) -> str:
    """What a container's `files` says about the files it gets."""
    return _hash(*contents)


@dataclasses.dataclass(frozen=True)
class DockerImage(CommandPrimitive):
    """A locally built image. The Dockerfile and the build context are
    written by the caller; this primitive runs `docker build`."""

    host: str
    image: str
    dockerfile: str
    context: str
    # A hash of what it is built from (its Dockerfile's content): its label.
    spec: str = ""

    @property
    def key(self) -> str:
        return f"docker.image:{self.image}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="docker.image",
                    fields={"image": self.image, "dockerfile": self.dockerfile},
                ),
            ),
            create_cmds=(
                Command(
                    f"image:{self.image} (build)",
                    (
                        "/usr/bin/docker",
                        "build",
                        "-t",
                        self.image,
                        "--label",
                        f"{SPEC_LABEL}={self.spec}",
                        "-f",
                        self.dockerfile,
                        self.context,
                    ),
                    timeout=IMAGE_TIMEOUT,
                ),
            ),
            delete_cmds=(
                Command(
                    f"image:{self.image} (delete)",
                    ("/usr/bin/docker", "image", "rm", self.image),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class DockerPull(CommandPrimitive):
    """A registry image this deployer pulled: create pulls it, delete
    removes it."""

    host: str
    image: str

    @property
    def key(self) -> str:
        return f"docker.image:{self.image}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(id=self.key, kind="docker.image", fields={"image": self.image}),
            ),
            create_cmds=(
                Command(
                    f"image:{self.image} (pull)",
                    ("/usr/bin/docker", "pull", self.image),
                    timeout=IMAGE_TIMEOUT,
                ),
            ),
            delete_cmds=(
                Command(
                    f"image:{self.image} (delete)",
                    ("/usr/bin/docker", "image", "rm", self.image),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class DockerPrimitive(CommandPrimitive):
    """One `docker run -d` container. The container is the object; its
    netns is reachable as `get_netns()` for addresses/routes/rules."""

    host: str
    name: str
    image: str
    command: tuple[str, ...] = ()
    privileged: bool = False
    network: str = "none"
    environment: tuple[tuple[str, str], ...] = ()
    volumes: tuple[tuple[str, str], ...] = ()
    run_flags: tuple[str, ...] = ()
    remove_existing: bool = True
    # A hash of the files it gets through its volumes (written by its unit):
    # part of its spec, so that a changed file restarts it.
    files: str = ""
    # What it is, by value (ADR 0010): a ZeroTier instance `zt:<node
    # address>`. Set as a label, so that a run finds it under any name.
    natural_key: str = ""

    @property
    def key(self) -> str:
        return f"docker.container:{self.name}"

    def get_netns(self) -> NetnsPrimitive:
        """The container's netns, owned by Docker. Use it for
        `add_addr`/`add_route`/`add_rule`; it never creates or deletes
        the namespace itself."""
        return NetnsPrimitive(host=self.host, name=self.name, owned=False)

    def exec_detached(self, argv: Sequence[str]) -> DockerExecPrimitive:
        """Start `argv` inside the running container (`docker exec -d`)."""
        return DockerExecPrimitive(host=self.host, container=self.name, argv=tuple(argv))

    @property
    def spec(self) -> str:
        """A hash of everything it is run with, its files too: its label."""
        return _hash(*self._run_argv(labelled=False), self.files)

    def _run_argv(self, labelled: bool = True) -> tuple[str, ...]:
        argv = ["/usr/bin/docker", "run", "-d"]
        if self.privileged:
            argv.append("--privileged")
        argv += ["--network", self.network, "--name", self.name]
        if labelled:
            argv += ["--label", f"{SPEC_LABEL}={self.spec}"]
            if self.natural_key:
                argv += ["--label", f"{NATURAL_KEY_LABEL}={self.natural_key}"]
        for key, value in sorted(self.environment):
            argv += ["-e", f"{key}={value}"]
        for host_path, container_path in self.volumes:
            argv += ["-v", f"{host_path}:{container_path}"]
        argv += list(self.run_flags)
        argv += [self.image, *self.command]
        return tuple(argv)

    def as_primitive(self) -> Primitive:
        create: list[Command] = []
        if self.remove_existing:
            create.append(
                Command(
                    f"docker:{self.name} (remove old)",
                    ("/usr/bin/docker", "rm", "-f", self.name),
                    optional=True,
                )
            )
        create.append(Command(f"docker:{self.name} (run)", self._run_argv()))
        return Primitive(
            key=self.key,
            nodes=(
                RawNodeTemplate(
                    id=self.key,
                    kind="docker.container",
                    fields={
                        "name": self.name,
                        "image": self.image,
                        "command": list(self.command),
                        "state": "running",
                        "netnsRef": f"netns:{self.name}",
                    },
                ),
            ),
            create_cmds=tuple(create),
            delete_cmds=(
                Command(
                    f"docker:{self.name} (delete)",
                    ("/usr/bin/docker", "rm", "-f", self.name),
                ),
            ),
        )


@dataclasses.dataclass(frozen=True)
class DockerExecPrimitive(CommandPrimitive):
    """A process started inside a running container (`docker exec -d`).
    Removing the container ends it, so DELETE does nothing."""

    host: str
    container: str
    argv: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"docker.exec:{self.container}"

    def as_primitive(self) -> Primitive:
        return Primitive(
            key=self.key,
            nodes=(),
            create_cmds=(
                Command(
                    f"{self.container} app",
                    ("/usr/bin/docker", "exec", "-d", self.container, *self.argv),
                ),
            ),
            delete_cmds=(),
        )
