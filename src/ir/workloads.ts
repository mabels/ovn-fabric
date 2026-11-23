// src/ir/workloads.ts — Workloads: service references on endpoints, their NICs
// and routes, container nodes, route metrics.

import type { NetworkDefinition } from "../define.ts";
import { type Addr, prefixOf, toAddr, toAddrs } from "../ipnet.ts";
import type { AddressIndex } from "../ir-addresses.ts";
import type {
  AddrRefValue,
  IRNode,
  KernelAppContainerNode,
  ServiceRef,
} from "../protocol.ts";
import type {
  CollisionDomain,
  OvnRouterEndpoint,
  Resolver,
  Router,
  RoutingDomain,
  Service,
} from "../types.ts";
import { lsId, shortIfaceName } from "./names.ts";

// The platform's control-plane leg + resolved global resolver — what a
// workload attachment on that domain inherits when it declares none
// (2026-09-19).
export type PlatformControlPlaneResolver = {
  readonly collisionDomain: CollisionDomain;
  readonly resolver?: Resolver;
};

// The destinations a RoutingDomain distributes. An explicit, non-empty
// `routes` on the domain OVERRIDES; otherwise they are CALCULATED — the dst
// prefixes of the route nodes the domain's own participants actually
// resolved (anchors + interconnect below), i.e. what the domain really
// distributes through the network. A domain that resolved no routes (a pure
// membership tag) distributes nothing. `routes: []` is treated the same as
// omitting it: no override, calculate.
function domainDsts(
  domain: RoutingDomain,
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
): readonly { dst: string; metric?: number }[] {
  // Only a RoutingDomainRoute's OWN metric is returned here; the
  // attachment's and the domain's own metric are applied by
  // workloadDomainRoutes so the full precedence (route > attachment >
  // domain) stays in one place (2026-09-19).
  if (domain.routes && domain.routes.length > 0) {
    return domain.routes.map((r) =>
      r.metric !== undefined
        ? { dst: prefixOf(r.dst).to_string(), metric: r.metric }
        : { dst: prefixOf(r.dst).to_string() }
    );
  }
  return [...(dstsByDomain.get(domain.name) ?? [])].map((dst) => ({ dst }));
}

// The routes a workload NIC inherits at the WORKLOAD layer: the calculated
// (or explicitly overridden) destinations of every RoutingDomain its
// endpoint's router participates in, next-hopped at THIS endpoint's own
// gateway (the address a container on this segment must use). A dst the
// endpoint is already ON-LINK for is skipped — the connected route already
// covers it. Explicit attachment routes (ref.routes) win outright; see
// resolveServiceRefs below. Each route carries the resolved metric (route >
// domain > attachment), the deployer's ranking of a workload's default
// routes (2026-09-19).
function workloadDomainRoutes(
  router: Router,
  endpoint: OvnRouterEndpoint,
  attachmentAddrs: readonly Addr[],
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
  addrs: AddressIndex,
  line: string,
  attachmentMetric?: number,
): { dst: string; via: AddrRefValue; metric?: number }[] {
  const domains = endpoint.routingDomains ?? router.routingDomains ?? [];
  const out: { dst: string; via: AddrRefValue; metric?: number }[] = [];
  for (const domain of domains) {
    for (const { dst, metric } of domainDsts(domain, dstsByDomain)) {
      const isV4 = !dst.includes(":");
      // The NIC can only use a next hop of a family it actually has an
      // address for — the ATTACHMENT's addresses, not the endpoint's: a
      // v4-only container NIC must not get a v6 route just because the
      // router port has a v6 address (the gateway would be unreachable and
      // `ip route add` fails, taking the rest of the script with it).
      if (!attachmentAddrs.some((a) => a.is_ipv4() === isV4)) continue;
      const gateway = endpoint.ipaddrs.find((a) => a.is_ipv4() === isV4);
      if (!gateway) continue;
      const onLink = attachmentAddrs.some((a) =>
        a.is_ipv4() === isV4 && a.network().to_string() === dst
      );
      if (onLink) continue;
      if (out.some((r) => r.dst === dst)) continue;
      // Metric precedence: the route's own metric, else THIS attachment's
      // (per-NIC) metric, else the routing domain's. Whole egress paths are
      // ranked by routing domain, a single NIC overrides that, and one
      // route overrides the NIC (2026-09-19).
      const resolved = metric ?? attachmentMetric ?? domain.metric;
      out.push({
        dst,
        via: addrs.ref(gateway, `${line}|route:${dst}|via`, [
          lsId(endpoint.l2Segment),
        ]),
        ...(resolved !== undefined ? { metric: resolved } : {}),
      });
    }
  }
  return out;
}

