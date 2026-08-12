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
// fact for. NAT, backdoors, and uplink discovery (DHCP/static) have no
// home in the new shape at all yet (see topology.cluster-draft.ts's own
// comments, written while designing this) — none of those are
// attempted here, not because they're forgotten, but because there's
// nothing yet to extract them FROM. Static routes (RoutingDomain, see
// computeRoutes/computeInterconnectRoutes below) and IPv6 RA/SLAAC (see
// RouterEndpointService, types.ts, resolved below) both ARE covered.
// The legacy Uplink/Segment path (still what's actually deployed) isn't
// covered either — toIR() only walks allCollisionDomains/allRouters/
// allHosts, not allUplinks/allSegments.

import { fnv1a32, macFromV4 } from "./addressing.ts";
import type { IPv4, IPv6 } from "./ip.ts";
import type {
  CollisionDomain,
  Host,
  OvnRouterEndpoint,
  Router,
  RouterEndpoint,
  RouterEndpointService,
} from "./types.ts";
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

// IFNAMSIZ (16) minus the NUL terminator — a real OVS bridge is a real
// kernel netdev, capped at 15 usable characters. Confirmed live,
// 2026-08-10: "br-voda-modem-v2" (16 chars) failed with ofproto
// "Invalid argument" on a real container while "br-voda-avm-v2" (14
// chars) right next to it succeeded.
const IFNAMSIZ_MAX = 15;

// Readable (`${prefix}${name}`) whenever it fits; only falls back to a
// short deterministic hash (fnv1a32, addressing.ts) when it doesn't.
// Resolved HERE, not in deployer/ir_to_shell.py (the translator) — same
// boundary reasoning as mac/gatewayChassis/routes throughout this file:
// the generator computes the FINAL name a real kernel object needs, the
// translator only ever applies an already-resolved fact. Moved here
// 2026-08-12 — this used to live translator-side as deployer/
// ir_to_shell.py's own `_bridge_name`/`_fnv1a_32`.
function shortIfaceName(prefix: string, name: string): string {
  const candidate = `${prefix}${name}`;
  if (candidate.length <= IFNAMSIZ_MAX) return candidate;
  return `${prefix}${fnv1a32(name).toString(16).padStart(8, "0")}`;
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
//
// `shortIfaceName`: the real OVS bridge name this domain binds to on
// any chassis with a bindable interface (see deployer/ir_to_shell.py's
// _emit_iface_bindings_create/_delete) — see shortIfaceName() above.
function collisionDomainToIR(
  domain: CollisionDomain,
  routers: readonly Router[],
): IRNode {
  const id = `ls:${domain.name}`;
  const interfaces: Array<{ host: string; iface: unknown }> = [];
  for (const router of routers) {
    for (const endpoint of [router.left, router.right]) {
      if (endpoint.kind !== "ovn" || endpoint.l2Segment.name !== domain.name) {
        continue;
      }
      for (const hi of endpoint.ifaces ?? []) {
        interfaces.push({ host: hi.host.name, iface: hi.iface });
      }
    }
  }
  return {
    id,
    kind: "ovn.ls",
    key: { name: domain.name },
    data: { interfaces, shortIfaceName: shortIfaceName("br-", domain.name) },
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
  const v4 = endpoint.ipaddrs.find((addr): addr is IPv4 => addr.is_ipv4());
  if (v4 === undefined) {
    throw new Error(
      `${lrp}: no IPv4 address to derive a MAC from, and no explicit ` +
        `RouterEndpoint.mac override`,
    );
  }
  return macFromV4(v4);
}

// Folds RouterEndpoint.services into the single ipv6_ra_configs smap
// OVN actually has (see RouterEndpointService, types.ts, for why these
// are two independently-toggleable services rather than one boolean).
// Resolved HERE, not in deployer/ir_to_shell.py, same boundary reasoning
// as resolveMac/resolveEncapIp above: the IR carries the FINAL key/value
// pairs to set, not "here's a services list, go figure out the ovn-nbctl
// options syntax." Undefined (not an empty object) when no service is
// declared, so the deployer can tell "nothing to set" apart from "set
// zero keys" without inspecting emptiness itself.
function resolveIpv6RaConfigs(
  services: readonly RouterEndpointService[] | undefined,
): Record<string, string> | undefined {
  if (services === undefined || services.length === 0) return undefined;
  const configs: Record<string, string> = {};
  for (const service of services) {
    if (service.kind === "ipv6.slaac") {
      configs.address_mode = "slaac";
      continue;
    }
    if (service.kind === "ipv6.ra") {
      configs.send_periodic = "true";
      if (service.minInterval !== undefined) {
        configs.min_interval = String(service.minInterval);
      }
      if (service.maxInterval !== undefined) {
        configs.max_interval = String(service.maxInterval);
      }
      continue;
    }
    // Exhaustive on purpose, not "anything that isn't slaac must be
    // ra" — confirmed live, 2026-08-12: a services entry with a kind
    // outside RouterEndpointService's declared union (e.g. a config
    // author's own in-progress sketch toward kernel-side NAT service
    // kinds, still unimplemented) silently got treated as "ipv6.ra"
    // and set send_periodic=true on an endpoint that never asked for
    // it — TypeScript's own exhaustiveness checking doesn't help here
    // if the config author's object literal reaches this function
    // without ever being checked (e.g. `deno run` without a preceding
    // `deno check`). A real runtime throw catches it instead.
    const unknownKind: never = service;
    throw new Error(
      `unknown RouterEndpointService kind: ${JSON.stringify(unknownKind)}`,
    );
  }
  return configs;
}

function routerEndpointToIR(
  router: Router,
  side: "left" | "right",
  endpoint: OvnRouterEndpoint,
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
      ipv6RaConfigs: resolveIpv6RaConfigs(endpoint.services),
    },
  };
}

