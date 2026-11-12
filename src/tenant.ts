// src/tenant.ts — tenants, wans and the internet core (ADR 0009).
//
// A layer over the network builder: each part is made of ordinary routers,
// collision domains and routing domains, named `<scope>-<code>[-<name>]`.
// The IR and the deployer see nothing new.
//
//   net.internet({ core })           internet-core
//   net.wan("avm", (wan) => …)       internet-wan-avm (+ internet-svc-avm-core)
//   net.tenant("home", (t) => …)     home-core, home-nat, home-rt-<n>,
//                                    home-cable-<n>, home-vpn-<n>, home-nat-<n>,
//                                    home-svc-<n>-core / -nat
//
// A tenant is one routing domain: its routers learn each other's cables
// over its core, and the default route of its NAT gateway (or of a VPN
// gateway marked `default`). No route leaves it: its NAT gateway
// masquerades onto internet-core, where a wan knows no tenant network.

import type { RouterBuilder } from "./builders.ts";
import { addHostOffset, IPv4, IPv6 } from "./ip.ts";
import { macFromAddrsOrName, type TransitNetwork } from "./addressing.ts";
import type {
  CollisionDomain,
  Host,
  InterfaceKind,
  OvnRouterEndpoint,
  OvnRouterOptions,
  Router,
  RouterEndpointRoute,
  RouterEndpointService,
  RoutingDomain,
} from "./types.ts";

/** The networks a core's addresses are allocated from (host 1, 2, …). */
export interface CorePrefixes {
  readonly ipv4?: IPv4;
  readonly ipv6?: IPv6;
}

/** The builder parts a tenant, wan or the internet core is made of. */
export interface ScopeContext {
  collisionDomain(name: string): CollisionDomain;
  routingDomain(name: string): RoutingDomain;
  defineOvnRouter(
    name: string,
    build: (router: RouterBuilder) => {
      readonly routingDomains: readonly RoutingDomain[];
      readonly endpoints: readonly OvnRouterEndpoint[];
      readonly options?: OvnRouterOptions;
    },
  ): Router;
}

/** Next addresses of a core, one per family, with the core's prefix length. */
export function allocator(prefixes: CorePrefixes): () => (IPv4 | IPv6)[] {
  let next = 1;
  return () => {
    const offset = next++;
    return [prefixes.ipv4, prefixes.ipv6]
      .filter((p): p is IPv4 | IPv6 => p !== undefined)
      .map((p) => addHostOffset(p, offset));
  };
}

/** The wan a tenant or gateway leaves through: the first active one. */
function firstActive(
  wans: readonly Wan[] | undefined,
  who: string,
): Wan | undefined {
  if (wans === undefined || wans.length === 0) return undefined;
  const wan = wans.find((w) => w.active);
  if (wan === undefined) {
    throw new Error(
      `${who}: none of its wans is active (${
        wans.map((w) => w.name).join(", ")
      })`,
    );
  }
  return wan;
}

/** Masquerade for each family `addrs` has (an IPv4-only core: IPv4 only),
 * of `sources` when given, else of the router's other legs. */
function masqFor(
  addrs: readonly (IPv4 | IPv6)[],
  sources?: readonly (IPv4 | IPv6)[],
): RouterEndpointService[] {
  const out: RouterEndpointService[] = [];
  if (addrs.some((a) => a.is_ipv4())) {
    out.push({
      kind: "ovn.ipv4.masq",
      ...(sources
        ? { sources: sources.filter((a): a is IPv4 => a.is_ipv4()) }
        : {}),
    });
  }
  if (addrs.some((a) => !a.is_ipv4())) {
    out.push({
      kind: "ovn.ipv6.masq",
      ...(sources
        ? { sources: sources.filter((a): a is IPv6 => !a.is_ipv4()) }
        : {}),
    });
  }
  return out;
}

