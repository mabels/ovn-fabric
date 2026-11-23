import type { EndpointBuilder } from "../builders.ts";
import type { AddrLike } from "../ipnet.ts";
import type { platformSvc } from "../platform-service.ts";
import type { CollisionDomain } from "./collision-domain.ts";
import type { Host } from "./host.ts";
import type {
  HostInterface,
  OvnRouterEndpoint,
  OvnRouterEndpointSpec,
} from "./router-endpoint.ts";
import type { RouterEndpointRoute, RoutingDomain } from "./routing.ts";
import type { Service } from "./service.ts";

// ── platform: the appliance's own service platform ────────────────────
// DRAFT (2026-09-16). net.dnsProvider() is REAL; net.certIssuer() and
// net.createPlatform() are TYPES ONLY for now (their implementations throw
// "not implemented"). The shapes below are settled enough to author
// topologies against and to build the implementation on, step by step.

/** External DNS provider the platform may write records through — a UNION
 * of per-kind specs, TODAY one: a Cloudflare API token. `token` is the
 * credential itself (inline in the topology, which is this draft's secret
 * store). */
export type DnsProviderSpec = {
  readonly kind: "cloudflare";
  readonly token: string;
};

/** An EXTERNAL DNS provider object, referenced by a PlatformZone. The
 * platform's OWN authoritative DNS is deliberately NOT a provider: a zone
 * with no `dnsProvider` is served by the platform itself. */
export interface DnsProvider {
  readonly name: string;
  readonly spec: DnsProviderSpec;
}

/** Certificate issuer — how the platform gets certs. A UNION of per-kind
 * specs, TODAY one: ACME. A `dns-01` challenge deliberately does NOT name a
 * provider: it takes the DNS provider from the ZONE it is used in, so the
 * two can never drift apart. */
export type CertIssuerSpec = {
  readonly kind: "acme";
  /** ACME directory URL (Let's Encrypt, or a private CA's endpoint). */
  readonly directory: string;
  /** Contact for expiry notices (account registration). */
  readonly email?: string;
  readonly challenge:
    | { readonly kind: "dns-01" } // provider resolved from the zone
    | { readonly kind: "http-01" }
    | { readonly kind: "tls-alpn-01" };
};

export interface CertIssuer {
  readonly name: string;
  readonly spec: CertIssuerSpec;
}

/** One zone entry the platform may publish into. `zones` lists the names it
 * covers (a forward zone and its reverse can share one entry). A missing
 * `dnsProvider` means "the platform's own DNS is authoritative" (an
 * internal zone); a missing `certIssuer` means no certs are issued for
 * names in it yet. */
export interface PlatformZone {
  readonly zones: readonly string[];
  readonly dnsProvider?: DnsProvider;
  readonly certIssuer?: CertIssuer;
}

/** Where the platform's own services live and how they are reached. NO
 * `router`: the TOPOLOGY owns routers and PLACES the platform's services on
 * their endpoints via ep.attachTo(platform.ingressService, …) — that
 * attachment is what binds a service to an address. */
/** Options for the platform's BUILT-IN services — ONE entry per built-in as
 * it lands. PlatformServiceId is DERIVED from this map, so adding an entry
 * here adds the id (and its options type to cfgPlatformSvc). */
export interface PlatformServiceOptions {
  readonly dns: PlatformServiceCoreDns;
}

export type PlatformServiceId = keyof PlatformServiceOptions;

/** One id's setting: its options, or platformSvc.IGNORE to DROP it from the
 * active set. */
export type PlatformServiceSetting<K extends PlatformServiceId> =
  | PlatformServiceOptions[K]
  | typeof platformSvc.IGNORE;

/** CoreDNS — the platform's own internal resolver (id "dns"): answers for
 * the platform's local names, forwards everything else. */
export interface PlatformServiceCoreDns {
  /** The resolver's address on the platform's CONTROL-PLANE leg. Omitted →
   * allocated like any other platform service (control-plane network +10,
   * +11, …). */
  readonly ipaddrs?: readonly AddrLike[];
  /** Upstreams for anything not served locally, in CoreDNS syntax — e.g.
   * "tls://1.1.1.1", "https://cloudflare-dns.com/dns-query". */
  readonly forwarders?: readonly string[];
  /** Local zones this instance answers authoritatively (the platform's own
   * names + the topology's internal zones) — rendered into its Corefile. */
  readonly zones?: readonly string[];
}

/** A workload's DNS resolver — rendered by the deployer as
 * `docker run --dns …` (Docker then writes /etc/resolv.conf; no file is
 * bind-mounted over it). Resolution is a chain, innermost set wins, same
 * substitution as route metric: attachment.resolver ?? control-plane
 * resolver ?? (control-plane default) the platform CoreDNS address
 * (2026-09-19). `search`/`options` land later. */
export interface Resolver {
  readonly nameservers: readonly AddrLike[];
}

export interface PlatformControlPlane {
  readonly collisionDomain: CollisionDomain;
  /** RoutingDomain memberships of the platform router. The CONTROL-PLANE
   * leg (and therefore every workload attached to it) inherits routes
   * from ALL of them, so an egress domain can supply the default. */
  readonly routingDomains?: readonly RoutingDomain[];
  readonly hosts: readonly Host[];
  /** The GLOBAL resolver for this control plane — what a control-plane
   * attachment with no `resolver` of its own is fed. Omitted → the
   * platform's own CoreDNS address (allocated on this leg). A nearer
   * `resolver` (attachment) wins; same substitution as route metric
   * (2026-09-19). */
  readonly resolver?: Resolver;
  /** The CONTROL-PLANE leg's addresses. createPlatform builds that endpoint
   * from these (the platform's router NAME is inferred from the platform
   * name); only the EGRESS leg(s) are added via addEgress/spec.egress. Optional:
   * without them the platform builds no router of its own. */
  readonly ipaddrs?: readonly AddrLike[];
  readonly ifaces?: readonly HostInterface[];
  /** The segments that may reach the control plane. The router that is the
   * segment's gateway gets a leg on the control plane, and masquerades the
   * segment out of it: a client needs no route of its own, and a workload
   * sees the leg's address (allocated like a service's). Needs `ipaddrs`. */
  readonly ingress?: readonly CollisionDomain[];
}

