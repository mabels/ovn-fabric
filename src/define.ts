// src/define.ts — defineNetwork(): a Vite-defineConfig / Jest-describe
// style builder. Lets config/topology.ts read as a declaration rather
// than a set of raw object literals, and validates as it goes (e.g. a
// segment can only reference an uplink that was already declared in
// the same defineNetwork call).

import type { UplinkBuilder } from "./factories.ts";
import type { IPv4, IPv6 } from "./ip.ts";
import {
  CollisionDomain,
  FixedUplink,
  type Host,
  type HostAddress,
  type InterfaceKind,
  type KernelApp,
  type KernelRouter,
  type KernelRouterEndpoint,
  type KernelRouterSide,
  localHost,
  type OvnClusterOptions,
  type OvnHostConfig,
  type OvnRouterEndpoint,
  type Router,
  type RouterEndpoint,
  type RoutingDomain,
  type SecurityGroup,
  type SecurityGroupRule,
  type Segment,
  sshHost,
  type Uplink,
  type UplinkSelector,
} from "./types.ts";

export interface NetworkDefinition {
  readonly name: string;
  readonly allUplinks: readonly Uplink[];
  readonly allSegments: readonly Segment[];
  /** Every Host declared via sshHost()/localHost() — not just the ones
   * referenced by an Uplink/Segment. A pure-central chassis (ADR 0003:
   * runs only ovn-central, hosts no uplink/segment of its own) would
   * otherwise be unreachable from a NetworkDefinition at all. */
  readonly allHosts: readonly Host[];
  /** Every bare collision domain declared via net.collisionDomain()/
   * net.backbone() — L2 only, see CollisionDomain (types.ts). Does NOT
   * include the logical switches Uplink/Segment create for themselves
   * (those stay implicit, derived at generation time, same as today). */
  readonly allCollisionDomains: readonly CollisionDomain[];
  /** The one collision domain declared via net.backbone(), if any —
   * OVN's own internal backbone switch (sw-backbone in real captured
   * data), made explicit instead of silently auto-created. */
  readonly backbone?: CollisionDomain;
  /** Every router declared via net.ovnRouter() — see Router (types.ts).
   * Does NOT include the routers Uplink/Segment create for themselves
   * today (still implicit, same as allCollisionDomains above). */
  readonly allRouters: readonly Router[];
  /** Every KernelRouter created by net.kernelRouterEndpoint() (see
   * KernelRouter, types.ts) — never declared directly, always as a side
   * effect of that method (define.ts's own NetworkBuilder.
   * kernelRouterEndpoint()). Real Linux netns instances, not OVN
   * Logical_Routers — src/ir.ts's toIR() emits three `kernel.router`
   * nodes per instance (one router-level, carrying the host reference;
   * one per side). */
  readonly allKernelRouters: readonly KernelRouter[];
  /** Every named route set declared via net.routingDomain() — see
   * RoutingDomain (types.ts). Referenced by name from Router.
   * routingDomains, resolved into real per-router routes at IR time
   * (src/ir.ts's computeRoutes), not here. */
  readonly allRoutingDomains: readonly RoutingDomain[];
  /** Every security group built via net.securityGroup() — see
   * SecurityGroup (types.ts). Includes the ones the `kernel.*.masq`
   * shortcut builds implicitly (masq-<router>) — same "never declared
   * directly, always as a side effect of kernelRouterEndpoint()"
   * reasoning as allKernelRouters. Serialized to one implementation-
   * abstract `security.group` IR node each (src/ir.ts's
   * securityGroupToIR). */
  readonly allSecurityGroups: readonly SecurityGroup[];
  /** Cluster-wide OVN settings (NB_Global) — see ovnGlobal() below.
   * Undefined means "OVN defaults for everything," not "no OVN
   * cluster" (that's whether any Host has an `ovn` role at all). */
  readonly ovnGlobal?: OvnClusterOptions;
}

/**
 * net.uplink(name, uplinkVlan({...})) / net.uplink(name, uplinkPhysical({...})) —
 * the second argument is an UplinkBuilder: a function of `slot` that a
 * factory in factories.ts returned. NetworkBuilder assigns the next
 * free slot (0-4095, see addressing.ts transferNet()) and calls it —
 * config/topology.ts never sees or chooses a slot itself, so two
 * uplinks can never collide on the same transfer-link block.
 */

type SegmentSpec = Omit<Segment, "name">;

