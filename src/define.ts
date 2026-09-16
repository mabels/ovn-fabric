// src/define.ts — NetworkBuilder + defineNetwork(): a Vite-defineConfig
// / Jest-describe style builder. Lets config/topology.ts read as a
// declaration rather than a set of raw object literals, and validates as
// it goes (e.g. a router can only reference a collision domain that was
// already declared in the same defineNetwork call).
//
// The builder SHAPES (RouterBuilder, EndpointBuilder, the service/docker
// app builders and their normalizers) live in builders.ts; the resolved
// NetworkDefinition in network-definition.ts; the security-group rule API
// in security-group.ts. All three are re-exported here so this module
// stays the one import surface configs (and mod.ts) use.

import type {
  OvnRouterSpec,
  RouterBuilder,
  RouterBuildResult,
  RouterEndpointSpec,
  ServiceBuilder,
} from "./builders.ts";
import type { NetworkDefinition } from "./network-definition.ts";
import {
  buildKernelRouterEndpoint,
  buildOvnRouterEndpoint,
  buildTunnelRouterEndpoint,
  type RouterEndpointContext,
} from "./router-endpoints.ts";
import { SecurityGroupBuilder } from "./security-group.ts";
import type { IPv4, IPv6 } from "./ip.ts";
import {
  CollisionDomain,
  type Host,
  type HostAddress,
  type HostOs,
  type KernelRouter,
  type KernelRouterSide,
  localHost,
  type OvnClusterOptions,
  type OvnHostConfig,
  type OvnRouterEndpoint,
  type Router,
  type RoutingDomain,
  type RoutingDomainRoute,
  type SecurityGroup,
  type Service,
  sshHost,
} from "./types.ts";

// Re-export the pieces split out of this file, so configs and mod.ts keep
// importing them from the one place.
export * from "./builders.ts";
export type { NetworkDefinition } from "./network-definition.ts";
export { SecurityGroupBuilder } from "./security-group.ts";

/**
 * The builder context passed into defineNetwork's callback. Each method
 * both registers the declared thing and returns a handle to it, so later
 * calls in the same callback can reference earlier ones directly
 * (`net.defineOvnRouter("home", (router) => {...})`), the same way
 * Jest's `describe`/`it` or Vite's defineConfig read top-to-bottom.
 */
export class NetworkBuilder implements RouterEndpointContext {
  private readonly hostsByName = new Map<string, Host>();
  private centralHostName: string | undefined;
  private ovnGlobalOptions: OvnClusterOptions | undefined;
  private readonly collisionDomainsByName = new Map<string, CollisionDomain>();
  private backboneDomain: CollisionDomain | undefined;
  private readonly routersByName = new Map<string, Router>();
  private readonly kernelRoutersByName = new Map<string, KernelRouter>();
  private readonly routingDomainsByName = new Map<string, RoutingDomain>();
  private readonly securityGroupsByName = new Map<string, SecurityGroup>();
  // Endpoint name → owning router. Endpoint names are the merge key across
  // the TS/Python boundary (EndpointBase.name, types.ts) AND the real
  // Logical_Router_Port names the deployer creates, which OVN treats as
  // globally unique — so a name may occur at most once in the whole
  // network, not just once per router. The derived form can't collide (it
  // folds the router name into the fnv1a64, define.ts/deriveEndpointName),
  // so this only ever fires for explicit `name` overrides — which is
  // exactly the case a per-router Set missed.
  private readonly endpointOwnerByName = new Map<string, string>();

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
      if (this.centralHostName) {
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
    os?: HostOs,
    ovn?: OvnHostConfig,
  ): Host {
    return this.registerHost(sshHost(name, address, user, os, ovn));
  }

  /** Declare the generator's own host — no SSH needed. */
  localHost(name: string, os?: HostOs, ovn?: OvnHostConfig): Host {
    return this.registerHost(localHost(name, os, ovn));
  }

