"""`python3 -m rt serve` runs the runtime; the other commands ask it.

python3 -m rt serve
python3 -m rt version
python3 -m rt resolve curl jq --timeout 300
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from . import generated as g
from . import logconfig
from .message import ProtocolError, hydrate_part
from .peer import Peer
from .runtime import Runtime
from .services import docker, docker_run, sysdeps, version
from .transport import Closed, connect, dump_wire, wire_log

# The runtime's logs for a request (LogMsg), shown at their own level:
# `--log remote=debug` shows e.g. apt's output line by line.
remote_log = logging.getLogger("rt.remote")

# Relative to the working directory, until the service has a fixed place.
DEFAULT_ADDRESS = "unix://./ovn-fabric.sock"


async def _serve(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    runtime = Runtime()
    runtime.register(
        version.service(),
        sysdeps.service(),
        docker.service(runtime.client),
        docker_run.service(runtime.client, files_root=args.files_root),
    )
    logging.getLogger("rt").info("listening on %s", args.address)
    await runtime.serve(args.address, stop)
    return 0


async def _ask(address: str, body: object) -> object | None:
    """Send one request; print everything carrying its reqId as it comes;
    return the final response's body (None when it never arrived)."""
    try:
        conn = await connect(address)
    except Closed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    if wire_log.isEnabledFor(logging.DEBUG):
        conn = dump_wire(conn)
    peer = Peer(conn, "cli")
    reader = asyncio.create_task(peer.run())
    try:
        request = await peer.request(body)
        async for msg in request:
            event = msg.body
            if isinstance(event, g.LogMsg):
                remote_log.log(logconfig.LEVELS[event.level.value], "%s", event.message)
            elif isinstance(event, g.DockerContainer):
                code = f" (exit {event.exitCode})" if event.exitCode is not None else ""
                note = f" — {event.message}" if event.message else ""
                print(f"{event.name}: {event.state.value}{code}{note}", file=sys.stderr)
            elif isinstance(event, g.DockerReady):
                print("docker ready", file=sys.stderr)
            elif isinstance(event, g.ResolveSystemDep):
                note = f" — {event.message}" if event.message else ""
                print(f"{event.dep}: {event.state.value}{note}", file=sys.stderr)
        final: object = request.result.body
        if isinstance(final, g.ErrorRes):
            print(f"error: {final.code}: {final.message}", file=sys.stderr)
            return None
        return final
    except Closed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    finally:
        await peer.close()
        await reader


async def _version(args: argparse.Namespace) -> int:
    res = await _ask(args.address, g.VersionReq(kind="VersionReq"))
    if not isinstance(res, g.VersionRes):
        return 1
    print(f"runtime {res.runtime} (protocol {res.protocol})")
    return 0


async def _resolve(args: argparse.Namespace) -> int:
    req = g.ResolveSystemDepsReq(kind="ResolveSystemDepsReq", deps=args.deps, timeout=args.timeout)
    res = await _ask(args.address, req)
    if not isinstance(res, g.ResolveSystemDepsRes):
        return 1
    for status in res.deps:
        print(f"{status.dep} {status.state.value}")
    return 0 if all(status.state is g.SystemDepState.ready for status in res.deps) else 1


class SpecError(ValueError):
    """A --spec that cannot be read or does not fit."""


def _read_spec(path: str, cls: type) -> list[Any]:
    """The --spec at `path` (- for stdin): a JSON object or list of objects,
    each validated as `cls`."""
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text()
        raw = json.loads(text)
    except OSError as exc:
        raise SpecError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"{path} is not JSON: {exc}") from exc
    items = raw if isinstance(raw, list) else [raw]
    try:
        return [hydrate_part(cls, item) for item in items]
    except ProtocolError as exc:
        raise SpecError(f"{path}: {exc}") from exc


async def _docker_prepare(args: argparse.Namespace) -> int:
    specs = [g.ImageSpec(image=image) for image in args.images]
    if args.spec is not None:
        try:
            specs += _read_spec(args.spec, g.ImageSpec)
        except SpecError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if not specs:
        print("error: no images (IMAGE ... or --spec FILE)", file=sys.stderr)
        return 2
    res = await _ask(args.address, g.DockerPrepareReq(kind="DockerPrepareReq", images=specs))
    if not isinstance(res, g.DockerPrepareRes):
        return 1
    for status in res.images:
        note = f" — {status.message}" if status.message else ""
        print(f"{status.image} {status.state.value}{note}")
    return 0 if all(status.state is g.ImageState.ready for status in res.images) else 1