// A workload attachment at this endpoint — REFERENCES the
// `kernel.app.container:<name>` node, with this segment's addressing.
// `routes` is explicit; otherwise the NIC inherits the routes of the
// RoutingDomains its endpoint's router participates in, next-hopped at this
// segment's gateway (workloadDomainRoutes above). An off-segment `via` on an
// explicit route is unreachable on this L2 and throws (2026-09-08).
export function resolveServiceRefs(
  router: Router,
  endpoint: OvnRouterEndpoint,
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
  addrs: AddressIndex,
  platformCp?: PlatformControlPlaneResolver,
): ServiceRef[] | undefined {
  const refs = (endpoint.services ?? []).filter((s) =>
    s.kind === "service.attach"
  );
  if (refs.length === 0) return undefined;
  // The control-plane attachment this endpoint represents, if any: an
  // attachment THERE with no resolver of its own is fed the platform's
  // GLOBAL resolver (controlPlane.resolver, else the CoreDNS address).
  const onControlPlane = platformCp !== undefined &&
    endpoint.l2Segment.name === platformCp.collisionDomain.name;
  const domain = lsId(endpoint.l2Segment);
  return refs.map((ref) => {
    const service = `kernel.app.container:${ref.srvRef.name}`;
    const nic = shortIfaceName("", endpoint.l2Segment.name);
    const owner = `${service}|nic:${nic}`;
    const nicAddrs = toAddrs(ref.ipaddrs);
    const routes = ref.routes && ref.routes.length > 0
      ? ref.routes.map((r) => {
        if (r.via && !viaIsOnSegment(endpoint, toAddr(r.via))) {
          throw new Error(
            `service "${ref.srvRef.name}" on ${router.name} ` +
              `(${endpoint.name}): route ` +
              `${prefixOf(r.dst).to_string()} via ${
                toAddr(r.via)
              } — the via is not on ` +
              `segment "${endpoint.l2Segment.name}" (${
                endpoint.ipaddrs.map((a) => a.toString()).join(", ")
              }); a container next-hop must be reachable on its own L2`,
          );
        }
        // Route metric wins, else this ATTACHMENT's metric (the NIC-level
        // default), else unset — the generator fills the execution-order
        // fallback in assignFallbackRouteMetrics below (2026-09-19).
        const metric = r.metric ?? ref.metric;
        const dst = prefixOf(r.dst).to_string();
        return {
          dst,
          ...(r.via
            ? {
              via: addrs.ref(toAddr(r.via), `${owner}|route:${dst}|via`, [
                domain,
              ]),
            }
            : {}),
          ...(metric !== undefined ? { metric } : {}),
        };
      })
      : workloadDomainRoutes(
        router,
        endpoint,
        nicAddrs,
        dstsByDomain,
        addrs,
        owner,
        ref.metric,
      );
    // Resolver precedence (innermost wins): the attachment's own, else the
    // platform's global control-plane resolver (2026-09-19).
    const resolver = ref.resolver ??
      (onControlPlane ? platformCp?.resolver : undefined);
    // A nameserver is a reference, or as written when it is not ours.
    const nameservers: (AddrRefValue | string)[] = [];
    for (const [i, n] of (resolver?.nameservers ?? []).entries()) {
      nameservers.push("");
      addrs.nameserver(toAddr(n), `${owner}|nameserver`, (v) => {
        nameservers[i] = v;
      });
    }
    return {
      service,
      // The in-container NIC name — resolved HERE (generator side), not by
      // the deployer: the segment name when it fits IFNAMSIZ, else the fnv1a
      // short form (shortIfaceName, same rule as every other short name).
      name: nic,
      // No `mac`: the NIC's MAC is derived from its resolved address by the
      // runtime (ADR 0011 §5) — still stable across the container being
      // recreated, so the segment router's MAC_Binding survives.
      ...(nicAddrs.length > 0
        ? { ipaddrs: addrs.own(nicAddrs, domain, owner) }
        : {}),
      ...(routes.length > 0 ? { routes: orderRoutes(routes) } : {}),
      ...(resolver ? { resolver: { nameservers } } : {}),
    };
  });
}