export interface PlatformSpec {
  readonly controlPlane: PlatformControlPlane;
  readonly zones: readonly PlatformZone[];
  /** Admin NICs for the control-plane leg, given UP FRONT — the same specs
   * platform.addService(...) takes. Both are supported and ADDITIVE: use
   * this to declare the platform as one value, addService() to extend it
   * later (and both are chainable). */
  readonly services?: readonly PlatformServiceSpec[];
  /** EGRESS leg(s) of the platform's own router — what the control plane is
   * joined to. Given up front, or added later with platform.addEgress().
   * The CONTROL-PLANE leg itself is built from controlPlane params. */
  readonly egress?: readonly PlatformEgressSpec[];
  /** BUILT-IN platform services, tuned per id: an omitted id uses the
   * built-in's default; platformSvc.IGNORE (or delPlatformSvc(id)) drops it
   * from the active set. */
  readonly platformServices?: {
    readonly [K in PlatformServiceId]?: PlatformServiceSetting<K>;
  };
}

export interface Platform {
  readonly name: string;
  readonly spec: PlatformSpec;
  /** The ingress service DESCRIPTION the topology places via
   * ep.attachTo(...) on an endpoint with a concrete address. */
  readonly ingressService: Service;
  /** Bind a DNS name + cert to an attached service, through this platform. */
  publish(spec: PublishSpec): Publish;
  /** Add an EGRESS leg to the platform's own router — the CONTROL-PLANE
   * leg is built from controlPlane params. Same spec/callback shape as
   * RouterBuilder.ovnRouterEndpoint, so ifaces and `ep.attachTo(...)`
   * services work exactly the same. Chainable. */
  addEgress(input: PlatformEgressSpec): Platform;
  /** Add a SERVICE (an admin NIC) to the platform's CONTROL-PLANE leg. A
   * plain spec — addService() calls attachTo() internally on the leg's own
   * endpoint builder, and injects the platform into `publish`. Chainable. */
  addService(spec: PlatformServiceSpec): Platform;
  /** Configure a BUILT-IN platform service (id → options; merged over its
   * defaults). Chainable. */
  cfgPlatformSvc<K extends PlatformServiceId>(
    id: K,
    options: PlatformServiceOptions[K],
  ): Platform;
  /** DROP a built-in platform service from the active set. Chainable. */
  delPlatformSvc(id: PlatformServiceId): Platform;
}

/** One EGRESS leg of the platform's own router — the spec/callback shape
 * of RouterBuilder.ovnRouterEndpoint, without `services`: an egress leg
 * masquerades with `masq: ["ipv4", "ipv6"]` (ADR 0011 §3b) and runs no
 * workloads. Given up front via PlatformSpec.egress[] or added later with
 * platform.addEgress(). */
export type PlatformEgressSpec =
  | Omit<OvnRouterEndpointSpec, "kind" | "services">
  | ((ep: EndpointBuilder) => Omit<OvnRouterEndpointSpec, "kind" | "services">);

/** One admin NIC for the platform's control-plane leg (platform.addService).
 * Deliberately NO `(ep) => ep.attachTo(...)`: the platform owns that
 * endpoint, so it does the attach itself. */
export interface PlatformServiceSpec {
  readonly service: Service;
  /** Fill a built-in platform role instead of adding a plain admin NIC.
   * A service with `role: "dns"` REPLACES the built-in `platform.dns`:
   * the built-in is not instantiated, and only its platform semantics
   * (control-plane attachment, address allocation, global resolver)
   * carry over. The Service itself — image, cmd, files, env, build — is
   * used verbatim; nothing is merged. At most one service may claim a
   * role. */
  readonly role?: PlatformServiceId;
  /** Omitted → ALLOCATED from the control-plane leg's network, starting at
   * host +10 and counting up (the k8s convention: .1 is the gateway, the
   * first workload lands at .10) — e.g. a 10.99.77.1/24 leg yields
   * 10.99.77.10/24, then .11, … Already-used addresses are skipped. */
  readonly ipaddrs?: readonly AddrLike[];
  readonly routes?: readonly RouterEndpointRoute[];
  /** A single name/cert binding — the platform injects itself as the
   * publish's platform (no platform.publish(...) at the call site). */
  readonly publish?: PublishSpec;
}

/** A name + cert binding, authored as platform.publish(...) so it carries
 * the platform (hence its zones → provider + issuer). The TARGET ADDRESS is
 * deliberately not here: it resolves from wherever the ingress service is
 * attached. */
export interface PublishSpec {
  readonly host: string;
  /** Named service port to route to (required for `via: "ingress"`). */
  readonly port?: string;
  readonly via?: "ingress" | "direct";
  readonly tls?: "auto" | "none";
  readonly dns?: { readonly ttl?: number; readonly proxied?: boolean };
  readonly path?: string;
}

/** INJECTED, never authored: `platform` at publish() time; `endpoint` +
 * `service` when the publish is carried by an ep.attachTo(...) (same
 * pattern as EndpointService.endpoint). */
export type Publish = PublishSpec & {
  readonly platform: Platform;
  readonly endpoint?: OvnRouterEndpoint;
  readonly service?: Service;
};