/**
 * The builder context passed into NetworkBuilder.ovnRouter()'s own
 * callback (2026-08-12) — same "context object, void callback" idiom as
 * defineNetwork's own `net`, one level down. `routingDomains`/`left`/
 * `right` are plain SETTABLE attributes, not a returned object or
 * separate constructor arguments: kernelRouterEndpoint() needs to read
 * `routingDomains` at the moment it's called (to stamp a KernelRouter
 * with the same membership — see KernelRouter.routingDomains, types.ts)
 * — set `router.routingDomains` before calling
 * `router.kernelRouterEndpoint()` in the callback body, same
 * declare-before-use discipline every other builder method in this file
 * already requires (e.g. a collisionDomain must exist before a router
 * references it). ovnRouterEndpoint()/kernelRouterEndpoint() are ONLY
 * reachable through this object now, not as NetworkBuilder methods —
 * matches ovnRouter()'s own doc comment for why.
 */
export interface RouterBuilder {
  routingDomains?: readonly RoutingDomain[];
  left?: RouterEndpoint;
  right?: RouterEndpoint;
  ovnRouterEndpoint(input: Omit<OvnRouterEndpoint, "kind">): OvnRouterEndpoint;
  kernelRouterEndpoint(
    input: Omit<KernelRouterEndpoint, "kind">,
  ): OvnRouterEndpoint;
}

/**
 * The builder context passed into defineNetwork's callback. Each method
 * both registers the declared thing and returns a handle to it, so later
 * calls in the same callback can reference earlier ones directly
 * (`net.segment("home", { uplink: avm, ... })`), the same way Jest's
 * `describe`/`it` or Vite's defineConfig read top-to-bottom.
 */
export class NetworkBuilder {
  private readonly uplinksByName = new Map<string, Uplink>();
  private readonly usedUplinkIds = new Set<number>();
  private nextSlot = 0;
  private readonly segmentsByName = new Map<string, Segment>();
  private readonly usedSegmentIds = new Set<number>();
  private readonly hostsByName = new Map<string, Host>();
  private centralHostName: string | undefined;
  private ovnGlobalOptions: OvnClusterOptions | undefined;
  private readonly collisionDomainsByName = new Map<string, CollisionDomain>();
  private backboneDomain: CollisionDomain | undefined;
  private readonly routersByName = new Map<string, Router>();
  private readonly kernelRoutersByName = new Map<string, KernelRouter>();
  private readonly routingDomainsByName = new Map<string, RoutingDomain>();
  private readonly securityGroupsByName = new Map<string, SecurityGroup>();

  // Both host-declaring methods route through this — the "at most one
  // central chassis per cluster" check has to live in exactly one
  // place, not be duplicated between sshHost/localHost. HA central (a
  // 3-node clustered NB/SB, more than one "central" chassis) is real
  // OVN capability but not yet verified against a live cluster (ADR
  // 0003, "Open questions") — rejected here rather than silently
  // accepted and producing untested behavior.
  private registerHost(host: Host): Host {
    if (this.hostsByName.has(host.name)) {
      throw new Error(`host "${host.name}" declared more than once`);
    }
    if (host.ovn?.role.kind === "central") {
      if (this.centralHostName !== undefined) {
        throw new Error(
          `host "${host.name}" declares a second central chassis — ` +
            `"${this.centralHostName}" already is one. HA central (multiple ` +
            `central chassis) isn't supported yet, see ADR 0003.`,
        );
      }
      this.centralHostName = host.name;
    }
    this.hostsByName.set(host.name, host);
    return host;
  }

  /** Declare a host reachable via SSH. Returns a handle for reuse. */
  sshHost(
    name: string,
    address: HostAddress,
    user: string,
    ovn?: OvnHostConfig,
  ): Host {
    return this.registerHost(sshHost(name, address, user, ovn));
  }

  /** Declare the generator's own host — no SSH needed. */
  localHost(name: string, ovn?: OvnHostConfig): Host {
    return this.registerHost(localHost(name, ovn));
  }

  /** Cluster-wide OVN settings (NB_Global) — see OvnClusterOptions,
   * types.ts. At most once per defineNetwork call, same "declared more
   * than once" fail-fast as every other builder method. */
  ovnGlobal(options: OvnClusterOptions): void {
    if (this.ovnGlobalOptions !== undefined) {
      throw new Error("ovnGlobal() called more than once");
    }
    this.ovnGlobalOptions = options;
  }

  /** Declare a bare collision domain — an OVN logical switch, L2 only.
   * See CollisionDomain (types.ts) for how this differs from
   * uplink()/segment() (a superset: addressing, routes, NAT on top). */
  collisionDomain(name: string): CollisionDomain {
    if (this.collisionDomainsByName.has(name)) {
      throw new Error(`collision domain "${name}" declared more than once`);
    }
    const domain = new CollisionDomain(name);
    this.collisionDomainsByName.set(name, domain);
    return domain;
  }