// Defaults FIRST, and all IPv4 before all IPv6, then lexically. The
// deployer applies these in order inside a `set -e` script: a route that
// can't be installed (e.g. a v6 gateway on a NIC whose v6 address is still
// tentative) would otherwise abort before the default ever landed, leaving
// the container with no default at all (hit live, 2026-09-16). Putting the
// v4 default first makes that failure survivable; sorting the rest keeps
// the IR diff-stable.
function orderRoutes<R extends { readonly dst: string }>(
  routes: readonly R[],
): R[] {
  const key = (dst: string): [number, number, string] => [
    dst.includes(":") ? 1 : 0, // IPv4 before IPv6
    dst === "0.0.0.0/0" || dst === "::/0" ? 0 : 1, // default before the rest
    dst,
  ];
  return [...routes].sort((a, b) => {
    const ka = key(a.dst);
    const kb = key(b.dst);
    if (ka[0] !== kb[0]) return ka[0] - kb[0];
    if (ka[1] !== kb[1]) return ka[1] - kb[1];
    return ka[2] < kb[2] ? -1 : ka[2] > kb[2] ? 1 : 0;
  });
}

// A `kernel.app.docker` service listed on a segment-facing OVN endpoint's
// services[] resolves into one `kernel.app.container` node per referenced
// service (the workload), with the referencing endpoints carrying the NICs: a
// container the endpoint's leg HOST runs, bound to the leg's collision
// domain (an L2 endpoint on that segment, not a router) (2026-09-08).
// A container's routes come from its attachment's explicit `routes` if any,
// else from the RoutingDomains its segment's router participates in
// (workloadDomainRoutes above).

// A container's route `via` must be an address reachable on the segment it
// sits on — i.e. inside the network of one of the hosting endpoint's OWN
// ipaddrs (same family). A via off-segment would be an unreachable
// next-hop for a single-homed container (2026-09-08).
function viaIsOnSegment(endpoint: OvnRouterEndpoint, via: Addr): boolean {
  // A pinned via by its number; an allocated one by its net.
  const value = via.pinned() ?? via.network();
  for (const addr of endpoint.ipaddrs) {
    if (addr.is_ipv4() !== via.is_ipv4()) continue;
    if (addr.network().includes(value)) return true;
  }
  return false;
}