const bare = (a: IPv4 | IPv6): IPv4 | IPv6 =>
  a.is_ipv4() ? IPv4.parse(a.to_s()) : IPv6.parse(a.to_s());

// ── the internet core ─────────────────────────────────────────────────────

export class Internet {
  readonly core: CollisionDomain;
  readonly allocate: () => (IPv4 | IPv6)[];

  constructor(ctx: ScopeContext, spec: { readonly core: CorePrefixes }) {
    this.core = ctx.collisionDomain("internet-core");
    this.allocate = allocator(spec.core);
  }
}

// ── wans ──────────────────────────────────────────────────────────────────

export interface WanSpec {
  readonly host: Host;
  readonly iface: InterfaceKind;
  /** `static` (default): a kernel netns with `ipaddrs` and `routes` on the
   * interface, masquerading. `dhcp`: not modelled yet — an OVN router on
   * the interface's cable, as today. */
  readonly addressing?: "static" | "dhcp";
  readonly ipaddrs?: readonly (IPv4 | IPv6)[];
  readonly routes?: readonly RouterEndpointRoute[];
  /** The cable between its OVN side and its netns (`static` only). */
  readonly svc?: TransitNetwork;
  /** Whether tenants may leave through it (default true). A tenant uses
   * the first active wan of its list; an inactive one is still built. */
  readonly active?: boolean;
}

/** What a wan's callback gets: nothing to declare yet. */
export type WanBuilder = Record<string, never>;

export class Wan {
  readonly name: string;
  readonly host: Host;
  readonly router: Router;
  /** Its address on internet-core, per family: a NAT gateway's next hop. */
  readonly coreAddrs: readonly (IPv4 | IPv6)[];
  readonly active: boolean;

  constructor(
    ctx: ScopeContext,
    internet: Internet,
    name: string,
    build: (wan: WanBuilder) => WanSpec,
  ) {
    const spec = build({});
    this.name = name;
    this.host = spec.host;
    this.active = spec.active ?? true;
    this.coreAddrs = internet.allocate();
    const routerName = `internet-wan-${name}`;
    const domain = ctx.routingDomain(routerName);
    const coreLeg = { l2Segment: internet.core, ipaddrs: this.coreAddrs };
    if (spec.addressing === "dhcp") {
      this.router = ctx.defineOvnRouter(routerName, (router) => ({
        routingDomains: [domain],
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: ctx.collisionDomain(`internet-cable-${name}`),
            ipaddrs: [],
            // No address to fold a MAC from: one from the name.
            mac: macFromAddrsOrName([], `internet-cable-${name}`),
            ifaces: [{ host: spec.host, iface: spec.iface }],
          }),
          router.ovnRouterEndpoint(coreLeg),
        ],
      }));
      return;
    }
    if (spec.svc === undefined) {
      throw new Error(`wan "${name}": a static wan needs its svc cable`);
    }
    const svc = spec.svc;
    this.router = ctx.defineOvnRouter(routerName, (router) => ({
      routingDomains: [domain],
      endpoints: [
        router.kernelRouterEndpoint({
          host: spec.host,
          transit: svc,
          transitName: `internet-svc-${name}-core`,
          ipaddrs: [...(spec.ipaddrs ?? [])],
          ifaces: [{ host: spec.host, iface: spec.iface }],
          routes: [...(spec.routes ?? [])],
          services: [{ kind: "kernel.ipv4.masq" }, {
            kind: "kernel.ipv6.masq",
          }],
        }),
        router.ovnRouterEndpoint(coreLeg),
      ],
    }));
  }

  /** The default routes of a NAT gateway leaving through this wan. */
  defaultRoutes(): RouterEndpointRoute[] {
    return this.coreAddrs.map((a) => ({
      dst: a.is_ipv4() ? IPv4.parse("0.0.0.0/0") : IPv6.parse("::/0"),
      via: bare(a),
    }));
  }
}

