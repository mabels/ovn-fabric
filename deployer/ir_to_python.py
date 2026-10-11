# deployer/ir_to_python.py — the all-in-one Python deployer generator:
# every emitter lives in deployer/ir_common.py (shared with the
# shell-script generator, deployer/ir_to_shell.py) and builds through
# the Emitter interface; this module provides the PYTHON rendering of it
# (PythonEmitter) and wraps everything into ONE self-contained Python
# file that picks what to run at runtime (2026-08-19, refactored
# 2026-08-23):
#   --action create|delete   (default create; create aborts on the first
#                             failed command, delete warns and continues)
#   --cluster                only the cluster-wide ovn-nbctl pass
#   --host NAME              only that host's pass
#   --verbose                echo every command before running it
# With neither --cluster nor --host, everything runs in ONE order for
# `create` — every host first (each installs its OS dependencies and, on
# the central, exposes the NB/SB DBs), then the cluster-wide ovn-nbctl
# pass, which needs both — and the reverse for `delete`: the cluster
# topology first, then every host (2026-09-19). Cluster-only (`--cluster`)
# keeps its plain meaning: run just the cluster pass.
#
# Unlike the shell front-end, the generated file is NOT a big shell
# block embedded verbatim: each emitted operation becomes a direct
# Python call — a command becomes run_cmd(argv), a file append (e.g. a
# systemd unit) becomes write_file(path, content) — each with the
# runtime's own error/verbose policy. That is what makes the Python
# deployment genuinely controllable: no shell syntax (heredocs,
# redirection) survives into the generated file for the runtime to
# re-parse.
# python3 is required on every target host (2026-08-19, confirmed).

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Sequence

from protocol import generated as pt

from .ir_common import Action, Emitter, Ref, _emit_cluster_body, _emit_host_script, _python_token


def _py_str(s: str) -> str:
    """Embed `s` verbatim as a triple-quoted Python string literal."""
    escaped = s.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    # Guard the closing `"""` against merging with a trailing quote in
    # the content (e.g. a wire script ending in `"$container"`) — a real
    # trailing newline keeps the delimiter unambiguous, and write_file()
    # already writes exactly one trailing newline either way.
    if escaped.endswith('"'):
        escaped += "\n"
    return '"""' + escaped + '"""'


class PythonEmitter(Emitter):
    """The Python rendering of the shared emitter interface: a command
    becomes a run_cmd(...) call whose command is the string produced by
    ir_common's own `_sh` (shlex.join) — the SAME function the shell
    front-end uses to render the line — and a file append becomes a
    write_file(...) call; both wrapped in the runtime's own
    error/verbose handling. `action` (create/delete) is carried on the
    emitter — the SAME parameter every ir_common emitter branches on —
    so a create and its reverse (delete) live in one emitter function."""

    def __init__(self, label: str, action: Action) -> None:
        self.label = label
        self._indent = 0
        super().__init__(action)

    def _push(self, line: str) -> None:
        # Indent by the emitter's current block depth — begin_block()/
        # end_block() bracket the `if not skip_deps:` guard around the OS
        # dependency commands. _python_fn adds the function body's own
        # single level on top of this.
        self.lines.append(("    " * self._indent + line) if line else "")

    def begin_block(self, header: str) -> None:
        self._push(header)
        self._indent += 1

    def end_block(self) -> None:
        self._indent -= 1

    def comment(self, text: str) -> None:
        self._push(text)

    def blank(self) -> None:
        self.lines.append("")

    def sh(
        self, argv: Sequence[str | Ref], background: bool = False, optional: bool = False
    ) -> None:
        # `background` is only meaningful in the nested shell router-body
        # emitter (_ShellBody) — the Python runtime has no `&` notion.
        # `optional=True` retires the create-action abort for this one
        # command: it warns and continues (the runtime's own policy).
        # A `Ref` token becomes the bare Python variable of that name, so
        # the runtime passes the variable's VALUE as one argv element.
        abort = "False" if optional else "abort_on_error"
        tokens = ", ".join(_python_token(token) for token in argv)
        self._push(f"run_cmd({self.label!r}, _sh([{tokens}]), verbose, {abort})")

    def capture(self, var: str, argv: list[str]) -> None:
        self._push(f"{var} = capture_cmd({self.label!r}, _sh({argv!r}), verbose, abort_on_error)")

    def append(self, path: str, content: str) -> None:
        self._push(
            f"write_file({self.label!r}, {path!r}, {_py_str(content)}, verbose, abort_on_error)"
        )


