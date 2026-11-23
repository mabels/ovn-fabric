// src/ir/routes.ts — ipv4.route / ipv6.route: RoutingDomain routes and
// interconnect routes.

import type { NetworkDefinition } from "../define.ts";
import type { IPv4, IPv6 } from "../ip.ts";
import { type Addr, prefixOf, toAddr } from "../ipnet.ts";
import type { AddressIndex } from "../ir-addresses.ts";
import type { RouteNode } from "../protocol.ts";
import type { OvnRouterEndpoint, Router, RoutingDomain } from "../types.ts";
import { routerLegs } from "./names.ts";

function isDefaultDst(dst: IPv4 | IPv6): boolean {
  const s = dst.to_string();
  return s === "0.0.0.0/0" || s === "::/0";
}

// ── ipv4.route / ipv6.route ──────────────────────────────────────────
// Matches ADR 0002's own node-kind table exactly (`ovnrouter:<scope>|
// route:<prefix>` — the shape fits Router/RoutingDomain).
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
// the legacy generator's ipv6_ra_configs) — a source that doesn't resolve
// for a given router/family just isn't the one supplying it there.

interface Anchor {
  readonly router: Router;
  readonly endpoint: OvnRouterEndpoint;
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
function isSameFamily(
  a: { is_ipv4(): boolean },
  b: { is_ipv4(): boolean },
): boolean {
  return a.is_ipv4() === b.is_ipv4();
}

// Two endpoints "share a CollisionDomain" only when BOTH are OVN-side —
// a KernelRouterEndpoint has no l2Segment at all, so it never
// participates in collision-domain-based routing (RoutingDomain/
// computeRoutes/computeInterconnectRoutes are all OVN-world concepts
// today; a kernel endpoint's own reachability, once built, will need
// its own mechanism, not this one).
function sharesL2Segment(a: OvnRouterEndpoint, b: OvnRouterEndpoint): boolean {
  return a.kind === "ovn" && b.kind === "ovn" &&
    a.l2Segment.name === b.l2Segment.name;
}

// The anchor's OWN address, of `dst`'s family, on whichever endpoint
// `router` actually shares a CollisionDomain with — i.e. NOT the anchor
// endpoint itself (that's what `via` already sits on), but one of the
// anchor router's OTHER endpoints, the one facing the rest of the cluster.
// With an endpoint array there may be several such endpoints; the first
// (in the router's own sorted order) that both shares a domain with
// `router` and carries an address of `dst`'s family wins.
function anchorAddressSharedWith(
  router: Router,
  anchor: Anchor,
  dst: IPv4 | IPv6,
): Addr | undefined {
  for (const candidate of anchor.router.endpoints) {
    if (candidate === anchor.endpoint) continue;
    const shared = router.endpoints.some((e) => sharesL2Segment(e, candidate));
    if (!shared) continue;
    const addr = candidate.ipaddrs.find((a) => isSameFamily(a, dst));
    if (addr) return addr;
  }
  return undefined;
}

// `domain` names WHICH RoutingDomain produced this route (net.
// routingDomain()'s own name) — every route, from either mechanism
// below, always belongs to exactly one. Confirmed live, 2026-08-12:
// without this, fabric/emit.py had no way to group a
// router's routes into labeled blocks in the generated script — every
// route landed in one flat, unlabeled dump instead of being grouped
// with its router and its source, unlike every other section of the
// script.
function routeToIR(
  addrs: AddressIndex,
  router: Router,
  dst: IPv4 | IPv6,
  nexthop: Addr,
  securityGroups: readonly {
    readonly render: "kernel" | "ovn";
    readonly name: string;
  }[],
  domain: string,
  metric: number,
): RouteNode {
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
        `next-hop of the wrong address family (${nexthop}) — ` +
        `dst and via/nexthop must both be IPv4 or both be IPv6`,
    );
  }
  const prefix = dst.to_string();
  const id = `ovnrouter:${router.name}|route:${domain}|${prefix}`;
  return {
    id,
    kind: dst.is_ipv4() ? "ipv4.route" : "ipv6.route",
    key: { ovnrouter: router.name, prefix },
    data: {
      nexthop: addrs.ref(nexthop, `${id}|nexthop`, routerLegs(router)),
      securityGroups: [...securityGroups],
      domain,
      metric,
    },
  };
}

