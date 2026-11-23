import type { TransitNetwork } from "../addressing.ts";
import type { Addr, AddrLike, Prefix } from "../ipnet.ts";
import type { CollisionDomain } from "./collision-domain.ts";
import type { RouterEndpointService } from "./endpoint-service.ts";
import type { Host } from "./host.ts";
import type { InterfaceKind } from "./interface.ts";
import type { KernelApp } from "./kernel-app.ts";
import type { RouterEndpointRoute, RoutingDomain } from "./routing.ts";
import type { SecurityGroupRef } from "./security-group.ts";
import type { EndpointService } from "./service.ts";

// ── Router: connects exactly two collision domains ──────────────────
// The L3 primitive underneath Uplink/Segment (which are becoming a
// superset built on top of this + CollisionDomain, see ADR 0003) — one
// OVN Logical_Router, with exactly two Logical_Router_Ports (`left`/
// `right`), each bound into a different CollisionDomain. Real OVN
// routers can have more than two ports; this project's actual deployed
// topology never uses that — every router today is a bridge between
// exactly its own segment/uplink and the shared backbone (confirmed:
// router-home's real LRPs are lrp-home + lrp-home-bb, nothing else) —
// so N-way routers are a deliberate non-goal here, not an oversight.
// 3+-way connectivity is achieved by chaining through a shared
// CollisionDomain (the backbone), not by one router with many legs.
//
// Deliberately no `routes` field yet — static routes are a
// GLOBAL-topology concern (which hops does traffic between two
// arbitrary domains actually cross), not something to hand-declare
// per Router. See net.reachability() (define.ts) for the intended
// direction: declare "domain A needs to reach domain B," compute the
// path (and its return path) by walking the graph of CollisionDomains
// connected by Routers, instead of manually restating the same fact at
// every hop along the way.
//
// A MAC is set only explicitly (RouterEndpoint.mac); otherwise the
// runtime folds it from the resolved IPv4 (ADR 0011 §5).
/** One real (host, interface) pair — see RouterEndpoint.ifaces. */
export interface HostInterface {
  readonly host: Host;
  readonly iface: InterfaceKind;
}

/** The INPUT (topology-definition) half of the endpoint identity
 * contract, shared by every endpoint implementation: `name` is OPTIONAL.
 * An author may pin one, but it is normally derived from configuration by
 * the builder (deriveEndpointName, router-endpoints.ts). Optional (not
 * absent) precisely because a KernelRouterEndpoint/TunnelRouterEndpoint
 * INPUT cannot know the enclosing router's derived transit domain, yet
 * still must conform to the same contract as the OVN endpoint it resolves
 * into. */
export interface EndpointDefinition {
  /** Optional explicit port name — overrides the derived one. */
  readonly name?: string;
}

/** The INTERNAL half of the same contract: `name` is REQUIRED. Every
 * endpoint a Router actually stores (its `endpoints[]`) satisfies this,
 * whichever builder produced it. */
export interface EndpointBase extends EndpointDefinition {
  /** The endpoint's OVN port name AND its identity: `lrp-<fnv1a64>`
   * unless an explicit `name` was given. This is the ovn.lrp key/name
   * across the TS/Python boundary — the reconciler keys a real port by
   * its own name (reconciler/ovn/reconcile.py), so this must be stable
   * under endpoint reordering and derivable from configuration, never
   * positional (no array index) and never from mutable state. */
  readonly name: string;
}

// ── RouterEndpoint: OVN-side or kernel-side, discriminated by `kind` ──
// Everything a router endpoint needs REGARDLESS of which world its
// OTHER side touches (an OVN CollisionDomain, or — not yet built, see
// the netnsBridge/KernelRouterEndpoint design discussion, 2026-08-12 —
// a Linux kernel netns) lives on the shared INPUT base below. `kind` lets
// generation code (src/ir.ts's toIR()) branch to a different emission
// strategy per side, instead of a structural ("does it have l2Segment")
// check — same discriminated-union pattern InterfaceKind already uses
// in this file.
export interface RouterEndpointInputBase<
  S = RouterEndpointService,