  /** Declare THE cluster's backbone collision domain — OVN's own
   * internal transit switch (sw-backbone in real captured data),
   * previously always auto-created and never visible in topology.ts at
   * all. At most one per cluster, same "declared more than once"
   * fail-fast as the central-chassis check above — a cluster has
   * exactly one backbone, not several. */
  backbone(name: string): CollisionDomain {
    if (this.backboneDomain !== undefined) {
      throw new Error(
        `backbone collision domain "${name}" declared more than once — ` +
          `"${this.backboneDomain.name}" already is one`,
      );
    }
    const domain = this.collisionDomain(name);
    this.backboneDomain = domain;
    return domain;
  }

  // Shared by both endpoints of router() below — same "must already be
  // declared via this builder" fail-fast as uplink()'s backdoor.via
  // check and segment()'s uplink check.
  private checkRouterEndpoint(
    routerName: string,
    endpoint: RouterEndpoint,
  ): void {
    if (
      endpoint.kind === "ovn" &&
      !this.collisionDomainsByName.has(endpoint.l2Segment.name)
    ) {
      throw new Error(
        `router "${routerName}" references collision domain ` +
          `"${endpoint.l2Segment.name}", which was not declared via ` +
          `net.collisionDomain()/net.backbone() in this defineNetwork call`,
      );
    }
    if (
      endpoint.gatewayChassis !== undefined &&
      !this.hostsByName.has(endpoint.gatewayChassis.name)
    ) {
      throw new Error(
        `router "${routerName}" pins an endpoint to gateway chassis ` +
          `"${endpoint.gatewayChassis.name}", which was not declared via ` +
          `net.sshHost()/net.localHost() in this defineNetwork call`,
      );
    }
  }

  // gatewayChassis's natural default when a config author leaves it
  // unset: whichever host `ifaces` already names for this endpoint, IF
  // there's exactly one. A distributed router port with no
  // gateway-chassis pin and no bound VIF anywhere never gets scheduled
  // onto ANY chassis at all (confirmed live, 2026-08-10: `ovn-appctl -t
  // ovn-controller debug/dump-local-datapaths` on a real test chassis
  // listed nothing for a topology whose routers left gatewayChassis
  // unset, despite northd having compiled correct logical flows for
  // them). generate-ovn.ts's older model never hit this because it
  // pinned every LRP unconditionally (its own comment: "a chassis
  // cannot be scheduled for ANY of them without this flag") — there was
  // only ever one possible chassis to pin to. Injected HERE, at
  // declaration time, not left for toIR()/the deployer to guess later
  // — same "resolve it once, at the boundary that has the real Host
  // objects" reasoning as macFromV4 (ir.ts). Ambiguous (0 or 2+ hosts
  // on ifaces) leaves gatewayChassis unset, same as an explicit
  // omission — this only fills in the unambiguous case.
  private deriveGatewayChassis(endpoint: RouterEndpoint): RouterEndpoint {
    if (endpoint.gatewayChassis !== undefined) return endpoint;
    const hosts = new Set((endpoint.ifaces ?? []).map((hi) => hi.host));
    if (hosts.size !== 1) return endpoint;
    return { ...endpoint, gatewayChassis: [...hosts][0] };
  }

  /** Declare a named group of routers that should learn about each
   * other's routes — see RoutingDomain (types.ts). No `routes`
   * parameter here anymore: those live directly on whichever
   * RouterEndpoint is the real anchor (RouterEndpoint.routes) — this
   * only registers the membership tag, the same "register + fail fast
   * on duplicates, resolve later" split every other builder method
   * already follows. */
  routingDomain(name: string): RoutingDomain {
    if (this.routingDomainsByName.has(name)) {
      throw new Error(`routing domain "${name}" declared more than once`);
    }
    const domain: RoutingDomain = { name };
    this.routingDomainsByName.set(name, domain);
    return domain;
  }

  /** Declare a named security group — the ONE way a security group gets
   * built (see SecurityGroupBuilder for the per-call rule API; the
   * returned SecurityGroup is the fully-resolved name+rules object, not
   * a lazily-resolved reference). Any kernelRouterEndpoint() can then
   * attach it to its real-world-facing interface via its `securityGroup`
   * input — and the `kernel.*.masq` service shortcut builds one through
   * THIS same method (name `masq-<router>`), so implicit and explicit
   * groups are structurally identical by construction.
   *
   * Same "register + fail fast on duplicates, resolve later" split as
   * every other builder method here. The object returned is immutable
   * (a frozen copy of the builder's accumulation) — a caller can pass it
   * around by reference, like every other NetworkBuilder handle. */
  securityGroup(
    name: string,
    build: (group: SecurityGroupBuilder) => void,
  ): SecurityGroup {
    if (this.securityGroupsByName.has(name)) {
      throw new Error(`security group "${name}" declared more than once`);
    }
    const builder = new SecurityGroupBuilder(name);
    build(builder);
    const group = builder.build();
    this.securityGroupsByName.set(name, group);
    return group;
  }

