"""The emitter interface every _emit_* function builds through, and the shared basics."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from ip_address import IPAddress

from fabric.decide import Decide
from protocol import generated as pt

Action = str  # "create" | "delete"


_BINDABLE_IFACE_KINDS = {"vlan", "physical", "veth", "xdp"}


class Ref:
    """A runtime VARIABLE reference inside an emitted command's argv —
    e.g. a chassis id resolved at runtime via `ovs-vsctl`/`ovn-sbctl`
    (see lrp_set_gateway_chassis below): the plan turns it into a
    reference to the value captured at run time — never a quoted
    literal. Everything else in an argv is a plain string."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


def _network_of(addr: str) -> str:
    """`192.168.128.1/24` → `192.168.128.0/24`."""
    parsed = IPAddress.parse(addr)
    if parsed is None:
        raise ValueError(f"not an address: {addr!r}")
    return str(parsed.network().to_string())


# ── the emitter interface ─────────────────────────────────────────────
# Every _emit_* function below builds its output through an `Emitter`
# sink, NOT by returning shell lines: fabric/plan.py's PlanEmitter
# implements it — so the emitters never render, they only APPEND
# operations (units, their steps, files, captures), which the runtime
# then runs with its own error and logging policy.
#
# `append(path, content)` semantics: the file ends up holding `content`
# plus a single trailing newline (fabric/lifecycle.py's write).
def unit_name(kind: str, subject: str | None = None) -> str:
    """A unit's name: "<kind>: <subject>", or the kind alone."""
    return f"{kind}: {subject}" if subject else kind


class Emitter:
    label: str

    def __init__(self, action: Action) -> None:
        self.action = action
        self.lines: list[str] = []
        self._locals: set[str] = set()

    def local(self, name: str) -> str:
        """A collision-free local variable derived from an OVN object
        name, so generated code can express ownership:
        `router_home_v2.add_route(...)`."""
        stem = re.sub(r"[^0-9A-Za-z_]", "_", name).strip("_") or "ovn"
        if stem[0].isdigit():
            stem = f"_{stem}"
        if stem in {"lc", "host", "abort_on_error", "job", "ctx"}:
            stem = f"ovn_{stem}"
        candidate = stem
        index = 2
        while candidate in self._locals:
            candidate = f"{stem}_{index}"
            index += 1
        self._locals.add(candidate)
        return candidate

    def sh(
        self,
        argv: Sequence[str | Ref],
        background: bool = False,
        optional: bool | None = None,
    ) -> None:
        """`optional=None`: the action policy — DELETE never aborts (it is
        tearing down whatever is left; a missing object is not an error),
        CREATE does. `optional=True` forces non-fatal in either action.
        `optional=True`: this command failing must NOT stop the rest of
        the pass — it is reported and skipped. Used for per-unit
        `systemctl enable --now` so one broken workload/router can't block
        every other operation (2026-09-16). An argv token may be a `Ref`
        (see above), rendered as a runtime variable."""
        raise NotImplementedError

    def ovn(
        self,
        primitive: Any,
        *,
        bind: str | None = None,
        parent: str | None = None,
        decide: Decide = Decide.footprint,
    ) -> None:
        """A typed OVN northbound object. `bind` runs it and keeps the
        result in a local variable; `parent` attaches it to an already
        bound object (`router.add_route(...)`). `decide`: who decides in a
        run whether it is applied, skipped or replaced (fabric/decide.py)."""
        raise NotImplementedError

    def netns_symlink(self, container_expr: str, pid_var: str) -> None:
        raise NotImplementedError

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        """Run `argv` and bind its trimmed stdout to the runtime variable
        `var`, for later use as a `Ref(var)` in an argv — the only way a
        command can consume a value that only exists on the target host
        (e.g. a chassis's OVS `system-id`)."""
        raise NotImplementedError

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        """Like capture(), but waits (bounded) for the value to appear —
        used for a chassis that may still be registering."""
        self.capture(var, argv)

    def py(self, line: str) -> None:
        """A raw Python statement, for the few body operations that move a
        runtime value into a path/expression (Python-bodied jobs only)."""
        raise NotImplementedError

    def helper(
        self,
        name: str,
        *args: object,
        undo: str | None = None,
        undo_args: Sequence[object] = (),
    ) -> None:
        """Call the runtime helper `name(job, label, *args)` at this point
        of the body on CREATE, and `undo(job, label, *undo_args)` at the
        mirrored point on DELETE (Python-bodied jobs only). TRANSITIONAL:
        each helper becomes a typed primitive."""
        raise NotImplementedError

    def append(self, path: str, content: str) -> None:
        raise NotImplementedError

    def write_file(
        self, path: str, content: str, mode: int | None, uid: int | None, gid: int | None
    ) -> None:
        """A file with its mode and owner (uid/gid), set before it is in
        place: a workload's file as its user reads it."""
        raise NotImplementedError

    def seed(
        self, path: str, content: str, mode: int | None, uid: int | None, gid: int | None
    ) -> None:
        """A file written only where there is none: a seed the app keeps
        up to date itself (a tailscaled.state)."""
        raise NotImplementedError

    def unit(self, kind: str, subject: str | None = None, *, key: str | None = None) -> Emitter:
        """Open a unit of work and return it: what is declared on the
        returned emitter belongs to it — one logical switch, one router
        port, one kernel app. `kind` is what it is ("router port"),
        `subject` which one ("lrp-x (home)"); `key` the IR node it comes
        from. Only the newest unit takes operations (see fabric/plan.py).

        Here (the generated code) it is a section comment,
        `# --- <name> ---`, and the unit is this same emitter."""
        self.comment(f"# --- {unit_name(kind, subject)} ---")
        return self

    def comment(self, text: str) -> None:
        self.lines.append(text)

    def blank(self) -> None:
        self.lines.append("")


# An interface binding as either side of the IR carries it: an ovn.ls
# `interfaces[].iface` or a kernel.router `ifaces[].iface` (protocol.ts's
# OvnLsIface/KernelIface — the same kinds, each with its own IR-resolved
# extras).
Iface = pt.OvnLsIface | pt.KernelIface


# The closed OVN smap records of the IR — a Logical_Router's `options`, a
# Logical_Router_Port's `ipv6_ra_configs` (see _ovn_settings).
OvnSettings = pt.OvnLrOptions | pt.Ipv6RaConfigs
