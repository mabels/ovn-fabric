// src/ir.ts — toIR(NetworkDefinition): the DESIRED-state half of the IR
// this project has been building toward since ADR 0002 (docs/adr/
// 0002-intermediate-representation.md). The reconciler package (Python,
// this repo's sibling `reconciler`/`ladops` work) already produces this
// exact {id, kind, key, data} envelope shape from LIVE router state —
// this is the same shape, produced from topology.ts's DECLARED state
// instead, so the two sides can eventually be diffed against each
// other (ADR 0002 § Decision, point 5 — a plain function over the
// union of both key sets, not an "engine").
//
// Plain TypeScript objects, not ArkType schemas: the reconciler side
// never needed schema validation either (ladops/reconciler are all
// plain dicts, no runtime validation library at all) — this data is
// internal, produced by this project's own generator, not adversarial
// input. ArkType (ADR 0002 § Decision, point 4; § "Type stability
// across the TypeScript/Python boundary") is a real, separate follow-up
// for when cross-language type generation is actually needed, not a
// prerequisite for toIR() to exist and be useful.
//
// Scope, deliberately: only what the CURRENT primitives (CollisionDomain,
// Router/RouterEndpoint, Host — see types.ts) can produce a DESIRED-state
// fact for. NAT, static routes/reachability, slaac, backdoors, and
// uplink discovery (DHCP/static) have no home in the new shape at all
// yet (see topology.cluster-draft.ts's own comments, written while
// designing this) — none of those are attempted here, not because
// they're forgotten, but because there's nothing yet to extract them
// FROM. The legacy Uplink/Segment path (still what's actually deployed)
// isn't covered either — toIR() only walks allCollisionDomains/
// allRouters/allHosts, not allUplinks/allSegments.

import { macFromV4 } from "./addressing.ts";
import { IPv4 } from "./ip.ts";
import type { CollisionDomain, Host, Router, RouterEndpoint } from "./types.ts";
import type { NetworkDefinition } from "./define.ts";

export interface IRNode {
  readonly id: string;
  readonly kind: string;
  readonly key: Record<string, unknown>;
  readonly data: Record<string, unknown>;
}

// RouterEndpoint.ipaddrs is a flat (IPv4 | IPv6)[], one entry per
// address — not NetId[] (see RouterEndpoint, types.ts: a router
// endpoint has no segment/uplink id for NetId's id()/vlan() to report).
// to_string() (not to_s()) is the one that includes the prefix length.
function addrStrings(ipaddrs: RouterEndpoint["ipaddrs"]): string[] {
  return ipaddrs.map((addr) => addr.to_string());
}

// ── infra.host ───────────────────────────────────────────────────────
// Matches the reconciler side's own infra.host kind (reconciler/host/
// reconcile.py) — but NOT the same data shape. The live side reports
// OBSERVED facts (uname -a, a reconcile timestamp) that have no
// "desired" analog — you can't declare what `uname -a` should say. The
// desired side instead reports the one thing that genuinely IS a
// declared intent: whether this host should run ovn-central or be a
// plain chassis (see OvnHostConfig, types.ts). A diff between the two
// sides' infra.host nodes is therefore only ever comparing key
// presence (does this host exist at all), never comparing `data`
// field-for-field — the two `data` shapes are deliberately incomparable.
// encapIp resolution matches OvnHostConfig.encapIp's own documented
// default (types.ts): explicit override, else address.ipv4, else
// address.ipv6 — never address.fqdn (the SB DB's Chassis.encaps.ip
// column is a real address, not a hostname, confirmed live 2026-08-09).
// undefined only when a host declares nothing but an fqdn — the
// shell-conversion step is what actually needs this to exist, so it's
// left optional here rather than throwing at IR-build time.
function resolveEncapIp(host: Host): string | undefined {
  return (host.ovn?.encapIp ?? host.address.ipv4 ?? host.address.ipv6)?.to_s();
}