  /** Declare a router connecting exactly two collision domains — see
   * Router/RouterEndpoint (types.ts) for why exactly two, not N. Named
   * ovnRouter(), not router(): kernelRouterEndpoint()/
   * ovnRouterEndpoint() (only reachable through the RouterBuilder this
   * passes into `build`, not as their own NetworkBuilder methods
   * anymore — 2026-08-12) already anticipate a future net.kernelRouter()
   * sibling — see that session's design discussion on why "OVN router"
   * needed a name of its own even before the kernel-side counterpart
   * existed.
   *
   * A single void callback, not `{left, right, routingDomains}` — moved
   * here 2026-08-12: kernelRouterEndpoint() needs to know this router's
   * OWN routingDomains at the moment it's called (to stamp the SAME
   * membership onto the KernelRouter it creates, so a kernel-side route
   * is gated by "is this router actually a participant of some
   * RoutingDomain" the exact same way an OVN-side route already is —
   * see KernelRouter.routingDomains, types.ts) — routingDomains can't be
   * a value returned alongside left/right, since kernelRouterEndpoint()
   * runs BEFORE that return happens. Setting it as an attribute on the
   * SAME builder object passed into the callback (`router.routingDomains
   * = [...]`, read live off `router` by kernelRouterEndpoint() below,
   * not captured at builder-construction time) — set it before calling
   * router.kernelRouterEndpoint(), same top-to-bottom declare-before-use
   * discipline every other builder method in this file already
   * requires. */
  ovnRouter(name: string, build: (router: RouterBuilder) => void): Router {
    if (this.routersByName.has(name)) {
      throw new Error(`router "${name}" declared more than once`);
    }
    const router: RouterBuilder = {
      routingDomains: undefined,
      left: undefined,
      right: undefined,
      ovnRouterEndpoint: (input) => this.buildOvnRouterEndpoint(input),
      kernelRouterEndpoint: (input) =>
        this.buildKernelRouterEndpoint(input, router.routingDomains, name),
    };
    build(router);

    if (router.left === undefined || router.right === undefined) {
      throw new Error(
        `router "${name}": both router.left and router.right must be ` +
          `set inside the ovnRouter() callback`,
      );
    }
    this.checkRouterEndpoint(name, router.left);
    this.checkRouterEndpoint(name, router.right);
    for (const domain of router.routingDomains ?? []) {
      if (this.routingDomainsByName.get(domain.name) !== domain) {
        throw new Error(
          `router "${name}" references routing domain "${domain.name}", ` +
            `which was not declared via net.routingDomain() in this ` +
            `defineNetwork call`,
        );
      }
    }
    const built: Router = {
      name,
      left: this.deriveGatewayChassis(router.left),
      right: this.deriveGatewayChassis(router.right),
      routingDomains: router.routingDomains,
    };
    this.routersByName.set(name, built);
    return built;
  }

  /** Tags a plain input object as the `kind: "ovn"` RouterEndpoint —
   * no real transformation, just keeps `kind: "ovn"` from ever being
   * hand-typed at a net.ovnRouter() call site. Private: only reachable
   * as `router.ovnRouterEndpoint()` inside an ovnRouter() callback
   * (2026-08-12) — matches kernelRouterEndpoint() below, which
   * genuinely needs the callback's own RouterBuilder for
   * routingDomains; this one carries no such need but stays alongside
   * it for symmetry rather than being reachable a different way. */
  private buildOvnRouterEndpoint(
    input: Omit<OvnRouterEndpoint, "kind">,
  ): OvnRouterEndpoint {
    return { kind: "ovn", ...input };
  }

  /** Declare a kernel router — a real Linux netns forwarding between two
   * real interfaces, not an OVN Logical_Router (see KernelRouter/
   * KernelRouterSide, types.ts). Never called directly by a config
   * author — always a side effect of kernelRouterEndpoint() below,
   * which is the real public entry point; same "register + fail fast
   * on duplicates" split every other builder method here follows. */
  kernelRouter(
    name: string,
    endpoints: {
      readonly host: Host;
      readonly left: KernelRouterSide;
      readonly right: KernelRouterSide;
      readonly transitDomain?: CollisionDomain;
      readonly transitPeerAddrs?: readonly (IPv4 | IPv6)[];
      readonly routingDomains?: readonly RoutingDomain[];
    },
  ): KernelRouter {
    if (this.kernelRoutersByName.has(name)) {
      throw new Error(`kernel router "${name}" declared more than once`);
    }
    const router: KernelRouter = { name, ...endpoints };
    this.kernelRoutersByName.set(name, router);
    return router;
  }

