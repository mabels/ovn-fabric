"""`python3 -m fabric serve` runs the runtime; the other commands ask it.

python3 -m fabric serve [--host H]
python3 -m fabric version
python3 -m fabric resolve curl traceroute --timeout 300
python3 -m fabric abstract-deps docker ovn=ovn-central,ovn-host
python3 -m deployer.deps py.ir --host H | python3 -m fabric abstract-deps --spec -
python3 -m deployer.images py.ir --host H | python3 -m fabric docker-prepare --spec -
python3 -m fabric docker-run --follow --image busybox -- sh -c 'echo hi; echo oops >&2'
python3 -m deployer.containers py.ir --host H | python3 -m fabric docker-run --spec - --name N
python3 -m fabric ir-put < py.ir              # or: ir-put py.ir; --name N
python3 -m fabric ir-list
python3 -m fabric ir-get sha256:… > ir.json   # by hash or by name
python3 -m fabric ir-delete sha256:…
python3 -m fabric ir-apply create < py.ir     # store from stdin, then apply
python3 -m fabric ir-apply create --put py.ir # store from a file, then apply
python3 -m fabric ir-apply delete sha256:…    # a stored IR, by hash or name
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime import generated as rg
from runtime import logconfig
from runtime.message import Msg, ProtocolError, hydrate_part, plain
from runtime.peer import Peer
from runtime.runtime import Runtime
from runtime.transport import Closed, connect, dump_wire, wire_log

from . import generated as g
from .services import abstract_deps, apply, docker, docker_run, ir, sysdeps, version
from .services import unit as unit_executor
from .services.packages import MANAGERS

# The runtime's logs for a request (LogMsg), shown at their own level and
# named by their sender's component (`INFO sysdeps: …`, a container's name
# for its output): `--log remote=debug` shows e.g. apt's output line by line.
remote_log = logging.getLogger("fabric.remote")


def _show_remote_by_component() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(component)s: %(message)s"))
    remote_log.addHandler(handler)
    remote_log.propagate = False


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
        abstract_deps.service(runtime.client),
        docker.service(runtime.client),
        docker_run.service(runtime.client, files_root=args.files_root),
        ir.service(args.state_root),
        apply.service(runtime.client, args.host),
        unit_executor.service(args.host),
    )
    logging.getLogger("fabric").info("listening on %s", args.address)
    await runtime.serve(args.address, stop)
    return 0


DEBUG, INFO, WARNING, ERROR = logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR


def _threshold(component: str) -> int:
    """The level from which a component's lines are shown: its own --log
    level (`--log NIC_management-v2_on_br-d0868885=debug`), else remote's."""
    return logconfig.COMPONENTS.get(component, remote_log.getEffectiveLevel())


def _show(level: int, component: str, message: str, *, force: bool = False) -> None:
    """One line on stderr, in the one format: `LEVEL component: message`."""
    if force or level >= _threshold(component):
        record = remote_log.makeRecord(
            remote_log.name, level, "", 0, "%s", (message,), None, extra={"component": component}
        )
        remote_log.handle(record)


# -- how the CLI shows what a request emits: one entry per message kind --------
# `hidden` holds the lines of the running unit of work that were not shown
# (its debug): shown should it fail.

Hidden = list[tuple[int, str, str]]


def _show_log(msg: Msg[Any], hidden: Hidden) -> None:
    event: rg.LogMsg = msg.body
    component = event.component or msg.header.source
    level = logconfig.LEVELS[event.level.value]
    if level < _threshold(component):
        hidden.append((level, component, event.message))
    _show(level, component, event.message)


def _show_container(msg: Msg[Any], hidden: Hidden) -> None:
    event: g.DockerContainer = msg.body
    code = f" (exit {event.exitCode})" if event.exitCode is not None else ""
    note = f" — {event.message}" if event.message else ""
    failed = event.state is g.ContainerState.failed
    _show(WARNING if failed else INFO, "cli", f"{event.name}: {event.state.value}{code}{note}")


def _show_abstract_dep(msg: Msg[Any], hidden: Hidden) -> None:
    event: g.AbstractDep = msg.body
    packages = ", ".join(f"{s.dep} {s.state.value}" for s in event.deps)
    ready = event.state is g.SystemDepState.ready
    _show(INFO if ready else WARNING, "cli", f"{event.name}: {event.state.value} ({packages})")