// ── ipv4.route / ipv6.route ──────────────────────────────────────────
// Matches ADR 0002's own node-kind table exactly (`router:<scope>|
// route:<prefix>`, originally drafted for the legacy Uplink/Segment
// model — the same shape fits Router/RoutingDomain unchanged).
//
// The ANCHOR — whichever (router, side) a route's `via` is actually
// reachable from — is DECLARED, not inferred: it's simply whichever
// RouterEndpoint carries the route in its own `.routes` array (see
// RouterEndpointRoute, types.ts). An earlier design searched every
// router's every endpoint for one whose declared subnet happened to
// contain `via` — removed 2026-08-12 as unnecessary indirection once
// routes moved onto the endpoint directly: the config author already
// knows router-voda-avm-v2's WAN-facing `left` is the real egress, so
// there's nothing left to search for. Every OTHER router in the SAME
// RoutingDomain gets `via` rewritten to the anchor's own address on
// whichever CollisionDomain the two routers actually share — not `via`
// itself, which usually isn't even reachable from anywhere but the
// anchor's own segment. Confirmed by hand against a real 3-router
// constellation (2026-08-11, live design discussion) before writing
// this: home/management both resolve to "default via voda-avm's OWN
// backbone address," not "via the real ISP gateway" — only voda-avm's
// own route gets the literal ISP address.
//
// No domain shared between a given router and the anchor, or the
// family just doesn't match anything reachable — that router simply
// gets no route from that entry. Not an error: a declared route is one
// possible SOURCE of reachability among several (SLAAC/RA on a segment
// being the other one already real in this project, see
// generate-ovn.ts's ipv6_ra_configs) — a source that doesn't resolve
// for a given router/family just isn't the one supplying it there.

interface Anchor {
  readonly router: Router;
  readonly side: "left" | "right";
}

// A real type guard (not just a boolean check) — narrows `b` to the
// SAME branded family as `a`, so a caller like anchorAddressSharedWith
// below can call the family-specific `.includes()` without an unsafe
// cast. Plain boolean, not a type guard: a `b is T` predicate here
// narrows FINE on the true branch but incorrectly narrows the false
// branch to `never` whenever both arguments already share the same
// declared union type (confirmed live, 2026-08-12, trying exactly that
// on routeToIR's validation below) — and `.includes()` never actually
// needed narrowed arguments in the first place (its inherited base
// IPAddress signature already accepts either family).
function isSameFamily(a: IPv4 | IPv6, b: IPv4 | IPv6): boolean {
  return a.is_ipv4() === b.is_ipv4();
}

