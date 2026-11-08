"""Who decides, in a run, whether a step is applied, skipped or replaced.

The generator picks one per primitive (`emit.ovn(primitive, decide=…)`):

- `footprint` (the default): apply decides before the unit, from the
  step's learned RawIR footprint against the live RawIR graph
  (fabric/desired.py) — found: skipped, otherwise applied;
- the others are decided by the unit executor right before the step, from
  the step's own live state — what RawIR cannot tell (the config a
  WireGuard tunnel runs with, the image a container runs from).

A decided step carries what its decider needs as a `decide` spec in the
protocol (a kind-tagged union): `SPECS` builds it from the typed
primitive when the plan is made, `DECIDERS` decides from it on the host.
Both are tables keyed by the kind; a decider gets the decisions of the
unit's earlier steps as an explicit handle (`Decided`).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
from collections.abc import Awaitable, Callable
from typing import Any

from . import generated as g
from .lifecycle import LifecycleAction, LifecycleController, Primitive, written


class Decide(enum.Enum):
    """The generator's choice for a primitive."""

    footprint = "footprint"
    always = "always"
    wireguard = "wireguard"
    docker_container = "docker.container"
    docker_image = "docker.image"
    docker_pull = "docker.pull"
    docker_exec = "docker.exec"


# -- the plan: the decide spec of a typed primitive --------------------------


def _always(primitive: Any) -> g.StepDecide:
    return g.AlwaysDecide(kind="always")


def _wireguard(primitive: Any) -> g.StepDecide:
    return g.WireguardDecide(
        kind="wireguard",
        netns=primitive.netns,
        iface=primitive.iface,
        confPath=primitive.conf_path,
        confSha256=hashlib.sha256(written(primitive.conf).encode()).hexdigest(),
    )


def _docker_container(primitive: Any) -> g.StepDecide:
    return g.DockerContainerDecide(
        kind="docker.container", name=primitive.name, image=primitive.image, spec=primitive.spec
    )


def _docker_image(primitive: Any) -> g.StepDecide:
    return g.DockerImageDecide(kind="docker.image", image=primitive.image, spec=primitive.spec)


def _docker_pull(primitive: Any) -> g.StepDecide:
    return g.DockerPullDecide(kind="docker.pull", image=primitive.image)


def _docker_exec(primitive: Any) -> g.StepDecide:
    return g.DockerExecDecide(kind="docker.exec", container=primitive.container)


SPECS: dict[Decide, Callable[[Any], g.StepDecide]] = {
    Decide.always: _always,
    Decide.wireguard: _wireguard,
    Decide.docker_container: _docker_container,
    Decide.docker_image: _docker_image,
    Decide.docker_pull: _docker_pull,
    Decide.docker_exec: _docker_exec,
}


def spec(decide: Decide, primitive: Any) -> g.StepDecide | None:
    """The `decide` spec of a step (None: by its footprint)."""
    if decide is Decide.footprint:
        return None
    return SPECS[decide](primitive)


# -- the executor: deciding on the host ---------------------------------------


class Decision(enum.Enum):
    skip = "skip"
    apply = "apply"
    replace = "replace"  # undo, then apply


@dataclasses.dataclass
class Decided:
    """The decisions of a unit's steps so far (by the decided key)."""

    by_key: dict[str, Decision] = dataclasses.field(default_factory=dict)


Decider = Callable[[Any, Decided, LifecycleController], Awaitable[Decision]]


async def _decide_always(spec: Any, decided: Decided, lc: LifecycleController) -> Decision:
    return Decision.apply


async def _decide_wireguard(
    spec: g.WireguardDecide, decided: Decided, lc: LifecycleController
) -> Decision:
    """Down: apply. Up with the config it should have: skip. Up with
    another one: replace (down, then up again with the new file)."""
    netns = ("ip", "netns", "exec", spec.netns)
    running = await lc.capture_cmd(
        f"wireguard {spec.iface} (running?)", [*netns, "wg", "show", "interfaces"], optional=True
    )
    if spec.iface not in running.split():
        return Decision.apply
    on_disk = await lc.capture_cmd(
        f"wireguard {spec.iface} (config)", ["sha256sum", spec.confPath], optional=True
    )
    same = on_disk.split()[:1] == [spec.confSha256]
    return Decision.skip if same else Decision.replace