def _show_deps_ready(msg: Msg[Any], hidden: Hidden) -> None:
    event: g.AbstractDepsReady = msg.body
    _show(INFO, "cli", f"all ready: {' '.join(event.names)}")


def _show_apply_progress(msg: Msg[Any], hidden: Hidden) -> None:
    event: g.IrApplyProgress = msg.body
    unit = event.unit
    if unit.state is g.ApplyState.running:
        hidden.clear()  # a new unit
    note = f" — {unit.message}" if unit.message else ""
    level = INFO if unit.state in (g.ApplyState.running, g.ApplyState.done) else WARNING
    _show(level, "cli", f"{unit.name}: {unit.state.value}{note}")
    if unit.state is g.ApplyState.failed and hidden:
        _show(WARNING, "cli", f"--- what the failed unit {unit.name} did ---")
        for line_level, component, line in hidden:
            _show(line_level, component, line, force=True)
        _show(WARNING, "cli", "---")
        hidden.clear()


def _show_docker_ready(msg: Msg[Any], hidden: Hidden) -> None:
    _show(INFO, "cli", "docker ready")


def _show_system_dep(msg: Msg[Any], hidden: Hidden) -> None:
    event: g.ResolveSystemDep = msg.body
    note = f" — {event.message}" if event.message else ""
    ready = event.state is g.SystemDepState.ready
    _show(INFO if ready else WARNING, "cli", f"{event.dep}: {event.state.value}{note}")


# A kind not listed (a response, an event nobody shows) is not printed.
_SHOW: dict[str, Callable[[Msg[Any], Hidden], None]] = {
    "LogMsg": _show_log,
    "DockerContainer": _show_container,
    "AbstractDep": _show_abstract_dep,
    "AbstractDepsReady": _show_deps_ready,
    "IrApplyProgress": _show_apply_progress,
    "DockerReady": _show_docker_ready,
    "ResolveSystemDep": _show_system_dep,
}


async def _ask(address: str, body: object) -> object | None:
    """Send one request; show everything carrying its reqId as it comes;
    return the final response's body (None when it never arrived)."""
    try:
        conn = await connect(address)
    except Closed as exc:
        _show(ERROR, "cli", str(exc))
        return None
    if wire_log.isEnabledFor(logging.DEBUG):
        conn = dump_wire(conn)
    peer = Peer(conn, "cli")
    reader = asyncio.create_task(peer.run())
    # The lines of the unit of work running now that were not shown (its
    # debug): shown should it fail.
    hidden: Hidden = []
    try:
        request = await peer.request(body)
        async for msg in request:
            show = _SHOW.get(msg.kind)
            if show is not None:
                show(msg, hidden)
        final: object = request.result.body
        if isinstance(final, rg.ErrorRes):
            _show(ERROR, "cli", f"{final.code}: {final.message}")
            return None
        return final
    except Closed as exc:
        _show(ERROR, "cli", str(exc))
        return None
    finally:
        await peer.close()
        await reader


def _package_manager(args: argparse.Namespace) -> g.PackageManager | None:
    return g.PackageManager(args.package_manager) if args.package_manager else None


async def _version(args: argparse.Namespace) -> int:
    res = await _ask(args.address, g.VersionReq(kind="VersionReq"))
    if not isinstance(res, g.VersionRes):
        return 1
    print(f"runtime {res.runtime} (protocol {res.protocol})")
    return 0


async def _resolve(args: argparse.Namespace) -> int:
    req = g.ResolveSystemDepsReq(
        kind="ResolveSystemDepsReq",
        packageManager=g.PackageManager(args.package_manager),
        deps=args.deps,
        timeout=args.timeout,
    )
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