_GENERATED_RUNTIME = r"""#!/usr/bin/env python3
# Generated by ovn-fabric deployer/ir_to_python.py — DO NOT EDIT.
#
# One file for every pass the generator knows: the cluster-wide ovn-nbctl
# action (run once, from whichever chassis reaches the shared NB DB) and
# each per-host OVS/netns action (run on the host itself), for both
# create and delete. Pick what to run with --action / --cluster / --host;
# with neither --cluster nor --host, create runs every host first (each
# installs its OS dependencies, then brings up OVS/OVN and, on the
# central, exposes the NB/SB DBs) and the cluster-wide ovn-nbctl pass
# LAST — it needs ovn-nbctl installed and the central's DBs reachable, so
# running it first on a fresh host fails (2026-09-19). Delete reverses
# that: the cluster topology goes first, then the hosts. --skip-deps
# drops only the per-host package installation (apt/apk/…), for hosts
# that already carry the packages.
#
# Every command/file-write is a direct Python call into run_cmd()/
# write_file() below, each with the same error policy: create aborts on
# the first failure, delete warns and keeps going.

import argparse
import shlex
import subprocess
import sys

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


# The command stringifier from deployer/ir_common.py (_sh) — embedded
# verbatim here so the generated file stays self-contained on the target
# host, which only needs python3, not the ovn-fabric package.
def _sh(argv: list[str]) -> str:
    return shlex.join(argv)


def run_cmd(label: str, cmd: str, verbose: bool, abort_on_error: bool) -> None:
    if verbose:
        print(f"{GREEN}{label}$ {cmd}{RESET}")
    try:
        proc = subprocess.run(shlex.split(cmd))
    except OSError as exc:
        # e.g. the binary isn't installed on this host — same
        # "command failed" policy, not a crash (2026-08-19).
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            print(
                f"{RED}error: cannot run in {label}{detail}: {cmd}{RESET}",
                file=sys.stderr,
            )
            sys.exit(127)
        print(
            f"{YELLOW}warning: cannot run in {label}{detail}: {cmd}{RESET}",
            file=sys.stderr,
        )
        return
    if proc.returncode == 0:
        return
    if abort_on_error:
        print(
            f"{RED}error: failed in {label}: {cmd}{RESET}",
            file=sys.stderr,
        )
        sys.exit(proc.returncode)
    print(
        f"{YELLOW}warning: failed in {label}: {cmd}{RESET}",
        file=sys.stderr,
    )


def capture_cmd(label: str, cmd: str, verbose: bool, abort_on_error: bool) -> str:
    # The Emitter.capture() operation at runtime: run `cmd` and return its
    # trimmed stdout for a later command to use as a value (e.g. a
    # chassis's OVS system-id). Same error policy as run_cmd(); on any
    # failure the empty string is returned (the generated `test -n` guard
    # right after a capture turns that into a loud, explicit failure).
    if verbose:
        print(f"{GREEN}{label}$ {cmd}{RESET}")
    try:
        proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True)
    except OSError as exc:
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            print(
                f"{RED}error: cannot run in {label}{detail}: {cmd}{RESET}",
                file=sys.stderr,
            )
            sys.exit(127)
        print(
            f"{YELLOW}warning: cannot run in {label}{detail}: {cmd}{RESET}",
            file=sys.stderr,
        )
        return ""
    if proc.returncode != 0:
        if abort_on_error:
            print(f"{RED}error: failed in {label}: {cmd}{RESET}", file=sys.stderr)
            sys.exit(proc.returncode)
        print(f"{YELLOW}warning: failed in {label}: {cmd}{RESET}", file=sys.stderr)
        return ""
    return proc.stdout.strip()


def write_file(label: str, path: str, content: str, verbose: bool, abort_on_error: bool) -> None:
    # The append() emitter operation at runtime: write `content` (plus a
    # single trailing newline, matching the shell front-end's heredoc
    # body) to `path`, with the same error policy as run_cmd().
    if verbose:
        print(f"{GREEN}{label}$ write {path} ({len(content.splitlines())} lines){RESET}")
    try:
        with open(path, "w", encoding="utf-8") as out:
            out.write(content + ("" if content.endswith("\n") else "\n"))
    except OSError as exc:
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            print(f"{RED}error: cannot write in {label}{detail}: {path}{RESET}", file=sys.stderr)
            sys.exit(127)
        print(f"{YELLOW}warning: cannot write in {label}{detail}: {path}{RESET}", file=sys.stderr)


__CLUSTER_FUNCS__


__HOST_FUNCS__

HOSTS = {
__HOSTS__
}


def main() -> int:
    parser = argparse.ArgumentParser(description="ovn-fabric generated deployer")
    parser.add_argument(
        "--action",
        choices=["create", "delete"],
        default="create",
        help="create (default) aborts on the first failed command; delete warns and continues",
    )
    parser.add_argument(
        "--cluster",
        action="store_true",
        help="run only the cluster-wide ovn-nbctl pass",
    )
    parser.add_argument("--host", metavar="NAME", help="run only this host's pass")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="echo every command before running it",
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="skip the per-host OS dependency installation (apt/apk/…); use when already present",
    )
    args = parser.parse_args()
    abort_on_error = args.action == "create"

    if args.host is not None:
        if args.host not in HOSTS:
            print(
                f"{RED}error: unknown host {args.host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        HOSTS[args.host][args.action](args.verbose, abort_on_error, args.skip_deps)
    elif args.cluster:
        cluster = cluster_create if args.action == "create" else cluster_delete
        cluster(args.verbose, abort_on_error)
    elif args.action == "create":
        # Chassis first: each host's pass installs its OS dependencies and
        # (on the central) exposes the shared NB/SB DBs. The cluster-wide
        # ovn-nbctl pass below needs both — running it first on a fresh
        # host fails with ovn-nbctl missing (2026-09-19). --skip-deps
        # drops only the package installation, keep the rest.
        for name in HOSTS:
            HOSTS[name]["create"](args.verbose, abort_on_error, args.skip_deps)
        cluster_create(args.verbose, abort_on_error)
    else:
        # Delete reverses create: drop the logical topology first, then
        # tear every host down.
        cluster_delete(args.verbose, abort_on_error)
        for name in HOSTS:
            HOSTS[name]["delete"](args.verbose, abort_on_error, args.skip_deps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def _python_fn(
    name: str,
    label: str,
    action: Action,
    body: Callable[[Emitter], None],
    extra_params: str = "",
) -> str:
    """One generated function: run `body(emit)` through a PythonEmitter
    carrying `action` (the ONE create/delete parameter) and render its
    lines as an indented function body. `extra_params` appends parameters
    after `verbose, abort_on_error` — the per-host functions take
    `skip_deps` (see --skip-deps); the cluster pass has no dependencies,
    so it takes none."""
    emit = PythonEmitter(label, action)
    body(emit)
    body_lines = [f"    {line}" if line else "" for line in emit.lines]
    return f"def {name}(verbose, abort_on_error{extra_params}):\n" + "\n".join(body_lines) + "\n"


def _host_fn_name(host_name: str, action: str) -> str:
    return f"_host_{re.sub(r'[^A-Za-z0-9]', '_', host_name)}_{action}"


def generate_python_deployer(nodes: list[pt.Model]) -> str:
    """The all-in-one Python deployer: every action of every pass, as
    native Python functions over the shared emitters (2026-08-19,
    refactored 2026-08-23)."""
    host_defs: list[str] = []
    host_entries: list[str] = []
    for node in nodes:
        if node.kind != "infra.host":
            continue
        host_name = node.key.host
        for action in ("create", "delete"):
            fn = _host_fn_name(host_name, action)
            host_defs.append(
                _python_fn(
                    fn,
                    f"host:{host_name} ({action})",
                    action,
                    functools.partial(_emit_host_script, node, nodes, guard_deps=True),
                    extra_params=", skip_deps",
                )
            )
        host_entries.append(
            f"    {host_name!r}: {{\n"
            f'        "create": {_host_fn_name(host_name, "create")},\n'
            f'        "delete": {_host_fn_name(host_name, "delete")},\n'
            "    },"
        )

    cluster_defs = "\n\n".join(
        _python_fn(
            f"cluster_{action}",
            f"cluster ({action})",
            action,
            lambda emit: _emit_cluster_body(nodes, emit),
        )
        for action in ("create", "delete")
    )

    return (
        _GENERATED_RUNTIME.replace("__CLUSTER_FUNCS__", cluster_defs)
        .replace("__HOST_FUNCS__", "\n\n".join(host_defs))
        .replace("__HOSTS__", "\n".join(host_entries))
    )