// ── tenants ───────────────────────────────────────────────────────────────

export interface VpnGatewaySpec {
  readonly kind: "wireguard" | "zerotier" | "tailscale";
  readonly host: Host;
  /** The two cables: to the tenant's core, to its own NAT gateway. */
  readonly svc: { readonly core: TransitNetwork; readonly nat: TransitNetwork };
  /** Where its encrypted traffic leaves (its own NAT gateway). */
  readonly wans: readonly Wan[];
  /** The tenant's default route goes into the tunnel. */
  readonly default?: boolean;
  /** Networks reached through the tunnel (besides a default). */
  readonly routes?: readonly RouterEndpointRoute[];
  readonly masq?: readonly ("ipv4" | "ipv6")[];
  // wireguard
  readonly ifaceName?: string;
  readonly config?: unknown;
  // zerotier
  readonly networkId?: string;
  readonly identity?: { readonly public: string; readonly secret: string };
  // tailscale
  readonly loginServer?: string;
  readonly authKey?: string;
  readonly upArgs?: readonly string[];
}

/** What a tenant's callback returns. */
export interface TenantSpec {
  readonly core: CorePrefixes;
  /** Ordered: the first is used (switching comes later). None: no NAT
   * gateway — the tenant leaves only through a `default` VPN gateway. */
  readonly wans?: readonly Wan[];
  readonly routers: readonly TenantRouter[];
}

/** A router declared in a tenant; built once the tenant's core is known. */
export interface TenantRouter {
  readonly name: string;
  readonly build: (
    tenant: TenantBuild,
  ) => Router;
}

interface TenantBuild {
  readonly domain: RoutingDomain;
  readonly core: CollisionDomain;
  readonly allocate: () => (IPv4 | IPv6)[];
}

export interface TenantBuilder {
  /** A cable of the tenant: `<tenant>-cable-<name>`. */
  collisionDomain(name: string): CollisionDomain;
  /** A router of the tenant, `<tenant>-rt-<name>`; its leg on the core is
   * added. */
  defineOvnRouter(
    name: string,
    build: (router: RouterBuilder) => {
      readonly endpoints: readonly OvnRouterEndpoint[];
      readonly options?: OvnRouterOptions;
    },
  ): TenantRouter;
  /** A tunnel of the tenant, `<tenant>-vpn-<name>`, with its own NAT
   * gateway `<tenant>-nat-<name>`. */
  vpnGateway(name: string, spec: VpnGatewaySpec): TenantRouter;
}

export class Tenant {
  readonly name: string;
  readonly collisionDomains: Record<string, CollisionDomain> = {};
  readonly routers: Router[] = [];