  /** The entry point where an OVN<->kernel transit link actually gets
   * created ("that was the entrypoint where we created the transit" —
   * 2026-08-12 design discussion). Creates a fresh, auto-named transit
   * CollisionDomain (`transit-<routerName>` — `router-voda-avm-v2` ->
   * a `ls:transit-router-voda-avm-v2` IR node, whose bridge/bridge-
   * mapping/localnet-port names the deployer derives from the same
   * name) AND a KernelRouter named after the enclosing
   * net.ovnRouter() (a `kernel.router` IR node named
   * `router-voda-avm-v2`, whose netns the deployer derives as
   * `ns-router-voda-avm-v2`), then returns the OVN side of the transit
   * link as a plain
   * OvnRouterEndpoint, ready to drop straight into net.ovnRouter()'s
   * left/right.
   *
   * `input.transit` is a TransitNetwork — always built by calling
   * transitNetwork(ipv4, ipv6) (addressing.ts) in the topology itself,
   * never assembled by hand, so it's always a valid transit pair here.
   * `.left` becomes the OVN side's own address (folded into the
   * returned OvnRouterEndpoint below); `.right` becomes the
   * KernelRouter's OWN transit-facing side (confirmed live, 2026-08-12:
   * `ip netns exec ns-uplink-voda-avm ip a` — veth-krn-0, the kernel
   * side of the SAME veth pair). `input.ipaddrs` becomes the
   * KernelRouter's real-world-facing side (confirmed live: ens18.1280,
   * the actual WAN interface in that same capture) — no real iface
   * binding for either KernelRouter side yet, that's its own next step
   * (KernelRouterSide, types.ts). No l2Segment on the input at all:
   * that's a purely OVN concept, and this input describes the KERNEL
   * side — it has no business naming an OVN domain to bind to.
   * Everything else (`Omit<KernelRouterEndpoint, "kind">` minus `host`/
   * `transit`/`ifaces`, consumed below — ipaddrs, mac, gatewayChassis,
   * securityGroup, services, routes) carries straight through to the
   * returned OvnRouterEndpoint, symmetric with ovnRouterEndpoint()
   * above taking `Omit<OvnRouterEndpoint, "kind">` — EXCEPT `services`
   * (split: the `kernel.*` kinds are consumed here into the
   * KernelRouter's security group, only the `ipv6.*` kinds reach the
   * OVN endpoint) and `securityGroup` (consumed here — attached to the
   * KernelRouter's `right`, masq shortcut expansion, see below). `ifaces`
   * is the exception that lands ONLY on the paired KernelRouter's `right`
   * (KernelRouterSide.ifaces, types.ts — the real-world-facing
   * interface the deployer moves into the netns). The transit domain
   * still gets its localnet port/gateway-chassis pin/bridge binding/
   * bridge-mapping from the returned OVN endpoint's OWN ifaces, which
   * are the transit veth (constructed explicitly here, 2026-08-18). `routes` in
   * particular matters here: the returned endpoint is a real
   * OvnRouterEndpoint, and it's frequently the RoutingDomain anchor
   * (e.g. a real ISP default route) despite being kernel-backed, so it
   * needs the same `.routes` a hand-declared OvnRouterEndpoint would
   * (RouterEndpointRoute, types.ts) — EXCEPT that a route's `via` is
   * ALWAYS rewritten to the paired KernelRouter's transit-facing
   * address here, regardless of whether the config author declared one.
   * The OVN side of a transit link has exactly ONE reachable peer — the
   * paired KernelRouter — so a literal `via` (e.g. the real ISP gateway
   * 192.168.132.1 in the voda-avm config) is a fact about the kernel
   * netns's WAN side, not about anything OVN can ARP on the transit
   * link: keeping it made the OVN logical router believe
   * 192.168.132.0/24 was directly connected on the transit port and
   * emit ARP requests for its WAN gateway out the transit veth
   * (confirmed live, 2026-08-21). This also supersedes the "via-less
   * means handled elsewhere" reading a client-facing endpoint gives a
   * route (RouterEndpointRoute's own doc comment: "e.g. SLAAC/RA on a
   * client-facing segment... or an existing less-specific route already
   * covering it there") — there is no SLAAC/RA on a transit link, so no
   * route declared here may be left for computeRoutes (src/ir.ts) to
   * skip. The literal `via` is NOT lost: `input.routes` lands
   * unmodified on the KernelRouter's own `right` side (below), where
   * the kernel netns applies it as its own real default gateway.
   *

   * `routingDomains` isn't part of `input` — it's whatever the
   * enclosing ovnRouter() callback's `router.routingDomains` is set to
   * AT THE TIME this runs (read live off the RouterBuilder by
   * ovnRouter() above, not an argument a config author passes here
   * directly), stamped onto the KernelRouter this creates so its own
   * routes are gated by the same RoutingDomain-membership rule an
   * OVN-side route already is (src/ir.ts's kernelRouterSideToIR). */
  private buildKernelRouterEndpoint(
    input: Omit<KernelRouterEndpoint, "kind">,
    routingDomains: readonly RoutingDomain[] | undefined,
    routerName: string,
  ): OvnRouterEndpoint {
    const {
      transit: link,
      host,
      ipaddrs,
      ifaces,
      services,
      securityGroup,
      ...rest
    } = input;
    const transitDomain = this.collisionDomain(`transit-${routerName}`);
    const ovnSideAddrs = [link.left.ipv4, link.left.ipv6]
      .filter((a): a is IPv4 | IPv6 => a !== undefined);
    const kernelSideAddrs = [link.right.ipv4, link.right.ipv6]
      .filter((a): a is IPv4 | IPv6 => a !== undefined);

    // Kernel-side services (the `kernel.*` kinds of RouterEndpointService
    // — today `kernel.ipv4.masq`/`kernel.ipv6.masq`, later docker/
    // wireguard...) apply INSIDE the KernelRouter's netns, never to the
    // OVN twin's LRP — so they're split off here before `...rest`
    // reaches buildOvnRouterEndpoint, where resolveIpv6RaConfigs
    // (src/ir.ts) would throw on a kind it doesn't know. The OVN
    // endpoint keeps only the `ipv6.*` services.
    const kernelServices = services?.filter((s) =>
      s.kind.startsWith("kernel.")
    );
    const ovnServices = services?.filter((s) => !s.kind.startsWith("kernel."));
    // `.masq` is a SHORTCUT for a self-defined security group: it
    // expands (through net.securityGroup() — the SAME builder a config
    // author would call explicitly) to a group named `masq-<router>`
    // containing one MASQUERADE rule per declared masq family. An
    // EXPLICIT `securityGroup` on the endpoint wins outright — masq is
    // then IGNORED (the author takes responsibility for the group's own
    // content, so no rules are derived from the services) — and must
    // have been declared via net.securityGroup() in this same
    // defineNetwork call, same "register + fail fast" split as every
    // other cross-reference here.
    if (
      securityGroup !== undefined &&
      this.securityGroupsByName.get(securityGroup.name) !== securityGroup
    ) {
      throw new Error(
        `kernelRouterEndpoint: security group "${securityGroup.name}" was ` +
          `not declared via net.securityGroup() in this defineNetwork call`,
      );
    }
    const masqKinds = (kernelServices ?? []).filter((s) =>
      s.kind === "kernel.ipv4.masq" || s.kind === "kernel.ipv6.masq"
    );
    const securityGroupDef = securityGroup !== undefined
      ? securityGroup
      : masqKinds.length > 0
      ? this.securityGroup(`masq-${routerName}`, (group) => {
        for (const s of masqKinds) {
          group.masq(s.kind === "kernel.ipv4.masq" ? "ipv4" : "ipv6");
        }
      })
      : undefined;
    // `kernel.app.*` services resolve into app descriptors running INSIDE
    // the netns on the right side's real interface — independent of the
    // security-group shortcut above (an explicit `securityGroup` does
    // NOT suppress them; only the masq services it overrides).
    const appServices = (kernelServices ?? []).filter((s) =>
      s.kind.startsWith("kernel.app.")
    );
    const apps: KernelApp[] = [];
    for (const s of appServices) {
      if (s.kind === "kernel.app.dhcp-client") {
        apps.push({ kind: "dhcp-client", style: s.style });
      }
    }

    // `routes` also lands on the KernelRouter's own right side (the
    // real-world-facing one, see KernelRouterSide's own doc comment) —
    // it's a physical fact about that real device's own routing table,
    // not just the OVN logical router's — UNMODIFIED (ovnSideRoutes
    // above is a separate, via-rewritten copy for the OVN side only;
    // the kernel netns's own real gateway, e.g. the actual ISP address,
    // is a different fact that belongs on the kernel side of the
    // transit link, exactly where the unmodified copy keeps it).
    const ovnSideRoutes = input.routes?.map((route) => {
      // The OVN side of a transit link has exactly ONE reachable peer —
      // the paired KernelRouter — so every route declared on this
      // endpoint is rewritten to point at the kernel router's OWN
      // transit-facing address, regardless of whether the config author
      // declared a literal `via` (e.g. the real ISP gateway). A literal
      // `via` is a fact about the kernel netns's WAN side, not about
      // anything OVN can reach on the transit link (confirmed live,
      // 2026-08-21: keeping 192.168.132.1 made the OVN logical router
      // ARP for its WAN gateway out the transit veth).
      const via = route.dst.is_ipv4() ? link.right.ipv4 : link.right.ipv6;
      if (via === undefined) {
        throw new Error(
          `kernelRouterEndpoint: route to ${route.dst.to_string()} has ` +
            `no matching-family kernel-side address on the transit link ` +
            `to route via`,
        );
      }
      return { ...route, via };
    });

    // The transit link's own veth pair — the OVN-side endpoint on the
    // transit network (.first() side) carries it EXPLICITLY, so the
    // transit ovn.ls keeps its bridge binding (localnet port, gateway
    // chassis, ovn-bridge-mappings) from the endpoint's ifaces, the
    // same source every other domain uses — no ir.ts backfill (2026-08-
    // 18). LONG names here on purpose — the IFNAMSIZ-safe shortening is
    // the lower layers' job (src/ir.ts's kernelRouterSideToIR and
    // collisionDomainToIR, same as bridges' shortName).
    const transitVeth: InterfaceKind = {
      kind: "veth",
      ifaceName: `veth-krn-${routerName}`,
      peerName: `veth-ovn-${routerName}`,
    };

    this.kernelRouter(routerName, {
      host,
      left: {
        ipaddrs: kernelSideAddrs,
        // The transit link's own veth pair, constructed implicitly from
        // the enclosing ovnRouter()'s name — no config surface for it
        // (2026-08-18). `ifaceName` is the leg that lives in this
        // kernel router's netns (addresses/routes bind to it),
        // `peerName` the root-side leg the transit domain's bridge
        // attaches to.
        ifaces: [{ host, iface: transitVeth }],
      },
      right: {
        ipaddrs,
        routes: input.routes,
        ifaces,
        apps: apps.length > 0 ? apps : undefined,
        securityGroup: securityGroupDef,
      },
      transitDomain,
      transitPeerAddrs: ovnSideAddrs,
      routingDomains,
    });

    return this.buildOvnRouterEndpoint({
      ...rest,
      routes: ovnSideRoutes,
      l2Segment: transitDomain,
      services: ovnServices !== undefined && ovnServices.length > 0
        ? ovnServices
        : undefined,
      // Only the transit-side addresses live on the OVN port: `ipaddrs`
      // (the real-world-facing ones, e.g. 192.168.132.93/24) belong to
      // the kernel netns's OWN interface (KernelRouterSide.right above),
      // not to the OVN side of the transit link. Putting them here made
      // OVN treat the WAN subnet as directly connected on the transit
      // port and ARP for the WAN gateway out the transit veth
      // (confirmed live, 2026-08-21).
      ipaddrs: ovnSideAddrs,
      ifaces: [{ host, iface: transitVeth }],
    });
  }

