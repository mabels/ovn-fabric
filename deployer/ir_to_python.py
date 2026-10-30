# deployer/ir_to_python.py — the Python deployer generator:
# every emitter lives in deployer/ir_common.py and builds through
# the Emitter interface; this module provides the PYTHON rendering of it
# (PythonEmitter) and emits `generated_deployer.py`, the entry module of
# the deployer PEX. It picks what to run at runtime (2026-08-19,
# refactored 2026-08-23, PEX layout 2026-10-26):
#   --action create|delete   (default create; create aborts on the first
#                             failed command, delete warns and continues)
#   --cluster                only the cluster-wide ovn-nbctl pass
#   --host NAME              only that host's pass
#   --verbose                log every command (DEBUG)
#   --quiet                  log only warnings and errors
#   --ovn-purge              instead of an action: reset OVS and OVN to a
#                            freshly-installed state (see ovn_purge in the
#                            runtime below)
# With neither --cluster nor --host, everything runs in ONE order for
# `create` — every host first (each installs its OS dependencies and, on
# the central, exposes the NB/SB DBs), then the cluster-wide ovn-nbctl
# pass, which needs both — and the reverse for `delete`: the cluster
# topology first, then every host (2026-09-19). Cluster-only (`--cluster`)
# keeps its plain meaning: run just the cluster pass.
#
# The generated file is NOT a big shell block embedded verbatim: each
# emitted operation becomes a direct
# Python call — a command becomes run_cmd(argv), a file append (e.g. a
# systemd unit) becomes write_file(path, content), and the host's
# packages/images become one INSTALL object run by run_install() — each
# with the runtime's own logging/error policy. That is what makes the
# Python deployment genuinely controllable: no shell syntax (heredocs,
# redirection) survives into the generated file for the runtime to
# re-parse.
#
# Only the IR-derived parts are generated. The job runtime lives in the
# `runtime` package, which the PEX ships next to this module, so the
# generated code imports it instead of carrying an inlined copy.
# python3 is required on every target host (2026-08-19, confirmed).

from __future__ import annotations

import functools
import re
from collections.abc import Callable, Sequence
from typing import Any

from protocol import generated as pt

from .ir_common import (
    Action,
    Emitter,
    Ref,
    _abstract,
    _configure_dependencies,
    _emit_cluster_body,
    _emit_fabric_start,
    _emit_host_configure,
    _emit_host_script,
    _emit_kernel_router_owner,
    _emit_service_up,
    _install_plan,
    _job_dependencies,
    _kernel_router_owners,
    _netns_name,
    _python_token,
    _service_faces,
)
from .primitives_docker import DockerImage, DockerPrimitive, DockerPull
from .primitives_fs import ChmodPrimitive, DirectoryPrimitive, RemovePathPrimitive
from .primitives_host import LinkPrimitive, NetnsPrimitive, VlanPrimitive
from .primitives_net import (
    AddrPrimitive,
    DhclientPrimitive,
    IpRulePrimitive,
    IptablesRule,
    NetnsLinkPrimitive,
    RoutePrimitive,
    SysctlFilePrimitive,
    SysctlPrimitive,
    WireguardPrimitive,
)
from .primitives_ovn import (
    OVNNbCtlAcl,
    OVNNbCtlConnection,
    OVNNbCtlLr,
    OVNNbCtlLrp,
    OVNNbCtlLs,
    OVNNbCtlLsp,
    OVNNbCtlNat,
    OVNNbCtlRoute,
    OVNSbCtlConnection,
)
from .primitives_ovs import (
    OVSAfxdpPort,
    OVSBridge,
    OVSExternalId,
    OVSIpfix,
    OVSMapping,
    OVSPort,
)


def _ovn_method_call(primitive: object) -> str:
    """A child call on a bound parent: router, netns, or kernel."""
    if isinstance(primitive, OVNNbCtlLsp):
        return f"bind_switch({primitive.switch!r}, {primitive.name!r})"
    if isinstance(primitive, OVNNbCtlLrp):
        gateway = (
            _python_token(primitive.gateway_chassis)
            if isinstance(primitive.gateway_chassis, Ref)
            else repr(primitive.gateway_chassis)
        )
        return (
            f"add_port({primitive.name!r}, {primitive.mac!r}, "
            f"{primitive.networks!r}, gateway_chassis={gateway}, "
            f"ipv6_ra_configs={primitive.ipv6_ra_configs!r})"
        )
    if isinstance(primitive, OVNNbCtlRoute):
        return f"add_route({primitive.prefix!r}, {primitive.nexthop!r})"
    if isinstance(primitive, OVNNbCtlNat):
        return (
            f"add_nat({primitive.type!r}, {primitive.external_ip!r}, "
            f"{primitive.logical_ip!r}, reset={primitive.reset!r})"
        )
    if isinstance(primitive, OVNNbCtlAcl):
        return (
            f"add_acl({primitive.switch!r}, {primitive.direction!r}, "
            f"{primitive.priority!r}, {primitive.match!r}, {primitive.action!r})"
        )
    if isinstance(primitive, VlanPrimitive):
        return f"add_vlan({primitive.parent!r}, {primitive.name!r}, {primitive.vlan_id!r})"
    if isinstance(primitive, LinkPrimitive):
        return (
            f"add_if({primitive.name!r}, kind={primitive.kind!r}, "
            f"peer={primitive.peer!r}, mac={primitive.mac!r}, up={primitive.up!r})"
        )
    if isinstance(primitive, NetnsLinkPrimitive):
        return (
            f"add_link({primitive.name!r}, {primitive.kind!r}, "
            f"parent={primitive.parent!r}, vlan_id={primitive.vlan_id!r}, "
            f"mac={primitive.mac!r}, root_leg={primitive.root_leg!r}, "
            f"bridge={primitive.bridge!r})"
        )
    if isinstance(primitive, AddrPrimitive):
        return f"add_addr({primitive.link!r}, {primitive.addr!r})"
    if isinstance(primitive, RoutePrimitive):
        return (
            f"add_route({primitive.dst!r}, {primitive.dev!r}, "
            f"via={primitive.via!r}, metric={primitive.metric!r}, "
            f"table={primitive.table!r}, family={primitive.family!r})"
        )
    if isinstance(primitive, IpRulePrimitive):
        return f"add_ip_rule({primitive.source!r}, {primitive.table!r}, " f"{primitive.family!r})"
    if isinstance(primitive, SysctlPrimitive):
        return f"set_sysctl({primitive.key!r}, {primitive.value!r})"
    if isinstance(primitive, SysctlFilePrimitive):
        return f"set_sysctl_file({primitive.path!r}, {primitive.value!r})"
    if isinstance(primitive, IptablesRule):
        return (
            f"add_rule({primitive.binary!r}, {primitive.table!r}, "
            f"{primitive.chain!r}, {primitive.args!r})"
        )
    if isinstance(primitive, WireguardPrimitive):
        return f"wireguard({primitive.iface!r}, {primitive.conf_path!r})"
    if isinstance(primitive, DhclientPrimitive):
        return f"dhclient({primitive.dev!r}, {primitive.lease!r})"
    raise TypeError(f"{primitive!r} cannot be attached to a parent")