> extends EndpointDefinition {
  /** A router port's own addresses — plain parsed IPv4/IPv6 values, one
   * array entry per address (IPv4.parse(...), IPv6.parse(...)). A
   * router endpoint has no segment/uplink identity for a fold rule to
   * derive an address from, so its addresses are simply declared. */
  readonly ipaddrs: readonly AddrLike[];
  /** Explicit MAC — the only one the IR carries. Needed when there is
   * no IPv4 to fold one from (a DHCP-assigned leg); undefined: the
   * runtime folds it from the resolved IPv4 (ADR 0011 §5). */
  readonly mac?: string;
  /** Real physical/tunnel attachment(s) for this endpoint's side of the
   * collision domain — a segment's localnet port needs one (it bridges
   * OVN's virtual world onto a real NIC/VLAN/tunnel), a router's
   * backbone-facing port doesn't (OVN-internal transit never touches
   * real hardware — northd compiles it straight into OpenFlow on
   * br-int). Optional and plural for exactly that reason: not every
   * endpoint has one, and a domain's physical presence isn't
   * necessarily one interface on one chassis (redundant attachment
   * across chassis is a real case, not just tolerated). */
  readonly ifaces?: readonly HostInterface[];
  /** Pins this endpoint's LRP to a specific chassis (OVN's native
   * gateway-chassis mechanism — real captured data already carries
   * this on every LRP, confirmed live 2026-08-09) instead of being
   * fully distributed. Typically only the endpoint that needs
   * NAT/external egress sets this, not both. Undefined means "fully
   * distributed, no pinning" — OVN's own default. */
  readonly gatewayChassis?: Host;
  /** A leg only for what this router itself routes into its network (the
   * platform's ingress leg): no other router learns that network through
   * it, and this router learns nothing through it. */
  readonly routeIsolated?: boolean;
  /** Not designed yet — see SecurityGroupRef above. */
  readonly securityGroup?: SecurityGroupRef;
  /** IPv6 RA/SLAAC behavior on this endpoint's LRP — see
   * RouterEndpointService above. Undefined/empty means neither
   * ipv6_ra_configs key gets set (OVN's own default: no RA at all). The
   * array also carries ServiceAttachments (`endpoint.attachTo(sv, {...})`)
   * — a workload's NIC on this endpoint's segment (2026-09-08).
   *
   * INTERNAL: the EndpointBuilder (define.ts) injects `endpoint` into every
   * entry — see EndpointService — so each service carries a reference to
   * the endpoint it's attached to (its l2Segment/ipaddrs/routes), which the
   * generator (src/ir.ts) reads to wire the service's NIC. The author writes
   * the plain kind; `endpoint` is added by the builder, never by hand. */
  readonly services?: readonly S[];
  /** Routes this endpoint is the ANCHOR for — see RouterEndpointRoute
   * below. Declaring a route here IS what makes this (router, side)
   * the anchor; nothing infers it from address containment anymore.
   * Only takes effect for routers that also declare a shared
   * Router.routingDomains membership (see RoutingDomain below) — a
   * route declared here with no domain membership on this router
   * reaches no one but this router itself. */
  readonly routes?: readonly RouterEndpointRoute[];
  /** The RoutingDomains this endpoint's DECLARED ROUTES participate in
   * (2026-08-23) — per-endpoint membership that OVERRIDES the router-
   * level Router.routingDomains for THIS endpoint when set. A router can
   * legitimately anchor one domain from its left and participate in
   * another from its right (a tunnelRouterEndpoint does exactly that:
   * the left anchors Neighbor-defaultRoute, the right joins
   * Voda-defaultRoute). Undefined = inherit the router's own
   * routingDomains (the pre-2026-08-23 behavior). */
  readonly routingDomains?: readonly RoutingDomain[];
  /** Per-leg flow-export switch (2026-10-10). Undefined = default: a
   * SEGMENT leg (an ovnRouterEndpoint with `ifaces` on a real
   * vlan/physical interface) is sampled whenever its Host declares
   * `monitoring.ipfix`; a TRANSIT leg (a kernel/tunnel router's transit
   * or tunnel-backdoor veth — internal mesh plumbing, not segment
   * traffic) defaults OFF. `true`/`false` override either way (so
   * `flow: true` opts a transit link IN). The concrete unit is the
   * leg's OVS bridge (`br-<shortName>`, src/ir.ts's
   * collisionDomainToIR) — sampling attaches to a bridge, not a port,
   * so a bridge shared by several legs is OFF if ANY of them is off
   * (off wins). A backbone leg rides br-int and a kernel/tunnel leg's
   * real interface lives in a netns, so neither has a per-leg bridge to
   * switch: the flag is recorded there but has no OVS effect (a known
   * partial-effect gap, not a silent one). */
  readonly flow?: boolean;
}

