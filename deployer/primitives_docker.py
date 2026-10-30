from __future__ import annotations

import dataclasses

from .lifecycle import (
    Command,
    CommandPrimitive,
    Primitive,
    RawNodeTemplate,
)
from .primitives_host import NetnsPrimitive


@dataclasses.dataclass(frozen=True)
class DockerImage(CommandPrimitive):
    """A locally built image. The Dockerfile is written by the caller;
    this primitive creates the build context and runs `docker build`."""

    host: str
    image: str
    dockerfile: str
    context: str

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
                Command(f"image:{self.image} (context)", ("mkdir", "-p", self.context)),
                Command(
                    f"image:{self.image} (build)",
                    (
                        "/usr/bin/docker",
                        "build",
                        "-t",
                        self.image,
                        "-f",
                        self.dockerfile,
                        self.context,
                    ),
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
                Command(f"image:{self.image} (pull)", ("/usr/bin/docker", "pull", self.image)),
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

    @property
    def key(self) -> str:
        return f"docker.container:{self.name}"

    def get_netns(self) -> NetnsPrimitive:
        """The container's netns, owned by Docker. Use it for
        `add_addr`/`add_route`/`add_rule`; it never creates or deletes
        the namespace itself."""
        return NetnsPrimitive(host=self.host, name=self.name, owned=False)

    def _run_argv(self) -> tuple[str, ...]:
        argv = ["/usr/bin/docker", "run", "-d"]
        if self.privileged:
            argv.append("--privileged")
        argv += ["--network", self.network, "--name", self.name]
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
        for host_path, _container_path in self.volumes:
            create.append(
                Command(
                    f"docker:{self.name} (mkdir {host_path})",
                    ("mkdir", "-p", host_path),
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
