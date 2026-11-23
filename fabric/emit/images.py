"""Docker images: Dockerfiles, image units, and the host's image jobs."""

from __future__ import annotations

import hashlib
from typing import Any

from fabric.decide import Decide
from fabric.primitives.host import HostPrimitive
from protocol import generated as pt

from .emitter import Emitter
from .lookups import _kernel_router_owners, _kernel_router_sides


def _docker_dockerfile(build: pt.DockerBuild) -> str:
    """The Dockerfile body for a docker app's optional `build` — a full
    `dockerfile` wins; otherwise FROM <from> + install the packages with the
    manager the base image implies (apk for alpine/arch, apt otherwise)
    (2026-08-31)."""
    if build.dockerfile:
        return build.dockerfile
    pkgs = " ".join(build.packages or [])
    base = build.from_
    if "alpine" in base or "arch" in base:
        return f"FROM {base}\nRUN apk add --no-cache {pkgs}"
    return f"FROM {base}\nRUN apt-get update && apt-get install -y {pkgs}"


def _emit_app_image(app: pt.DockerApp, container: str, host: HostPrimitive, emit: Emitter) -> None:
    """A router app's image, as a unit of its own (kind `image`)."""
    image = app.image or f"ovn-fabric-{container}"
    dockerfile = _docker_dockerfile(app.build) if app.build else None
    _emit_image(
        host,
        image,
        dockerfile,
        f"/var/lib/ovn-fabric/build/{container}",
        emit.unit("image", image),
    )


def _emit_image(
    host: HostPrimitive, image: str, dockerfile: str | None, build_dir: str, emit: Emitter
) -> None:
    """A container's image: built from its Dockerfile (context dir,
    Dockerfile, build) or pulled from the registry."""
    if dockerfile is None:
        # Pulled while run decides: its container compares with it.
        emit.ovn(host.docker_pull(image), decide=Decide.docker_pull)
        return
    dockerfile_path = f"{build_dir}/Dockerfile"
    emit.ovn(host.make_dir(build_dir))
    emit.append(dockerfile_path, dockerfile)
    # Built anew only when its Dockerfile changed (its label).
    spec = hashlib.sha256(dockerfile.encode()).hexdigest()
    emit.ovn(host.docker_image(image, dockerfile_path, build_dir, spec), decide=Decide.docker_image)


def _image_jobs(host_id: str, nodes: list[pt.Model]) -> list[dict[str, Any]]:
    """One install entry per docker image a unit on this host runs — a
    kernel router's docker apps and the host's workload containers — with
    the Dockerfile to build it from when the IR declares a `build` (else
    it is pulled)."""
    by_name: dict[str, dict[str, Any]] = {}

    def add(image: str, build: pt.DockerBuild | None) -> None:
        by_name.setdefault(
            image,
            {
                "kind": "image",
                "image": image,
                "dockerfile": _docker_dockerfile(build) if build else None,
            },
        )

    for owner in _kernel_router_owners(host_id, nodes):
        for side_node in _kernel_router_sides(owner.key.name, nodes):
            for app in side_node.data.apps or []:
                if app.kind == "docker":
                    container = app.name or f"{owner.key.name}-docker"
                    add(app.image or f"ovn-fabric-{container}", app.build)
    for node in nodes:
        if node.kind == "kernel.app.container" and node.data.host == host_id:
            add(node.data.image, node.data.build)
    return [by_name[name] for name in sorted(by_name)]