def _ovn_call(primitive: object) -> str:
    """A `host.ovn_nbctl_*(...)` expression for one typed primitive."""
    if isinstance(primitive, OVNNbCtlLs):
        return f"ovn_nbctl_ls({primitive.name!r})"
    if isinstance(primitive, OVNNbCtlLsp):
        return (
            f"ovn_nbctl_lsp({primitive.switch!r}, {primitive.name!r}, "
            f"type={primitive.type!r}, addresses={primitive.addresses!r}, "
            f"options={primitive.options!r})"
        )
    if isinstance(primitive, OVNNbCtlLr):
        return (
            f"ovn_nbctl_lr({primitive.name!r}, options={primitive.options!r}, "
            f"external_ids={primitive.external_ids!r})"
        )
    if isinstance(primitive, OVNNbCtlLrp):
        gateway = (
            _python_token(primitive.gateway_chassis)
            if isinstance(primitive.gateway_chassis, Ref)
            else repr(primitive.gateway_chassis)
        )
        return (
            f"ovn_nbctl_lrp({primitive.router!r}, {primitive.name!r}, "
            f"{primitive.mac!r}, {primitive.networks!r}, "
            f"gateway_chassis={gateway}, "
            f"ipv6_ra_configs={primitive.ipv6_ra_configs!r})"
        )
    if isinstance(primitive, OVNNbCtlRoute):
        return (
            f"ovn_nbctl_route({primitive.router!r}, {primitive.prefix!r}, "
            f"{primitive.nexthop!r})"
        )
    if isinstance(primitive, OVNNbCtlNat):
        return (
            f"ovn_nbctl_nat({primitive.router!r}, {primitive.type!r}, "
            f"{primitive.external_ip!r}, {primitive.logical_ip!r}, "
            f"reset={primitive.reset!r})"
        )
    if isinstance(primitive, OVNNbCtlAcl):
        return (
            f"ovn_nbctl_acl({primitive.switch!r}, {primitive.direction!r}, "
            f"{primitive.priority!r}, {primitive.match!r}, {primitive.action!r})"
        )
    if isinstance(primitive, OVSBridge):
        return (
            f"ovs_bridge({primitive.name!r}, datapath={primitive.datapath!r}, "
            f"fail_mode={primitive.fail_mode!r})"
        )
    if isinstance(primitive, OVSPort):
        return (
            f"ovs_port({primitive.bridge!r}, {primitive.port!r}, "
            f"tag={primitive.tag!r}, trunks={primitive.trunks!r})"
        )
    if isinstance(primitive, OVSAfxdpPort):
        return (
            f"ovs_afxdp_port({primitive.bridge!r}, {primitive.port!r}, "
            f"{primitive.mode!r}, {primitive.n_rxq!r}, trunks={primitive.trunks!r})"
        )
    if isinstance(primitive, OVSMapping):
        return f"ovs_mapping({primitive.mappings!r})"
    if isinstance(primitive, OVSExternalId):
        return f"ovs_external_id({primitive.key_name!r}, {primitive.value!r})"
    if isinstance(primitive, OVNNbCtlConnection):
        return "ovn_nb_connection()"
    if isinstance(primitive, OVNSbCtlConnection):
        return "ovn_sb_connection()"
    if isinstance(primitive, DirectoryPrimitive):
        if primitive.owned:
            return f"state_dir({primitive.path!r})"
        return f"make_dir({primitive.path!r})"
    if isinstance(primitive, OVSIpfix):
        return f"ovs_ipfix({primitive.bridge!r}, {primitive.script!r})"
    if isinstance(primitive, ChmodPrimitive):
        return f"chmod({primitive.path!r}, {primitive.mode!r})"
    if isinstance(primitive, RemovePathPrimitive):
        return (
            f"remove_tree({primitive.path!r})"
            if primitive.recursive
            else f"remove_file({primitive.path!r})"
        )
    if isinstance(primitive, VlanPrimitive):
        return (
            f"netns({primitive.netns!r}).add_vlan("
            f"{primitive.parent!r}, {primitive.name!r}, {primitive.vlan_id!r})"
        )
    if isinstance(primitive, LinkPrimitive):
        return (
            f"netns({primitive.netns!r}).add_if({primitive.name!r}, "
            f"kind={primitive.kind!r}, peer={primitive.peer!r}, "
            f"mac={primitive.mac!r}, up={primitive.up!r})"
        )
    if isinstance(primitive, NetnsPrimitive):
        if primitive.name is None:
            return "rootNs()"
        return f"netns({primitive.name!r})"
    if isinstance(primitive, DockerPull):
        return f"docker_pull({primitive.image!r})"
    if isinstance(primitive, DockerImage):
        return (
            f"docker_image({primitive.image!r}, {primitive.dockerfile!r}, "
            f"{primitive.context!r})"
        )
    if isinstance(primitive, DockerPrimitive):
        return (
            f"docker({primitive.name!r}, {primitive.image!r}, "
            f"command={primitive.command!r}, privileged={primitive.privileged!r}, "
            f"network={primitive.network!r}, environment={primitive.environment!r}, "
            f"volumes={primitive.volumes!r}, run_flags={primitive.run_flags!r}, "
            f"remove_existing={primitive.remove_existing!r})"
        )
    if isinstance(primitive, NetnsLinkPrimitive):
        return (
            f"netns({primitive.netns!r}).add_link({primitive.name!r}, "
            f"{primitive.kind!r}, rename_to={primitive.rename_to!r}, parent={primitive.parent!r}, "
            f"vlan_id={primitive.vlan_id!r}, mac={primitive.mac!r}, "
            f"root_leg={primitive.root_leg!r}, bridge={primitive.bridge!r})"
        )
    if isinstance(primitive, AddrPrimitive):
        return f"netns({primitive.netns!r}).add_addr({primitive.link!r}, {primitive.addr!r})"
    if isinstance(primitive, RoutePrimitive):
        return (
            f"netns({primitive.netns!r}).add_route({primitive.dst!r}, "
            f"{primitive.dev!r}, via={primitive.via!r}, metric={primitive.metric!r}, "
            f"table={primitive.table!r}, family={primitive.family!r})"
        )
    if isinstance(primitive, SysctlPrimitive):
        return f"netns({primitive.netns!r}).set_sysctl(" f"{primitive.key!r}, {primitive.value!r})"
    if isinstance(primitive, IptablesRule):
        return (
            f"netns({primitive.netns!r}).add_rule({primitive.binary!r}, "
            f"{primitive.table!r}, {primitive.chain!r}, {primitive.args!r})"
        )
    if isinstance(primitive, WireguardPrimitive):
        return f"netns({primitive.netns!r}).wireguard({primitive.iface!r}, {primitive.conf_path!r})"
    raise TypeError(f"unknown OVN primitive {primitive!r}")


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


def _python_argv_expr(argv: Sequence[str | Ref]) -> str:
    # Render `ip netns exec <ns> ...` as in_netns('<ns>', [...]) so the
    # generated code stays readable.
    if len(argv) >= 4 and argv[0] == "ip" and argv[1] == "netns" and argv[2] == "exec":
        netns = _python_token(argv[3])
        rest = ", ".join(_python_token(token) for token in argv[4:])
        return f"in_netns({netns}, [{rest}])"
    return "[" + ", ".join(_python_token(token) for token in argv) + "]"


class PythonEmitter(Emitter):
    """The Python rendering of the shared emitter interface: every
    operation becomes one queued `lc.run(...)` of a lifecycle operation,
    so the rendered body is a pure declaration that the
    LifecycleController runs forward (CREATE) or rewinds (DELETE)."""

    def __init__(self, label: str, action: Action) -> None:
        self.label = label
        self._indent = 0
        super().__init__(action)

    def _push(self, line: str) -> None:
        # Indent by the emitter's current block depth — begin_block()/
        # end_block() bracket a conditional block (e.g. the docker build).
        # _python_fn adds the function body's own
        # single level on top of this.
        if self._lazy and line.startswith("await lc.run(") and line.endswith(")"):
            # Inside `with emit.lazy()`: the controller runs it only in the
            # lazy phase.
            line = line[:-1] + ", lazy=True)"
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
        self,
        argv: Sequence[str | Ref],
        background: bool = False,
        optional: bool | None = None,
    ) -> None:
        # A bare command has no inverse, so DELETE could not undo it:
        # everything a declaration creates must be a lifecycle primitive.
        raise TypeError(f"{self.label}: bare command {list(argv)!r} — use a primitive")

    def ovn(
        self,
        primitive: object,
        *,
        bind: str | None = None,
        parent: str | None = None,
    ) -> None:
        if bind is not None:
            rhs = (
                f"{parent}.{_ovn_method_call(primitive)}"
                if parent is not None
                else f"host.{_ovn_call(primitive)}"
            )
            self._push(f"{bind} = {rhs}")
            self._push(f"await lc.run({bind})")
        elif parent is not None:
            self._push(f"await lc.run({parent}.{_ovn_method_call(primitive)})")
        else:
            self._push(f"await lc.run(host.{_ovn_call(primitive)})")

    def py(self, line: str) -> None:
        self._push(line)

    def assign(self, primitive: object, var: str) -> None:
        self._push(f"{var} = host.{_ovn_call(primitive)}")

    def run_var(self, var: str) -> None:
        self._push(f"await lc.run({var})")

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(f"{var} = lc.capture({var!r}, {self.label!r}, {expr})")

    def netns_symlink(self, container_expr: str, pid_var: str) -> None:
        self._push(f"await lc.run(SymlinkNetns({container_expr}, {pid_var}))")

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(
            f"{var} = lc.wait_capture("
            f"{var!r}, {self.label!r}, {expr}, 30, result_key='CHASSIS_ID')"
        )

    def append(self, path: str, content: str) -> None:
        self._push(f"await lc.run(WriteFile({path!r}, {_py_str(content)}))")

    def install_self(self, path: str) -> None:
        self._push(f"await lc.run(InstallSelf({path!r}))")

    def helper(
        self,
        name: str,
        *args: object,
        undo: str | None = None,
        undo_args: Sequence[object] = (),
    ) -> None:
        # TRANSITIONAL: a runtime helper call, queued as a Step so it runs
        # in order with the primitives around it (and its undo, if it
        # leaves something behind, at the mirrored point on DELETE).
        def call(fn: str, params: Sequence[object]) -> str:
            rendered = "".join(f", {param!r}" for param in params)
            return f"lambda lc: {fn}(lc.job, {self.label!r}{rendered})"

        steps = f"create={call(name, args)}"
        if undo is not None:
            steps += f", delete={call(undo, undo_args)}"
        self._push(f"await lc.run(Step({name!r}, {steps}))")

    def daemon_reload(self) -> None:
        self._push("await lc.run(DaemonReload())")

    def enable_unit(self, unit: str, optional: bool = True) -> None:
        # --no-start (the boot restore pass) only enables the unit.
        self._push(
            f"await lc.run(SystemdUnit({unit!r}, start=not ctx.opts.no_start, "
            "restart=not ctx.opts.no_start))"
        )

    def start_unit(self, unit: str) -> None:
        # --no-start: install everything, start nothing.
        self._push(f"await lc.run(SystemdStart({unit!r}, start=not ctx.opts.no_start))")