/** The RESOLVED (stored) endpoint base — RouterEndpointInputBase plus
 * the one thing that cannot be optional on it: the stable,
 * configuration-derived `name` (the merge key). Every endpoint a Router
 * stores satisfies EndpointBase; the INPUT shapes
 * (KernelRouterEndpoint/TunnelRouterEndpoint/OvnRouterEndpointSpec) only
 * satisfy EndpointDefinition, since the builder computes their name with
 * the enclosing router's name and the endpoint's role. */
export interface RouterEndpointBase<
  S = RouterEndpointService,
> extends RouterEndpointInputBase<S> {
  readonly name: string;
  /** Stored as Addr (ADR 0011): a written IPv4/IPv6 becomes a pinned one. */
  readonly ipaddrs: readonly Addr[];
}

/** Today's ONLY concrete shape — adds the one field that's actually
 * OVN-specific: which CollisionDomain (Logical_Switch) this LRP binds
 * into. */
export interface OvnRouterEndpoint
  extends RouterEndpointBase<EndpointService<RouterEndpointService>> {
  readonly kind: "ovn";
  readonly l2Segment: CollisionDomain;
  /** Security groups attached to this OVN endpoint/LRP. */
  readonly securityGroups?: readonly SecurityGroupRef<"ovn">[];
}

/** The input shape for NetworkBuilder.kernelRouterEndpoint() (define.ts)
 * — Meno's own design idea, 2026-08-12: describes the real-world
 * (WAN-facing) side of an OVN<->kernel transit link. `transit` is
 * always built by calling transitNetwork(ipv4, ipv6) (addressing.ts) in
 * the topology itself, never assembled by hand. `host`: which real
 * chassis the netns this eventually creates (KernelRouter, below)
 * actually runs on — required, since nothing else in scope at a
 * net.kernelRouterEndpoint() call site supplies it. NAT
 * (`kernel.*.masq` services) and apps still land here; real-interface
 * discovery is not modeled yet beyond "create the netns, assign it
 * these addresses" (see KernelRouter's own doc comment). */
export interface KernelRouterEndpoint extends RouterEndpointInputBase {
  readonly kind: "kernel";
  readonly host: Host;
  readonly transit: TransitNetwork;
  /** EXPLICIT, fully-built security groups (net.securityGroup(),
   * define.ts) to attach to this endpoint's real-world-facing
   * interface. When non-empty, the `kernel.*.masq` services are IGNORED
   * (the author owns the groups' content); when absent, the masq
   * shortcut builds `masq-<router>` implicitly. Every group must have
   * been declared via net.securityGroup() in the same defineNetwork call. */
  readonly securityGroups?: readonly SecurityGroupRef<"kernel">[];
  /** The generated name of its transit collision domain (a tenant's or a
   * wan's `svc` cable, ADR 0009); default `transit-<router>`. */
  readonly transitName?: string;
}

