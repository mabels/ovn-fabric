// src/ir/to-ir.ts — toIR(): the whole network definition as IR nodes.

import { macFromName } from "../addressing.ts";
import type { NetworkDefinition } from "../define.ts";
import { AddressIndex } from "../ir-addresses.ts";
import type { IRNode } from "../protocol.ts";
import { CollisionDomain } from "../types.ts";
import { collisionDomainToIR } from "./collision-domain.ts";
import { hostDependencies, hostToIR } from "./host.ts";
import { kernelRouterSideToIR, kernelRouterToIR } from "./kernel-router.ts";
import { transitBridgeName, vethShortId } from "./names.ts";
import {
  computeOvnSecurityGroups,
  routerEndpointToIR,
  routerToIR,
} from "./router.ts";
import { computeInterconnectRoutes, computeRoutes } from "./routes.ts";
import { securityGroupToIR } from "./security-group.ts";
import { assignFallbackRouteMetrics, serviceNodes } from "./workloads.ts";

export function toIR(network: NetworkDefinition): Record<string, IRNode> {
  const nodes: Record<string, IRNode> = {};
  // Every address of the IR goes through this index (ADR 0011): carriers
  // register theirs, everything else refers to them; finish() below fills
  // the references and gives the `ip.net` nodes.
  const addrs = new AddressIndex();

  // Route nodes FIRST (2026-09-16): a RoutingDomain's CALCULATED destinations
  // are the prefixes its participants actually resolved (anchors +
  // interconnect), and a workload inherits them (workloadDomainRoutes) — so
  // they must exist before the endpoints are emitted. Insertion order is
  // irrelevant here; the final map is sorted by id below.
  const interconnectRoutes = computeInterconnectRoutes(network, addrs);
  const domainRoutes = computeRoutes(network, addrs);
  // Infrastructure subnets (OVN-internal transit/backdoor links and the
  // backbone) are NOT destinations a workload should route to: they're
  // router plumbing. Their prefixes leak into a domain's calculated set via
  // interconnect (a tunnel's upstream router is a Voda-defaultRoute
  // participant, so its transit subnets get distributed), so filter them out
  // of what workloads inherit.
  const infraPrefixes = new Set<string>();
  for (const router of network.allRouters) {
    for (const endpoint of router.endpoints) {
      const name = endpoint.l2Segment.name;
      const infra = name === network.backbone?.name ||
        name.startsWith("transit-") || name.startsWith("backdoor-");
      if (!infra) continue;
      for (const addr of endpoint.ipaddrs) {
        infraPrefixes.add(addr.network().to_string());
      }
    }
  }
  const dstsByDomain = new Map<string, Set<string>>();
  for (const route of [...interconnectRoutes, ...domainRoutes]) {
    const domain = route.data.domain;
    const prefix = route.key.prefix;
    if (infraPrefixes.has(prefix)) continue;
    let set = dstsByDomain.get(domain);
    if (!set) {
      set = new Set();
      dstsByDomain.set(domain, set);
    }
    set.add(prefix);
  }

  for (const host of network.allHosts) {
    const node = hostToIR(host, hostDependencies(host, network));
    nodes[node.id] = node;
  }

  for (const domain of network.allCollisionDomains) {
    const node = collisionDomainToIR(domain);
    nodes[node.id] = node;
  }

  const ovnGroups = computeOvnSecurityGroups(network).byRouter;
  for (const group of ovnGroups.values()) {
    const node = securityGroupToIR(group, addrs);
    nodes[node.id] = node;
  }
  for (const router of network.allRouters) {
    const implicit = ovnGroups.get(router.name);
    const endpointGroups = router.endpoints.flatMap((endpoint) =>
      (endpoint.securityGroups ?? []).map((g) => ({
        render: g.render,
        name: g.name,
      }))
    );
    const refs = [
      ...endpointGroups,
      ...(implicit ? [{ render: implicit.render, name: implicit.name }] : []),
    ];
    const routerNode = routerToIR(
      router,
      refs as readonly { render: "kernel" | "ovn"; name: string }[],
    );
    nodes[routerNode.id] = routerNode;
    for (const endpoint of router.endpoints) {
      if (endpoint.kind !== "ovn") {
        // KernelRouterEndpoint (types.ts) — no emission strategy built
        // for this kind yet (nothing in this codebase constructs one
        // today). Throws rather than silently skipping, so the day a
        // config author actually declares one, this says so loudly
        // instead of quietly producing an incomplete IR.
        throw new Error(
          `router "${router.name}": endpoint "${endpoint.name}" is kind ` +
            `"${endpoint.kind}" — toIR() has no emission strategy for it yet`,
        );
      }
      const node = routerEndpointToIR(
        router,
        endpoint,
        dstsByDomain,
        addrs,
        network.platformControlPlane,
      );
      nodes[node.id] = node;
    }
  }

  // Workloads: one `kernel.app.container` node per referenced service; the
  // endpoints referencing it carry the NICs (OvnLrpData.serviceRefs,
  // emitted in the router loop above) (2026-09-08).
  for (const service of serviceNodes(network)) {
    nodes[service.id] = service;
  }

  // Interconnect (peer-to-peer among a domain's own participants)
  // first, RoutingDomain via-routes second — so if a via-route ever
  // targets the same prefix an interconnect route already computed for
  // the same domain, the explicit via-route wins (last write to
  // `nodes` on a shared `id` takes it). Computed BEFORE the kernel
  // router loop below (moved 2026-08-13) — kernelRouterSideToIR's own
  // back-route mirroring needs the FULL, already-deduped route set to
  // mirror from.
  for (const route of interconnectRoutes) {
    nodes[route.id] = route;
  }
  for (const route of domainRoutes) {
    nodes[route.id] = route;
  }
  const routeNodes = Object.values(nodes).filter((n) =>
    n.kind === "ipv4.route" || n.kind === "ipv6.route"
  );

  for (const kernelRouter of network.allKernelRouters) {
    const ownerNode = kernelRouterToIR(kernelRouter);
    nodes[ownerNode.id] = ownerNode;
    for (const side of ["left", "right"] as const) {
      const node = kernelRouterSideToIR(
        kernelRouter,
        side,
        network.allRouters,
        routeNodes,
        addrs,
      );
      nodes[node.id] = node;
    }
  }

  // A WAN VLAN shared by several kernel routers cannot be moved into
  // each router's netns (one 8021q device per parent+tag, cluster-wide).
  // Emit a host-local `bridge` node carrying the shared VLAN leg plus one
  // veth per router, and turn each router side into the veth that plugs
  // into it. A topology can also declare this bridge explicitly with
  // net.collisionDomain(name, { host }) + addInterface().
  const vlanDomainKey = (host: string, parent: string, vlanId: number) =>
    `${host}\u0000${parent}\u0000${vlanId}`;
  const vlanUsers = new Map<string, Set<string>>();
  for (const node of Object.values(nodes)) {
    if (node.kind !== "kernel.router" || node.key.side === undefined) continue;
    for (const entry of node.data.ifaces ?? []) {
      if (entry.iface.kind !== "vlan") continue;
      const key = vlanDomainKey(
        entry.host,
        entry.iface.vlanParent.name,
        entry.iface.vlanId,
      );
      const users = vlanUsers.get(key) ?? new Set<string>();
      users.add(node.key.name);
      vlanUsers.set(key, users);
    }
  }
  const hostsByName = new Map(
    network.allHosts.map((host) => [host.name, host]),
  );
  const sharedDomains = new Map<string, CollisionDomain>();
  for (const [id, node] of Object.entries(nodes)) {
    if (
      node.kind !== "kernel.router" || node.key.side === undefined ||
      node.data.ifaces === undefined
    ) {
      continue;
    }
    let changed = false;
    const ifaces = node.data.ifaces.map((entry) => {
      if (entry.iface.kind !== "vlan") return entry;
      const key = vlanDomainKey(
        entry.host,
        entry.iface.vlanParent.name,
        entry.iface.vlanId,
      );
      if ((vlanUsers.get(key)?.size ?? 0) < 2) return entry;
      changed = true;
      const bridge = transitBridgeName(
        entry.iface.vlanParent.name,
        entry.iface.vlanId,
      );
      const host = hostsByName.get(entry.host);
      if (host === undefined) {
        throw new Error(`unknown host "${entry.host}" for shared WAN bridge`);
      }
      let domain = sharedDomains.get(bridge);
      if (domain === undefined) {
        domain = new CollisionDomain(bridge, { host });
        domain.addInterface({
          kind: "vlan",
          vlanParent: {
            kind: "physical",
            name: entry.iface.vlanParent.name,
          },
          vlanId: entry.iface.vlanId,
          ifaceName: `${entry.iface.vlanParent.name}.${entry.iface.vlanId}`,
        });
        sharedDomains.set(bridge, domain);
      }
      // The WAN leg and the transit leg are two different veth pairs on
      // the same router, so they need different short ids.
      const shortId = vethShortId(`veth-krn-${node.key.name}-wan`);
      const mac = entry.iface.mac ??
        macFromName(`${node.key.name}-${node.key.side}`);
      domain.addInterface({
        kind: "veth",
        ifaceName: `veth-krn-${shortId}`,
        peerName: `veth-ovn-${shortId}`,
        mac,
      }, host);
      return {
        host: entry.host,
        iface: {
          kind: "veth" as const,
          ifaceName: `veth-krn-${shortId}`,
          peerName: `veth-ovn-${shortId}`,
          shortName: shortId,
          mac,
        },
      };
    });
    if (changed) nodes[id] = { ...node, data: { ...node.data, ifaces } };
  }
  for (const domain of sharedDomains.values()) {
    const node = collisionDomainToIR(domain);
    nodes[node.id] = node;
  }

  // Every security group built via net.securityGroup() (define.ts) —
  // including the implicit `masq-<router>` shortcut expansions — becomes
  // one implementation-abstract `security.group` node. The ATTACHMENT
  // (which side/interface carries it) lives on the kernel.router side
  // nodes via `data.securityGroup`, not here.
  for (const group of network.allSecurityGroups) {
    const node = securityGroupToIR(group, addrs);
    nodes[node.id] = node;
  }

  // Resolve any route metric the topology left implicit, so the IR always
  // carries a concrete value (2026-09-19).
  assignFallbackRouteMetrics(nodes);

  // Every reference filled; one `ip.net` node per collision domain.
  for (const node of addrs.finish()) nodes[node.id] = node;

  // Deterministic output order: sorted by node id. The caller (cli.ts's
  // generate-ir) serializes Object.values(nodes) as-is, so insertion
  // order IS the JSON order — sorting here makes the whole IR
  // diff-stable run to run, independent of declaration/registration
  // order. Code-unit compare, deliberately not localeCompare (which is
  // locale-dependent).
  return Object.fromEntries(
    Object.entries(nodes).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  );
}