def _container_spec(args: argparse.Namespace) -> g.ContainerSpec | None:
    if args.spec is None:
        if args.image is None:
            print("error: --image IMAGE or --spec FILE", file=sys.stderr)
            return None
        return g.ContainerSpec(
            name=args.name or "rt-run", image=args.image, command=args.command or None
        )
    try:
        specs: list[g.ContainerSpec] = _read_spec(args.spec, g.ContainerSpec)
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    chosen = [s for s in specs if args.name is None or s.name == args.name]
    if len(chosen) != 1:
        names = ", ".join(s.name for s in specs)
        print(f"error: pick one container with --name (have: {names})", file=sys.stderr)
        return None
    return chosen[0]


async def _docker_run(args: argparse.Namespace) -> int:
    spec = _container_spec(args)
    if spec is None:
        return 2
    req = g.DockerRunReq(kind="DockerRunReq", container=spec, follow=args.follow or None)
    res = await _ask(args.address, req)
    if not isinstance(res, g.DockerRunRes):
        return 1
    if not args.follow:  # started: the runtime owns it now
        return 0 if res.state is g.ContainerState.started else 1
    if res.state is not g.ContainerState.stopped:
        return 1
    return res.exitCode or 0


def _common(parser: argparse.ArgumentParser, *, top: bool) -> None:
    # On `rt` itself with their defaults, and again on every command, so
    # both `rt --log debug serve` and `rt serve --log debug` work (a
    # command's own value wins; SUPPRESS keeps an absent one from
    # overwriting the top-level default).
    parser.add_argument(
        "--socket",
        default=DEFAULT_ADDRESS if top else argparse.SUPPRESS,
        help="a socket path or address (unix:///path, mem://name); default ./ovn-fabric.sock",
    )
    parser.add_argument(
        "--log",
        action="append",
        default=[] if top else argparse.SUPPRESS,
        metavar="[COMPONENT=]LEVEL",
        help="log level, for all or one component (rt.<component>): --log debug, "
        "--log wire=debug (every frame sent >> and received <<); repeatable, comma-separated",
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="rt")
    _common(parser, top=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the runtime")
    serve.add_argument(
        "--files-root",
        default=docker_run.FILES_ROOT,
        help="where containers' files are written and mounted from (the docker daemon must "
        f"see the same path); default {docker_run.FILES_ROOT}",
    )
    serve.set_defaults(run=_serve)
    version_cmd = commands.add_parser("version", help="ask the runtime's version")
    version_cmd.set_defaults(run=_version)
    resolve = commands.add_parser("resolve", help="make system dependencies (apt) present")
    resolve.add_argument("deps", nargs="+", metavar="PACKAGE")
    resolve.add_argument(
        "--timeout", type=float, help="seconds to wait (default: as long as it takes)"
    )
    resolve.set_defaults(run=_resolve)
    prepare = commands.add_parser(
        "docker-prepare", help="make docker usable and the images present (pull or build)"
    )
    prepare.add_argument("images", nargs="*", metavar="IMAGE", help="an image to pull")
    prepare.add_argument(
        "--spec",
        metavar="FILE",
        help='JSON list of {"image", "dockerfile"?} (- for stdin), e.g. from deployer.images',
    )
    prepare.set_defaults(run=_docker_prepare)
    run_cmd = commands.add_parser(
        "docker-run",
        help="run a container (its image prepared first); returns once it is started — "
        "the runtime owns it then; with --follow, until it stops (Ctrl-C stops it)",
    )
    run_cmd.add_argument(
        "--follow",
        action="store_true",
        help="stay with the container: its output here, wait until it stops, Ctrl-C stops it",
    )
    run_cmd.add_argument(
        "--spec",
        metavar="FILE",
        help="JSON container spec(s) (- for stdin), e.g. from deployer.containers",
    )
    run_cmd.add_argument("--name", help="the container (with --spec: which one)")
    run_cmd.add_argument("--image", help="without --spec: the image to run")
    run_cmd.add_argument("command", nargs="*", help="without --spec: the command (after --)")
    run_cmd.set_defaults(run=_docker_run)
    for command in (serve, version_cmd, resolve, prepare, run_cmd):
        _common(command, top=False)
    args = parser.parse_args()
    try:
        logconfig.configure(args.log)
    except ValueError as exc:
        parser.error(str(exc))
    args.address = args.socket if "://" in args.socket else f"unix://{args.socket}"
    try:
        code: int = asyncio.run(args.run(args))
    except KeyboardInterrupt:  # the request was ended (and its connection closed)
        return 130
    return code


if __name__ == "__main__":
    sys.exit(main())
