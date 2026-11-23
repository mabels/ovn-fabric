"""Per-host configuration: chassis registration, interface bindings, IPFIX."""

from __future__ import annotations

from fabric.primitives.host import HostPrimitive
from protocol import generated as pt

from .bindings import _emit_iface_bindings
from .emitter import Emitter
from .ipfix import _emit_ipfix
from .lookups import _find_central_host, _gateway_eligible_hosts


def _emit_chassis_registration(
    host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter
) -> None:
    """The OVS `external_ids` a chassis needs to register with OVN —
    emitted EARLY in the create pass (right after the packages land), so
    the just-installed `ovn-controller` has time to register while the
    heavier kernel-router/OVS/service steps below run; the cluster pass
    then resolves this chassis's runtime `system-id` by the `hostname`
    set here (see _emit_gateway_chassis_refs)."""
    data = host_node.data
    host_name = host_node.key.host
    host = HostPrimitive(host_name)
    if data.ovnRole == pt.OvnRole.central:
        emit.comment("# central: expose the shared NB/SB DBs to every other chassis")
        emit.ovn(host.ovn_nb_connection())
        emit.ovn(host.ovn_sb_connection())
        emit.ovn(host.ovs_external_id("ovn-remote", "unix:/var/run/ovn/ovnsb_db.sock"))
    else:
        central = _find_central_host(nodes)
        if central is None:
            raise ValueError(
                f"host {host_name}: no central chassis declared in this network — "
                "a chassis needs one to point ovn-remote at"
            )
        if central.data.encapIp is None:
            raise ValueError(
                f"central host {central.key.host}: no encapIp/address.ipv4/"
                "address.ipv6 to build ovn-remote from"
            )
        emit.ovn(host.ovs_external_id("ovn-remote", f"tcp:{central.data.encapIp}:6642"))

    # The topology's own host name (NOT the auto FQDN OVS reports): the
    # cluster pass looks this chassis up by it to get the system-id, the
    # only identifier `lrp-set-gateway-chassis` matches (2026-09-19).
    emit.ovn(host.ovs_external_id("hostname", host_name))

    if data.encapIp is not None:
        emit.ovn(host.ovs_external_id("ovn-encap-type", "geneve"))
        emit.ovn(host.ovs_external_id("ovn-encap-ip", data.encapIp))

    if host_name in _gateway_eligible_hosts(nodes):
        emit.ovn(host.ovs_external_id("ovn-cms-options", "enable-chassis-as-gw"))


def _emit_host_configure(host_node: pt.InfraHostNode, nodes: list[pt.Model], emit: Emitter) -> None:
    """The idempotent part of the host pass: chassis registration,
    interface bindings and IPFIX. The job tree's configure job."""
    host_name = host_node.key.host
    registration_unit = emit.unit("chassis registration", host_name, key=host_node.id)
    _emit_chassis_registration(host_node, nodes, registration_unit)
    _emit_iface_bindings(host_name, nodes, emit)
    _emit_ipfix(host_node, nodes, emit)