async def _abstract_deps(args: argparse.Namespace) -> int:
    try:
        specs: list[g.AbstractDepSpec] = (
            _read_spec(args.spec, g.AbstractDepSpec) if args.spec is not None else []
        )
    except SpecError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for item in args.deps:  # NAME (the service's table) or NAME=pkg,pkg
        name, has_packages, packages = item.partition("=")
        own = [p for p in packages.split(",") if p] if has_packages else None
        specs.append(g.AbstractDepSpec(name=name, systemDeps=own))
    if not specs:
        print("error: no dependencies (NAME[=pkg,...] or --spec FILE)", file=sys.stderr)
        return 2
    req = g.AbstractDepsReq(
        kind="AbstractDepsReq",
        packageManager=g.PackageManager(args.package_manager),
        deps=specs,
        timeout=args.timeout,
    )
    res = await _ask(args.address, req)
    if not isinstance(res, g.AbstractDepsRes):
        return 1
    for status in res.deps:
        print(f"{status.name} {status.state.value}")
    return 0 if all(s.state is g.SystemDepState.ready for s in res.deps) else 1


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
    req = g.DockerPrepareReq(
        kind="DockerPrepareReq", images=specs, packageManager=_package_manager(args)
    )
    res = await _ask(args.address, req)
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
    req = g.DockerRunReq(
        kind="DockerRunReq",
        container=spec,
        follow=args.follow or None,
        packageManager=_package_manager(args),
    )
    res = await _ask(args.address, req)
    if not isinstance(res, g.DockerRunRes):
        return 1
    if not args.follow:  # started: the runtime owns it now
        return 0 if res.state is g.ContainerState.started else 1
    if res.state is not g.ContainerState.stopped:
        return 1
    return res.exitCode or 0


def _read_ir(path: str) -> g.IrCollection | None:
    """The IR at `path` (- for stdin): its node list (generate-ir's output)
    or an IrCollection — validated here, before it is sent."""
    if path == "-" and sys.stdin.isatty():
        # Nothing piped in: waiting for a terminal to type JSON only looks hung.
        print(
            "error: no IR: give a stored one (its hash or name, e.g. `deployed`), "
            "or pipe one in (< ir.json)",
            file=sys.stderr,
        )
        return None
    try:
        raw = json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    except OSError as exc:
        print(f"error: cannot read {path}: {exc.strerror or exc}", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"error: {path} is not JSON: {exc}", file=sys.stderr)
        return None
    if isinstance(raw, list):
        raw = {"kind": "IrCollection", "collection": raw}
    try:
        ir = hydrate_part(g.IrCollection, raw)
    except ProtocolError as exc:
        print(f"error: {path} is not a valid IR: {exc}", file=sys.stderr)
        return None
    assert isinstance(ir, g.IrCollection)
    return ir


def _print_irs(res: g.IrListRes) -> None:
    # The hash first and whole: it is the IR's identity (copy it for
    # ir-get/ir-delete); the name, if any, last.
    print(f"{'HASH':<71}  {'NODES':>5}  {'STORED':<19}  {'HOSTS':<20}  NAME")
    for info in res.irs:
        stored = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info.storedAt))
        name = "-" if info.name == info.hash else info.name
        hosts = ", ".join(info.hosts)
        print(f"{info.hash:<71}  {info.nodes:>5}  {stored:<19}  {hosts:<20}  {name}")


async def _ir_put(args: argparse.Namespace) -> int:
    """Store an IR, then answer with the list of stored IRs."""
    ir_ = _read_ir(args.file)
    if ir_ is None:
        return 2
    res = await _ask(args.address, g.IrPutReq(kind="IrPutReq", name=args.name, ir=ir_))
    if not isinstance(res, g.IrPutRes):
        return 1  # the result itself came as the ir service's log line
    return await _ir_list(args)


async def _ir_apply(args: argparse.Namespace) -> int:
    """Apply a stored IR — or store one first (--put FILE, or from stdin
    when no REF is given) and apply that. `run` and `delete` without REF
    and with nothing piped in: the IR that is deployed (the store's name
    `deployed`, set by create)."""
    ref = args.ref
    put = args.put
    piped = not sys.stdin.isatty()
    if args.action in ("run", "delete") and ref is None and put is None and not piped:
        ref = "deployed"
    if ref == "-" or (ref is None and put is None):
        ref, put = None, "-"  # the IR comes on stdin
    if put is not None:
        if ref is not None:
            print("error: either a stored IR (REF) or --put FILE, not both", file=sys.stderr)
            return 2
        ir_ = _read_ir(put)
        if ir_ is None:
            return 2
        stored = await _ask(args.address, g.IrPutReq(kind="IrPutReq", name=args.name, ir=ir_))
        if not isinstance(stored, g.IrPutRes):
            return 1
        ref = stored.hash  # exactly what was stored, whatever the name does meanwhile
    assert ref is not None
    req = g.IrApplyReq(
        kind="IrApplyReq",
        ref=ref,
        action=g.ApplyAction(args.action),
        host=args.host,
        dryRun=args.dry_run or None,
        commandTimeout=args.command_timeout,
        keepDeployed=args.keep_deployed or None,
    )
    res = await _ask(args.address, req)
    if not isinstance(res, g.IrApplyRes):
        return 1
    print(
        f"{res.action.value} {res.hash} on {res.host}: {res.state.value} ({len(res.units)} units)"
    )
    for unit in res.units:
        note = f" — {unit.message}" if unit.message else ""
        print(f"  {unit.name}: {unit.state.value}{note}")
    return 0 if res.state is g.IrApplyOutcome.done else 1


