import type { AddrLike, Prefix } from "../ipnet.ts";
import type { SecurityGroupRef } from "./security-group.ts";

/** One route entry declared directly on the RouterEndpoint that IS the
 * anchor for it — `dst` reachable via `via`, optionally NAT'd. Moved
 * here from an earlier design where routes lived inside RoutingDomain
 * itself and the anchor was INFERRED by searching every router's every
 * endpoint for one whose declared subnet happened to contain `via`
 * (confirmed live, 2026-08-12: that inference is unnecessary — the
 * config author already knows which endpoint is the real egress, e.g.
 * router-voda-avm-v2's WAN-facing `left`, so declaring the route right
 * there removes a whole class of "which router owns this subnet"
 * ambiguity instead of resolving it algorithmically). The anchor is now
 * simply "whichever (router, side) this array lives on" — see
 * computeRoutes, src/ir.ts, which no longer searches for it at all.
 * `securityGroup` (source NAT/masq) is only collected here, at the
 * anchor — never replicated onto routers that merely relay toward it.
 * The group's rules are the source of intent; the IR carries its name.
 *
 * `via` is OPTIONAL on the ANCHOR side only — omitting it means "the
 * anchor needs no literal route of its own here, it's handled some
 * other way on its own side" (e.g. SLAAC/RA on a client-facing segment
 * for an IPv6 default, or an existing less-specific route already
 * covering it there — see RouterEndpointService above). It does NOT
 * mean "no route at all": every OTHER router in the same RoutingDomain
 * still gets a route to `dst`, rewritten to the anchor's own address on
 * whichever CollisionDomain they share with it, exactly as when `via`
 * IS given (confirmed live, 2026-08-12: "you need to add that route to
 * all hops so that all packets will be transmitted to [the anchor's own
 * side]" — computeRoutes() (src/ir.ts) only skips the ANCHOR's own
 * emission when `via` is absent, every other participant is
 * unaffected). */
export interface RouterEndpointRoute {
  readonly dst: Prefix;
  readonly via?: AddrLike;
  /** Security groups whose rules apply to traffic for this route. The
   * groups are the single source of what "masq"/NAT means; the IR carries
   * their references, not a boolean. */
  readonly securityGroups?: readonly SecurityGroupRef[];
  /** Preference of THIS route among equal-prefix routes (lower wins, standard
   * Linux metric) — chiefly to rank two endpoints' default routes when a
   * workload has several NICs (e.g. management-v2 preferred over
   * control-plane). Falls back to the holding list's own `metric`, then to
   * execution order (2026-09-19). */
  readonly metric?: number;
}

/** One destination a RoutingDomain distributes — the DST only; the next
 * hop is resolved per consumer (a participating router's address on the
 * shared CollisionDomain, or a workload's gateway on the segment it
 * attaches to). */
export interface RoutingDomainRoute {
  readonly dst: Prefix;
  /** Per-destination override of the domain's `metric` — see
   * RouterEndpointRoute.metric (2026-09-19). */
  readonly metric?: number;
}

/** A named group of routers that should all learn about each other's
 * anchor routes — declared once via net.routingDomain(), then
 * referenced by any number of routers' Router.routingDomains. Purely a
 * membership tag now, no routes of its own (see RouterEndpointRoute
 * above for where those actually live): every participant router's own
 * RouterEndpoint.routes get propagated to every OTHER participant,
 * rewritten to the anchor's own address on whichever CollisionDomain
 * the two routers actually share (src/ir.ts's computeRoutes) — and the
 * SAME participant set also gets direct peer-to-peer routes to each
 * other's own subnets regardless of whether any `routes` entry exists
 * at all (src/ir.ts's computeInterconnectRoutes — "interconnect only
 * exists if a routing domain exists," confirmed live 2026-08-12). A
 * router with no shared domain to a given anchor, or where `via`'s
 * family doesn't resolve at all (e.g. an IPv6-only route on a router
 * whose own connectivity there is SLAAC/RA-derived, not static), simply
 * gets no route from that entry — not an error, just a different route
 * source owning that family for that router (confirmed live design
 * discussion, 2026-08-11: "if you don't know the next hop you don't
 * apply anything"). */
export interface RoutingDomain {
  readonly name: string;
  /** Default metric for every route this domain distributes to its
   * participants and (transitively) to the workloads on their segments —
   * the one place to express an egress PREFERENCE ("Management-default is
   * 100, ControlPlane-Route is 1000"). A RoutingDomainRoute's own `metric`
   * overrides it; neither set → execution order (2026-09-19). Lower wins. */
  readonly metric?: number;
  /** The destinations this domain distributes to its participants
   * (routers) and, transitively, to workloads attached to their
   * segments (src/ir.ts's domainDsts/workloadDomainRoutes).
   *
   * Omitted OR empty → CALCULATED: the destinations the domain's
   * participants actually resolved (anchor routes + interconnect),
   * minus infrastructure transit/backdoor/backbone prefixes. An explicit,
   * non-empty array (net.routingDomain()'s 2nd arg) OVERRIDES that
   * calculated set — e.g. a domain that distributes specific prefixes
   * only. There is no fixed implicit default; a `*-defaultRoute` domain
   * gets its default because its anchor declares one. */
  readonly routes?: readonly RoutingDomainRoute[];
}