class PythonClusterEmitter(PythonEmitter):
    """The cluster pass runs under one LifecycleController; there is no
    per-host `job` parameter in its generated body."""

    def capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(f"{var} = lc.capture({var!r}, {self.label!r}, {expr})")

    def netns_symlink(self, container_expr: str, pid_var: str) -> None:
        self._push(f"await lc.run(SymlinkNetns({container_expr}, {pid_var}))")

    def wait_capture(self, var: str, argv: Sequence[str | Ref]) -> None:
        expr = _python_argv_expr(argv)
        self._push(
            f"{var} = lc.wait_capture("
            f"{var!r}, {self.label!r}, {expr}, 30, result_key='CHASSIS_ID')"
        )


def _body_fn_name(host_name: str, name: str, action: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "_", f"{host_name}_{name}_{action}")
    return f"_job_{clean}"


_GENERATED_RUNTIME = r"""#!/usr/bin/env python3
# Generated by ovn-fabric deployer/ir_to_python.py — DO NOT EDIT.
#
# The entry module of the deployer PEX. It holds every pass the generator
# knows: the cluster-wide ovn-nbctl action (run once, from whichever
# chassis reaches the shared NB DB) and each per-host OVS/netns action
# (run on the host itself), for both create and delete. Pick what to run
# with --action / --cluster / --host; with neither --cluster nor --host,
# create runs every host first (each installs its OS dependencies, then
# brings up OVS/OVN and, on the central, exposes the NB/SB DBs) and the
# cluster-wide ovn-nbctl pass LAST — it needs ovn-nbctl installed and the
# central's DBs reachable, so running it first on a fresh host fails
# (2026-09-19). Delete reverses that: the cluster topology goes first,
# then the hosts.
#
# The IR's dependency/image data is merged into ONE install object per
# host (`BIDIRECTIONAL_DEPS[host]`) and executed directly before OVS/OVN.
#
# --ovn-purge replaces the action: it wipes OVS's database and, on the
# central, OVN's NB/SB databases, and restarts the daemons — back to the
# state right after the packages were installed (see ovn_purge below).
#
# Every command/file-write is a direct Python call into job.run_cmd()/
# write_file()/run_install() below, each with the same error policy:
# create aborts on the first failure, delete warns and keeps going.
#
# The job runtime is NOT inlined: `runtime.service` ships in the same
# PEX and is imported. Only the IR-derived functions and tables are
# generated into this module.

from __future__ import annotations

from deployer.context import Ctx, Options
from deployer.lifecycle import (
    DaemonReloadOperation as DaemonReload,
    InstallSelfOperation as InstallSelf,
    LifecycleAction,
    LifecycleController,
    Step,
    SymlinkNetnsOperation as SymlinkNetns,
    SystemdStartOperation as SystemdStart,
    SystemdUnitOperation as SystemdUnit,
    WriteFileOperation as WriteFile,
)
from deployer.primitives_host import HostPrimitive
from deployer.dependency import (
    APT_UPDATE_STAMP,
    INSTALL_INTERVAL,
    _apt_retry,
    _dpkg_version,
    _read_stamp,
    _write_stamp,
    docker_image_exists as _docker_image_exists,
    install_image as _install_image,
    install_packages as _install_packages,
)
from deployer.jobtree import DepWaiter, package_waiters, register
from runtime.service import Job, JobError, capture_cmd, run_cmd

import argparse
import asyncio
import functools
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import tempfile
import time

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"

_deploy_log = logging.getLogger("ovn-fabric.deployer")


def _job_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"ovn-fabric.deployer.{name}")


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as out:
        out.write(content + ("" if content.endswith("\n") else "\n"))


async def _ensure_image(
    job: Job, label: str, image: str, dockerfile: str | None
) -> None:
    # Build the image when the IR declares a Dockerfile; otherwise let
    # `docker run` pull it.
    if dockerfile is None or await _docker_image_exists(image):
        return
    build_dir = tempfile.mkdtemp()
    try:
        await write_file(
            job, label, os.path.join(build_dir, "Dockerfile"), dockerfile, True
        )
        await job.run_cmd(
            label, ["/usr/bin/docker", "build", "-t", image, build_dir], True
        )
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)


def _netns_link(container: str) -> str:
    return f"/var/run/netns/{container}"


async def _symlink_netns(job: Job, label: str, container: str, pid: str) -> None:
    await job.run_cmd(label, ["mkdir", "-p", "/var/run/netns"], True)
    link = _netns_link(container)
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(f"/proc/{pid}/ns/net", link)


async def _docker_veth_up(
    job: Job,
    label: str,
    container: str,
    image: str,
    dockerfile: str | None,
    cmd: list[str],
    netns: str,
    veth: str,
    router_ip: str,
    container_ip: str,
    gateway: str,
    run_flags: list[str] | None = None,
) -> None:
    # One veth injected into the router netns, the peer into the
    # container's netns as eth0 (CNI/Multus-style).
    peer = f"{veth}-c"
    await _ensure_image(job, label, image, dockerfile)
    await job.run_cmd(label, ["/usr/bin/docker", "rm", "-f", container], False)
    await job.run_cmd(
        label,
        [
            "/usr/bin/docker",
            "run",
            "-d",
            "--network",
            "none",
            "--name",
            container,
            *(run_flags or []),
            image,
            *cmd,
        ],
        True,
    )
    pid = await job.capture_cmd(
        label, ["/usr/bin/docker", "inspect", "-f", "{{.State.Pid}}", container], True
    )
    await _symlink_netns(job, label, container, pid)
    await job.run_cmd(
        label, ["ip", "link", "add", veth, "type", "veth", "peer", "name", peer], True
    )
    await job.run_cmd(label, ["ip", "link", "set", peer, "netns", container], True)
    await job.run_cmd(label, ["ip", "link", "set", veth, "netns", netns], True)
    await job.run_cmd(
        label, ["ip", "netns", "exec", netns, "ip", "link", "set", veth, "up"], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", netns, "ip", "addr", "add", router_ip, "dev", veth], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", container, "ip", "link", "set", peer, "name", "eth0"], True
    )
    await job.run_cmd(
        label, ["ip", "netns", "exec", container, "ip", "link", "set", "eth0", "up"], True
    )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", container, "ip", "addr", "add", container_ip, "dev", "eth0"],
        True,
    )
    await job.run_cmd(
        label,
        [
            "ip",
            "netns",
            "exec",
            container,
            "ip",
            "route",
            "add",
            "default",
            "via",
            gateway,
            "dev",
            "eth0",
        ],
        True,
    )
    await job.run_cmd(label, ["/usr/bin/docker", "wait", container], True)


async def _docker_attach_legs(
    job: Job,
    label: str,
    container: str,
    netns: str,
    dev: str,
    transit_leg: str | None,
) -> None:
    # Hand the router's legs to the already-running container (its netns
    # is linked under /var/run/netns by then) and enable forwarding. On
    # DELETE nothing to undo: the legs die with the container's netns.
    if transit_leg:
        await job.run_cmd(
            label,
            ["ip", "netns", "exec", netns, "ip", "link", "set", transit_leg, "netns", container],
            True,
        )
        await job.run_cmd(
            label,
            ["ip", "netns", "exec", container, "ip", "link", "set", transit_leg, "up"],
            True,
        )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", netns, "ip", "link", "set", dev, "netns", container],
        True,
    )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", container, "ip", "link", "set", dev, "up"],
        True,
    )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", container, "sysctl", "-w", "net.ipv4.ip_forward=1"],
        True,
    )
    await job.run_cmd(
        label,
        ["ip", "netns", "exec", container, "sysctl", "-w", "net.ipv6.conf.all.forwarding=1"],
        True,
    )


async def _docker_down(job: Job, label: str, container: str) -> None:
    # The undo of _docker_veth_up: the container (its netns and the
    # injected veth go with it) and its /var/run/netns link.
    await job.run_cmd(label, ["/usr/bin/docker", "rm", "-f", container], False)
    await job.run_cmd(label, ["rm", "-f", _netns_link(container)], False)


async def _docker_exec(job: Job, label: str, container: str, cmd: list[str], dev: str) -> None:
    # Detached: the app runs inside the container (whose main process
    # keeps it alive); the pass goes on to the next operation.
    await job.run_cmd(label, ["/usr/bin/docker", "exec", "-d", container, *cmd, dev], True)


async def _zerotier_up(
    job: Job,
    label: str,
    netns: str,
    home: str,
    network: str,
    masq: list[str],
    routes: list[str],
) -> None:
    # `/usr/sbin/zerotier-one <home>` WITHOUT `-d` is the foreground
    # daemon: run it as this job's tracked child, do the wiring while it
    # runs, then await it. The job blocks for the daemon's whole life;
    # Job.stop() kills the process group, so `down` needs no pkill and no
    # PID bookkeeping.
    await job.run_cmd(label, ["mkdir", "-p", home], True)
    daemon = asyncio.create_task(
        job.run_cmd(
            label,
            ["ip", "netns", "exec", netns, "/usr/sbin/zerotier-one", home],
            True,
        )
    )
    try:
        for _ in range(10):
            if await job.capture_cmd(
                label,
                ["ip", "netns", "exec", netns, "zerotier-cli", f"-D{home}", "info"],
                False,
            ):
                break
            await asyncio.sleep(1)
        await job.run_cmd(
            label,
            ["ip", "netns", "exec", netns, "zerotier-cli", f"-D{home}", "join", network],
            True,
        )
        var = ""
        for _ in range(30):
            raw = await job.capture_cmd(
                label,
                [
                    "ip",
                    "netns",
                    "exec",
                    netns,
                    "zerotier-cli",
                    f"-D{home}",
                    "-j",
                    "listnetworks",
                ],
                False,
            )
            if raw:
                try:
                    for entry in json.loads(raw):
                        if entry.get("nwid") == network:
                            var = entry.get("portDeviceName") or ""
                            break
                except ValueError:
                    var = ""
            if var and var != "null":
                break
            await asyncio.sleep(1)
        if not var or var == "null":
            raise RuntimeError(f"zerotier network {network}: no portDeviceName in {netns}")
        await job.run_cmd(
            label, ["ip", "netns", "exec", netns, "ip", "link", "set", var, "up"], True
        )
        for family in masq:
            binary = "iptables" if family == "ipv4" else "ip6tables"
            await job.run_cmd(
                label,
                [
                    "ip",
                    "netns",
                    "exec",
                    netns,
                    binary,
                    "-t",
                    "nat",
                    "-A",
                    "POSTROUTING",
                    "-o",
                    var,
                    "-j",
                    "MASQUERADE",
                ],
                True,
            )
        for dst in routes:
            await job.run_cmd(
                label,
                ["ip", "netns", "exec", netns, "ip", "route", "add", dst, "dev", var],
                True,
            )
    except BaseException:
        daemon.cancel()
        raise
    # Block for the daemon; stop kills it and this returns.
    await daemon


async def write_file(
    job: Job, label: str, path: str, content: str, abort_on_error: bool
) -> None:
    # The append() emitter operation at runtime: write `content` (plus a
    # single trailing newline, so the file always ends in one) to
    # `path`, with the same error policy as run_cmd(). The file I/O runs
    # off the event loop.
    job.debug(f"{label}$ write {path} ({len(content.splitlines())} lines)")
    try:
        await asyncio.to_thread(_write_text, path, content)
    except OSError as exc:
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            job.error(f"cannot write in {label}{detail}: {path}")
            raise JobError(f"cannot write in {label}{detail}: {path}", 127)
        job.warn(f"cannot write in {label}{detail}: {path}")


def _install_copy(src: str, path: str) -> None:
    shutil.copyfile(src, path)
    os.chmod(path, 0o755)


async def install_self(job: Job, label: str, path: str, abort_on_error: bool) -> None:
    # The install_self() emitter operation: copy THIS deployer to `path`,
    # so ovn-fabric-restore.service can re-run the host pass at boot.
    # Inside a PEX, `__file__` points into the archive and is not a real
    # file; the executable the operator invoked (sys.argv[0]) is. Direct
    # script runs keep working for the same reason.
    src = sys.argv[0] if sys.argv else ""
    if not src or not os.path.isfile(src):
        src = globals().get("__file__", "")
    if not src or not os.path.isfile(src):
        job.warn(
            f"cannot install the boot restore copy in {label}: "
            "run the deployer from a file, not stdin"
        )
        return
    if os.path.abspath(src) == os.path.abspath(path):
        return
    job.debug(f"{label}$ install {src} -> {path}")
    try:
        await asyncio.to_thread(_install_copy, src, path)
    except OSError as exc:
        detail = f" ({exc.strerror})" if exc.strerror else ""
        if abort_on_error:
            job.error(f"cannot install in {label}{detail}: {path}")
            raise JobError(f"cannot install in {label}{detail}: {path}", 127)
        job.warn(f"cannot install in {label}{detail}: {path}")


async def _docker_ready(job: Job) -> bool:
    delay = 1.0
    for _ in range(8):
        if await job.capture_cmd(
            "docker", ["/usr/bin/docker", "version"], False
        ):
            job.emit(
                {"kind": "abstract-dep", "name": "docker", "state": "ready"},
                queue=True,
            )
            return True
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)
    return False


# ── --ovn-purge ───────────────────────────────────────────────────
# Reset OVS and/or OVN on THIS machine to a freshly-installed state: stop
# the daemons, delete the database files, start the daemons again so each
# one's own init recreates an empty, schema-only database.
#   ovs=True      OVS's conf.db — every bridge/port/interface/external-id
#                 of this chassis, including its OVN registration.
#   central=True  OVN's NB and SB databases — the logical topology of the
#                 ENTIRE cluster, every chassis, not just this host. The
#                 files live on the central, so this part only does
#                 anything when it runs there.
# What create set up outside the databases (netns, veths, systemd units,
# containers) is NOT touched — that is --action delete's job. Afterwards
# everything create registered is gone with the databases: run create
# again to bring the fabric back.
#
# The chassis's system-id is read first and written back afterwards. It IS
# the chassis's identity in the SB database (Chassis.name); left to
# regenerate, every gateway-chassis pin made against the old id would
# silently reference a chassis that no longer exists.
#
# A daemon that is not running (or not installed) must not stop the
# purge, so the stops only warn; a database file that cannot be removed
# or a daemon that does not come back aborts.
OVS_DB = "/var/lib/openvswitch/conf.db"
OVN_DBS = [
    "/var/lib/ovn/ovnnb_db.db",
    "/var/lib/ovn/ovnsb_db.db",
    "/var/lib/ovn/.ovnnb_db.db.~lock~",
    "/var/lib/ovn/.ovnsb_db.db.~lock~",
]
OVN_CENTRAL_DB_SERVICES = ["ovn-ovsdb-server-nb", "ovn-ovsdb-server-sb"]


async def ovn_purge(job: Job, label: str, ovs: bool, central: bool) -> None:
    system_id = ""
    if ovs:
        system_id = (
            await job.capture_cmd(
                label,
                [
                    "ovs-vsctl",
                    "--if-exists",
                    "get",
                    "open_vswitch",
                    ".",
                    "external_ids:system-id",
                ],
                False,
            )
        ).strip('"')
        await job.run_cmd(label, ["systemctl", "stop", "ovn-controller"], False)
    if central:
        await job.run_cmd(
            label,
            ["systemctl", "stop", "ovn-northd", *OVN_CENTRAL_DB_SERVICES],
            False,
        )
    if ovs:
        await job.run_cmd(
            label, ["systemctl", "stop", "ovs-vswitchd", "ovsdb-server"], False
        )
        await job.run_cmd(label, ["rm", "-f", OVS_DB], True)
    if central:
        await job.run_cmd(label, ["rm", "-f", *OVN_DBS], True)
    if ovs:
        await job.run_cmd(
            label, ["systemctl", "start", "ovsdb-server", "ovs-vswitchd"], True
        )
        if system_id:
            setting = f"external_ids:system-id={system_id}"
            await job.run_cmd(
                label,
                ["ovs-vsctl", "set", "open_vswitch", ".", setting],
                True,
            )
            await write_file(
                job, label, "/etc/openvswitch/system-id.conf", system_id, True
            )
    if central:
        await job.run_cmd(
            label,
            ["systemctl", "start", *OVN_CENTRAL_DB_SERVICES, "ovn-northd"],
            True,
        )
    if ovs:
        await job.run_cmd(label, ["systemctl", "start", "ovn-controller"], True)


__CLUSTER_FUNCS__


__HOST_FUNCS__

HOSTS = {
__HOSTS__
}

SERVICES = __SERVICES__

BIDIRECTIONAL_DEPS = {
__BIDIRECTIONAL_DEPS__
}


def concrete_deps(kind: str, host: str | None = None) -> list[dict]:
    # Unique concrete dependencies of one kind from BIDIRECTIONAL_DEPS.
    # kind: "apt-dep" | "sys-cmd-dep" | "docker-image".
    hosts = [host] if host is not None else list(BIDIRECTIONAL_DEPS)
    seen: set[tuple] = set()
    out: list[dict] = []
    for host_name in hosts:
        for entry in BIDIRECTIONAL_DEPS.get(host_name, {}).values():
            for dep in entry["concrete"]:
                if dep["kind"] != kind:
                    continue
                key = (
                    dep["kind"],
                    dep.get("package"),
                    dep.get("name"),
                    dep.get("image"),
                )
                if key in seen:
                    continue
                seen.add(key)
                out.append(dep)
    return out


def apt_deps(host: str | None = None) -> list[dict]:
    return concrete_deps("apt-dep", host)


def sys_cmd_deps(host: str | None = None) -> list[dict]:
    return concrete_deps("sys-cmd-dep", host)


def docker_image_deps(host: str | None = None) -> list[dict]:
    return concrete_deps("docker-image", host)


_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
_deploy_log = logging.getLogger("ovn-fabric.deployer")


async def _handle_log(job: Job, event: object) -> None:
    # The ONE central log sink: every job's `log()`/`warn()`/... bubbles
    # up here with the job name already attached.
    if not isinstance(event, dict) or event.get("kind") != "log":
        return
    level = _LOG_LEVELS.get(event.get("level"), logging.INFO)
    _deploy_log.log(level, "%s: %s", event.get("name", "?"), event.get("message", ""))


def _root_job(name: str = "ovn-fabric") -> Job:
    # Every tree's root, so every job's log events reach the sink: a
    # job's log() goes to its ANCESTORS only, never to itself.
    root = Job(name, logger_factory=_job_logger)
    root.on_emit(_handle_log)
    return root


async def install_zerotier(job: Job) -> None:
    # Custom installer, separate from the apt/sys-cmd system-deps job.
    # The job is gated on the concrete curl dependency; only when that is
    # ready does it run the vendor installer and announce the abstract
    # zerotier dependency.
    await job.run_cmd(
        "install-zerotier",
        ["sh", "-c", "curl -s https://install.zerotier.com | bash"],
        True,
        600,
    )
    job.emit(
        {"kind": "abstract-dep", "name": "zerotier", "state": "installed"},
        queue=True,
    )


def _resolve_dependency_job(
    ctx: Ctx,
    root: Job,
    host: str,
    image_deps: list[dict],
    repeat: bool,
    with_images: bool = True,
    custom_installers: dict[str, list[dict[str, str]]] | None = None,
) -> None:
    waiters = package_waiters(host, BIDIRECTIONAL_DEPS)
    packages = root.job("install-packages", initiate=True, ctx=waiters)

    async def packages_start(job: Job) -> None:
        async def translate(job: Job, event: object, ctx: object = None) -> None:
            waiters_ctx = ctx if isinstance(ctx, dict) else {}
            for abstract, waiter in waiters_ctx.items():
                async def emit(name: str = abstract) -> None:
                    job.info(f"abstract-dep {name} fulfilled")
                    job.emit(
                        {"kind": "abstract-dep", "name": name, "state": "installed"},
                        queue=True,
                    )

                await waiter.waitfor(event, emit)

        job.on_emit(translate)
        while True:
            await _install_packages(job, BIDIRECTIONAL_DEPS[host], dry_run=ctx.opts.dry_run)
            if not repeat:
                break
            await asyncio.sleep(INSTALL_INTERVAL)

    packages.on_start(packages_start)

    for name, dependencies in (custom_installers or {}).items():
        installer = root.job(f"install-{name}", initiate=True)
        if name == "zerotier":
            register(installer, dependencies, install_zerotier, timeout=ctx.opts.timeout)

    if not with_images:
        return

    # One STATIC job per docker-image, each gated on the docker abstract
    # dependency. Each emits its own queued docker-image ready event.
    images = [
        entry["concrete"][0]
        for entry in BIDIRECTIONAL_DEPS.get(host, {}).values()
        if entry["abstract"].get("kind") == "docker-image"
    ]
    for entry in images:
        # Name the job after the image itself; the BIDIRECTIONAL filter
        # already gives the complete, unique list.
        image_job = root.job(f"image:{entry['image']}", initiate=True)

        async def install_image(job: Job, image: dict = entry) -> None:
            await _install_image(job, image, dry_run=ctx.opts.dry_run)

        register(image_job, image_deps, install_image, timeout=ctx.opts.timeout)


def _run_common(
    ctx: Ctx,
    root: Job,
    host: str,
    image_deps: list[dict],
    configure_deps: list[dict],
    custom_installers: dict[str, list[dict]],
) -> Job:
    _resolve_dependency_job(
        ctx, root, host, image_deps, repeat=True, custom_installers=custom_installers
    )

    async def configure(job: Job) -> None:
        await CONFIGURE[host](ctx, job)
        job.emit(
            {"kind": "abstract-dep", "name": "configure", "state": "ready"},
            queue=True,
        )

    configure_job = root.job("configure", initiate=True)
    register(configure_job, configure_deps, configure, timeout=ctx.opts.timeout)
    return configure_job


async def _apply(
    ctx: Ctx, job: Job, action: LifecycleAction, host: str, name: str, declare: Any
) -> None:
    # One scope, ONE declaration: CREATE runs it forward, DELETE rewinds
    # it — whatever the declaration creates, its rewind deletes.
    lc = LifecycleController(ctx, job, action, host, name=name)
    await declare(ctx, lc, HostPrimitive(host))
    await lc.execute()


async def _create_scopes(ctx: Ctx, job: Job, host: str, scopes: list) -> None:
    # Every scope's synchronous part, in order; the last scope starts the
    # service, which runs the lazy phase (see _lazy_scopes).
    for label, declare in scopes:
        await _apply(ctx, job, LifecycleAction.CREATE, host, f"{label} (create)", declare)


async def _lazy_scopes(
    ctx: Ctx, job: Job, action: LifecycleAction, host: str, scopes: list
) -> None:
    # The lazy phase of every scope: images, then the containers that
    # need them — forward on LAZY_CREATE, rewound on LAZY_DELETE.
    for label, declare in scopes if action.creating else reversed(scopes):
        await _apply(ctx, job, action, host, f"{label} ({action.value})", declare)


async def _delete_scopes(ctx: Ctx, job: Job, host: str, scopes: list) -> None:
    # The exact rewind of create plus the lazy phase it started: stop the
    # service (the last scope), rewind the lazy phase, then every scope.
    *rest, (label, declare) = scopes
    await _apply(ctx, job, LifecycleAction.DELETE, host, f"{label} (delete)", declare)
    await _lazy_scopes(ctx, job, LifecycleAction.LAZY_DELETE, host, scopes)
    for label, declare in reversed(rest):
        await _apply(ctx, job, LifecycleAction.DELETE, host, f"{label} (delete)", declare)


def _python_job(
    ctx: Ctx, parent: Job, name: str, dependencies: list[str], up: Any, down: Any
) -> Job:
    # One kernel router/service job, gated on its IR-derived deps (which
    # always include configure). DepWait is the generated deployer's.
    job = parent.job(name, initiate=True)
    register(
        job, dependencies, functools.partial(up, ctx), keep_alive=True, timeout=ctx.opts.timeout
    )
    job.on_stop(functools.partial(down, ctx))
    return job



__JOB_FUNCS__

JOB_BODIES = {
__JOB_BODIES__
}

CONFIGURE = {
__CONFIGURE__
}

__TREE_FUNCS__

__CREATE_FUNCS__

__DELETE_FUNCS__

RUN_JOBS = {
__TREES__
}

CREATE_JOBS = {
__CREATE_JOBS__
}

DELETE_JOBS = {
__DELETE_JOBS__
}


async def _run_host(ctx: Ctx, host: str) -> int:
    root = RUN_JOBS[host](ctx)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    running = asyncio.create_task(root.run())
    stopped = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait({running, stopped}, return_when=asyncio.FIRST_COMPLETED)
    if stopped in done:
        await root.stop()
    else:
        stopped.cancel()
    await running
    return 0


async def _run_job_set(ctx: Ctx, factory: Any, label: str) -> None:
    root = factory(ctx)
    if ctx.opts.timeout <= 0:
        await root.run()
        return
    try:
        await asyncio.wait_for(root.run(), timeout=ctx.opts.timeout)
    except asyncio.TimeoutError:
        await root.stop()
        print(
            f"{RED}error: {label} did not complete within "
            f"{ctx.opts.timeout:g}s; dependencies could not be installed "
            f"(missing packages or no network){RESET}",
            file=sys.stderr,
        )
        raise SystemExit(1)


async def purge(ctx: Ctx, host, cluster_only: bool) -> int:
    async def run_one(name: str) -> None:
        root = _root_job(f"ovn-fabric(purge:{name})")
        _resolve_dependency_job(ctx, root, name, [], repeat=False, with_images=False)
        job = root.job("ovn-fabric(purge)", initiate=True)

        async def action(j: Job) -> None:
            await _purge(j, name, cluster_only)

        register(job, [{"kind": "abstract", "name": "ovn"}], action, timeout=ctx.opts.timeout)
        await _run_job_set(ctx, lambda _ctx: root, f"purge on {name}")

    if cluster_only:
        central = next(
            (name for name, data in HOSTS.items() if data["role"] == "central"),
            None,
        )
        if central is None:
            print(f"{RED}error: no central host for --cluster{RESET}", file=sys.stderr)
            return 2
        await run_one(central)
        return 0
    if host is not None:
        if host not in HOSTS:
            print(
                f"{RED}error: unknown host {host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        if HOSTS[host]["role"] is None:
            print(
                f"{YELLOW}host:{host} is not part of the OVN cluster — nothing to purge{RESET}"
            )
            return 0
        await run_one(host)
        return 0
    for name in HOSTS:
        if HOSTS[name]["role"] is None:
            print(
                f"{YELLOW}host:{name} is not part of the OVN cluster — nothing to purge{RESET}"
            )
            continue
        await run_one(name)
    return 0


async def _purge(job: Job, host, cluster_only: bool) -> int:
    if cluster_only:
        await ovn_purge(job, "cluster (purge)", ovs=False, central=True)
        return 0
    if host is not None and host not in HOSTS:
        print(
            f"{RED}error: unknown host {host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
            file=sys.stderr,
        )
        return 2
    for name in HOSTS if host is None else [host]:
        role = HOSTS[name]["role"]
        if role is None:
            print(
                f"{YELLOW}host:{name} is not part of the OVN cluster — nothing to purge{RESET}"
            )
            continue
        await ovn_purge(job, f"host:{name} (purge)", ovs=True, central=role == "central")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="ovn-fabric generated deployer")
    parser.add_argument(
        "--action",
        choices=["create", "delete", "run", "lazy-create", "lazy-delete"],
        help="create (default) aborts on the first failed command; delete warns and continues. "
        "lazy-create/lazy-delete run only the lazy phase (image pulls/builds): create starts "
        "the lazy-create unit, delete removes the images last",
    )
    parser.add_argument(
        "--cluster",
        action="store_true",
        help="run only the cluster-wide ovn-nbctl pass",
    )
    parser.add_argument("--host", metavar="NAME", help="run only this host's pass")
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        metavar="SECONDS",
        help="dependency/action timeout in seconds (0 disables it; default 180)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="alias for --action run (the generated service uses --action run)",
    )
    parser.add_argument(
        "--job",
        metavar="NAME",
        help="run one generated kernel router/service body (the systemd units call this)",
    )
    parser.add_argument(
        "--job-action",
        choices=["up", "down"],
        help="which body of --job to run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="dump the ordered primitive queue(s) and exit without running them",
    )
    loudness = parser.add_mutually_exclusive_group()
    loudness.add_argument(
        "--verbose",
        action="store_true",
        help="log at DEBUG level: every command and its output, every record timestamped",
    )
    loudness.add_argument(
        "--quiet",
        action="store_true",
        help="log only warnings and errors",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="write and enable the fabric's units but do not start them now (systemd starts "
        "them after this pass; ovn-fabric-restore.service uses it at boot)",
    )
    parser.add_argument(
        "--explode",
        metavar="DIR",
        help="extract this generated PEX into DIR (readable source tree) and exit",
    )
    parser.add_argument(
        "--ovn-purge",
        action="store_true",
        help="INSTEAD of an action: wipe the OVS database and, on the central, OVN's NB/SB "
        "databases, and restart the daemons (a freshly-installed state). With --host only "
        "that host's part; with --cluster only the NB/SB databases (run it on the central)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=(
            logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
        ),
        # Verbose: timestamp every record, at every level. Normal: keep
        # the compact level/message form.
        format=(
            "%(asctime)s.%(msecs)03d %(levelname)s %(message)s"
            if args.verbose
            else "%(levelname)s %(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    if args.explode is not None:
        import zipfile

        try:
            with zipfile.ZipFile(sys.argv[0]) as archive:
                archive.extractall(args.explode)
        except (OSError, zipfile.BadZipFile) as exc:
            print(f"{RED}error: cannot explode {sys.argv[0]}: {exc}{RESET}", file=sys.stderr)
            return 1
        print(f"exploded {sys.argv[0]} -> {args.explode}")
        return 0
    ctx = Ctx(
        Options(
            dry_run=args.dry_run,
            timeout=args.timeout,
            no_start=args.no_start,
            verbose=args.verbose,
        )
    )
    if args.job is not None:
        if args.job_action is None:
            parser.error("--job needs --job-action")
        if args.job not in JOB_BODIES:
            print(
                f"{RED}error: unknown job {args.job!r}; known jobs: {', '.join(JOB_BODIES)}{RESET}",
                file=sys.stderr,
            )
            return 2
        up, down = JOB_BODIES[args.job]

        async def run_job() -> None:
            root = _root_job()
            job = root.job(args.job, initiate=True)
            action = up if args.job_action == "up" else down

            async def start(h: Job) -> None:
                await action(ctx, h)

            job.on_start(start)
            # start() only schedules; run() observes the started set.
            await root.run()

        asyncio.run(run_job())
        return 0
    if args.job_action is not None:
        parser.error("--job-action needs --job")
    if args.action == "run" or args.run:
        if args.host is None:
            parser.error("--action run needs --host")
        if args.cluster or args.ovn_purge:
            parser.error("--action run replaces the other actions")
        if args.host not in RUN_JOBS:
            print(
                f"{RED}error: unknown host {args.host!r}; "
                f"known hosts: {', '.join(RUN_JOBS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        return asyncio.run(_run_host(ctx, args.host))
    if args.ovn_purge:
        # A purge is its own run, never a silent prelude or epilogue to
        # create/delete: say so instead of ignoring an --action.
        if args.action is not None:
            parser.error("--ovn-purge replaces the action; run --action as a separate step")
        return asyncio.run(purge(ctx, args.host, args.cluster))
    args.action = args.action or "create"

    if args.action in ("lazy-create", "lazy-delete"):
        if args.cluster:
            parser.error("--cluster has no lazy phase")
        lazy_action = LifecycleAction(args.action)
        hosts = [args.host] if args.host is not None else list(HOSTS)
        unknown = [name for name in hosts if name not in HOSTS]
        if unknown:
            print(
                f"{RED}error: unknown host {unknown[0]!r}; known hosts: {', '.join(HOSTS)}{RESET}",
                file=sys.stderr,
            )
            return 2

        async def run_lazy() -> None:
            root = _root_job()
            await root.start()
            try:
                for name in hosts:
                    job = root.job(f"ovn-fabric({args.action}:{name})")
                    await HOSTS[name]["lazy"](ctx, job, lazy_action)
            finally:
                await root.stop()

        asyncio.run(run_lazy())
        return 0

    if args.host is not None:
        if args.host not in HOSTS:
            print(
                f"{RED}error: unknown host {args.host!r}; known hosts: {', '.join(HOSTS)}{RESET}",
                file=sys.stderr,
            )
            return 2
        jobs = CREATE_JOBS if args.action == "create" else DELETE_JOBS
        asyncio.run(
            _run_job_set(ctx, jobs[args.host], f"{args.action} on {args.host}")
        )
    elif args.cluster:
        action = LifecycleAction.CREATE if args.action == "create" else LifecycleAction.DELETE
        central = next(
            (name for name, data in HOSTS.items() if data["role"] == "central"),
            None,
        )
        if central is None:
            print(f"{RED}error: no central host for --cluster{RESET}", file=sys.stderr)
            return 2

        async def run_cluster() -> None:
            root = _root_job()
            await root.start()
            job = root.job("cluster")
            lc = LifecycleController(ctx, job, action, central, name="cluster")
            try:
                await cluster(lc, HostPrimitive(central))
                await lc.execute()
            finally:
                await root.stop()

        asyncio.run(run_cluster())
    elif args.action == "create":
        async def run_create() -> None:
            for name in CREATE_JOBS:
                await _run_job_set(ctx, CREATE_JOBS[name], f"create on {name}")

        asyncio.run(run_create())
    else:
        async def run_delete() -> None:
            order = sorted(
                DELETE_JOBS, key=lambda name: HOSTS[name]["role"] != "central"
            )
            for name in order:
                await _run_job_set(ctx, DELETE_JOBS[name], f"delete on {name}")

        asyncio.run(run_delete())
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


def _declare_fn(name: str, label: str, body: Callable[[Emitter], None]) -> str:
    """One scope's declaration: run `body(emit)` through a PythonEmitter
    and render it as a function that only queues lifecycle operations on
    `lc`. The SAME declaration serves both directions — `_apply` runs it
    forward for CREATE and rewound for DELETE."""
    emit = PythonEmitter(label, "create")
    body(emit)
    lines = [f"    {line}" if line else "" for line in emit.lines]
    if not any(line.strip() and not line.strip().startswith("#") for line in lines):
        lines.append("    return")
    return (
        f"async def {name}(ctx: Ctx, lc: LifecycleController, host: HostPrimitive) -> None:\n"
        + "\n".join(lines)
        + "\n"
    )


def _apply_fn(name: str, action: Action, host: str, label: str, declare: str) -> str:
    """`name(ctx, job)`: apply the declaration `declare` for `action`."""
    life = "LifecycleAction.DELETE" if action == "delete" else "LifecycleAction.CREATE"
    return (
        f"async def {name}(ctx: Ctx, job: Job) -> None:\n"
        f"    await _apply(ctx, job, {life}, {host!r}, {label!r}, {declare})\n"
    )


def _python_cluster_fn(
    name: str,
    label: str,
    body: Callable[[Emitter], None],
) -> str:
    """The single cluster body. It only queues primitives; CREATE and
    DELETE are produced by executing that queue forward/reverse."""
    emit = PythonClusterEmitter(label, "create")
    body(emit)
    lines = [f"    {line}" if line else "" for line in emit.lines]
    return f"async def {name}(lc, host):\n" + "\n".join(lines) + "\n"


def _declare_fn_name(host_name: str, name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]", "_", f"{host_name}_{name}")
    return f"_declare_{clean}"


def _host_fn_name(host_name: str, action: str) -> str:
    return f"_host_{re.sub(r'[^A-Za-z0-9]', '_', host_name)}_{action}"


def _run_fn_name(host_name: str) -> str:
    return f"_run_{re.sub(r'[^A-Za-z0-9]', '_', host_name)}"


def _service_manifest(nodes: list[pt.Model]) -> dict[str, list[dict[str, str]]]:
    """The kernel routers/workloads each host owns, as the generated
    tree's `SERVICES` manifest (logical name, kind)."""
    out: dict[str, list[dict[str, str]]] = {}
    for node in nodes:
        if node.kind != "infra.host":
            continue
        entries: list[dict[str, str]] = []
        for owner in _kernel_router_owners(node.id, nodes):
            name = owner.key.name
            entries.append(
                {
                    "name": name,
                    "kind": "router",
                }
            )
        for n in nodes:
            if n.kind in ("kernel.app.container", "kernel.app.service") and n.data.host == node.id:
                name = n.key.name
                entries.append(
                    {
                        "name": name,
                        "kind": "service",
                    }
                )
        out[node.key.host] = entries
    return out