/** The input shape for NetworkBuilder.tunnelRouterEndpoint() (define.ts)
 * — the generic "ANY TUNNEL" pattern (WireGuard today, ZeroTier later;
 * see InterfaceKind's "wireguard"/"zerotier" variants): a kernel netns
 * with a MESH transit on one side (left — same as
 * kernelRouterEndpoint()'s `transit`, connecting the netns to the OVN
 * mesh), an UPSTREAM transit on the other (right — the tunnel's own
 * endpoint handshake/keepalive UDP must reach the real internet via a
 * mundane path, never through the tunnel itself, so the netns's default
 * route goes out this leg), and the tunnel interface in the middle. The
 * tunnel egress is the ANCHOR's default: the `routes` field carries the
 * via-less `0.0.0.0/0`/`::/0` the routing domain rewrites to other
 * participants.
 *
 * Deliberately NOT extending RouterEndpointBase: this endpoint has no
 * `ipaddrs` of its own (every address is derived from the two transits
 * at build time), and it's consumed immediately by
 * tunnelRouterEndpoint() into a plain OvnRouterEndpoint — the tunnel
 * itself is never an IR endpoint, only the netns + wireguard app it
 * builds. */
export interface TunnelRouterEndpoint extends EndpointDefinition {
  readonly kind: "tunnel";
  readonly host: Host;
  /** The mesh-side transit (left, veth-krn-* style) — same mechanics as
   * kernelRouterEndpoint()'s `transit`. */
  readonly transit: TransitNetwork;
  /** The upstream-side transit (right, veth-bdk-* style) — back toward
   * the physical path the tunnel endpoint is reached through. */
  readonly upstream: TransitNetwork;
  // NOTE: the tunnel WORKLOAD is no longer a `tunnel` field — it's a
  // `wireguard`/`zerotier` service in `services[]` below (define.ts maps it
  // onto the generic kernel.app.service).
  /** The upstream-peer router's BACKBONE-facing port (2026-08-23): the
   * tunnel netns's upstream/backdoor leg terminates on an OVN router
   * (`<router>-upstream`) that tunnelRouterEndpoint defines INTERNALLY —
   * it carries the backdoor LRP on `upstream` AND this backbone port, so
   * the tunnel's endpoint traffic enters the mesh (whose default routes
   * it on to the physical WAN, e.g. via voda-avm) instead of dying in
   * the root namespace. */
  readonly upstreamBackbone: {
    readonly l2Segment: CollisionDomain;
    readonly ipaddrs: readonly AddrLike[];
    /** Its leg's own routes, services (masq), gateway chassis and domains
     * — a VPN gateway's NAT gateway onto internet-core (ADR 0009). */
    readonly routes?: readonly RouterEndpointRoute[];
    readonly services?: readonly RouterEndpointService[];
    readonly masq?: readonly ("ipv4" | "ipv6")[];
    readonly masqSources?: readonly Prefix[];
    readonly gatewayChassis?: Host;
    readonly routingDomains?: readonly RoutingDomain[];
  };
  /** Generated names (ADR 0009): its two cables and its upstream router;
   * defaults `transit-<router>`, `backdoor-<router>`, `<router>-upstream`. */
  readonly names?: {
    readonly transit?: string;
    readonly backdoor?: string;
    readonly upstream?: string;
  };
  /** The RoutingDomains the INTERNAL upstream-peer router joins
   * (2026-08-23) — it needs the mesh's default route (learned via the
   * domain anchor rewrite) to forward the tunnel's endpoint traffic out
   * its backbone leg to the physical WAN (e.g. Voda-defaultRoute →
   * voda-avm). Typically the same domain as the enclosing router's
   * backbone/right side. */
  readonly upstreamDomains?: readonly RoutingDomain[];
  /** The anchor's default route(s) — via-less, so computeRoutes rewrites
   * them to the domain's other participants. */
  readonly routes?: readonly RouterEndpointRoute[];
  /** Per-endpoint routing domain membership (2026-08-23) — stamped onto
   * the returned OVN endpoint AND the KernelRouter. */
  readonly routingDomains?: readonly RoutingDomain[];
  /** The OVN-side services (ipv6.*) and the kernel masq services (whose
   * families the wireguard app masquerades out the tunnel). */
  readonly services?: readonly RouterEndpointService[];
}