_DOCKER = "/usr/bin/docker"
_LABEL = '{{index .Config.Labels "ovn-fabric.spec"}}'  # SPEC_LABEL (primitives/docker.py)


def _image_key(image: str) -> str:
    return f"docker.image:{image}"  # DockerImage's and DockerPull's key


async def _decide_docker_image(
    spec: g.DockerImageDecide, decided: Decided, lc: LifecycleController
) -> Decision:
    """Built from what it should be built from (its label): skip; else
    build it."""
    label = await lc.capture_cmd(
        f"image {spec.image} (built from?)",
        [_DOCKER, "image", "inspect", "-f", _LABEL, spec.image],
        optional=True,
    )
    return Decision.skip if label == spec.spec else Decision.apply


async def _decide_docker_pull(
    spec: g.DockerPullDecide, decided: Decided, lc: LifecycleController
) -> Decision:
    """Pulled now, while deciding: its container's decider compares the
    image it runs from with the one just pulled."""
    await lc.run_cmd(f"image {spec.image} (pull)", [_DOCKER, "pull", spec.image], optional=True)
    return Decision.skip


async def _decide_docker_container(
    spec: g.DockerContainerDecide, decided: Decided, lc: LifecycleController
) -> Decision:
    """Not running: apply. Running with what it should run with (its label)
    from the image there is now (pulled or built): skip. Else, or its
    image is built anew in this unit: replace (restart)."""
    running = await lc.capture_cmd(
        f"docker:{spec.name} (running?)",
        [_DOCKER, "inspect", "-f", "{{.State.Running}} {{.Image}} " + _LABEL, spec.name],
        optional=True,
    )
    state, _, rest = running.partition(" ")
    if state != "true":
        return Decision.apply
    image_id, _, label = rest.partition(" ")
    if decided.by_key.get(_image_key(spec.image)) is Decision.apply:
        return Decision.replace  # its image is built anew below
    wanted = await lc.capture_cmd(
        f"image {spec.image} (id)",
        [_DOCKER, "image", "inspect", "-f", "{{.Id}}", spec.image],
        optional=True,
    )
    same = label == spec.spec and image_id == wanted
    return Decision.skip if same else Decision.replace


async def _decide_docker_exec(
    spec: g.DockerExecDecide, decided: Decided, lc: LifecycleController
) -> Decision:
    """Started with its container: when that one was (re)started."""
    container = decided.by_key.get(f"docker.container:{spec.container}")
    fresh = container in (Decision.apply, Decision.replace)
    return Decision.apply if fresh else Decision.skip


DECIDERS: dict[str, Decider] = {
    "always": _decide_always,
    "wireguard": _decide_wireguard,
    "docker.image": _decide_docker_image,
    "docker.pull": _decide_docker_pull,
    "docker.container": _decide_docker_container,
    "docker.exec": _decide_docker_exec,
}


# Kinds whose apply or replace starts afresh what the unit's later steps
# build on (a container's netns: its addresses, routes, lent links are
# gone) — those steps run again, even where apply had found them.
STARTS_AFRESH = ("docker.container",)


async def decide(spec: Any, decided: Decided, lc: LifecycleController) -> Decision:
    return await DECIDERS[spec.kind](spec, decided, lc)


async def undo(primitive: Primitive, lc: LifecycleController) -> None:
    """A primitive's delete, best effort, in a create."""
    for command in primitive.delete_cmds:
        await lc.run_cmd(command.label, command.argv, optional=True, timeout=command.timeout)


@dataclasses.dataclass(frozen=True)
class Replaced:
    """A step decided `replace`: its undo (best effort), then its create."""

    primitive: Primitive

    @property
    def key(self) -> str:
        return self.primitive.key

    async def create(self, lc: LifecycleController) -> None:
        await undo(self.primitive, lc)
        await self.primitive.create(lc)

    async def delete(self, lc: LifecycleController) -> None:
        await self.primitive.delete(lc)

    def describe(self, action: LifecycleAction) -> list[str]:
        if action is LifecycleAction.CREATE:
            undo = self.primitive.describe(LifecycleAction.DELETE)
            return [f"(replace) {line}" for line in undo] + self.primitive.describe(action)
        return self.primitive.describe(action)
