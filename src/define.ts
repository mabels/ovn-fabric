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
  EndpointBuilder,
  OvnRouterSpec,
  RouterBuilder,
  RouterBuildResult,
  RouterEndpointSpec,
  ServiceBuilder,
} from "./builders.ts";
import type { NetworkDefinition } from "./network-definition.ts";
import {
  defaultPlatformServiceSetting,
  PLATFORM_SERVICE_IDS,
  platformSvc,
  resolvePlatformService,
} from "./platform-service.ts";
import {
  buildKernelRouterEndpoint,
  buildOvnRouterEndpoint,
  buildTunnelRouterEndpoint,
  type RouterEndpointContext,
} from "./router-endpoints.ts";
import { SecurityGroupBuilder } from "./security-group.ts";
import { addHostOffset } from "./ip.ts";
import type { IPv4, IPv6 } from "./ip.ts";
import {
  type CertIssuer,
  type CertIssuerSpec,
  CollisionDomain,
  type DnsProvider,
  type DnsProviderSpec,
  type Host,
  type HostAddress,
  type HostMonitoring,
  type HostOs,
  type KernelRouter,
  type KernelRouterSide,
  localHost,
  type OvnClusterOptions,
  type OvnHostConfig,
  type OvnRouterEndpoint,
  type OvnRouterEndpointSpec,
  type OvnRouterOptions,
  type Platform,
  type PlatformEgressSpec,
  type PlatformServiceId,
  type PlatformServiceOptions,
  type PlatformServiceSetting,
  type PlatformServiceSpec,
  type PlatformSpec,
  type Publish,
  type PublishSpec,
  resolveOvnRouterOptions,
  type Resolver,
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

/** k8s-style default: the leg's `.1` is the gateway, so an
 * auto-allocated service address starts at host +10 and counts up. */
const DEFAULT_SERVICE_HOST_OFFSET = 10;

/** The appliance's own service platform — the IMPLEMENTATION of the
 * Platform interface (types.ts).
 *
 * The control-plane leg is built HERE from the `controlPlane` parameters;
 * the OTHER legs are collected via addRouterEndpoint(). The router itself
 * is defined by initialize(), which the BUILDER runs once, AFTER the
 * defineNetwork() callback has returned — deliberately NOT lazily, so that
 * a topology using platform.ingressService/platform.publish never depends
 * on WHICH of those ran first.
 *
 * Step 1 (2026-09-16): the router (control-plane leg + added legs) is real;
 * services on it (addService), the zone → provider/issuer resolution and
 * the ingress template come next. */
class PlatformImpl implements Platform {
  readonly name: string;
  readonly spec: PlatformSpec;
  readonly #netRef: NetworkBuilder;
  readonly #ingressService: Service;
  readonly #egress: PlatformEgressSpec[];
  readonly #services: PlatformServiceSpec[];
  readonly #platformServices: {
    [K in PlatformServiceId]?: PlatformServiceSetting<K>;
  };
  #initialized = false;

  constructor(netRef: NetworkBuilder, name: string, spec: PlatformSpec) {
    this.#netRef = netRef;
    this.name = name;
    this.spec = spec;
    // Seeded from the spec's `services: []` / `egress: []`, then extended
    // by addService() / addEgress().
    this.#services = [...(spec.services ?? [])];
    this.#egress = [...(spec.egress ?? [])];
    this.#platformServices = { ...(spec.platformServices ?? {}) };
    // The service description the topology attaches to PLACE the ingress;
    // a placeholder until the platform's ingress template exists.
    this.#ingressService = {
      name: `${name}-ingress`,
      image: "PLACEHOLDER",
      endpointRefs: [],
    };
  }

  get ingressService(): Service {
    return this.#ingressService;
  }

  publish(publishSpec: PublishSpec): Publish {
    return { ...publishSpec, platform: this };
  }

  /** Add an EGRESS leg to the platform's own router — the CONTROL-PLANE leg
   * is built from controlPlane params. Chainable. */
  addEgress(input: PlatformEgressSpec): Platform {
    this.#egress.push(input);
    return this;
  }

  /** Add a SERVICE (an admin NIC) to the CONTROL-PLANE leg — a plain spec;
   * the attachTo() call happens in initialize(), on this leg's own endpoint
   * builder. Chainable. */
  addService(spec: PlatformServiceSpec): Platform {
    this.#services.push(spec);
    return this;
  }

  /** Configure a BUILT-IN platform service (id → options, over its
   * defaults). Chainable. */
  cfgPlatformSvc<K extends PlatformServiceId>(
    id: K,
    options: PlatformServiceOptions[K],
  ): Platform {
    this.#platformServices[id] = options;
    return this;
  }

  /** DROP a built-in platform service from the active set (same as setting
   * it to platformSvc.IGNORE in the spec). Chainable. */
  delPlatformSvc(id: PlatformServiceId): Platform {
    this.#platformServices[id] = platformSvc.IGNORE;
    return this;
  }

  /** ONE-TIME, run by NetworkBuilder.build() after the topology callback
   * returned. Builds the platform's router from the control-plane leg
   * (controlPlane params) + the legs added via addRouterEndpoint. */
  initialize(): void {
    if (this.#initialized) return;
    this.#initialized = true;
    const cp = this.spec.controlPlane;
    // No wiring declared → the platform is just a tag (segment/domain/hosts).
    if (!cp.ipaddrs) return;
    const ipaddrs = cp.ipaddrs;
    // The BUILT-IN platform services that are in the active set, in REGISTRY
    // order (an omitted id takes its default; IGNORE / delPlatformSvc drops
    // it). Resolved BEFORE the leg is built, so they attach through the very
    // same path as the author's own admin NICs.
    const builtIns: PlatformServiceSpec[] = [];
    for (const id of PLATFORM_SERVICE_IDS) {
      const setting = this.#platformServices[id] ??
        defaultPlatformServiceSetting(id);
      if (setting === platformSvc.IGNORE) continue;
      const resolved = resolvePlatformService(id, setting);
      if (resolved) builtIns.push(resolved);
    }
    const attached = [...this.#services, ...builtIns];
    // k8s-style ALLOCATION for any service that declares no address: the
    // control-plane network's host +10, +11, … (its own .1 is the gateway),
    // skipping addresses anything already claims.
    const used = new Set<string>(ipaddrs.map((a) => a.to_string()));
    for (const spec of attached) {
      for (const a of spec.ipaddrs ?? []) used.add(a.to_string());
    }
    const nextOffset = new Map<string, number>();
    const allocateAddrs = (): (IPv4 | IPv6)[] =>
      ipaddrs.map((legAddr) => {
        const family = legAddr.is_ipv4() ? "v4" : "v6";
        let offset = nextOffset.get(family) ?? DEFAULT_SERVICE_HOST_OFFSET;
        for (;;) {
          const candidate = addHostOffset(legAddr, offset);
          offset += 1;
          if (!used.has(candidate.to_string())) {
            nextOffset.set(family, offset);
            used.add(candidate.to_string());
            return candidate;
          }
        }
      });
    // Captured while attaching: the platform CoreDNS's control-plane
    // address, which becomes the DEFAULT global resolver when the topology
    // doesn't set controlPlane.resolver (2026-09-19).
    const corednsNameservers: (IPv4 | IPv6)[] = [];
    const controlPlaneLeg = (
      ep: EndpointBuilder,
    ): Omit<OvnRouterEndpointSpec, "kind"> => ({
      l2Segment: cp.collisionDomain,
      ipaddrs,
      ...(cp.ifaces ? { ifaces: cp.ifaces } : {}),
      // Author admin NICs + built-ins, one attachment path. The platform
      // does the attachTo() itself and injects itself into each publish.
      ...(attached.length > 0
        ? {
          services: attached.map((spec) => {
            const addrs = spec.ipaddrs ?? allocateAddrs();
            if (spec.service.name === "platform.dns") {
              for (const a of addrs) {
                if (a.is_ipv4()) corednsNameservers.push(a);
              }
            }
            return ep.attachTo(spec.service, {
              ipaddrs: addrs,
              ...(spec.routes ? { routes: spec.routes } : {}),
              ...(spec.publish
                ? { publish: [this.publish(spec.publish)] }
                : {}),
            });
          }),
        }
        : {}),
    });
    // The router NAME is inferred from the platform name. It lands in the
    // network's router set because the platform defines it THROUGH the
    // network reference it was created with — nothing to report.
    this.#netRef.defineOvnRouter(`router-${this.name}`, (router) => ({
      routingDomains: cp.routingDomain ? [cp.routingDomain] : [],
      endpoints: [
        router.ovnRouterEndpoint(controlPlaneLeg),
        ...this.#egress.map((input) => router.ovnRouterEndpoint(input)),
      ],
    }));
    // The GLOBAL resolver: controlPlane.resolver wins; otherwise the
    // platform CoreDNS address (if the built-in is in the active set).
    const resolver: Resolver | undefined = cp.resolver ??
      (corednsNameservers.length > 0
        ? { nameservers: corednsNameservers }
        : undefined);
    this.#netRef.registerPlatformControlPlane(cp.collisionDomain, resolver);
  }
}

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
  private readonly dnsProvidersByName = new Map<string, DnsProvider>();
  private readonly platforms: PlatformImpl[] = [];
  // Set by createPlatform (see registerPlatformControlPlane): the platform's
  // control-plane leg + its resolved global resolver, so a workload
  // attachment there with no resolver of its own inherits it (2026-09-19).
  private platformControlPlane:
    | { collisionDomain: CollisionDomain; resolver?: Resolver }
    | undefined;
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
    monitoring?: HostMonitoring,
  ): Host {
    return this.registerHost(sshHost(name, address, user, os, ovn, monitoring));
  }

  /** Declare the generator's own host — no SSH needed. */
  localHost(
    name: string,
    os?: HostOs,
    ovn?: OvnHostConfig,
    monitoring?: HostMonitoring,
  ): Host {
    return this.registerHost(localHost(name, os, ovn, monitoring));
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
    spec?: {
      readonly routes?: readonly RoutingDomainRoute[];
      readonly metric?: number;
    },
  ): RoutingDomain {
    if (this.routingDomainsByName.has(name)) {
      throw new Error(`routing domain "${name}" declared more than once`);
    }
    const domain: RoutingDomain = {
      name,
      ...(spec?.routes ? { routes: spec.routes } : {}),
      ...(spec?.metric !== undefined ? { metric: spec.metric } : {}),
    };
    this.routingDomainsByName.set(name, domain);
    return domain;
  }

  /** An EXTERNAL DNS provider (Cloudflare, …) the platform may write
   * records through — a plain OBJECT referenced by PlatformZone entries,
   * same "register + fail fast on duplicates, resolve later" split as the
   * other builders. NOTE: the platform's OWN authoritative DNS is not a
   * provider; a zone without one is served by the platform itself. */
  dnsProvider(name: string, spec: DnsProviderSpec): DnsProvider {
    if (this.dnsProvidersByName.has(name)) {
      throw new Error(`dns provider "${name}" declared more than once`);
    }
    const provider: DnsProvider = { name, spec };
    this.dnsProvidersByName.set(name, provider);
    return provider;
  }

  /** Certificate issuer — NOT IMPLEMENTED YET (2026-09-16). Emits an error
   * and returns a PLACEHOLDER instead of throwing: an unimplemented issuer
   * must not abort loading a topology (nothing consumes the issuer yet —
   * zones only reference it). */
  certIssuer(name: string, spec: CertIssuerSpec): CertIssuer {
    console.error(
      `net.certIssuer(): not implemented yet — using a placeholder for "${name}"`,
    );
    return { name, spec };
  }

  /** The appliance's own service platform — STEP 1 (2026-09-16): the
   * one-time initialize() is real (Platform.ingressService /
   * Platform.publish trigger it; it currently just runs the spec's
   * `initialize` callback, where the platform's own router gets DEFINED),
   * everything above that (zones → provider/issuer resolution, ingress
   * config, IR emission) is still to come. */
  createPlatform(name: string, spec: PlatformSpec): Platform {
    const platform = new PlatformImpl(this, name, spec);
    this.platforms.push(platform);
    return platform;
  }

  /** @internal — createPlatform registers the platform's control-plane leg
   * here during initialize(), so the global resolver reaches workload
   * attachments (2026-09-19). */
  registerPlatformControlPlane(
    collisionDomain: CollisionDomain,
    resolver?: Resolver,
  ): void {
    this.platformControlPlane = { collisionDomain, resolver };
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
      ...(svc.files && svc.files.length > 0 ? { files: svc.files } : {}),
      ...(svc.env && Object.keys(svc.env).length > 0 ? { env: svc.env } : {}),
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
    // Both forms may carry the router's own OVN `options`; whatever is
    // unset (or the whole object) is defaulted, and a bad value throws
    // here, at declaration time.
    let options: Required<OvnRouterOptions>;
    if (typeof build === "function") {
      const result = build(router);
      routingDomains = result.routingDomains;
      resolved = result.endpoints;
      options = resolveOvnRouterOptions(name, result.options);
    } else {
      routingDomains = build.routingDomains;
      options = resolveOvnRouterOptions(name, build.options);
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
      options,
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
    // PLATFORMS initialize HERE — once, after the topology callback has
    // returned. Each holds the network reference it was created with
    // (new PlatformImpl(netRef)) and defines its router THROUGH it, so that
    // router is registered like any other — the network simply KNOWS all its
    // routers, and nothing has to be listed or collected on the side.
    for (const platform of this.platforms) platform.initialize();
    const allRouters = [...this.routersByName.values()].sort(byName);
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
      ...(this.platformControlPlane
        ? { platformControlPlane: this.platformControlPlane }
        : {}),
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