/** One side of a KernelRouter's own netns — an IP assignment, plus
 * (right side only, in practice — see NetworkBuilder.kernelRouterEndpoint(),
 * define.ts) whichever RouterEndpointRoute entries the config author
 * declared for the real-world-facing side. A route with no `via` means
 * exactly what it means everywhere else RouterEndpointRoute is read
 * (src/ir.ts's computeRoutes): "this side needs no literal route of its
 * own for this — handled elsewhere," e.g. IPv6 RA/DHCP on the real
 * segment, not yet modeled here — src/ir.ts's kernelRouterSideToIR
 * drops those entirely rather than emitting a route with no nexthop to
 * apply. `ifaces` carries the real physical attachment(s) this side's
 * addresses/routes actually bind to (same shape as RouterEndpointBase.
 * ifaces — see its own doc comment above) — the deployer creates/moves
 * the interface into the netns instead of a dummy stand-in
 * (fabric/emit.py's own _emit_kernel_router_create). Only
 * populated for `right` today (via NetworkBuilder.kernelRouterEndpoint(),
 * which copies the kernelRouterEndpoint() input's `ifaces` onto this
 * side in ADDITION to the OVN-side endpoint's own copy, 2026-08-18 —
 * the endpoint keeps its ifaces so the transit domain still gets its
 * localnet port/bridge binding/bridge-mapping); the `left` side's real
 * iface binding is still its own next step. */
export interface KernelRouterSide {
  readonly ipaddrs: readonly Addr[];
  readonly routes?: readonly RouterEndpointRoute[];
  /** Real physical/tunnel attachment(s) for this side — mirrors
   * RouterEndpointBase.ifaces (types.ts): `host` is the chassis the
   * interface actually exists on (the KernelRouter's own host in
   * practice), `iface` is the InterfaceKind to create/move into this
   * router's netns. A side with no `ifaces` still gets a dummy device
   * from the deployer so its addresses/routes have something to bind
   * to. */
  readonly ifaces?: readonly HostInterface[];
  /** An APPLICATION running inside this side's netns, on this side's
   * real interface (right side only in practice — set by
   * buildKernelRouterEndpoint, define.ts, from the `kernel.app.*`
   * service kinds; see KernelApp). Resolved once at declaration time;
   * the deployer turns each into the actual `ip netns exec` service
   * script — and MUST stop/release it on delete, since a running app
   * daemon holds the netns alive past `ip netns delete`. */
  readonly apps?: readonly KernelApp[];
  /** The FULLY-RESOLVED security group attached to this side's real
   * interface (right side only in practice) — an object reference, same
   * "no string that could drift" discipline as l2Segment/routingDomains.
   * Computed once, in buildKernelRouterEndpoint (define.ts): an explicit
   * `securityGroups` on the kernelRouterEndpoint() input win outright
   * (the `kernel.*.masq` services are then IGNORED), otherwise the masq
   * shortcut builds `{ name: "masq-<router>", rules: [...] }` through
   * net.securityGroup() — see SecurityGroup, types.ts. Serialized into
   * the implementation-abstract `security.group` IR node (src/ir.ts's
   * securityGroupToIR); this side node is the ATTACHMENT point. */
  readonly securityGroups?: readonly SecurityGroupRef<"kernel">[];
}

/** A router whose two ports are real Linux interfaces inside ONE netns
 * running on `host` — NOT an OVN Logical_Router at all (contrast
 * Router, below). Confirmed live, 2026-08-12 (`ip netns exec
 * ns-uplink-voda-avm ip a`): `left` is the transit-facing side (the
 * veth pair's own kernel-side leg — veth-krn-0, 10.99.0.2/28 in that
 * capture, matching transitNetwork()'s `.right`, addressing.ts);
 * `right` is the real-world-facing side (ens18.1280 in that capture,
 * the actual WAN interface). Built via
 * NetworkBuilder.kernelRouterEndpoint() (define.ts), not declared
 * directly — see that method's own doc comment for why. src/ir.ts's
 * toIR() emits a `kernel.router` IR node per instance: creates the real
 * netns and assigns it these addresses — no real iface binding yet,
 * see KernelRouterSide's own doc comment. */
