// src/builders.ts — the small mutable builder shapes a topology config
// interacts with (endpoint/service/docker-app callbacks), plus their
// normalizers. Pure shapes + construction; NetworkBuilder (define.ts)
// owns the state.

import type { TransitNetwork } from "./addressing.ts";
import type { IPv4, IPv6 } from "./ip.ts";
import type {
  Host,
  HostInterface,
  KernelRouterEndpoint,
  OvnRouterEndpoint,
  OvnRouterEndpointSpec,
  RouterEndpointRoute,
  RouterEndpointService,
  RoutingDomain,
  SecurityGroup,
  Service,
  TunnelRouterEndpoint,
} from "./types.ts";

/** The mutable builder passed to `endpoint.buildAppDocker(name, (app) => {...})`
 * (2026-08-31). `image` is OPTIONAL — it defaults to the router name at
 * resolve time. */
export interface DockerAppBuilder {
  /** Optional — the image; defaults to the router name when omitted. */
  image(image: string): void;
  /** The container's command. */
  cmd(cmd: string | readonly string[]): void;
  /** Build the image on first use if missing (packages installed at
   * image-build time; `from` determines apk vs apt, `dockerfile` the
   * full-escape-hatch). */
  build(build: {
    from: string;
    packages?: readonly string[];
    dockerfile?: string;
  }): void;
  /** veth-mode only — the container's address on the router's segment. */
  ip(ip: string): void;
}

/** The mutable builder passed to `router.kernelRouterEndpoint((endpoint) =>
 * {...})` (2026-08-31) — configure the endpoint's fields directly and use
 * `buildAppDocker` to add docker apps (a method that ONLY exists on kernel
 * endpoints, not ovn/tunnel). */
export interface KernelRouterEndpointBuilder {
  host: Host;
  transit: TransitNetwork;
  ipaddrs?: readonly (IPv4 | IPv6)[];
  ifaces?: readonly HostInterface[];
  services?: RouterEndpointService[];
  securityGroup?: SecurityGroup;
  routes?: readonly RouterEndpointRoute[];
  routingDomains?: readonly RoutingDomain[];
  buildAppDocker(name: string, build: (app: DockerAppBuilder) => void): void;
}

/** `router.kernelRouterEndpoint()` accepts EITHER a plain input object (the
 * pre-2026-08-31 form) OR a builder function (the new form, which exposes
 * `endpoint.buildAppDocker`). */
export type KernelEndpointBuilderFn =
  | Omit<KernelRouterEndpoint, "kind">
  | ((endpoint: KernelRouterEndpointBuilder) => void);

export function normalizeKernelEndpoint(
  input: KernelEndpointBuilderFn,
): Omit<KernelRouterEndpoint, "kind"> {
  if (typeof input !== "function") return input;
  const services: RouterEndpointService[] = [];
  const endpoint: KernelRouterEndpointBuilder = {
    host: undefined as unknown as Host,
    transit: undefined as unknown as TransitNetwork,
    services,
    buildAppDocker: (name, build) => {
      const app: {
        kind: "kernel.app.docker";
        name: string;
        image?: string;
        cmd?: string | readonly string[];
        build?: {
          from: string;
          packages?: readonly string[];
          dockerfile?: string;
        };
        ip?: string;
      } = { kind: "kernel.app.docker", name };
      const b: DockerAppBuilder = {
        image: (image) => {
          app.image = image;
        },
        cmd: (cmd) => {
          app.cmd = cmd;
        },
        build: (bd) => {
          app.build = bd;
        },
        ip: (ip) => {
          app.ip = ip;
        },
      };
      build(b);
      services.push(app);
    },
  };
  input(endpoint);
  if (!endpoint.host || !endpoint.transit) {
    throw new Error(
      "kernelRouterEndpoint: builder must set `host` and `transit`",
    );
  }
  return {
    host: endpoint.host,
    transit: endpoint.transit,
    ipaddrs: endpoint.ipaddrs ?? [],
    ...(endpoint.ifaces ? { ifaces: endpoint.ifaces } : {}),
    ...(services.length > 0 ? { services } : {}),
    ...(endpoint.securityGroup
      ? { securityGroup: endpoint.securityGroup }
      : {}),
    ...(endpoint.routes ? { routes: endpoint.routes } : {}),
    ...(endpoint.routingDomains
      ? { routingDomains: endpoint.routingDomains }
      : {}),
  } as Omit<KernelRouterEndpoint, "kind">;
}

