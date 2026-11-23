"""Per-host interface bindings: real VLANs/NICs on OVS bridges, and the XDP trunk."""

from __future__ import annotations

from typing import Protocol

from fabric.primitives.host import HostPrimitive
from protocol import generated as pt

from .emitter import _BINDABLE_IFACE_KINDS, Emitter
from .lookups import _host_bindings, _host_bridge_bindings, _iface_real_name, _network_name

# The real (host, interface) pairs this host owns: CREATE creates the
# kernel VLAN sub-interface (if any) or brings the transit veth's root
# leg up, creates the bridge, adds the interface as a port, and collects
# one ovn-bridge-mappings entry per binding — a single comma-joined
# external-ids write, not one per domain (ovs-vsctl's column is one
# string; a second `set` would overwrite the first, not append to it).
# DELETE tears the same down: del-br cascades its own ports (no separate
# del-port call — same reasoning as ladops.ovn.ls_del_argv), plus an
# explicit vlan link delete, and removes the ovn-bridge-mappings entry.
# Create/delete branches adjacent, 2026-08-19 (flag schema).
# ── XDP trunk ────────────────────────────────────────────────────────
# One netdev bridge per host owns every AF_XDP port. A single AF_XDP
# port can only live on ONE bridge, so it cannot also be the per-domain
# segment bridge (there is one of those per collision domain): each
# segment instead gets a veth pair, tagged on the trunk side, whose
# other end is the segment bridge — which therefore stays `system` and
# keeps its patch to `br-int`. The VLAN split lives in OVS (`tag=`), not
# in the kernel: an xdp parent has no `<parent>.<vlan>` device.
_XDP_TRUNK_BRIDGE = "br-xdp"


# The per-segment veths are kernel netdevs; OVS's netdev datapath accepts
# kernel netdevs (the same pattern as its `br-phy` underlay bridge), so
# only this one bridge is netdev and the rest of the fabric stays `system`.


def _short_suffix(name: str) -> str:
    """FNV-1a 32 of `name`, first 6 hex chars — the IFNAMSIZ-safe suffix
    for the per-segment legs (`veth-xdp-<6>` / `veth-seg-<6>`, 15 chars
    each). Same fold src/ir.ts uses for over-long bridge names."""
    h = 0x811C9DC5
    for byte in name.encode():
        h ^= byte
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"[:6]


class _XdpPort(Protocol):
    """What an AF_XDP port looks like to this module, whether it arrives
    as a VLAN parent (`generated.VlanParent`) or as a standalone
    `ovn.ls` iface (`generated.OvnLsIface1` — datamodel-codegen's name
    for the anonymous `OvnLsIfaceXdp` component). Structural so the
    emitter never depends on that generated class name."""

    name: str
    mode: pt.Mode
    nRxq: int


def _xdp_trunks(
    bindings: list[tuple[str, pt.OvnLsIface]],
) -> dict[str, list[int]]:
    """VLAN ids each AF_XDP parent must trunk. The parent is a trunk (the
    wire's tagged frames arrive on it); the per-segment veth legs are the
    access ports (`tag=<vid>`)."""
    trunks: dict[str, list[int]] = {}
    for _domain, iface in bindings:
        if iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
            trunks.setdefault(iface.vlanParent.name, []).append(iface.vlanId)
    return trunks


def _xdp_ports(
    bindings: list[tuple[str, pt.OvnLsIface]],
) -> dict[str, _XdpPort]:
    """Every AF_XDP port this host owns, keyed by netdev name — one
    entry whether the port appears as a VLAN parent or as a standalone
    `xdp` interface. Deduping here is what makes "five segments, one
    port" possible."""
    ports: dict[str, _XdpPort] = {}
    for _domain, iface in bindings:
        if iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
            ports.setdefault(iface.vlanParent.name, iface.vlanParent)
        elif iface.kind == "xdp":
            ports.setdefault(iface.name, iface)
    return ports


def _emit_xdp_leg(
    host: HostPrimitive,
    kernel_var: str,
    segment_bridge: str,
    vlan_id: int | None,
    emit: Emitter,
) -> None:
    suffix = _short_suffix(segment_bridge)
    trunk_leg = f"veth-xdp-{suffix}"
    seg_leg = f"veth-seg-{suffix}"
    emit.ovn(
        host.kernel.add_if(trunk_leg, kind="veth", peer=seg_leg, up=True),
        parent=kernel_var,
    )
    emit.ovn(host.ovs_port(_XDP_TRUNK_BRIDGE, trunk_leg, tag=vlan_id))
    emit.ovn(host.ovs_port(segment_bridge, seg_leg))