async def _ir_list(args: argparse.Namespace) -> int:
    res = await _ask(args.address, g.IrListReq(kind="IrListReq"))
    if not isinstance(res, g.IrListRes):
        return 1
    _print_irs(res)
    return 0


async def _ir_get(args: argparse.Namespace) -> int:
    res = await _ask(args.address, g.IrGetReq(kind="IrGetReq", ref=args.ref))
    if not isinstance(res, g.IrGetRes):
        return 1
    json.dump(plain(res.ir.collection), sys.stdout, indent=2)
    print()
    return 0


async def _ir_delete(args: argparse.Namespace) -> int:
    res = await _ask(args.address, g.IrDeleteReq(kind="IrDeleteReq", ref=args.ref))
    return 0 if isinstance(res, g.IrDeleteRes) else 1  # reported by the service's log line


# What the runtime and its services need on a host (their imports).
BUNDLE_PACKAGES = ("fabric", "runtime", "deployer", "protocol", "ladops", "raw_reconciler")


async def _bundle(args: argparse.Namespace) -> int:
    """Package the runtime with fabric's services as one PEX — what a host
    runs (`python3 fabric.pex serve`). Built from the source checkout."""
    from ovn_fabric.bundle import build_pex, source_root  # build tooling: the checkout only

    root = source_root()
    missing = [
        path
        for path in ("runtime/generated.py", "fabric/generated.py", "protocol/generated.py")
        if not (root / path).is_file()
    ]
    if missing:
        print(
            f"error: {', '.join(missing)} missing: run `deno run -A runtime/schema/generate.ts`, "
            "`deno run -A fabric/schema/generate.ts` and `deno run -A src/cli.ts generate-pytypes` "
            "first",
            file=sys.stderr,
        )
        return 2
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    build_pex(output, "fabric", packages=BUNDLE_PACKAGES)
    print(output)
    return 0


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
        help="log level, for all or one component (runtime.<c> / fabric.<c>): --log debug, "
        "--log wire=debug (every frame sent >> and received <<); repeatable, comma-separated",
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="fabric")
    _common(parser, top=True)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the runtime")
    serve.add_argument(
        "--files-root",
        default=docker_run.FILES_ROOT,
        help="where containers' files are written and mounted from (the docker daemon must "
        f"see the same path); default {docker_run.FILES_ROOT}",
    )
    serve.add_argument(
        "--state-root",
        default=ir.STATE_ROOT,
        help=f"where the runtime keeps its state (stored IRs: <root>/ir); default {ir.STATE_ROOT}",
    )
    serve.add_argument(
        "--host",
        help="which of an IR's infra.hosts this runtime is (default: its hostname)",
    )
    serve.set_defaults(run=_serve)
    version_cmd = commands.add_parser("version", help="ask the runtime's version")
    version_cmd.set_defaults(run=_version)
    resolve = commands.add_parser("resolve", help="make system dependencies (packages) present")
    resolve.add_argument("deps", nargs="+", metavar="PACKAGE")
    resolve.add_argument(
        "--package-manager",
        choices=sorted(MANAGERS),
        required=True,
        help="the host's package manager",
    )
    resolve.add_argument(
        "--timeout", type=float, help="seconds to wait (default: as long as it takes)"
    )
    resolve.set_defaults(run=_resolve)
    abstract = commands.add_parser(
        "abstract-deps", help="make abstract dependencies ready (via their system dependencies)"
    )
    abstract.add_argument(
        "deps",
        nargs="*",
        metavar="NAME[=PKG,...]",
        help="e.g. docker (the service's table) or ovn=ovn-central,ovn-host",
    )
    abstract.add_argument(
        "--spec",
        metavar="FILE",
        help="JSON [{name, systemDeps}] (- for stdin), e.g. from deployer.deps",
    )
    abstract.add_argument(
        "--package-manager",
        choices=sorted(MANAGERS),
        required=True,
        help="the host's package manager (picks the packages)",
    )
    abstract.add_argument(
        "--timeout", type=float, help="seconds to wait (default: as long as it takes)"
    )
    abstract.set_defaults(run=_abstract_deps)
    prepare = commands.add_parser(
        "docker-prepare", help="make docker usable and the images present (pull or build)"
    )
    prepare.add_argument("images", nargs="*", metavar="IMAGE", help="an image to pull")
    prepare.add_argument(
        "--spec",
        metavar="FILE",
        help='JSON list of {"image", "dockerfile"?} (- for stdin), e.g. from deployer.images',
    )
    prepare.add_argument(
        "--package-manager",
        choices=sorted(MANAGERS),
        help="make docker's packages present first (default: docker must be installed)",
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
    run_cmd.add_argument(
        "--package-manager",
        choices=sorted(MANAGERS),
        help="make docker's packages present first (default: docker must be installed)",
    )
    run_cmd.set_defaults(run=_docker_run)
    ir_put = commands.add_parser(
        "ir-put", help="store an IR (under --name, else its hash); shows the stored IRs"
    )
    ir_put.add_argument("file", nargs="?", default="-", help="the IR (JSON); - or none for stdin")
    ir_put.add_argument("--name", help="store it under this name (default: its hash)")
    ir_put.set_defaults(run=_ir_put)
    ir_list = commands.add_parser("ir-list", help="the stored IRs")
    ir_list.set_defaults(run=_ir_list)
    ir_get = commands.add_parser("ir-get", help="a stored IR (normalized) to stdout")
    ir_get.add_argument("ref", help="its name or its hash (sha256:…)")
    ir_get.set_defaults(run=_ir_get)
    ir_delete = commands.add_parser("ir-delete", help="remove a stored IR (by name or hash)")
    ir_delete.add_argument("ref", help="its name, or the hash of one stored without a name")
    ir_delete.set_defaults(run=_ir_delete)
    ir_apply = commands.add_parser(
        "ir-apply",
        help="apply a stored IR on the runtime's host (create, delete rewound, or run "
        "— converge to the deployed IR: create what is missing, of the primitives "
        "reconciled so far; --dry-run: the comparison and what it would do)",
    )
    ir_apply.add_argument("action", choices=[a.value for a in g.ApplyAction])
    ir_apply.add_argument(
        "ref",
        nargs="?",
        help="a stored IR: its hash or name; none: the IR on stdin (-: stdin always), "
        "stored, then applied — for run and delete with nothing piped in: the deployed "
        "one (`deployed`)",
    )
    ir_apply.add_argument(
        "--put", metavar="FILE", help="store this IR first (- for stdin), then apply it"
    )
    ir_apply.add_argument("--name", help="with --put: store it under this name")
    ir_apply.add_argument("--host", help="which of the IR's hosts (default: the runtime's)")
    ir_apply.add_argument("--dry-run", action="store_true", help="describe, run nothing")
    ir_apply.add_argument(
        "--keep-deployed",
        action="store_true",
        help="delete: keep the name `deployed` on the deleted IR (default: drop it)",
    )
    ir_apply.add_argument(
        "--command-timeout",
        type=float,
        metavar="SECONDS",
        help="kill a command running longer and fail its unit (0: no limit; default 300)",
    )
    ir_apply.set_defaults(run=_ir_apply)
    bundle = commands.add_parser(
        "bundle", help="package the runtime and fabric's services as one PEX (for a host)"
    )
    bundle.add_argument("-o", "--output", default="dist/fabric.pex", help="default dist/fabric.pex")
    bundle.set_defaults(run=_bundle)
    for command in (
        serve,
        version_cmd,
        resolve,
        abstract,
        prepare,
        run_cmd,
        ir_put,
        ir_list,
        ir_get,
        ir_delete,
        ir_apply,
        bundle,
    ):
        _common(command, top=False)
    args = parser.parse_args()
    try:
        logconfig.configure(args.log)
    except ValueError as exc:
        parser.error(str(exc))
    _show_remote_by_component()
    args.address = args.socket if "://" in args.socket else f"unix://{args.socket}"
    try:
        code: int = asyncio.run(args.run(args))
    except KeyboardInterrupt:  # the request was ended (and its connection closed)
        return 130
    return code


if __name__ == "__main__":
    sys.exit(main())