  /** Cluster-wide OVN settings (NB_Global) — see OvnClusterOptions,
   * types.ts. At most once per defineNetwork call, same "declared more
   * than once" fail-fast as every other builder method. */
  ovnGlobal(options: OvnClusterOptions): void {
    if (this.ovnGlobalOptions) {
      throw new Error("ovnGlobal() called more than once");
    }
    this.ovnGlobalOptions = options;
  }

  /** Declare a bare collision domain — an OVN logical switch, L2 only.
   * See CollisionDomain (types.ts). */
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
    if (this.backboneDomain) {
      throw new Error(
        `backbone collision domain "${name}" declared more than once — ` +
          `"${this.backboneDomain.name}" already is one`,
      );
    }
    const domain = this.collisionDomain(name);
    this.backboneDomain = domain;
    return domain;
  }

  // Shared by both endpoints of a router — the "must already be
  // declared via this builder" fail-fast every reference in this file
  // follows.
  private checkRouterEndpoint(
    routerName: string,
    endpoint: OvnRouterEndpoint,
  ): void {
    if (
      !this.collisionDomainsByName.has(endpoint.l2Segment.name)
    ) {
      throw new Error(
        `router "${routerName}" references collision domain ` +
          `"${endpoint.l2Segment.name}", which was not declared via ` +
          `net.collisionDomain()/net.backbone() in this defineNetwork call`,
      );
    }
    if (
      endpoint.gatewayChassis &&
      !this.hostsByName.has(endpoint.gatewayChassis.name)
    ) {
      throw new Error(
        `router "${routerName}" pins an endpoint to gateway chassis ` +
          `"${endpoint.gatewayChassis.name}", which was not declared via ` +
          `net.sshHost()/net.localHost() in this defineNetwork call`,
      );
    }
  }

  /** Claim one endpoint name network-wide (endpointOwnerByName above) —
   * the enforcement half of EndpointBase.name (types.ts). The derived form
   * never reaches a collision (router name is folded into the hash), so
   * this only fires for an explicit `name` override: either twice in one
   * router or once each in two routers. */
  private registerEndpointName(routerName: string, endpointName: string): void {
    const owner = this.endpointOwnerByName.get(endpointName);
    if (owner === undefined) {
      this.endpointOwnerByName.set(endpointName, routerName);
      return;
    }
    throw new Error(
      owner === routerName
        ? `router "${routerName}": two endpoints resolve to the same name ` +
          `"${endpointName}" — give one an explicit \`name\``
        : `endpoint name "${endpointName}" is already used by router ` +
          `"${owner}" — endpoint names are unique across the whole ` +
          `network; give one an explicit \`name\``,
    );
  }

  // gatewayChassis's natural default when a config author leaves it
  // unset: whichever host `ifaces` already names for this endpoint, IF
  // there's exactly one. A distributed router port with no
  // gateway-chassis pin and no bound VIF anywhere never gets scheduled
  // onto ANY chassis at all (confirmed live, 2026-08-10: `ovn-appctl -t
  // ovn-controller debug/dump-local-datapaths` on a real test chassis
  // listed nothing for a topology whose routers left gatewayChassis
  // unset, despite northd having compiled correct logical flows for
  // them). the legacy generator's older model never hit this because it
  // pinned every LRP unconditionally (its own comment: "a chassis
  // cannot be scheduled for ANY of them without this flag") — there was
  // only ever one possible chassis to pin to. Injected HERE, at
  // declaration time, not left for toIR()/the deployer to guess later
  // — same "resolve it once, at the boundary that has the real Host
  // objects" reasoning as macFromV4 (ir.ts). Ambiguous (0 or 2+ hosts
  // on ifaces) leaves gatewayChassis unset, same as an explicit
  // omission — this only fills in the unambiguous case.
  private deriveGatewayChassis(endpoint: OvnRouterEndpoint): OvnRouterEndpoint {
    if (endpoint.gatewayChassis) return endpoint;
    const hosts = new Set((endpoint.ifaces ?? []).map((hi) => hi.host));
    if (hosts.size !== 1) return endpoint;
    const [onlyHost] = hosts;
    if (onlyHost === undefined) return endpoint;
    return { ...endpoint, gatewayChassis: onlyHost };
  }

  /** Declare a named group of routers that should learn about each
   * other's routes — see RoutingDomain (types.ts). No `routes`
   * parameter here anymore: those live directly on whichever
   * RouterEndpoint is the real anchor (RouterEndpoint.routes) — this
   * only registers the membership tag, the same "register + fail fast
   * on duplicates, resolve later" split every other builder method
   * already follows.
   *
   * 2nd arg (2026-09-16): an explicit, non-empty `{ routes }` OVERRIDES the
   * destinations this domain would otherwise distribute; omit it (or pass
   * an empty array) to let them be CALCULATED from what the domain's
   * participants actually resolve — see RoutingDomain.routes (types.ts). */
  routingDomain(
    name: string,
    spec?: { readonly routes: readonly RoutingDomainRoute[] },
  ): RoutingDomain {
    if (this.routingDomainsByName.has(name)) {
      throw new Error(`routing domain "${name}" declared more than once`);
    }
    const domain: RoutingDomain = {
      name,
      ...(spec ? { routes: spec.routes } : {}),
    };
    this.routingDomainsByName.set(name, domain);
    return domain;
  }

  /** Declare a reusable WORKLOAD — an image + command + optional build,
   * network-free. Bind it to segments by ATTACHING it at endpoints
   * (`endpoint.attachTo(svc, {...})`): each attachment is one NIC on that
   * endpoint's segment; attaching one service at N endpoints is one
   * container with N NICs (the k8s pod model, 2026-09-08). See Service
   * (types.ts). */
  service(name: string, build: (svc: ServiceBuilder) => void): Service {
    const svc: ServiceBuilder = { image: "" };
    build(svc);
    if (svc.image === "") {
      throw new Error(`service "${name}": builder must set an image`);
    }
    const cmd = typeof svc.cmd === "string"
      ? svc.cmd.trim().split(/\s+/).filter(Boolean)
      : svc.cmd;
    return {
      name,
      image: svc.image,
      ...(cmd && cmd.length > 0 ? { cmd } : {}),
      ...(svc.build ? { build: svc.build } : {}),
      // Filled in by the endpoint builders when this service is attached.
      endpointRefs: [],
    };
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

  /** True iff `group` is the exact object net.securityGroup() returned for
   * its name in this call — the fail-fast check a kernelRouterEndpoint's
   * explicit securityGroup must pass (RouterEndpointContext). */
  securityGroupDeclared(group: SecurityGroup): boolean {
    return this.securityGroupsByName.get(group.name) === group;
  }

  /** Declare a router connecting exactly two collision domains — see
   * Router/RouterEndpoint (types.ts) for why exactly two, not N. Named
   * defineOvnRouter(), not router(): kernelRouterEndpoint()/
   * ovnRouterEndpoint() (only reachable through the RouterBuilder this
   * passes into `build`, not as their own NetworkBuilder methods
   * anymore — 2026-08-12) already anticipate a future net.kernelRouter()
   * sibling — see that session's design discussion on why "OVN router"
   * needed a name of its own even before the kernel-side counterpart
   * existed.
   *
   * The `build` callback returns the router's `routingDomains` (REQUIRED
   * on the return type) — read AFTER the callback runs, so the endpoint
   * methods no longer depend on call-time ordering. The router-level
   * membership is stamped onto any KernelRouter the endpoints created,
   * when that endpoint didn't declare its own per-endpoint
   * routingDomains (2026-09-08, superseding the pre-2026-09-08 mutable
   * `router.routingDomains = [...]` attribute).
   * Declare-before-use discipline every other builder method in this file
   * requires (e.g. a collisionDomain must exist before a router references
   * it). */
  /** Resolve a kind-tagged endpoint spec (the defineOvnRouter() object
   * form's endpoints[]) into a stored OvnRouterEndpoint — dispatching to
   * the same builders the router.endpoint methods use (2026-09-08). */
  private resolveEndpointSpec(
    spec: RouterEndpointSpec,
    routingDomains: readonly RoutingDomain[],
    routerName: string,
    subRouters: Router[],
  ): OvnRouterEndpoint {
    if (spec.kind === "ovn") {
      const { kind: _kind, ...rest } = spec;
      return buildOvnRouterEndpoint(rest, { routerName, role: "ovn" });
    }
    if (spec.kind === "kernel") {
      const { kind: _kind, ...rest } = spec;
      return buildKernelRouterEndpoint(
        this,
        rest,
        routingDomains,
        routerName,
      );
    }
    const { kind: _kind, ...rest } = spec;
    return buildTunnelRouterEndpoint(this, rest, routerName, subRouters);
  }

  defineOvnRouter(
    name: string,
    build: OvnRouterSpec | ((router: RouterBuilder) => RouterBuildResult),
  ): Router {
    if (this.routersByName.has(name)) {
      throw new Error(`router "${name}" declared more than once`);
    }
    // Sub-routers a tunnel endpoint creates (its `<name>-upstream` peer)
    // are attached to THIS router, so the author lists only this top-level
    // router and the sub-router is flattened at IR time (2026-09-08).
    const subRouters: Router[] = [];
    const router: RouterBuilder = {
      ovnRouterEndpoint: (input) =>
        buildOvnRouterEndpoint(input, { routerName: name, role: "ovn" }),
      // Router-level routingDomains is only known from the callback's
      // RETURN value (read after the callback runs) — so it can't be
      // passed in at endpoint-call time anymore. Each endpoint may carry
      // its own per-endpoint routingDomains; the router-level default is
      // stamped onto the KernelRouter by defineOvnRouter() below (2026-09-08).
      kernelRouterEndpoint: (input) =>
        buildKernelRouterEndpoint(this, input, undefined, name),
      tunnelRouterEndpoint: (input) =>
        buildTunnelRouterEndpoint(this, input, name, subRouters),
    };
    // The object form carries routingDomains up-front, so its kernel/tunnel
    // specs are resolved with it directly (no deferred stamp needed); the
    // builder form returns routingDomains alongside its endpoints.
    let routingDomains: readonly RoutingDomain[];
    let resolved: readonly OvnRouterEndpoint[];
    if (typeof build === "function") {
      const result = build(router);
      routingDomains = result.routingDomains;
      resolved = result.endpoints;
    } else {
      routingDomains = build.routingDomains;
      resolved = build.endpoints.map((spec) =>
        this.resolveEndpointSpec(spec, build.routingDomains, name, subRouters)
      );
    }

    // A router exists to join domains: one endpoint joins nothing, so at
    // least two are required (2026-09-16). No fixed left/right — an
    // endpoint is addressed by its own `name`.
    if (resolved.length < 2) {
      throw new Error(
        `router "${name}": needs at least 2 endpoints, got ` +
          `${resolved.length} — return them from defineOvnRouter()'s ` +
          `callback as \`endpoints: [...]\`.`,
      );
    }
    for (const endpoint of resolved) {
      this.checkRouterEndpoint(name, endpoint);
    }
    // Endpoint names are the merge key (EndpointBase.name, types.ts) and
    // the real OVN port names — unique across the WHOLE network, not just
    // within this router (registerEndpointName, above).
    for (const endpoint of resolved) {
      this.registerEndpointName(name, endpoint.name);
    }
    for (const domain of routingDomains) {
      if (this.routingDomainsByName.get(domain.name) !== domain) {
        throw new Error(
          `router "${name}" references routing domain "${domain.name}", ` +
            `which was not declared via net.routingDomain() in this ` +
            `defineNetwork call`,
        );
      }
    }
    // Stamp the router-level membership onto the KernelRouter this router
    // created, when its endpoint didn't carry its own per-endpoint
    // routingDomains (2026-09-08).
    const kernelRouter = this.kernelRoutersByName.get(name);
    if (
      kernelRouter && !kernelRouter.routingDomains
    ) {
      this.kernelRoutersByName.set(name, {
        ...kernelRouter,
        routingDomains,
      });
    }
    const built: Router = {
      name,
      // Sorted by name (see Router.endpoints, types.ts) — declaration
      // order must not leak into the IR/deployer output.
      endpoints: resolved.map((endpoint) => this.deriveGatewayChassis(endpoint))
        .sort(byName),
      routingDomains,
      subRouters,
    };
    this.routersByName.set(name, built);
    return built;
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
      readonly transitDomain: CollisionDomain;
      readonly transitPeerAddrs: readonly (IPv4 | IPv6)[];
      readonly upstreamPeerAddrs: readonly (IPv4 | IPv6)[];
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

  /** @internal used by defineNetwork to extract the final declarations. The
   * returned `routers` (and optional `hosts`) are the SOURCE OF TRUTH —
   * every router/host registered during the callback must be listed in
   * `decl`, so an author can't forget to declare one (2026-09-08). The
   * derived sets (collision domains, kernel routers, routing domains,
   * security groups) come from the Maps — they're only ever created by the
   * returned routers' registration, so they stay consistent. */
  build(
    name: string,
    decl: {
      readonly hosts?: readonly Host[];
      readonly routers: readonly Router[];
    },
  ): NetworkDefinition {
    // The author lists only TOP-level routers; their subRouters (tunnel
    // upstream peers) are flattened here. Every registered router must be
    // reachable in that flattened tree (2026-09-08).
    const allRouters = flattenRouters(decl.routers);
    for (const router of this.routersByName.values()) {
      if (!allRouters.includes(router)) {
        throw new Error(
          `router "${router.name}" was declared but is not reachable in ` +
            `defineNetwork()'s returned routers[] (or one of their sub-routers)`,
        );
      }
    }
    if (decl.hosts) {
      for (const host of this.hostsByName.values()) {
        if (!decl.hosts.includes(host)) {
          throw new Error(
            `host "${host.name}" was declared but not listed in ` +
              `defineNetwork()'s returned hosts[]`,
          );
        }
      }
    }
    return {
      name,
      allHosts: [...(decl.hosts ?? this.hostsByName.values())].sort(byName),
      allCollisionDomains: [...this.collisionDomainsByName.values()].sort(
        byName,
      ),
      ...(this.backboneDomain ? { backbone: this.backboneDomain } : {}),
      allRouters,
      allKernelRouters: [...this.kernelRoutersByName.values()].sort(byName),
      allRoutingDomains: [...this.routingDomainsByName.values()].sort(byName),
      allSecurityGroups: [...this.securityGroupsByName.values()].sort(byName),
      ...(this.ovnGlobalOptions ? { ovnGlobal: this.ovnGlobalOptions } : {}),
    };
  }
}

export function defineNetwork(
  name: string,
  build: (net: NetworkBuilder) => {
    readonly hosts?: readonly Host[];
    readonly routers: readonly Router[];
  },
): NetworkDefinition {
  const builder = new NetworkBuilder();
  const decl = build(builder);
  return builder.build(name, decl);
}

// Lexical, UTF-16 code-unit order — deliberately NOT localeCompare(),
// which is locale/environment dependent and would make the IR (and so
// the deployer output) differ between machines for the same config. The
// whole point of sorting here is diff-STABLE output.
function byName(
  a: { readonly name: string },
  b: { readonly name: string },
): number {
  return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
}

// Flatten a router and its subRouters (recursively) into the full router
// set — a config author lists only top-level routers; a tunnel's upstream
// peer rides on the tunnel router's subRouters (2026-09-08). Sorted by
// name so the flattened order is independent of declaration order.
function flattenRouters(routers: readonly Router[]): Router[] {
  const out: Router[] = [];
  for (const router of routers) {
    out.push(router);
    out.push(...flattenRouters(router.subRouters));
  }
  return out.sort(byName);
}