  uplink(name: string, builder: UplinkBuilder): Uplink {
    if (this.uplinksByName.has(name)) {
      throw new Error(`uplink "${name}" declared more than once`);
    }

    // Passed as an allocator, not a single slot: most uplinks call this
    // once (their own front-door transfer link), but one with a
    // `backdoor` (see Backdoor, types.ts) calls it again for the
    // backdoor's own, separate slot — same sequential pool either way,
    // so nothing declared via this builder can ever collide on a slot.
    const allocSlot = (): number => {
      if (this.nextSlot > 4095) {
        throw new Error(
          "no transfer-link slots remaining (4096 max, see addressing.ts " +
            "transferNet()) — this network has more uplinks (and/or " +
            "backdoors) than the 10.99.0.0/16 transfer-link space can " +
            "address",
        );
      }
      return this.nextSlot++;
    };

    const spec = builder(allocSlot, name);

    const id = spec.addresses[0]?.id();
    if (id !== undefined && this.usedUplinkIds.has(id)) {
      const existing = [...this.uplinksByName.entries()].find(
        ([, u]) => u.addresses[0]?.id() === id,
      )?.[0];
      throw new Error(
        `uplink "${name}" reuses id ${id}, ` +
          `already used by uplink "${existing}"`,
      );
    }

    // Same "must already be declared via this builder" fail-fast as
    // segment()'s uplink check below — a backdoor's `via` has to be a
    // real, already-registered uplink, not an arbitrary object that
    // happens to match the Uplink shape (and, since it must be
    // declared BEFORE this one, its own emitUplinkTransfer/netns setup
    // is guaranteed to already exist by the time this uplink's backdoor
    // is emitted — see generate-ovn.ts, scriptForHost).
    if (
      spec.backdoor !== undefined &&
      !this.uplinksByName.has(spec.backdoor.via.name)
    ) {
      throw new Error(
        `uplink "${name}" has a backdoor via "${spec.backdoor.via.name}", ` +
          `which was not declared via net.uplink() before this uplink`,
      );
    }

    const uplink: Uplink = { name, ...spec };

    this.uplinksByName.set(name, uplink);
    if (id !== undefined) this.usedUplinkIds.add(id);
    return uplink;
  }