/** The context passed to `router.ovnRouterEndpoint((ep) => ({...}))` — the
 * callback RETURNS the endpoint spec (so required fields like `ipaddrs`
 * are enforced), and `ep.attachTo(sv, {...})` produces a NIC for a service
 * on THIS endpoint's segment (2026-09-08). */
export interface EndpointBuilder {
  attachTo(
    service: Service,
    attachment: {
      readonly ipaddrs: readonly (IPv4 | IPv6)[];
      readonly routes?: readonly RouterEndpointRoute[];
      readonly primary?: boolean;
    },
  ): Extract<RouterEndpointService, { kind: "service.attach" }>;
}

export function endpointBuilder(): EndpointBuilder {
  return {
    attachTo: (service, attachment) => ({
      kind: "service.attach",
      srvRef: service,
      ...attachment,
    }),
  };
}

export type OvnEndpointFn = (
  ep: EndpointBuilder,
) => Omit<OvnRouterEndpointSpec, "kind">;

/** The mutable spec handed to `net.service(name, (svc) => {...})` — set
 * `image` (required), `cmd`, `build`. Returns a re-usable Service handle. */
export interface ServiceBuilder {
  image: string;
  cmd?: string | readonly string[];
  build?: {
    readonly from: string;
    readonly packages?: readonly string[];
    readonly dockerfile?: string;
  };
}

/**
 * The builder context passed into defineNetwork's callback per router —
 * same "context object, void callback" idiom as defineNetwork's own `net`,
 * one level down. `left`/`right` are plain SETTABLE attributes, not a
 * returned object: kernelRouterEndpoint() needs to read them at the moment
 * it resolves, and ovnRouterEndpoint()/kernelRouterEndpoint()/
 * tunnelRouterEndpoint() are ONLY reachable through this object, not as
 * NetworkBuilder methods — the router-level routingDomains is REQUIRED on
 * the callback's return value (define.ts), so an author can't forget its
 * membership (2026-09-08). Always the OVN side: every endpoint builder
 * (ovn/kernel/tunnel) returns a plain OvnRouterEndpoint; the
 * KernelRouterEndpoint and TunnelRouterEndpoint shapes are INPUT types,
 * never stored here.
 */
export interface RouterBuilder {
  left?: OvnRouterEndpoint;
  right?: OvnRouterEndpoint;
  ovnRouterEndpoint(
    input: Omit<OvnRouterEndpointSpec, "kind"> | OvnEndpointFn,
  ): OvnRouterEndpoint;
  kernelRouterEndpoint(
    input: KernelEndpointBuilderFn,
  ): OvnRouterEndpoint;
  tunnelRouterEndpoint(
    input: Omit<TunnelRouterEndpoint, "kind">,
  ): OvnRouterEndpoint;
}

/** The DECLARATIVE object form of defineOvnRouter() (2026-09-08): a router
 * as one typed value — routingDomains (required) up-front, and left/right
 * as kind-tagged endpoint specs. RoutingDomains is known here BEFORE the
 * endpoints are resolved, so a kernel/tunnel spec is built with it
 * directly (no deferred stamp). Endpoint services are plain objects in
 * each spec's `services` array. */
export interface OvnRouterSpec {
  readonly routingDomains: readonly RoutingDomain[];
  readonly left:
    | OvnRouterEndpointSpec
    | KernelRouterEndpoint
    | TunnelRouterEndpoint;
  readonly right:
    | OvnRouterEndpointSpec
    | KernelRouterEndpoint
    | TunnelRouterEndpoint;
}
