"""Kernel routers: the netns, its sides and their networks, and its kernel apps."""

from __future__ import annotations

import dataclasses
import hashlib
import posixpath

from fabric.addresses import Addresses
from fabric.decide import Decide
from fabric.primitives.host import HostPrimitive, NetnsPrimitive
from ladops import linux_net as linux_net_ops
from ladops import netns as netns_ops
from protocol import KernelApp
from protocol import generated as pt

from .container import _start_owning_container
from .emitter import _BINDABLE_IFACE_KINDS, Emitter
from .images import _docker_dockerfile, _emit_app_image
from .lookups import _dummy_name, _iface_real_name, _kernel_router_sides, _side
from .security_groups import _emit_security_group_rules, _masq_families, _security_groups_from_refs


def _apply_side_network(
    groups: list[pt.SecurityGroupNode],
    ipaddrs: list[str] | None,
    routes: list[pt.KernelRouterRoute] | None,
    addrs: Addresses,
    netns: str,
    dev: str,
    emit: Emitter,
    netns_primitive: NetnsPrimitive | None = None,
    netns_var: str | None = None,
) -> None:
    """Apply a kernel router side's static addressing (addresses + routes)
    and security-group rules (masq) to a target namespace — the router's
    netns OR a docker container's netns (the dhcpcd-in-docker path). The
    ONLY difference between the two is the namespace name, so both go
    through this one function (2026-08-31)."""
    for addr in ipaddrs or []:
        if netns_primitive is not None:
            emit.ovn(netns_primitive.add_addr(dev, addr), parent=netns_var)
        else:
            argv = linux_net_ops.add_addr_argv(addr, dev)
            emit.sh(netns_ops.netns_exec_argv(argv, netns))
    for route in routes or []:
        via = addrs.ref(route.via) if route.via is not None else None
        if netns_primitive is not None:
            emit.ovn(
                netns_primitive.add_route(route.dst, dev, via, route.metric),
                parent=netns_var,
            )
        else:
            argv = linux_net_ops.add_route_argv(route.dst, dev, via, route.metric)
            emit.sh(netns_ops.netns_exec_argv(argv, netns))
    for group in groups:
        _emit_security_group_rules(group, netns, dev, emit, addrs, netns_primitive, netns_var)


def _wg_conf_lines(app: pt.WireguardApp) -> list[str]:
    """The wg-quick conf body for a wireguard app — mirrors
    generate-netns.ts's emitWireguardInterface byte-for-byte."""
    cfg = app.config
    lines = [
        "[Interface]",
        f"PrivateKey = {cfg.privateKey}",
        f"Address = {cfg.address}",
    ]
    if cfg.listenPort is not None:
        lines.append(f"ListenPort = {cfg.listenPort:g}")
    if cfg.dns is not None:
        lines.append(f"DNS = {cfg.dns}")
    lines.extend(
        [
            "[Peer]",
            f"PublicKey = {cfg.peer.publicKey}",
            f"AllowedIPs = {cfg.peer.allowedIps}",
            f"Endpoint = {cfg.peer.endpoint}",
        ]
    )
    if cfg.peer.persistentKeepalive is not None:
        lines.append(f"PersistentKeepalive = {cfg.peer.persistentKeepalive:g}")
    return lines


@dataclasses.dataclass
class _SideContext:
    """The facts a kernel router SIDE carries into its apps — the target
    netns and the side's real interface `dev`."""

    netns: str
    dev: str
    addrs: Addresses