def _strip_unused_helpers(source: str) -> str:
    """Drop optional helper definitions the generated bodies never call
    (e.g. ZeroTier/client-mode Docker when this IR doesn't use them)."""
    import ast

    optional = {
        "_zerotier_up",
        "_docker_veth_up",
        "_docker_ready",
        "_docker_attach_legs",
        "_docker_exec",
        "_docker_down",
    }
    remove: list[tuple[int, int]] = []
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in optional:
            continue
        if source.count(f"{node.name}(") > 1:
            continue
        remove.append((node.lineno, node.end_lineno or node.lineno))
    lines = source.splitlines(keepends=True)
    for first, last in sorted(remove, reverse=True):
        del lines[first - 1 : last]
    return "".join(lines)


def generate_python_deployer(nodes: list[pt.Model]) -> str:
    """The all-in-one Python deployer: every action of every pass, as
    native Python functions over the shared emitters (2026-08-19,
    refactored 2026-08-23). Kernel routers and services are generated BODY
    functions (run_cmd/write_file calls), not shell scripts: their systemd
    units exec this file's `--job NAME --job-action up|down`."""
    service_manifest = _service_manifest(nodes)
    install_plans = {
        node.key.host: _install_plan(node, nodes) for node in nodes if node.kind == "infra.host"
    }
    bidirectional: dict[str, dict[str, dict[str, Any]]] = {}
    for host, plan in install_plans.items():
        per_host: dict[str, dict[str, Any]] = {}
        for name, resolved in plan.get("resolved", {}).items():
            if resolved["kind"] == "apt":
                concrete = [
                    {"kind": "apt-dep", "package": package} for package in resolved["packages"]
                ]
            else:
                concrete = []
                for required in resolved.get("requires", []):
                    required_resolved = plan.get("resolved", {}).get(required)
                    if required_resolved and required_resolved["kind"] == "apt":
                        concrete.extend(
                            {
                                "kind": "apt-dep",
                                "package": package,
                                "order": 0,
                            }
                            for package in required_resolved["packages"]
                        )
                concrete.append(
                    {
                        "kind": "sys-cmd-dep",
                        "name": name,
                        "cmd": resolved["cmd"],
                        "order": 10,
                    }
                )
            per_host[name] = {
                "abstract": {"kind": "abstract", "name": name},
                "concrete": concrete,
            }
        for entry in plan["jobs"]:
            if entry["kind"] != "image":
                continue
            descriptor = {
                "kind": "docker-image",
                "image": entry["image"],
                "dockerfile": entry["dockerfile"],
                "order": 20,
            }
            per_host[f"docker-image:{entry['image']}"] = {
                "abstract": {"kind": "docker-image", "image": entry["image"]},
                "concrete": [descriptor],
            }
        bidirectional[host] = per_host

    host_defs: list[str] = []
    host_entries: list[str] = []
    job_funcs: list[str] = []
    job_entries: list[str] = []
    configure_entries: list[str] = []
    tree_defs: list[str] = []
    tree_entries: list[str] = []
    create_defs: list[str] = []
    create_entries: list[str] = []
    delete_defs: list[str] = []
    delete_entries: list[str] = []
    for node in nodes:
        if node.kind != "infra.host":
            continue
        host_name = node.key.host

        # Every scope of this host, in declaration order: the lazy phase
        # walks them all (forward to create, rewound to delete).
        scopes: list[tuple[str, str]] = []

        def scope(name: str, label: str, body: Callable[[Emitter], None]) -> str:
            """Declare one scope of this host; returns its declare fn."""
            declare = _declare_fn_name(host_name, name)
            job_funcs.append(_declare_fn(declare, label, body))
            scopes.append((label, declare))
            return declare

        scope("host", f"host:{host_name}", functools.partial(_emit_host_script, node, nodes))
        role = node.data.ovnRole.value if node.data.ovnRole is not None else None
        host_entries.append(
            f"    {host_name!r}: {{\n"
            f'        "create": {_host_fn_name(host_name, "create")},\n'
            f'        "delete": {_host_fn_name(host_name, "delete")},\n'
            f'        "lazy": {_host_fn_name(host_name, "lazy")},\n'
            f'        "role": {role!r},\n'
            "    },"
        )

        configure_declare = scope(
            "configure",
            f"{host_name}:configure",
            functools.partial(_emit_host_configure, node, nodes),
        )
        configure_fns: list[str] = []
        for action, label in (("create", "up"), ("delete", "down")):
            fn = _body_fn_name(host_name, "configure", label)
            job_funcs.append(
                _apply_fn(
                    fn, action, host_name, f"{host_name}:configure ({label})", configure_declare
                )
            )
            configure_fns.append(fn)
        configure_entries.append(f"    {host_name!r}: {configure_fns[0]},")
        # The service's ExecStart/ExecStop run this job: `up` drives the
        # tree (and blocks until SIGTERM); `down` rewinds what the tree
        # created — every router/service, then configure.
        tree_job = f"ovn-fabric-{host_name}"
        up_fn = _body_fn_name(host_name, "run", "up")
        down_fn = _body_fn_name(host_name, "run", "down")
        job_funcs.append(
            f"async def {up_fn}(ctx: Ctx, job: Job) -> None:\n"
            f"    await _run_host(ctx, {host_name!r})\n"
        )
        down_all = _body_fn_name(host_name, "all", "down")
        job_funcs.append(
            f"async def {down_fn}(ctx: Ctx, job: Job) -> None:\n"
            f"    await {down_all}(ctx, job)\n"
        )
        job_entries.append(f"    {tree_job!r}: ({up_fn}, {down_fn}),")

        declarations: dict[str, Callable[[Emitter], None]] = {}
        for owner in _kernel_router_owners(node.id, nodes):
            declarations[owner.key.name] = functools.partial(
                _emit_kernel_router_owner, owner, _netns_name(owner.key.name), nodes
            )
        for n in nodes:
            if n.kind not in ("kernel.app.container", "kernel.app.service"):
                continue
            if n.data.host != node.id:
                continue
            faces = _service_faces(n.key.name, host_name, nodes)
            declarations[n.key.name] = functools.partial(_emit_service_up, n, faces)
        bodies: dict[str, tuple[str, str]] = {}
        for name, body in declarations.items():
            declare = scope(name, f"{host_name}:{name}", body)
            functions: list[str] = []
            for action, label in (("create", "up"), ("delete", "down")):
                fn = _body_fn_name(host_name, name, label)
                job_funcs.append(
                    _apply_fn(fn, action, host_name, f"{host_name}:{name} ({label})", declare)
                )
                functions.append(fn)
            bodies[name] = (functions[0], functions[1])

        # Last scope: start the service (the lazy phase) once every
        # scope's synchronous part is there; its rewind stops it first.
        scope("start", f"host:{host_name}:start", functools.partial(_emit_fabric_start, node))
        scope_list = "[" + ", ".join(f"({label!r}, {declare})" for label, declare in scopes) + "]"
        for action in ("create", "delete", "lazy"):
            signature = (
                "ctx: Ctx, job: Job, action: LifecycleAction"
                if action == "lazy"
                else "ctx: Ctx, job: Job"
            )
            call = {
                "create": f"_create_scopes(ctx, job, {host_name!r}, {scope_list})",
                "delete": f"_delete_scopes(ctx, job, {host_name!r}, {scope_list})",
                "lazy": f"_lazy_scopes(ctx, job, action, {host_name!r}, {scope_list})",
            }[action]
            host_defs.append(
                f"async def {_host_fn_name(host_name, action)}({signature}) -> None:\n"
                f"    await {call}\n"
            )
        down_calls = [
            f"    await {down}(ctx, job)" for _up, down in reversed(list(bodies.values()))
        ] + [f"    await {configure_fns[1]}(ctx, job)"]
        job_funcs.append(
            f"async def {down_all}(ctx: Ctx, job: Job) -> None:\n" + "\n".join(down_calls) + "\n"
        )

        plan = install_plans[host_name]
        image_deps = (
            [{"kind": "abstract", "name": "docker"}]
            if any(entry["kind"] == "image" for entry in plan["jobs"])
            else []
        )
        custom_installers: dict[str, list[dict[str, str]]] = {}
        if "zerotier" in (node.data.dependencies or []):
            custom_installers["zerotier"] = [{"kind": "apt-dep", "package": "curl"}]
        configure_deps = _configure_dependencies(node, nodes)
        job_deps = _job_dependencies(node, nodes)
        services = service_manifest[host_name]
        tree_fn = _run_fn_name(host_name)
        tree_lines = [
            f"def {tree_fn}(ctx: Ctx) -> Job:",
            "    root = _root_job()",
            f"    configure = _run_common(ctx, root, {host_name!r}, "
            f"{image_deps!r}, {configure_deps!r}, {custom_installers!r})",
        ]
        for svc in services:
            up, down = bodies[svc["name"]]
            deps = job_deps.get(svc["name"], ["configure"])
            tree_lines.append(
                f"    _python_job(ctx, configure, {svc['name']!r}, {deps!r}, {up}, {down})"
            )
        tree_lines.append("    return root")
        tree_defs.append("\n".join(tree_lines))
        tree_entries.append(f"    {host_name!r}: {tree_fn},")

        all_deps = [
            entry["abstract"]
            for entry in bidirectional[host_name].values()
            if entry["abstract"].get("kind") == "abstract"
        ]
        # Create runs every scope's synchronous part: it needs what any
        # of them needs.
        create_deps = all_deps
        cluster = node.data.ovnRole == pt.OvnRole.central
        create_fn = _run_fn_name(host_name).replace("_run_", "_create_")
        create_lines = [
            f"def {create_fn}(ctx: Ctx) -> Job:",
            "    root = _root_job()",
            f"    _resolve_dependency_job(ctx, root, {host_name!r}, [], "
            f"repeat=False, with_images=False, "
            f"custom_installers={custom_installers!r})",
            "    async def create_action(job: Job) -> None:",
            f"        await HOSTS[{host_name!r}]['create'](ctx, job)",
            # The host unit's up job runs CONFIGURE as its first step.
            # Running it here TOO made HOSTS create and the service race
            # the same add-br/add-port commands; --may-exist used to hide
            # that, but create is strict now. Only the service configures.
            "        job.emit({'kind': 'abstract-dep', 'name': 'create-ready', "
            "'state': 'ready'}, queue=True)",
            "    create_job = root.job('ovn-fabric(create)', initiate=True)",
            f"    register(create_job, {create_deps!r}, create_action, timeout=ctx.opts.timeout)",
        ]
        if cluster:
            create_lines += [
                "    async def cluster_action(job: Job) -> None:",
                f"        lc = LifecycleController(ctx, job, LifecycleAction.CREATE, "
                f"{host_name!r}, name='cluster')",
                f"        await cluster(lc, HostPrimitive({host_name!r}))",
                "        await lc.execute()",
                "    cluster_job = root.job('cluster-create', initiate=True)",
                f"    register(cluster_job, "
                f"[{_abstract('ovn')!r}, {_abstract('create-ready')!r}], cluster_action, "
                "timeout=ctx.opts.timeout)",
            ]
        create_lines.append("    return root")
        create_defs.append("\n".join(create_lines))
        create_entries.append(f"    {host_name!r}: {create_fn},")

        delete_fn = _run_fn_name(host_name).replace("_run_", "_delete_")
        delete_lines = [
            f"def {delete_fn}(ctx: Ctx) -> Job:",
            "    root = _root_job()",
            f"    _resolve_dependency_job(ctx, root, {host_name!r}, [], "
            f"repeat=False, with_images=False, "
            f"custom_installers={custom_installers!r})",
        ]
        delete_deps = list(all_deps)
        if cluster:
            delete_lines += [
                "    async def cluster_action(job: Job) -> None:",
                f"        lc = LifecycleController(ctx, job, LifecycleAction.DELETE, "
                f"{host_name!r}, name='cluster')",
                f"        await cluster(lc, HostPrimitive({host_name!r}))",
                "        await lc.execute()",
                "        job.emit({'kind': 'abstract-dep', "
                "'name': 'cluster-deleted', 'state': 'ready'}, queue=True)",
                "    cluster_job = root.job('cluster-delete', initiate=True)",
                f"    register(cluster_job, [{_abstract('ovn')!r}], cluster_action, "
                "timeout=ctx.opts.timeout)",
            ]
            delete_deps.append({"kind": "abstract", "name": "cluster-deleted"})
        delete_lines += [
            # The rewind of the host declaration: stopping the service
            # rewinds its routers/services and configure (ExecStop).
            "    async def delete_action(job: Job) -> None:",
            f"        await HOSTS[{host_name!r}]['delete'](ctx, job)",
            "    delete_job = root.job('ovn-fabric(delete)', initiate=True)",
            f"    register(delete_job, {delete_deps!r}, delete_action, timeout=ctx.opts.timeout)",
            "    return root",
        ]
        delete_defs.append("\n".join(delete_lines))
        delete_entries.append(f"    {host_name!r}: {delete_fn},")
        for name, (up, down) in bodies.items():
            job_entries.append(f"    {name!r}: ({up}, {down}),")

    cluster_defs = _python_cluster_fn(
        "cluster",
        "cluster",
        lambda emit: _emit_cluster_body(nodes, emit),
    )

    source = (
        _GENERATED_RUNTIME.replace("__CLUSTER_FUNCS__", cluster_defs)
        .replace("__HOST_FUNCS__", "\n\n".join(host_defs))
        .replace("__HOSTS__", "\n".join(host_entries))
        .replace("__SERVICES__", repr(service_manifest))
        .replace(
            "__INSTALL__",
            "\n".join(f"    {host!r}: {plan!r}," for host, plan in install_plans.items()),
        )
        .replace(
            "__BIDIRECTIONAL_DEPS__",
            "\n".join(f"    {host!r}: {per_host!r}," for host, per_host in bidirectional.items()),
        )
        .replace("__JOB_FUNCS__", "\n\n".join(job_funcs))
        .replace("__JOB_BODIES__", "\n".join(job_entries))
        .replace("__CONFIGURE__", "\n".join(configure_entries))
        .replace("__TREE_FUNCS__", "\n\n".join(tree_defs))
        .replace("__TREES__", "\n".join(tree_entries))
        .replace("__CREATE_FUNCS__", "\n\n".join(create_defs))
        .replace("__DELETE_FUNCS__", "\n\n".join(delete_defs))
        .replace("__CREATE_JOBS__", "\n".join(create_entries))
        .replace("__DELETE_JOBS__", "\n".join(delete_entries))
    )
    return _strip_unused_helpers(source)