export interface KernelRouter {
  readonly name: string;
  readonly host: Host;
  readonly left: KernelRouterSide;
  readonly right: KernelRouterSide;
  /** The transit CollisionDomain this KernelRouter's `left` shares with
   * its OVN twin (kernelRouterEndpoint()'s own transitDomain, define.ts)
   * — an object reference, not a name, same "no string that could drift
   * out of sync" reasoning as l2Segment/gatewayChassis elsewhere in this
   * file. Lets src/ir.ts's toIR() find which Router endpoint is THIS
   * KernelRouter's other half (matching endpoint.l2Segment === this),
   * so it can mirror that router's own already-computed routes onto
   * this KernelRouter's `left` — the kernel netns otherwise has no way
   * to route back INTO the OVN mesh at all (confirmed live, 2026-08-13:
   * router-voda-avm-v2 got its own default route via kernel-0's
   * transit-facing address, but kernel-0 itself had no route back to
   * home/management's own subnets — nothing told it those exist).
   * Always set: both builders (kernelRouterEndpoint()/
   * tunnelRouterEndpoint()) create the KernelRouter as an OVN twin, so
   * there is always a transit domain to name. */
  readonly transitDomain: CollisionDomain;
  /** The OVN twin's OWN address on `transitDomain` (kernelRouterEndpoint()'s
   * own `ovnSideAddrs`) — the nexthop for every route mirrored onto
   * `left` per transitDomain's own doc comment above. NOT the same as
   * `left.ipaddrs` (this KernelRouter's OWN address on that same
   * domain) — this is the address on the OTHER end of that same wire.
   * Always set, like transitDomain above. */
  readonly transitPeerAddrs: readonly Addr[];
  /** The collision domain the `right` side's leg is plugged into, when it
   * is one of the fabric's (a tunnel's upstream switch). Absent for a
   * WAN side, whose LAN only that side is on (ADR 0011). */
  readonly upstreamDomain?: CollisionDomain;
  /** Same field, same meaning as Router.routingDomains below — a
   * KernelRouter's own routes (KernelRouterSide.routes) only apply if
   * it's actually a participant of some declared RoutingDomain, same
   * rule that already gates every OVN-side route (src/ir.ts's
   * computeRoutes). Set once, at declaration time (NetworkBuilder.
   * defineOvnRouter() stamps the router's returned routingDomains onto the
   * KernelRouter it creates, when the endpoint didn't carry its own
   * per-endpoint routingDomains, 2026-09-08) — never a second,
   * independently-stated copy that could drift from the owning Router's
   * own routingDomains. */
  readonly routingDomains?: readonly RoutingDomain[];
}

export type RouterEndpoint =
  | OvnRouterEndpoint
  | KernelRouterEndpoint
  | TunnelRouterEndpoint;

/** The AUTHOR-facing endpoint spec (before the builder injects `endpoint`
 * into each service): same as OvnRouterEndpoint but `services` is the plain
 * union. define.ts's buildOvnRouterEndpoint turns a spec into the stored
 * OvnRouterEndpoint by injecting the endpoint into every service. `id` is
 * omitted too — it's derived by the builder, never authored (an author who
 * wants a specific name sets `name` instead). */
export type OvnRouterEndpointSpec =
  & Omit<OvnRouterEndpoint, "services" | "name" | "ipaddrs">
  & {
    /** Optional explicit port name — see EndpointDefinition. */
    readonly name?: string;
    /** Written (IPv4/IPv6, pinned) or a LogicalIP's Addr (ADR 0011). */
    readonly ipaddrs: readonly AddrLike[];
    /** Masquerade what leaves through this leg, per family — the one DSL
     * spelling (ADR 0011 §3b); on an OVN leg an SNAT to the leg's own
     * address, which the runtime resolves. */
    readonly masq?: readonly ("ipv4" | "ipv6")[];
    /** The networks `masq` masquerades; default: the router's other legs. */
    readonly masqSources?: readonly Prefix[];
    readonly services?: readonly RouterEndpointService[];
  };