function hostToIR(host: Host): IRNode {
  const id = `host:${host.name}`;
  return {
    id,
    kind: "infra.host",
    key: { host: host.name },
    data: {
      connectAddress: host.connectAddress,
      ovnRole: host.ovn?.role.kind,
      encapIp: resolveEncapIp(host),
    },
  };
}

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
function collisionDomainToIR(
  domain: CollisionDomain,
  routers: readonly Router[],
): IRNode {
  const id = `ls:${domain.name}`;
  const interfaces: Array<{ host: string; iface: unknown }> = [];
  for (const router of routers) {
    for (const endpoint of [router.left, router.right]) {
      if (endpoint.l2Segment.name !== domain.name) continue;
      for (const hi of endpoint.ifaces ?? []) {
        interfaces.push({ host: hi.host.name, iface: hi.iface });
      }
    }
  }
  return {
    id,
    kind: "ovn.ls",
    key: { name: domain.name },
    data: { interfaces },
  };
}

// ── ovn.lrp ────────────────────────────────────────────────────────
// Matches ADR 0002's existing ovn.lrp kind exactly (already produced
// real, on the reconciler side, from real Logical_Router_Port data —
// see reconciler/ovn/reconcile.py) — this is the DESIRED-state half of
// the same fact. Key extends the ADR's `router:<scope>|lrp` with the
// endpoint's own side (left/right), not a port name the config author
// chose — Router/RouterEndpoint has no separate per-endpoint name field
// (see Router, types.ts), so `left`/`right` IS the local identity here,
// the same role a chosen LRP name plays on the reconciler side.
// Resolved HERE, not left for the shell-conversion step to guess: the
// IR is the desired-state boundary between this project's TypeScript
// half and its Python half (deployer/ir_to_shell.py) — it should carry
// the FINAL mac, not push "how do I fold one" across that boundary
// (macFromV4 only exists on this side). Same reasoning as
// resolveEncapIp above. Matches RouterEndpoint.mac's own doc comment
// (types.ts): explicit override wins outright, else fold the first
// IPv4 in ipaddrs — required to exist as one or the other, since an
// LRP with no mac at all can't be created.
function resolveMac(lrp: string, endpoint: RouterEndpoint): string {
  if (endpoint.mac !== undefined) return endpoint.mac;
  const v4 = endpoint.ipaddrs.find((addr): addr is IPv4 =>
    addr instanceof IPv4
  );
  if (v4 === undefined) {
    throw new Error(
      `${lrp}: no IPv4 address to derive a MAC from, and no explicit ` +
        `RouterEndpoint.mac override`,
    );
  }
  return macFromV4(v4);
}

function routerEndpointToIR(
  router: Router,
  side: "left" | "right",
  endpoint: RouterEndpoint,
): IRNode {
  const scope = `router:${router.name}`;
  const id = `${scope}|lrp:${side}`;
  const lrp = `lrp-${router.name}-${side}`;
  return {
    id,
    kind: "ovn.lrp",
    key: { router: router.name, side },
    data: {
      l2Segment: endpoint.l2Segment.name,
      addresses: addrStrings(endpoint.ipaddrs),
      mac: resolveMac(lrp, endpoint),
      gatewayChassis: endpoint.gatewayChassis?.name,
    },
  };
}

export function toIR(network: NetworkDefinition): Record<string, IRNode> {
  const nodes: Record<string, IRNode> = {};

  for (const host of network.allHosts) {
    const node = hostToIR(host);
    nodes[node.id] = node;
  }

  for (const domain of network.allCollisionDomains) {
    const node = collisionDomainToIR(domain, network.allRouters);
    nodes[node.id] = node;
  }

  for (const router of network.allRouters) {
    const left = routerEndpointToIR(router, "left", router.left);
    const right = routerEndpointToIR(router, "right", router.right);
    nodes[left.id] = left;
    nodes[right.id] = right;
  }

  return nodes;
}