  segment(name: string, spec: SegmentSpec): Segment {
    if (this.segmentsByName.has(name)) {
      throw new Error(`segment "${name}" declared more than once`);
    }
    const id = spec.addresses[0]?.id();
    if (id !== undefined && this.usedSegmentIds.has(id)) {
      const existing = [...this.segmentsByName.entries()].find(
        ([, s]) => s.addresses[0]?.id() === id,
      )?.[0];
      throw new Error(
        `segment "${name}" reuses id ${id}, ` +
          `already used by segment "${existing}"`,
      );
    }

    // undefined means "no uplink yet, deliberately isolated" (see
    // Segment.uplink, types.ts) — not every segment resolves to
    // something, so this must be checked before the "resolve" in
    // probe below, which throws on undefined.
    const selector: UplinkSelector | undefined = spec.uplink === undefined
      ? undefined
      : "resolve" in spec.uplink
      ? spec.uplink
      : new FixedUplink(spec.uplink);

    // fail fast: if an uplink WAS given, it must have been declared via
    // this same builder, not an arbitrary object that happens to match
    // the Uplink shape.
    if (selector !== undefined) {
      const resolved = selector.resolve();
      if (!this.uplinksByName.has(resolved.name)) {
        throw new Error(
          `segment "${name}" references an uplink ("${resolved.name}") ` +
            `that was not declared via net.uplink() in this defineNetwork call`,
        );
      }
    }

    const segment: Segment = { name, ...spec, uplink: selector };

    this.segmentsByName.set(name, segment);
    if (id !== undefined) this.usedSegmentIds.add(id);
    return segment;
  }

