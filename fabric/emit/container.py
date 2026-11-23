"""Starting a kernel router's owning container."""

from __future__ import annotations

import posixpath
from collections.abc import Sequence

from fabric.decide import Decide
from fabric.primitives.docker import DockerPrimitive, files_hash
from fabric.primitives.host import HostPrimitive
from protocol import generated as pt

from .emitter import Emitter

# What a container may do beyond Docker's defaults (SecurityEnable,
# src/types.ts) — the concrete docker flags per abstract permission. A
# container gets nothing else: no --privileged.
_SECURITY_FLAGS: dict[str, tuple[str, ...]] = {
    # A VPN client making its own tun device (tailscaled, zerotier-one).
    "TUN": ("--cap-add", "NET_ADMIN", "--device", "/dev/net/tun"),
    # Configuring the netns it lives in (dhcpcd owning a router's links).
    "NET_CONFIG": ("--cap-add", "NET_ADMIN"),
}


def _security_flags(enable: Sequence[pt.SecurityEnableEnum] | None) -> tuple[str, ...]:
    """The docker flags for what a container may do (each once, sorted)."""
    flags: list[str] = []
    for permission in sorted({e.value for e in enable or ()}):
        flags += _SECURITY_FLAGS[permission]
    return tuple(flags)


def _start_owning_container(
    app: pt.DockerApp, container: str, host: HostPrimitive, unit: Emitter
) -> DockerPrimitive:
    """Its image, state and container, up to its named netns: the router's
    interfaces are made right there (the same way as a workload's NICs)."""
    image = app.image or f"ovn-fabric-{container}"
    volumes = [
        {"host": volume.host, "container": volume.container} for volume in (app.volumes or [])
    ]
    identity = (
        {"public": app.identity.public, "secret": app.identity.secret} if app.identity else None
    )
    # (Its image: a unit of its own before this one, _emit_app_image.)
    # The app owns its volume directories: DELETE removes them with whatever
    # the app wrote into them.
    for volume in volumes:
        unit.ovn(host.state_dir(volume["host"]))
    if identity is not None:
        state_dir = next(
            (
                volume["host"]
                for volume in volumes
                if volume["container"] == "/var/lib/zerotier-one"
            ),
            None,
        )
        if state_dir is not None:
            public = f"{state_dir}/identity.public"
            secret = f"{state_dir}/identity.secret"
            unit.write_file(public, identity["public"], 0o644, None, None)
            unit.write_file(secret, identity["secret"], 0o600, None, None)
    state_files = list(app.stateFiles or [])
    for state_file in state_files:
        directory = posixpath.dirname(state_file.path)
        if directory not in {volume["host"] for volume in volumes}:
            unit.ovn(host.state_dir(directory))
        # Its mode and owner are set before it is in place (a key is never
        # readable by others, not even for a moment).
        mode = int(state_file.mode, 8) if state_file.mode is not None else None
        owner = (state_file.user, state_file.group)
        if state_file.content is not None and state_file.keep:
            # A seed (a tailscaled.state): written only where there is
            # none — the app keeps it up to date itself.
            unit.seed(state_file.path, state_file.content, mode, *owner)
        elif state_file.content is not None:
            unit.write_file(state_file.path, state_file.content, mode, *owner)
        else:
            # The app writes into it: not written over, whatever a run finds.
            unit.ovn(host.touch(state_file.path), decide=Decide.always)
    docker = host.docker(
        container,
        image,
        command=("sleep", "infinity"),
        volumes=tuple((volume["host"], volume["container"]) for volume in volumes),
        run_flags=(*_security_flags(app.securityEnable), *(app.run or ())),
        # Its natural key: the node address, the public part of its identity.
        natural_key=f"zt:{identity['public'].split(':')[0]}" if identity else None,
        # A changed identity or state file needs a restart: the daemon read it.
        # A seed is not: it is not written over a running app's own.
        files=files_hash(
            *(identity.values() if identity else ()),
            *(f"{f.path}={f.content}" for f in state_files if not f.keep),
        ),
    )
    # Running from its image with what it should run with: run leaves it;
    # else it is restarted, and the unit's later steps run again
    # (fabric/decide.py).
    unit.ovn(docker, bind=unit.local(container), decide=Decide.docker_container)
    pid_var = unit.local(f"{container}-pid")
    unit.capture(pid_var, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", container])
    unit.netns_symlink(repr(container), pid_var)
    return docker
