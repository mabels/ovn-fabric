// src/ir/collision-domain.ts — ovn.ls: a CollisionDomain as a bare OVN logical
// switch.

import type { BridgeNode, OvnLsInterface, OvnLsNode } from "../protocol.ts";
import type { CollisionDomain } from "../types.ts";
import { resolveIface } from "./interfaces.ts";
import { shortIfaceName, vethShortId } from "./names.ts";

// ── ovn.ls (NEW — not in ADR 0002's original node-kind table) ────────
// A bare OVN Logical_Switch — CollisionDomain's direct IR counterpart.
// Not host-scoped (see CollisionDomain's own doc comment, types.ts —
// confirmed live, ADR 0003: a real logical switch is cluster-wide, no
// single chassis owns it), so `key` carries no host at all, unlike
// almost every other kind in this project so far.
//
// `interfaces` is derived from every Router's endpoints that reference
// this domain (RouterEndpoint.ifaces), not from CollisionDomain's own
// addInterface()/allInterfaces — net.router() doesn't currently call
// addInterface() when it registers an endpoint's ifaces (a real,
// currently-unresolved gap between the two mechanisms, not something
// papered over here: relying on RouterEndpoint.ifaces directly sidesteps
// it rather than pretending it's already reconciled).
//
// `shortName`: the real OVS bridge name this domain binds to on any
// chassis with a bindable interface (see fabric/emit.py's
// _emit_iface_bindings_create/_delete) — see shortIfaceName() above.
// Carried on EACH interface entry, not as its own data field: `iface`
// is already the one place a real-world-binding fact lives per (host,
// interface) pair, and every entry for this domain resolves to the
// same value (deterministic from domain.name), so there's nothing lost
// repeating it there instead of hoisting it out.
// Whether flow export is OFF for one endpoint binding (types.ts
// RouterEndpointInputBase.flow). Explicit wins both ways; otherwise a
// TRANSIT link defaults OFF — a kernel/tunnel router's transit (and its
// tunnel backdoor) is an internal mesh veth, not segment traffic worth
// sampling, so its bridge stays out of IPFIX unless the author opts in
// with `flow: true`. Segment legs (vlan/physical) default ON.
export function collisionDomainToIR(
  domain: CollisionDomain,
): OvnLsNode | BridgeNode {
  if (domain.hostOnly) {
    // Host-local L2 segment: a bridge and its own physical ports. No
    // OVN logical switch/localnet/mapping is emitted for it. A domain
    // may already be named as its bridge (`br-...`); otherwise apply the
    // usual `br-` prefix.
    const shortName = domain.name.startsWith("br-")
      ? domain.name
      : shortIfaceName("br-", domain.name);
    return {
      id: `bridge:${shortName}`,
      kind: "bridge",
      key: { name: shortName },
      data: {
        host: `host:${domain.host!.name}`,
        ports: domain.hostPorts.map((port) => ({
          host: port.host.name,
          iface: { ...resolveIface(port.iface), bridge: shortName },
        })),
      },
    };
  }
  const shortName = shortIfaceName("br-", domain.name);
  const interfaces: OvnLsInterface[] = [];
  for (const port of domain.ovnPorts) {
    if (port.host === undefined || port.iface === undefined) continue;
    if (port.iface.kind !== "veth") {
      interfaces.push({
        host: port.host.name,
        iface: {
          ...resolveIface(port.iface),
          bridge: shortName,
          ...(port.flowOff ? { flow: false } : {}),
        },
      });
      continue;
    }
    // A veth on a transit domain: buildKernelRouterEndpoint stamps LONG
    // leg names; the deployer's binding emitter attaches `peerName` to
    // the bridge, so shorten both legs HERE to the same id
    // kernelRouterSideToIR resolves for the netns-side pair.
    const id = vethShortId(port.iface.ifaceName);
    interfaces.push({
      host: port.host.name,
      iface: {
        ...port.iface,
        ifaceName: `veth-krn-${id}`,
        peerName: `veth-ovn-${id}`,
        bridge: shortName,
        ...(port.flowOff ? { flow: false } : {}),
      },
    });
  }
  return {
    id: `ls:${domain.name}`,
    kind: "ovn.ls",
    key: { name: domain.name },
    data: { interfaces },
  };
}