def _emit_kernel_app_rules(
    apps: list[KernelApp],
    router: str,
    host: HostPrimitive,
    netns_var: str,
    emit: Emitter,
    ctx: _SideContext,
    nodes: list[pt.Model],
) -> None:
    """Start (create) / stop (delete) the apps attached to one side of a
    kernel router, INSIDE the router's netns — no per-app systemd units.
    The kernel router job owns
    them: its `up` starts them, its `down` stops them. Starts that are
    daemons or block (dhclient -d, the docker wire script which ends in
    `docker wait`, the zerotier `up` that waits ~40s for the interface)
    are backgrounded with `&` so the router `up`/`enable --now` returns
    as soon as the netns + veths exist (the transit veth must be present
    before _emit_iface_bindings binds it); wg-quick is in-kernel and
    returns immediately, so it stays in the foreground. Delete stops the
    apps BEFORE `ip netns delete` — a running app daemon holds its netns
    alive (2026-08-23)."""
    netns = ctx.netns
    dev = ctx.dev
    for app in apps:
        if app.kind == "dhcp-client":
            if app.style == pt.Style.dhclient:
                # `-lf` gives each router's dhclient its OWN lease database
                # (per-router path, not the host's shared
                # /var/lib/dhcp/dhclient.leases) — runtime-configurable,
                # nothing compiled in (2026-08-30).
                lease = f"/var/lib/dhcp/dhclient.{router}.leases"
                app_unit = emit.unit("kernel app", f"dhcp-client (dhclient) on {dev} in {netns}")
                app_unit.ovn(host.make_dir(posixpath.dirname(lease)))  # the lease's directory
                app_unit.ovn(host.netns(netns).dhclient(dev, lease), parent=netns_var)
            else:
                emit.comment(
                    f'# unsupported dhcp-client style "{app.style}" on {dev} in {netns} — '
                    "skipped, see fabric/emit.py"
                )
                continue
        elif app.kind == "docker":
            # The container gets ONE veth injected INTO the router netns
            # (CNI/Multus-style interface injection — the same primitive
            # a k8s pod + Multus uses, 2026-08-23). `--network host`
            # would share the docker DAEMON's netns (root), not the
            # router's, so the container runs `--network none` and the
            # generated Python attaches it: docker run (none) -> get its
            # PID -> veth pair -> one end into the container's netns
            # (eth0), the other into the router netns with `router_ip`;
            # the container's default route is via the router.
            container = app.name or f"{router}-docker"
            # Else: the client mode — one veth injected into the router netns
            # (CNI/Multus-style interface injection — the same primitive
            # a k8s pod + Multus uses, 2026-08-23). `--network host`
            # would share the docker DAEMON's netns (root), not the
            # router's, so the container runs `--network none` and the
            # generated Python attaches it: docker run (none) -> get its
            # PID -> veth pair -> one end into the container's netns
            # (eth0), the other into the router netns with `router_ip`;
            # the container's default route is via the router.
            if app.ip is None or app.routerIp is None:
                raise ValueError(
                    f'kernel app docker "{container}": the veth injection needs an `ip` '
                    "(the container's address on the router's services segment)"
                )
            app_unit = emit.unit("kernel app", f"docker ({container}) on {dev} in {netns}")
            veth = app.vethName or ("ve-" + hashlib.md5(container.encode()).hexdigest()[:8])
            container_ip = ctx.addrs.one(app.ip)
            router_ip = ctx.addrs.one(app.routerIp)
            gateway = router_ip.split("/")[0]
            image = app.image or f"ovn-fabric-{container}"
            app_unit.helper(
                "_docker_veth_up",
                container,
                image,
                _docker_dockerfile(app.build) if app.build else None,
                list(app.cmd or []),
                netns,
                veth,
                router_ip,
                container_ip,
                gateway,
                # Stopping a docker container is done THE HARD WAY — an
                # explicit `docker rm -f` via the daemon (SIGTERM through
                # the router's stop is not a reliable container shutdown).
                undo="_docker_down",
                undo_args=(container,),
            )
        elif app.kind == "wireguard":
            # A WireGuard tunnel inside the netns (the tunnelRouterEndpoint's
            # middle leg): write the conf (PrivateKey included — the
            # documented deliberate credential-in-git exception), `wg-quick
            # up` inside the netns (wg is in-kernel, returns immediately, no
            # daemon to supervise — so no backgrounding), and MASQUERADE the
            # declared families out the TUNNEL iface (not the upstream veth
            # — the tunnel is the egress, 2026-08-23). Down brings it down
            # and removes the rules before `ip netns delete`.
            if app.ifaceName is None or app.config is None:
                raise ValueError(
                    f"kernel app wireguard on {dev} in {netns}: missing ifaceName/config"
                )
            conf_path = f"/etc/wireguard/{app.ifaceName}.conf"
            conf = "\n".join(_wg_conf_lines(app))
            app_unit = emit.unit("kernel app", f"wireguard ({app.ifaceName}) in {netns}")
            app_unit.ovn(host.make_dir("/etc/wireguard"))
            # Its private key: only root reads it, from the moment it is there.
            app_unit.write_file(conf_path, conf, 0o600, None, None)
            netns_primitive = host.netns(netns)
            # Up with the config it should have: run leaves it; with another
            # one it brings it down and up again (fabric/decide.py).
            app_unit.ovn(
                netns_primitive.wireguard(app.ifaceName, conf_path, conf),
                parent=netns_var,
                decide=Decide.wireguard,
            )
            for family in _masq_families(list(app.securityGroups), nodes):
                app_unit.ovn(
                    netns_primitive.add_masq(family, app.ifaceName),
                    parent=netns_var,
                )
        elif app.kind == "zerotier":
            # A ZeroTier tunnel inside the netns (the tunnelRouterEndpoint's
            # middle leg): the zerotier-one daemon runs confined to the
            # netns with its OWN instanceDir, joins the network, and the
            # real interface name is captured at RUNTIME (ZeroTier names
            # it itself) — so the wiring is the script (like docker). The
            # router `up` runs it in the background (its up waits up to
            # ~40s for ZeroTier to name the interface, so it must not
            # block `enable --now`); the router `down` runs it with `down`
            # to kill the daemon (no `zerotier-cli leave`) BEFORE `ip
            # netns delete` (a running daemon holds its netns alive,
            # 2026-08-23).
            if app.networkId is None or app.instanceDir is None:
                raise ValueError(
                    f"kernel app zerotier on {dev} in {netns}: missing networkId/instanceDir"
                )
            app_unit = emit.unit("kernel app", f"zerotier in {netns}")
            masq = _masq_families(list(app.securityGroups), nodes)
            routes = [route.dst for route in (app.routes or [])]
            # The daemon is the job's tracked foreground child; the runtime
            # kills it on stop, and its rules/routes go with the netns.
            app_unit.helper("_zerotier_up", netns, app.instanceDir, app.networkId, masq, routes)
        else:
            emit.comment(
                f'# unsupported kernel app kind "{app.kind}" on {dev} in {netns} — '
                "skipped, see fabric/emit.py"
            )
            continue