  /** @internal used by defineNetwork to extract the final declarations */
  build(name: string): NetworkDefinition {
    return {
      name,
      allUplinks: [...this.uplinksByName.values()],
      allSegments: [...this.segmentsByName.values()],
      allHosts: [...this.hostsByName.values()],
      allCollisionDomains: [...this.collisionDomainsByName.values()],
      backbone: this.backboneDomain,
      allRouters: [...this.routersByName.values()],
      allKernelRouters: [...this.kernelRoutersByName.values()],
      allRoutingDomains: [...this.routingDomainsByName.values()],
      allSecurityGroups: [...this.securityGroupsByName.values()],
      ovnGlobal: this.ovnGlobalOptions,
    };
  }
}

export function defineNetwork(
  name: string,
  build: (net: NetworkBuilder) => void,
): NetworkDefinition {
  const builder = new NetworkBuilder();
  build(builder);
  return builder.build(name);
}

/** The per-declaration rule API handed to net.securityGroup()'s build
 * callback (define.ts) — the ONE place a rule's abstract shape gets
 * turned into the concrete SecurityGroupRule a SecurityGroup carries.
 * Today it only knows `masq(family)`; future kernel services (docker
 * containers, wireguard, ...) add their own methods here, and the
 * implementation-abstract IR/deployer side consumes them unchanged. */
export class SecurityGroupBuilder {
  private readonly rules: SecurityGroupRule[] = [];

  constructor(private readonly name: string) {}

  /** Add a masquerade rule for one address family — the expansion of the
   * `kernel.ipv4.masq`/`kernel.ipv6.masq` service shortcut (see
   * buildKernelRouterEndpoint). Chainable. */
  masq(family: "ipv4" | "ipv6"): this {
    this.rules.push({ family, kind: "masq" });
    return this;
  }

  /** Add an already-fully-shaped rule — for the day a service isn't a
   * simple masq. Chainable. */
  rule(rule: SecurityGroupRule): this {
    this.rules.push(rule);
    return this;
  }

  /** The resolved, immutable group — name + a snapshot of the rules
   * accumulated so far. */
  build(): SecurityGroup {
    return { name: this.name, rules: [...this.rules] };
  }
}