def _emit_iface_bindings(host_name: str, nodes: list[pt.Model], emit: Emitter) -> None:
    """Same body for the deploy pass and the boot bindings unit. It only
    ensures the OVS bridge/port rows and the netdevices it OWNS (kernel
    VLANs, XDP veth legs); the transit veths are created and upped by
    their kernel-router units, which may not have run yet here."""
    before = len(emit.lines)
    bindings = _host_bindings(nodes).get(host_name, [])
    bridge_bindings = _host_bridge_bindings(nodes).get(host_name, [])
    ovn_domains = {domain for domain, _iface in bindings}
    all_bindings = bindings + bridge_bindings
    host = HostPrimitive(host_name)
    # The root namespace is the parent of every host-local link node.
    kernel_var = emit.local("kernel")
    root_unit = emit.unit("root namespace", host_name)
    root_unit.ovn(host.rootNs(), bind=kernel_var)
    mappings: list[str] = []
    xdp_ports = _xdp_ports(all_bindings)
    xdp_trunks = _xdp_trunks(all_bindings)
    if xdp_ports:
        trunk_unit = emit.unit("XDP trunk", "userspace datapath, one port for all segments")
        trunk_unit.ovn(host.ovs_bridge(_XDP_TRUNK_BRIDGE, datapath="netdev"))
        for name, port in xdp_ports.items():
            trunk_unit.ovn(
                host.ovs_afxdp_port(
                    _XDP_TRUNK_BRIDGE,
                    name,
                    port.mode.value,
                    port.nRxq,
                    trunks=sorted(set(xdp_trunks.get(name, []))),
                )
            )
        trunk_unit.blank()
    # The IR carries one node per OVS bridge with all of its ports
    # (`ovn.ls.interfaces` or `bridge.ports`). Group by bridge name and
    # emit ensure-bridge ONCE, then only the ports.
    by_bridge: dict[str, list[tuple[str, pt.OvnLsIface]]] = {}
    for domain, iface in all_bindings:
        if iface.kind not in _BINDABLE_IFACE_KINDS:
            emit.comment(
                f'# unsupported interface kind "{iface.kind}" for domain '
                f"{domain} on {host_name} — skipped, see fabric/emit.py"
            )
            continue
        by_bridge.setdefault(iface.bridge, []).append((domain, iface))
    for bridge in sorted(by_bridge):
        bridge_unit = emit.unit("bridge", f"{bridge} on {host_name}")
        bridge_unit.ovn(host.ovs_bridge(bridge))
        for domain, iface in by_bridge[bridge]:
            is_xdp = iface.kind == "xdp" or (
                iface.kind == "vlan" and iface.vlanParent.kind == "xdp"
            )
            if iface.kind == "xdp":
                label = iface.name
            elif iface.kind == "vlan" and iface.vlanParent.kind == "xdp":
                label = f"{iface.vlanParent.name} vlan {iface.vlanId}"
            else:
                label = _iface_real_name(iface)
            iface_unit = bridge_unit.unit("interface", f"{domain} ({label})")
            if iface.kind == "vlan" and not is_xdp:
                iface_unit.ovn(
                    host.rootNs().add_vlan(
                        iface.vlanParent.name,
                        _iface_real_name(iface),
                        iface.vlanId,
                    ),
                    parent=kernel_var,
                )
            # A transit veth's ROOT leg is created AND upped by its own
            # kernel-router job (_emit_kernel_router_owner). This block
            # must not touch it (2026-10-20, boot-restore ordering).
            if is_xdp:
                vlan_id = iface.vlanId if iface.kind == "vlan" else None
                _emit_xdp_leg(host, kernel_var, bridge, vlan_id, iface_unit)
            elif iface.kind == "veth":
                pass
            else:
                iface_unit.ovn(host.ovs_port(bridge, _iface_real_name(iface)))
            if domain in ovn_domains:
                mappings.append(f"{_network_name(domain)}:{bridge}")
    if mappings:
        # Host-wide (one `ovn-bridge-mappings` for all bridges), so its own
        # unit — after every bridge it names exists.
        mappings_unit = emit.unit("bridge mappings", host_name)
        mappings_unit.ovn(host.ovs_mapping(tuple(mappings)))
    if len(emit.lines) > before:
        emit.blank()