# One real netns per KernelRouter (types.ts) bound to this host, then —
# per side — a real interface when the side declares `ifaces`
# (KernelRouterSide.ifaces, types.ts — populated for `left` by the
# implicit transit veth and for `right` by buildKernelRouterEndpoint,
# define.ts, 2026-08-18): `up` creates the kernel VLAN sub-interface
# or the transit veth pair (if any) in the root namespace, moves the
# netns-side device into the netns, and binds that side's
# already-resolved addresses/routes to it. A side with no `ifaces` keeps
# the dummy-device stand-in (the pre-2026-08-18 behavior). `down` removes
# the moved-in vlans from INSIDE the netns first (an explicit `ip link
# del` from inside cleans the parent's 8021q registry, while `ip netns
# delete` alone leaks a stale entry — hit live 2026-08-19 on an LXC),
# then tears down the netns (which destroys everything still inside it —
# the veth's netns-side leg), then removes the transit veth's ROOT-side
# leg, which lives in the global namespace (2026-08-19).
#
# `ip link add ... type vlan/type veth/type dummy` runs OUTSIDE the netns
# (a
# device is only visible to `ip link set <dev> netns <netns>` from
# whichever namespace it's CURRENTLY in — same reasoning ladops.linux_
# net.add_if_to_netns already documents, and the same create-in-global/
# move-in sequence this module already uses for a real host's VLAN
# sub-interfaces), THEN moved into the netns with that same `ip link
# set ... netns` call — confirmed live, 2026-08-12. Only after the move
# does anything run via `ip netns exec`: bring the link up, THEN assign
# addresses/routes to it — up before addr/route, not after (a route
# through a device that isn't up yet fails; an address on one is
# pointless until it is).
def _veth_root_bridge(root_leg: str, nodes: list[pt.Model]) -> str | None:
    """The OVS bridge a kernel-router veth's ROOT leg belongs to, from the
    same ovn.ls interface entry _emit_iface_bindings uses."""
    for node in nodes:
        if node.kind == "ovn.ls":
            entries = node.data.interfaces or []
        elif node.kind == "bridge":
            entries = node.data.ports or []
        else:
            continue
        for entry in entries:
            iface = entry.iface
            if iface.kind != "veth":
                continue
            if root_leg in (iface.ifaceName, iface.peerName):
                return iface.bridge
    return None