// Two endpoints "share a CollisionDomain" only when BOTH are OVN-side —
// a KernelRouterEndpoint has no l2Segment at all, so it never
// participates in collision-domain-based routing (RoutingDomain/
// computeRoutes/computeInterconnectRoutes are all OVN-world concepts
// today; a kernel endpoint's own reachability, once built, will need
// its own mechanism, not this one).
function sharesL2Segment(a: RouterEndpoint, b: RouterEndpoint): boolean {
  return a.kind === "ovn" && b.kind === "ovn" &&
    a.l2Segment.name === b.l2Segment.name;
}

// The anchor's OWN address, of `dst`'s family, on whichever endpoint
// `router` actually shares a CollisionDomain with — i.e. NOT the
// anchor-side endpoint itself (that's what `via` already sits on),
// its OTHER endpoint, the one facing the rest of the cluster.
function anchorAddressSharedWith(
  router: Router,
  anchor: Anchor,
  dst: IPv4 | IPv6,
): (IPv4 | IPv6) | undefined {
  const anchorOtherSide = anchor.side === "left"
    ? anchor.router.right
    : anchor.router.left;
  const shared = sharesL2Segment(router.left, anchorOtherSide) ||
    sharesL2Segment(router.right, anchorOtherSide);
  if (!shared) return undefined;
  return anchorOtherSide.ipaddrs.find((addr) => isSameFamily(addr, dst));
}

// `domain` names WHICH RoutingDomain produced this route (net.
// routingDomain()'s own name) — every route, from either mechanism
// below, always belongs to exactly one. Confirmed live, 2026-08-12:
// without this, deployer/ir_to_shell.py had no way to group a
// router's routes into labeled blocks in the generated script — every
// route landed in one flat, unlabeled dump instead of being grouped
// with its router and its source, unlike every other section of the
// script.
function routeToIR(
  router: Router,
  dst: IPv4 | IPv6,
  nexthop: IPv4 | IPv6,
  masq: boolean,
  domain: string,
): IRNode {
  // Every route this module builds funnels through here — the ONE
  // place that guarantees dst and nexthop never mismatch family. A
  // mismatch can only originate from a config author's own
  // RouterEndpointRoute (dst/via independently typed IPv4|IPv6, nothing
  // stops declaring one of each by mistake) — computeInterconnectRoutes
  // below can't produce one itself (it always pairs same-family
  // addresses within one domain's own participants), but validating
  // here, once, covers every caller instead of trusting each one
  // individually.
  if (!isSameFamily(dst, nexthop)) {
    throw new Error(
      `router "${router.name}": route to ${dst.to_string()} has a ` +
        `next-hop of the wrong address family (${nexthop.to_s()}) — ` +
        `dst and via/nexthop must both be IPv4 or both be IPv6`,
    );
  }
  const prefix = dst.to_string();
  const id = `router:${router.name}|route:${prefix}`;
  return {
    id,
    kind: dst.is_ipv4() ? "ipv4.route" : "ipv6.route",
    key: { router: router.name, prefix },
    data: { nexthop: nexthop.to_s(), masq, domain },
  };
}

function computeRoutes(network: NetworkDefinition): IRNode[] {
  const nodes: IRNode[] = [];
  for (const domain of network.allRoutingDomains) {
    const participants = network.allRouters.filter((r) =>
      r.routingDomains?.includes(domain)
    );
    // The anchor router must ITSELF be a participant of the domain its
    // routes are meant to propagate through — a route declared on an
    // endpoint whose own router isn't in this domain simply never gets
    // picked up by this domain's iteration (it might still be picked up
    // by a DIFFERENT domain the router does belong to).
    for (const anchorRouter of participants) {
      for (const side of ["left", "right"] as const) {
        const anchor: Anchor = { router: anchorRouter, side };
        for (const route of anchorRouter[side].routes ?? []) {
          const masq = route.with === "masq";
          for (const router of participants) {
            if (router.name === anchorRouter.name) {
              // No `via` here means "the anchor needs no literal route
              // of its own for this — handled elsewhere on its own
              // side" (e.g. SLAAC/RA, or its own less-specific default
              // already covers it). That's a statement about the
              // ANCHOR only, not about whether OTHER participants
              // should still learn to route toward it — they always
              // should (confirmed live, 2026-08-12: "you need to add
              // that route to all hops so that all packets will be
              // transmitted to left side of router-voda-avm-v2"), so
              // this `continue` is scoped to the anchor's own branch,
              // not the whole route entry.
              if (route.via === undefined) continue;
              nodes.push(
                routeToIR(router, route.dst, route.via, masq, domain.name),
              );
              continue;
            }
            const nexthop = anchorAddressSharedWith(router, anchor, route.dst);
            if (nexthop === undefined) continue; // no shared domain / family with the anchor
            nodes.push(
              routeToIR(router, route.dst, nexthop, masq, domain.name),
            );
          }
        }
      }
    }
  }
  return nodes;
}