  constructor(
    ctx: ScopeContext,
    internet: Internet | undefined,
    name: string,
    build: (tenant: TenantBuilder) => TenantSpec,
  ) {
    this.name = name;
    const builder: TenantBuilder = {
      collisionDomain: (cable) => {
        const domain = ctx.collisionDomain(`${name}-cable-${cable}`);
        this.collisionDomains[cable] = domain;
        return domain;
      },
      defineOvnRouter: (rt, buildRouter) => ({
        name: rt,
        build: (t) =>
          ctx.defineOvnRouter(`${name}-rt-${rt}`, (router) => {
            const result = buildRouter(router);
            return {
              routingDomains: [t.domain],
              endpoints: [
                ...result.endpoints,
                router.ovnRouterEndpoint({
                  l2Segment: t.core,
                  ipaddrs: t.allocate(),
                }),
              ],
              ...(result.options ? { options: result.options } : {}),
            };
          }),
      }),
      vpnGateway: (vpn, spec) => ({
        name: vpn,
        build: (t) => {
          if (internet === undefined) {
            throw new Error(
              `tenant "${name}": vpnGateway needs net.internet()`,
            );
          }
          const natName = `${name}-nat-${vpn}`;
          const natDomain = ctx.routingDomain(natName);
          const wan = firstActive(spec.wans, `vpnGateway "${vpn}"`);
          if (wan === undefined) {
            throw new Error(`vpnGateway "${vpn}": needs at least one wan`);
          }
          const workload = spec.kind === "wireguard"
            ? {
              kind: "wireguard" as const,
              ifaceName: spec.ifaceName ?? "wg0",
              config: spec.config,
              ...(spec.masq ? { masq: spec.masq } : {}),
            }
            : spec.kind === "tailscale"
            ? {
              kind: "tailscale" as const,
              loginServer: spec.loginServer,
              authKey: spec.authKey,
              ...(spec.upArgs ? { upArgs: spec.upArgs } : {}),
              ...(spec.masq ? { masq: spec.masq } : {}),
            }
            : {
              kind: "zerotier" as const,
              networkId: spec.networkId,
              identity: spec.identity,
              ...(spec.masq ? { masq: spec.masq } : {}),
            };
          const routes = [
            ...(spec.default
              ? [{ dst: IPv4.parse("0.0.0.0/0") }, { dst: IPv6.parse("::/0") }]
              : []),
            ...(spec.routes ?? []),
          ];
          const natAddrs = internet.allocate();
          return ctx.defineOvnRouter(`${name}-vpn-${vpn}`, (router) => ({
            routingDomains: [t.domain],
            endpoints: [
              router.tunnelRouterEndpoint({
                host: spec.host,
                transit: spec.svc.core,
                upstream: spec.svc.nat,
                // deno-lint-ignore no-explicit-any
                services: [workload as any],
                routes,
                routingDomains: [t.domain],
                upstreamBackbone: {
                  l2Segment: internet.core,
                  ipaddrs: natAddrs,
                  gatewayChassis: wan.host,
                  routes: wan.defaultRoutes(),
                  routingDomains: [natDomain],
                  services: masqFor(natAddrs),
                },
                upstreamDomains: [natDomain],
                names: {
                  transit: `${name}-svc-${vpn}-core`,
                  backdoor: `${name}-svc-${vpn}-nat`,
                  upstream: natName,
                },
              }),
              router.ovnRouterEndpoint({
                l2Segment: t.core,
                ipaddrs: t.allocate(),
              }),
            ],
          }));
        },
      }),
    };

    const spec = build(builder);
    const t: TenantBuild = {
      domain: ctx.routingDomain(name),
      core: ctx.collisionDomain(`${name}-core`),
      allocate: allocator(spec.core),
    };
    for (const router of spec.routers) this.routers.push(router.build(t));

    // The NAT gateway: masquerades the tenant's cables onto internet-core,
    // and gives the tenant its default route.
    const wan = firstActive(spec.wans, `tenant "${name}"`);
    if (wan === undefined) return;
    if (internet === undefined) {
      throw new Error(`tenant "${name}": wans need net.internet()`);
    }
    const cables = new Set(Object.values(this.collisionDomains));
    const sources = this.routers.flatMap((r) =>
      r.endpoints.filter((e) => cables.has(e.l2Segment)).flatMap((
        e,
      ) => [...e.ipaddrs])
    ).map((a) => a.network());
    const natAddrs = internet.allocate();
    const masq = masqFor(natAddrs, sources);
    this.routers.push(ctx.defineOvnRouter(`${name}-nat`, (router) => ({
      routingDomains: [t.domain],
      endpoints: [
        router.ovnRouterEndpoint({ l2Segment: t.core, ipaddrs: t.allocate() }),
        router.ovnRouterEndpoint({
          l2Segment: internet.core,
          ipaddrs: natAddrs,
          gatewayChassis: wan.host,
          routes: wan.defaultRoutes(),
          routingDomains: [t.domain],
          services: masq,
          // internet-core is not the tenant's to know: its default covers it.
          routeIsolated: true,
        }),
      ],
    })));
  }
}