def _emit_kernel_router_owner(
    owner: pt.KernelRouterNode,
    netns: str,
    nodes: list[pt.Model],
    addrs: Addresses,
    emit: Emitter,
) -> None:
    """One kernel router's declaration: its `up` creates it, its `down`
    is the same declaration rewound (the netns' moved-in links are deleted
    from inside the netns before the netns itself). See the module doc
    block above for the netns/side semantics."""
    host = HostPrimitive(owner.data.host.removeprefix("host:"))
    # A router whose container owns its interfaces has no netns of its own:
    # the container's is it. Its image is a unit of its own, first in the
    # scope (ADR 0012 §4: built in the background, in parallel); the rest is
    # one unit: a restart of the container makes everything in its netns again.
    owning = _owning_app(owner, nodes)
    if owning is not None:
        _emit_app_image(owning[0], owning[1], host, emit)
    kernel_var = emit.local("kernel")
    netns_var = emit.local(netns)
    netns_unit = emit.unit("kernel router", owner.key.name, key=owner.id)
    netns_unit.ovn(host.rootNs(), bind=kernel_var)
    docker = None
    if owning is None:
        kernel_netns = host.netns(netns)
        netns_unit.ovn(kernel_netns, bind=netns_var)
    else:
        docker = _start_owning_container(owning[0], owning[1], host, netns_unit)
        netns = owning[1]
        kernel_netns = docker.get_netns()
        netns_unit.ovn(kernel_netns, bind=netns_var)
        # A sysctl is set every run: no undo, the same value again.
        for key in ("net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"):
            netns_unit.ovn(
                kernel_netns.set_sysctl(key, "1"), parent=netns_var, decide=Decide.always
            )
    for side_node in _kernel_router_sides(owner.key.name, nodes):
        iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
        if iface is not None and iface.kind not in _BINDABLE_IFACE_KINDS:
            emit.comment(
                f'# unsupported kernel router side interface kind "{iface.kind}" '
                f"for {owner.key.name} ({_side(side_node).value}) — skipped, "
                "see fabric/emit.py"
            )
            continue
        if iface is not None and iface.kind == "veth":
            # The netns-side leg — `peerName` is the root-side leg
            # the transit domain's bridge attaches to (see
            # _emit_iface_bindings, which runs after this
            # block and finds the pair already created here). The
            # real device names come from the IFNAMSIZ-safe
            # `shortName` (src/ir.ts's kernelRouterSideToIR), not
            # the long `ifaceName`/`peerName` the model carries.
            dev = f"veth-krn-{iface.shortName}"
        else:
            dev = _iface_real_name(iface) if iface is not None else _dummy_name(_side(side_node))
        side_unit = (
            netns_unit
            if docker is not None
            else emit.unit("kernel router side", f"{owner.key.name} ({_side(side_node).value})")
        )
        if iface is not None:
            if iface.kind == "vlan":
                side_unit.ovn(
                    kernel_netns.add_link(
                        dev,
                        "vlan",
                        parent=iface.vlanParent.name,
                        vlan_id=iface.vlanId,
                    ),
                    parent=netns_var,
                )
            elif iface.kind == "veth":
                # `mac` (src/ir.ts's kernelRouterSideToIR) pins the
                # netns-side leg's MAC so the OVN router's MAC_Binding
                # for this side stays valid across a down/up.
                root_leg = f"veth-ovn-{iface.shortName}"
                # The bridge itself belongs to the host's configure
                # declaration (_emit_iface_bindings); the router only
                # plugs its root leg into it.
                bridge = _veth_root_bridge(root_leg, nodes)
                side_unit.ovn(
                    kernel_netns.add_link(
                        dev,
                        "veth",
                        mac=iface.mac or addrs.mac(side_node.data.ipaddrs, iface.ifaceName),
                        root_leg=root_leg,
                        bridge=bridge,
                    ),
                    parent=netns_var,
                )
            elif iface.kind == "physical":
                side_unit.ovn(kernel_netns.add_link(dev, "physical"), parent=netns_var)
        else:
            side_unit.ovn(kernel_netns.add_link(dev, "dummy"), parent=netns_var)
        # A kernel router's REAL-WORLD side (right) learns its IPv6 via
        # RA/SLAAC — the config's via-less `::/0` means exactly that.
        # `accept_ra=2` forces it to keep accepting RAs even though the
        # netns has forwarding on (which would otherwise force
        # accept_ra=0) — without it the WAN leg never gets an IPv6
        # default/address and clients' v6 dies at the kernel router
        # (confirmed live, 2026-08-30). Written to the proc file
        # directly: `sysctl` splits its key on every dot, so a vlan
        # iface name (`eth0.2280`) can't be a sysctl key component
        # ("cannot stat .../eth0/2280/accept_ra").
        if side_node.key.side == pt.Side.right:
            side_unit.ovn(
                kernel_netns.set_sysctl_file(f"/proc/sys/net/ipv6/conf/{dev}/accept_ra", "2"),
                parent=netns_var,
            )
        # The side's static addressing + security-group (masq) rules,
        # applied inside the netns — SAME code as the dhcpcd-in-docker
        # container path (only the target namespace differs).
        groups = _security_groups_from_refs(
            list(side_node.data.securityGroups or []),
            nodes,
            pt.Render.kernel,
            f"kernel router {owner.key.name} ({_side(side_node).value})",
        )
        _apply_side_network(
            groups,
            addrs.list(side_node.data.ipaddrs),
            list(side_node.data.routes or []),
            addrs,
            netns,
            dev,
            side_unit,
            kernel_netns,
            netns_var,
        )
        # The side's apps (right side in practice) run inside the
        # netns on this interface — started after the interface is up
        # and bound, so the DHCP client can immediately acquire a
        # lease on a ready device.
        apps = [a for a in side_node.data.apps or [] if owning is None or a is not owning[0]]
        if apps:
            _emit_kernel_app_rules(
                apps,
                owner.key.name,
                host,
                netns_var,
                side_unit,
                _SideContext(
                    netns=netns,
                    dev=dev,
                    addrs=addrs,
                ),
                nodes,
            )
    # Its app starts once its interfaces are there, and with its container.
    # A device the app makes itself is wired from here, not in its image:
    # its masq needs only the name; a route needs the device.
    if docker is not None and owning is not None:
        app = owning[0]
        if app.tunnel is not None:
            for family in _masq_families(list(app.securityGroups or []), nodes):
                netns_unit.ovn(kernel_netns.add_masq(family, app.tunnel.iface), parent=netns_var)
        netns_unit.ovn(
            docker.exec_detached([*(app.cmd or ["sh"]), owning[2]]),
            decide=Decide.docker_exec,
        )
        if app.tunnel is not None:
            netns_unit.ovn(
                kernel_netns.wait_link_up(app.tunnel.iface),
                parent=netns_var,
                decide=Decide.always,
            )
            for dst in app.tunnel.routes:
                netns_unit.ovn(kernel_netns.add_route(dst, app.tunnel.iface), parent=netns_var)


def _owning_app(
    owner: pt.KernelRouterNode, nodes: list[pt.Model]
) -> tuple[pt.DockerApp, str, str] | None:
    """The docker app that OWNS the router's interfaces ("the container IS the
    router", dhcpcd-in-docker): (app, its container, its side's device). It
    carries NO veth addressing in the IR — that structural fact (no `ip`) is
    how the TS side says so, not a concept name (2026-08-31)."""
    for side_node in _kernel_router_sides(owner.key.name, nodes):
        for app in side_node.data.apps or []:
            if app.kind == "docker" and app.ip is None:
                iface = side_node.data.ifaces[0].iface if side_node.data.ifaces else None
                if iface is None:
                    dev = _dummy_name(_side(side_node))
                elif iface.kind == "veth":
                    dev = f"veth-krn-{iface.shortName}"
                else:
                    dev = _iface_real_name(iface)
                return app, app.name or f"{owner.key.name}-docker", dev
    return None