// A router participates in `domain` if its router-level membership names
// it OR any of its endpoints does (per-endpoint routingDomains,
// 2026-08-23 — a tunnelRouterEndpoint participates in Neighbor-defaultRoute
// via its tunnel endpoint and Voda-defaultRoute via its backbone endpoint,
// neither on the router itself).
function isDomainParticipant(router: Router, domain: RoutingDomain): boolean {
  return router.routingDomains?.includes(domain) === true ||
    router.endpoints.some((e) => e.routingDomains?.includes(domain) === true);
}

export function computeRoutes(
  network: NetworkDefinition,
  addrs: AddressIndex,
): RouteNode[] {
  const nodes: RouteNode[] = [];
  // Anchor-outer (2026-08-23): routing-domain membership is PER ENDPOINT
  // now — an endpoint's declared routes participate in its OWN
  // routingDomains (falling back to the router-level ones), so a router
  // can anchor one domain from one endpoint and participate in another
  // from another (a tunnelRouterEndpoint does exactly that).
  //
  // Every router's OWN declared route prefixes (across all endpoints,
  // via-less or via) — a router's own route to a prefix wins over one it
  // would otherwise LEARN as a participant of another domain's anchor.
  // Without this a full-tunnel router (e.g. mullvad-de anchors 0.0.0.0/0
  // via-less for Neighbor, but its backbone endpoint also joins
  // Voda-defaultRoute) gets voda's default written over its OWN tunnel
  // egress — same node id, last-write-wins (hit live 2026-08-30).
  const ownPrefixes = new Map<string, Set<string>>();
  for (const r of network.allRouters) {
    const set = new Set<string>();
    for (const endpoint of r.endpoints) {
      for (const rt of endpoint.routes ?? []) {
        set.add(prefixOf(rt.dst).to_string());
      }
    }
    ownPrefixes.set(r.name, set);
  }
  for (const anchorRouter of network.allRouters) {
    for (const anchorEndpoint of anchorRouter.endpoints) {
      const anchor: Anchor = { router: anchorRouter, endpoint: anchorEndpoint };
      const routes = anchorEndpoint.routes ?? [];
      if (routes.length === 0) continue;
      const endpointDomains = anchorEndpoint.routingDomains ??
        anchorRouter.routingDomains ??
        [];
      for (const domain of endpointDomains) {
        // A router is a participant of `domain` if the router-level
        // membership names it OR any of its endpoints does.
        const participants = network.allRouters.filter((r) =>
          isDomainParticipant(r, domain)
        );
        for (const route of routes) {
          const securityGroups = (route.securityGroups ?? []).map((g) => ({
            render: g.render,
            name: g.name,
          }));
          for (const router of participants) {
            if (router.name === anchorRouter.name) {
              // No `via` here means "the anchor needs no literal route
              // of its own for this — handled elsewhere on its own
              // endpoint" (e.g. SLAAC/RA, a less-specific default, or
              // the tunnel egress inside a tunnelRouterEndpoint's
              // netns). That's a statement about the ANCHOR only, not
              // about whether OTHER participants should still learn to
              // route toward it — they always should (confirmed live,
              // 2026-08-12), so this `continue` is scoped to the
              // anchor's own branch, not the whole route entry.
              if (!route.via) continue;
              nodes.push(
                routeToIR(
                  addrs,
                  router,
                  prefixOf(route.dst),
                  toAddr(route.via),
                  securityGroups,
                  domain.name,
                  domain.metric ?? 400,
                ),
              );
              continue;
            }
            const nexthop = anchorAddressSharedWith(
              router,
              anchor,
              prefixOf(route.dst),
            );
            if (!nexthop) continue; // no shared domain / family with the anchor
            // A router's OWN declared route to this prefix is its own
            // authoritative egress — don't overwrite it with a learned one
            // (e.g. a full-tunnel router must keep its tunnel egress, not
            // take another domain's default, 2026-08-30).
            if (
              ownPrefixes.get(router.name)?.has(prefixOf(route.dst).to_string())
            ) {
              continue;
            }
            nodes.push(
              routeToIR(
                addrs,
                router,
                prefixOf(route.dst),
                nexthop,
                securityGroups,
                domain.name,
                domain.metric ?? 400,
              ),
            );
          }
        }
      }
    }
  }
  // Domain routes at the ROUTER layer (2026-09-16): every participant of a
  // domain also learns each NON-default dst the domain declares
  // (RoutingDomain.routes), routed via whichever participant OWNS it (an
  // endpoint subnet containing the dst) on a CollisionDomain the two share.
  // Defaults (0.0.0.0/0, ::/0) are deliberately excluded: they have no
  // owning participant, and the anchor loop above already covers them. This
  // deliberately overlaps computeInterconnectRoutes — same node id, same
  // next hop — because both describe "reach a peer's own subnet"; it only
  // adds anything for a domain dst that is NOT a participant's subnet.
  for (const domain of network.allRoutingDomains) {
    const participants = network.allRouters.filter((r) =>
      isDomainParticipant(r, domain)
    );
    // EXPLICIT overrides only — a domain's CALCULATED routes are the
    // anchor/interconnect routes already produced above, so distributing
    // them again here would just duplicate those node ids.
    for (const route of domain.routes ?? []) {
      if (isDefaultDst(prefixOf(route.dst))) continue;
      const dst = prefixOf(route.dst).to_string();
      for (const router of participants) {
        if (ownPrefixes.get(router.name)?.has(dst)) continue;
        const ownsDst = (
          candidate: Router,
        ): OvnRouterEndpoint | undefined =>
          candidate.endpoints.find((e) =>
            e.ipaddrs.some((a) =>
              a.is_ipv4() === prefixOf(route.dst).is_ipv4() &&
              a.network().to_string() === dst
            )
          );
        if (ownsDst(router)) continue; // already on-link for this dst
        const owner = participants.find((p) =>
          p.name !== router.name && ownsDst(p) !== undefined
        );
        if (!owner) continue;
        const ownerEndpoint = ownsDst(owner);
        if (!ownerEndpoint) continue;
        const nexthop = anchorAddressSharedWith(
          router,
          { router: owner, endpoint: ownerEndpoint },
          prefixOf(route.dst),
        );
        if (!nexthop) continue;
        nodes.push(
          routeToIR(
            addrs,
            router,
            prefixOf(route.dst),
            nexthop,
            [],
            domain.name,
            route.metric ?? domain.metric ?? 400,
          ),
        );
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
// resolve for a given family (see computeRoutes's `!via`
// skip, e.g. IPv6 default via SLAAC/RA) — a domain's participants
// still need direct routes to each other's own subnets regardless of
// whether the domain also has an external via-route.
//
// For every ordered pair of distinct participants (r1, r2) that share
// a CollisionDomain: r1 learns a route to each of r2's OTHER endpoints'
// subnets, next-hop r2's own address on the SHARED domain (not r2's
// address on a different endpoint — that's not reachable from r1 at
// all). Symmetric — running the same pair the other way round produces
// r2's own route back to r1. "Other endpoints" (plural now that a router
// owns an array): every endpoint of r2 except the one sharing the
// domain, which for the two-endpoint case is exactly the old single
// "other side".
export function computeInterconnectRoutes(
  network: NetworkDefinition,
  addrs: AddressIndex,
): RouteNode[] {
  const nodes: RouteNode[] = [];
  for (const domain of network.allRoutingDomains) {
    const participants = network.allRouters.filter((r) =>
      isDomainParticipant(r, domain)
    );
    for (const r1 of participants) {
      for (const r2 of participants) {
        if (r1.name === r2.name) continue;
        for (const r1side of r1.endpoints) {
          if (r1side.routeIsolated) continue;
          for (const r2side of r2.endpoints) {
            if (r2side.routeIsolated) continue;
            if (!sharesL2Segment(r1side, r2side)) continue;
            for (const nexthop of r2side.ipaddrs) {
              for (const other of r2.endpoints) {
                if (other === r2side || other.routeIsolated) continue;
                for (const peerAddr of other.ipaddrs) {
                  if (!isSameFamily(nexthop, peerAddr)) continue;
                  // On-link through an isolated leg: no route needed.
                  if (
                    r1.endpoints.some((own) =>
                      own.routeIsolated &&
                      own.ipaddrs.some((a) =>
                        isSameFamily(a, peerAddr) &&
                        a.network().to_string() ===
                          peerAddr.network().to_string()
                      )
                    )
                  ) continue;
                  nodes.push(
                    routeToIR(
                      addrs,
                      r1,
                      peerAddr.network(),
                      nexthop,
                      [],
                      domain.name,
                      domain.metric ?? 400,
                    ),
                  );
                }
              }
            }
          }
        }
      }
    }
  }
  return nodes;
}
