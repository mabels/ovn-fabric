"""Flow export (IPFIX) on a host's bridges."""

from __future__ import annotations

from fabric.primitives.host import HostPrimitive
from protocol import generated as pt

from .emitter import _BINDABLE_IFACE_KINDS, Emitter
from .lookups import _host_bindings


# ── flow export (IPFIX) ───────────────────────────────────────────────
# Per-bridge IPFIX sampling (2026-10-10). WHERE to export is host-level
# (infra.host.data.ipfix, from Host.monitoring.ipfix); WHETHER a given
# leg is sampled rides the ovn.ls interface blob as `iface.flow` (only
# present when a leg opts OUT — src/ir.ts collisionDomainToIR). The
# concrete unit is the SEGMENT's OVS bridge (br-<shortName>): IPFIX
# attaches to a Bridge, not a port, so a bridge shared by several legs
# is off if ANY of them carries flow=false (off wins — same reduction
# _ipfix_bridge_flags below does). Backbone legs (br-int) and
# kernel/tunnel legs (netns, no OVS bridge) have no bridge here and so
# can't be switched this way; that partial effect is by design.
def _ipfix_bridge_flags(host_name: str, nodes: list[pt.Model]) -> dict[str, bool]:
    """bridge -> sampled? for this host. OFF if any bindable interface
    bound into that bridge opted out (iface.flow is False); a bridge
    with no interfaces is absent entirely."""
    flags: dict[str, bool] = {}
    for _domain, iface in _host_bindings(nodes).get(host_name, []):
        if iface.kind not in _BINDABLE_IFACE_KINDS:
            continue
        bridge = iface.bridge
        flags[bridge] = flags.get(bridge, True) and iface.flow is not False
    return flags


def _ipfix_setargs(ipfix: pt.IpfixData) -> str:
    # OVS IPFIX column=value args; only what the config set (OVS keeps
    # its own defaults for the rest). `targets` is a set-of-strings and
    # its "<ip>:<port>" value contains `:`, which OVSDB reads as the
    # map-entry separator and rejects in a set element ("unexpected \":\"
    # parsing set of strings"). Quote it so it parses as one atom; the
    # backslash survives the outer shlex quoting and is unescaped by the
    # `sh -c` the step runs, handing ovs-vsctl literal quotes.
    args = [f'targets=\\"{ipfix.targets}\\"']
    if ipfix.sampling is not None:
        args.append(f"sampling={int(ipfix.sampling)}")
    if ipfix.cacheActiveTimeout is not None:
        args.append(f"cache_active_timeout={int(ipfix.cacheActiveTimeout)}")
    if ipfix.cacheMaxFlows is not None:
        args.append(f"cache_max_flows={int(ipfix.cacheMaxFlows)}")
    return " ".join(args)


def _ipfix_enable_script(bridge: str, ipfix: pt.IpfixData) -> str:
    # Idempotent attach, run via `sh -c` (same argv-safe precedent as the
    # zerotier installer): REUSE this bridge's existing IPFIX row if it
    # has one (just update its columns), else create one and point the
    # bridge at it. `ovs-vsctl create IPFIX` has no --may-exist, so
    # without this a re-run would leak a row and re-bounce sampling.
    setargs = _ipfix_setargs(ipfix)
    return (
        f"ipfix=$(ovs-vsctl --if-exists get Bridge {bridge} ipfix 2>/dev/null);"
        f' if [ -n "$ipfix" ] && [ "$ipfix" != "[]" ]; then'
        f' ovs-vsctl set IPFIX "$ipfix" {setargs};'
        f" else ovs-vsctl -- --id=@ipfix create IPFIX {setargs}"
        f" -- set Bridge {bridge} ipfix=@ipfix; fi"
    )


def _emit_ipfix(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """One IPFIX sampler per sampled bridge on this host (DELETE clears
    it). A bridge whose leg opted out gets none. No-op when the host has
    no `ipfix` config or no bindable segment bridges."""
    ipfix = host_node.data.ipfix
    if ipfix is None:
        return
    flags = _ipfix_bridge_flags(host_node.key.host, nodes)
    if not flags:
        return
    host = HostPrimitive(host_node.key.host)
    ipfix_unit = emit.unit("flow export", "IPFIX")
    for bridge in sorted(flags):
        if flags[bridge]:
            ipfix_unit.ovn(host.ovs_ipfix(bridge, _ipfix_enable_script(bridge, ipfix)))
    ipfix_unit.blank()