// One `kernel.app.container` node per referenced workload — the image/cmd/build
// plus the host it runs on. Networks are NOT here: they live on the
// endpoints that reference this service (OvnLrpData.serviceRefs,
// resolveServiceRefs above), so the container's NICs are DERIVED from the
// references at emit time (2026-09-08).
export function serviceNodes(
  network: NetworkDefinition,
): KernelAppContainerNode[] {
  const hosts = new Map<Service, string>();
  for (const router of network.allRouters) {
    for (const endpoint of router.endpoints) {
      if (endpoint.kind !== "ovn") continue;
      for (const entry of endpoint.services ?? []) {
        if (entry.kind !== "service.attach") continue;
        const service = entry.srvRef;
        const hostName = endpoint.ifaces?.[0]?.host.name;
        if (!hostName) {
          throw new Error(
            `service "${service.name}": attached at ${router.name} ` +
              `(${endpoint.name}) but that endpoint has no ifaces to name ` +
              `the host running the container`,
          );
        }
        const known = hosts.get(service);
        if (!known) {
          hosts.set(service, hostName);
        } else if (known !== hostName) {
          throw new Error(
            `service "${service.name}" is attached on two different hosts ` +
              `("${known}" and "${hostName}") — one container runs on one host`,
          );
        }
      }
    }
  }

  const out: KernelAppContainerNode[] = [];
  for (const [service, hostName] of hosts) {
    // A net.service is a docker container → the node kind IS the workload
    // kind (`kernel.app.container`), no `kernel.service` envelope.
    out.push({
      id: `kernel.app.container:${service.name}`,
      kind: "kernel.app.container",
      key: { name: service.name },
      data: {
        host: `host:${hostName}`,
        image: service.image,
        ...(service.cmd ? { cmd: service.cmd } : {}),
        ...(service.build ? { build: service.build } : {}),
        // Sorted and once each: the IR is diff-stable.
        ...(service.securityEnable && service.securityEnable.length > 0
          ? { securityEnable: [...new Set(service.securityEnable)].sort() }
          : {}),
        // Environment variables — keys sorted so the IR is diff-stable
        // regardless of declaration order.
        ...(service.env && Object.keys(service.env).length > 0
          ? {
            env: Object.fromEntries(
              Object.entries(service.env).sort((
                [a],
                [b],
              ) => (a < b ? -1 : a > b ? 1 : 0)),
            ),
          }
          : {}),
        // The in-memory filesystem — sorted by path so the IR is
        // diff-stable regardless of declaration order.
        ...(service.files && service.files.length > 0
          ? {
            files: [...service.files].sort((a, b) =>
              a.path < b.path ? -1 : a.path > b.path ? 1 : 0
            ),
          }
          : {}),
      },
    });
  }
  return out;
}

// Metric fallback when neither a route, its attachment, nor its routing
// domain declares one: assign by EXECUTION ORDER — a workload's first NIC
// gets the lowest metric (most preferred), each subsequent NIC +100. The
// GENERATOR owns this (the deployer never invents a metric; it only applies
// what the IR carries). Lower wins among equal-prefix routes (2026-09-19).
const _DEFAULT_ROUTE_METRIC_BASE = 100;

const _DEFAULT_ROUTE_METRIC_STEP = 100;

export function assignFallbackRouteMetrics(
  nodes: Record<string, IRNode>,
): void {
  // Per SERVICE (each workload is its own netns/routing tables, so ranks
  // are independent per workload). Nodes are walked in declaration order
  // (router → endpoint → attachment), which IS the execution order the
  // deployer would have used. An IR node is readonly, so a port whose
  // attachments gain a metric is REPLACED under its own id (same slot,
  // the walk order is unaffected) rather than edited in place.
  const rankByService = new Map<string, number>();
  for (const node of Object.values(nodes)) {
    if (node.kind !== "ovn.lrp") continue;
    const refs = node.data.serviceRefs;
    if (!refs) continue;
    const ranked = refs.map((ref): ServiceRef => {
      const routes = ref.routes;
      if (!routes || routes.length === 0) return ref;
      if (!routes.some((r) => r.metric === undefined)) return ref;
      const rank = rankByService.get(ref.service) ?? 0;
      const fallback = _DEFAULT_ROUTE_METRIC_BASE +
        rank * _DEFAULT_ROUTE_METRIC_STEP;
      rankByService.set(ref.service, rank + 1);
      return {
        ...ref,
        routes: routes.map((route) =>
          route.metric === undefined ? { ...route, metric: fallback } : route
        ),
      };
    });
    nodes[node.id] = { ...node, data: { ...node.data, serviceRefs: ranked } };
  }
}