// ── interconnect routes, scoped to a RoutingDomain's own participants ─
// "Interconnect only exists if a routing domain exists" (confirmed
// live, 2026-08-12, after the first cut of this function generated a
// route between EVERY pair of routers sharing the backbone — home,
// neighbor, usa, management, voda-avm, voda-modem, starlink all meshed
// together regardless of RoutingDomain membership. That's wrong: these
// are separate sites/tenants sharing physical hardware, and giving
// every one of them a route into every other one's LAN is a real
// privacy/security break, not just noise. The original hand-derived
// 3-router example (home/management/voda-avm, all three actually IN
// Voda-defaultRoute) never implied anything about neighbor/usa, who
// aren't in that domain at all.)
//
// So: routers only get mutual peer routes to each other if they're
// BOTH members of the SAME RoutingDomain — same scope computeRoutes
// already uses for `participants`. This still runs "any ways", i.e.
// unconditionally on whether that domain's own `route.via` entries
// resolve for a given family (see computeRoutes's `via === undefined`
// skip, e.g. IPv6 default via SLAAC/RA) — a domain's participants
// still need direct routes to each other's own subnets regardless of
// whether the domain also has an external via-route.
//
// For every ordered pair of distinct participants (r1, r2) that share
// a CollisionDomain: r1 learns a route to r2's OTHER endpoint's
// subnet, next-hop r2's own address on the SHARED domain (not r2's
// address on its other side — that's not reachable from r1 at all).
// Symmetric — running the same pair the other way round produces r2's
// own route back to r1.
function computeInterconnectRoutes(network: NetworkDefinition): IRNode[] {
  const nodes: IRNode[] = [];
  for (const domain of network.allRoutingDomains) {
    const participants = network.allRouters.filter((r) =>
      r.routingDomains?.includes(domain)
    );
    for (const r1 of participants) {
      for (const r2 of participants) {
        if (r1.name === r2.name) continue;
        for (const r1side of [r1.left, r1.right]) {
          for (const r2side of [r2.left, r2.right]) {
            if (!sharesL2Segment(r1side, r2side)) continue;
            const r2OtherSide = r2side === r2.left ? r2.right : r2.left;
            for (const nexthop of r2side.ipaddrs) {
              for (const peerAddr of r2OtherSide.ipaddrs) {
                if (!isSameFamily(nexthop, peerAddr)) continue;
                nodes.push(
                  routeToIR(
                    r1,
                    peerAddr.network(),
                    nexthop,
                    false,
                    domain.name,
                  ),
                );
              }
            }
          }
        }
      }
    }
  }
  return nodes;
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
    for (const side of ["left", "right"] as const) {
      const endpoint = router[side];
      if (endpoint.kind !== "ovn") {
        // KernelRouterEndpoint (types.ts) — no emission strategy built
        // for this kind yet (nothing in this codebase constructs one
        // today). Throws rather than silently skipping, so the day a
        // config author actually declares one, this says so loudly
        // instead of quietly producing an incomplete IR.
        throw new Error(
          `router "${router.name}": ${side} endpoint is kind "${endpoint.kind}" — ` +
            `toIR() has no emission strategy for it yet`,
        );
      }
      const node = routerEndpointToIR(router, side, endpoint);
      nodes[node.id] = node;
    }
  }

  // Interconnect (peer-to-peer among a domain's own participants)
  // first, RoutingDomain via-routes second — so if a via-route ever
  // targets the same prefix an interconnect route already computed for
  // the same domain, the explicit via-route wins (last write to
  // `nodes` on a shared `id` takes it).
  for (const route of computeInterconnectRoutes(network)) {
    nodes[route.id] = route;
  }
  for (const route of computeRoutes(network)) {
    nodes[route.id] = route;
  }

  return nodes;
}
