// src/graph.ts — the topology as a graph to look at (serve-graph, cli.ts).
//
// Built from the NetworkDefinition and the routes toIR() computes, by
// WHITELIST: names, addresses, routes and interface names only. Nothing
// else of an IR node is copied, so no key, identity or token can reach the
// page (a tunnel's config, a workload's env and files stay out).
//
// A router is boxed together with what only it serves: the segments no
// other router is on, the workloads there, and — for a kernel router —
// its namespace, the apps in it and the transit segments between them.

import type { NetworkDefinition } from "./define.ts";
import { toIR } from "./ir.ts";
import type {
  CollisionDomain,
  Host,
  InterfaceKind,
  KernelApp,
  KernelRouter,
  OvnRouterEndpoint,
} from "./types.ts";

export type GraphNodeKind =
  | "segment"
  | "router"
  | "netns"
  | "app"
  | "workload"
  | "interface"
  | "host";

export interface GraphRoute {
  readonly dst: string;
  readonly via: string;
  /** The routing domain it came from (an OVN router's routes). */
  readonly domain?: string;
  readonly metric?: number;
}

export interface GraphLeg {
  /** The segment (or interface) the leg is on. */
  readonly on: string;
  readonly addrs: readonly string[];
  readonly name?: string;
}

export interface GraphNode {
  readonly id: string;
  readonly kind: GraphNodeKind;
  readonly name: string;
  readonly legs: readonly GraphLeg[];
  readonly routes: readonly GraphRoute[];
  /** Where it runs, the interfaces it is bound to — plain words. */
  readonly notes: readonly string[];
  /** Its security groups' rules (masq, snat, filters), one per line. */
  readonly rules: readonly string[];
  /** The box it is drawn in (Graph.groups), if any. */
  readonly group?: string;
}

/** `link`: a leg on a segment; `binding`: a segment or leg on a host's
 * interface; `host`: an interface of a host; `runs`: an app in a netns. */
export type GraphEdgeRole = "link" | "binding" | "host" | "runs";

export interface GraphEdge {
  readonly id: string;
  readonly source: string;
  readonly target: string;
  readonly role: GraphEdgeRole;
  /** The addresses of the source on the target. */
  readonly addrs: readonly string[];
}

export interface GraphGroup {
  readonly id: string;
  readonly name: string;
}

export interface Graph {
  readonly name: string;
  readonly nodes: readonly GraphNode[];
  readonly edges: readonly GraphEdge[];
  readonly groups: readonly GraphGroup[];
}

type Mutable<T> = { -readonly [K in keyof T]: T[K] };
type Node = Mutable<GraphNode> & {
  legs: GraphLeg[];
  notes: string[];
  rules: string[];
};

interface IrRule {
  kind: string;
  family?: string;
  externalIp?: string;
  logicalIps?: string[];
  gatewayPort?: string;
  direction?: string;
  action?: string;
  sources?: string[];
  excludeSources?: string[];
  destinations?: string[];
  protocol?: string;
  ports?: { from: number; to: number }[];
  priority?: number;
}

/** A security-group rule in words (the IR's resolved form). */
function describeRule(rule: IrRule, out?: string): string {
  const where = out ? ` out ${out}` : "";
  switch (rule.kind) {
    case "masq":
      return `masq ${rule.family}${where}`;
    case "snat":
      return `snat ${(rule.logicalIps ?? []).join(" ")} → ${rule.externalIp}` +
        (rule.gatewayPort ? ` (port ${rule.gatewayPort})` : "");
    case "filter": {
      const from = rule.excludeSources?.length
        ? `not ${rule.excludeSources.join(" ")}`
        : (rule.sources ?? []).join(" ") || "any";
      const to = (rule.destinations ?? []).join(" ") || "any";
      const ports = (rule.ports ?? []).map((p) =>
        p.from === p.to ? `${p.from}` : `${p.from}-${p.to}`
      ).join(",");
      return `${rule.family} ${rule.direction} ${rule.action} ${from} → ${to}` +
        (rule.protocol ? ` ${rule.protocol}${ports ? `/${ports}` : ""}` : "") +
        (rule.priority !== undefined ? ` prio ${rule.priority}` : "");
    }
    default:
      return rule.kind;
  }
}

const segmentId = (domain: CollisionDomain | string): string =>
  `segment:${typeof domain === "string" ? domain : domain.name}`;

/** An interface in words: never more than its kind and names. */
export function describeIface(iface: InterfaceKind): string {
  switch (iface.kind) {
    case "vlan": {
      const parent = iface.vlanParent as { name?: string; kind?: string };
      return `vlan ${iface.vlanId} on ${parent.name ?? parent.kind ?? "?"}`;
    }
    case "physical":
      return `physical ${iface.name}`;
    case "xdp":
      return `xdp ${(iface as { name?: string }).name ?? ""}`.trim();
    case "veth":
      return `veth ${iface.ifaceName}`;
    default:
      return iface.kind;
  }
}

function describeHost(host: Host): string[] {
  return [
    host.access.method === "ssh"
      ? `ssh ${host.access.user}@${host.connectAddress}`
      : "local",
    ...(host.os ? [`${host.os.name} ${host.os.version}`] : []),
  ];
}

/** An app in words: what runs, never its config. */
function describeApp(app: KernelApp): { name: string; notes: string[] } {
  switch (app.kind) {
    case "wireguard":
      return { name: `wg-quick ${app.ifaceName}`, notes: ["wireguard"] };
    case "zerotier":
      return { name: "zerotier-one", notes: ["zerotier"] };
    case "dhcp-client":
      return { name: app.style, notes: ["dhcp client"] };
    case "docker":
      return {
        name: `docker ${app.name}`,
        notes: [
          `image ${app.image}`,
          ...(app.ip ? [`its own netns, ${app.ip}`] : ["owns the netns"]),
        ],
      };
  }
}

/** The namespace a kernel router lives in: a container's, when a docker app
 * without an address owns its interfaces (deployer/ir_common.py's
 * _owning_app), else its own `ns-<name>`. */
function netnsName(kr: KernelRouter): string {
  for (const app of [...(kr.left.apps ?? []), ...(kr.right.apps ?? [])]) {
    if (app.kind === "docker" && !app.ip) {
      return app.name ?? `${kr.name}-docker`;
    }
  }
  return `ns-${kr.name}`;
}

const addrs = (list: readonly { to_string(): string }[]): string[] =>
  list.map((a) => a.to_string());

export function buildGraph(net: NetworkDefinition): Graph {
  const nodes = new Map<string, Node>();
  const edges: GraphEdge[] = [];
  const edge = (
    source: string,
    target: string,
    role: GraphEdgeRole,
    list: readonly string[] = [],
  ) => {
    const id = `${source}->${target}`;
    if (!edges.some((e) => e.id === id)) {
      edges.push({ id, source, target, role, addrs: list });
    }
  };
  const add = (
    node: Omit<Node, "legs" | "routes" | "notes" | "rules"> & Partial<Node>,
  ) => {
    if (!nodes.has(node.id)) {
      nodes.set(node.id, {
        legs: [],
        routes: [],
        notes: [],
        rules: [],
        ...node,
      });
    }
    return nodes.get(node.id)!;
  };
  const segment = (domain: CollisionDomain | string) =>
    add({
      id: segmentId(domain),
      kind: "segment",
      name: typeof domain === "string" ? domain : domain.name,
    });

  for (const host of net.allHosts) {
    add({
      id: `host:${host.name}`,
      kind: "host",
      name: host.name,
      notes: describeHost(host),
    });
  }
  /** A host's interface; its edge to the host. */
  const iface = (host: Host, kind: InterfaceKind) => {
    const id = `interface:${host.name}|${describeIface(kind)}`;
    add({
      id,
      kind: "interface",
      name: describeIface(kind),
      notes: [`on ${host.name}`],
    });
    edge(`host:${host.name}`, id, "host");
    return id;
  };

  for (const domain of net.allCollisionDomains) {
    const node = segment(domain);
    if (domain.host) node.notes.push(`only on ${domain.host.name}`);
  }

  // The routes toIR() computes — only these fields.
  const routesOf = new Map<string, GraphRoute[]>();
  const kernelRoutes = new Map<string, GraphRoute[]>();
  // Security groups: their rules, and who refers to them (out of where).
  const groupRules = new Map<string, { rules: IrRule[]; defaults: string[] }>();
  const groupRefs: { node: string; group: string; out?: string }[] = [];
  const refNames = (value: unknown): string[] =>
    ((value ?? []) as { name: string }[]).map((g) => String(g.name));
  for (const irNode of Object.values(toIR(net))) {
    const n = irNode as {
      kind: string;
      key: Record<string, unknown>;
      data: Record<string, unknown>;
    };
    if (n.kind === "ipv4.route" || n.kind === "ipv6.route") {
      const router = String(n.key["ovnrouter"]);
      const list = routesOf.get(router) ?? [];
      list.push({
        dst: String(n.key["prefix"]),
        via: String(n.data["nexthop"]),
        domain: String(n.data["domain"]),
        metric: Number(n.data["metric"]),
      });
      routesOf.set(router, list);
    } else if (n.kind === "security.group") {
      const defaults = (n.data["default"] ?? {}) as Record<string, string>;
      groupRules.set(String(n.key["name"]), {
        rules: (n.data["rules"] ?? []) as IrRule[],
        defaults: Object.entries(defaults).map(([dir, action]) =>
          `default ${dir} ${action}`
        ),
      });
    } else if (n.kind === "ovn.lr") {
      for (const group of refNames(n.data["securityGroups"])) {
        groupRefs.push({ node: `router:${String(n.key["name"])}`, group });
      }
    } else if (n.kind === "kernel.router" && n.key["side"]) {
      const node = `netns:${String(n.key["name"])}`;
      // Only the group names and the interface they go out of — an app's
      // config (keys) is not read.
      const ifaces = (n.data["ifaces"] ?? []) as {
        iface: { kind: string; shortName?: string; name?: string };
      }[];
      const sideIface = ifaces[0]?.iface;
      const sideOut = sideIface?.kind === "veth"
        ? `veth-krn-${sideIface.shortName}`
        : sideIface?.name;
      for (const group of refNames(n.data["securityGroups"])) {
        groupRefs.push({ node, group, out: sideOut });
      }
      for (const app of (n.data["apps"] ?? []) as Record<string, unknown>[]) {
        const tunnel = app["tunnel"] as { iface?: string } | undefined;
        const out = typeof app["ifaceName"] === "string"
          ? app["ifaceName"]
          : tunnel?.iface ?? `${String(app["kind"])} device`;
        for (const group of refNames(app["securityGroups"])) {
          groupRefs.push({ node, group, out });
        }
      }
      const routes = (n.data["routes"] ?? []) as { dst: string; via: string }[];
      const id = `netns:${String(n.key["name"])}`;
      const list = kernelRoutes.get(id) ?? [];
      for (const r of routes) {
        list.push({ dst: String(r.dst), via: String(r.via) });
      }
      kernelRoutes.set(id, list);
    }
  }

  const workloads = new Map<string, { segments: Set<string>; image: string }>();
  for (const router of net.allRouters) {
    const id = `router:${router.name}`;
    const node = add({ id, kind: "router", name: router.name });
    for (const endpoint of router.endpoints as readonly OvnRouterEndpoint[]) {
      segment(endpoint.l2Segment);
      const list = addrs(endpoint.ipaddrs);
      node.legs.push({
        on: endpoint.l2Segment.name,
        addrs: list,
        name: endpoint.name,
      });
      edge(id, segmentId(endpoint.l2Segment), "link", list);
      if (endpoint.gatewayChassis) {
        node.notes.push(
          `${endpoint.l2Segment.name}: gateway on ${endpoint.gatewayChassis.name}`,
        );
      }
      // A segment reaches the wire through a host's interface (localnet).
      for (const hi of endpoint.ifaces ?? []) {
        if (hi.iface.kind === "veth") continue;
        edge(
          segmentId(endpoint.l2Segment),
          iface(hi.host, hi.iface),
          "binding",
        );
      }
      for (const service of endpoint.services ?? []) {
        if (service.kind !== "service.attach") continue;
        const name = service.srvRef.name;
        const wid = `workload:${name}`;
        const workload = workloads.get(name) ??
          { segments: new Set<string>(), image: service.srvRef.image };
        workload.segments.add(segmentId(endpoint.l2Segment));
        workloads.set(name, workload);
        const w = add({ id: wid, kind: "workload", name: `docker ${name}` });
        const nic = addrs(service.ipaddrs);
        w.legs.push({ on: endpoint.l2Segment.name, addrs: nic });
        edge(wid, segmentId(endpoint.l2Segment), "link", nic);
      }
    }
    node.routes = (routesOf.get(router.name) ?? []).sort((a, b) =>
      a.dst.localeCompare(b.dst)
    );
  }
  for (const [name, workload] of workloads) {
    nodes.get(`workload:${name}`)!.notes.push(
      `image ${workload.image}`,
      "its own netns",
    );
  }

  // A kernel router is a namespace: its left on the transit segment, its
  // right on the segment its veth is plugged into, or on a host interface.
  const vethSegment = new Map<string, string>();
  for (const domain of net.allCollisionDomains) {
    for (const port of domain.allPorts) {
      const kind = (port as { iface?: InterfaceKind }).iface;
      if (kind?.kind === "veth") {
        vethSegment.set(kind.ifaceName, domain.name);
        vethSegment.set(kind.peerName, domain.name);
      }
    }
  }
  const pairs: [string, string][] = []; // a kernel router and its own segments
  for (const kr of net.allKernelRouters as readonly KernelRouter[]) {
    const id = `netns:${kr.name}`;
    const node = add({
      id,
      kind: "netns",
      name: `netns ${netnsName(kr)}`,
      notes: [`kernel router ${kr.name}`, `on ${kr.host.name}`],
    });
    edge(`host:${kr.host.name}`, id, "host");
    segment(kr.transitDomain);
    const left = addrs(kr.left.ipaddrs);
    node.legs.push({ on: kr.transitDomain.name, addrs: left, name: "left" });
    edge(id, segmentId(kr.transitDomain), "link", left);
    pairs.push([id, segmentId(kr.transitDomain)]);
    const right = addrs(kr.right.ipaddrs);
    for (const hi of kr.right.ifaces ?? []) {
      const on = hi.iface.kind === "veth"
        ? vethSegment.get(hi.iface.ifaceName)
        : undefined;
      if (on !== undefined) {
        node.legs.push({ on, addrs: right, name: "right" });
        edge(id, segmentId(on), "link", right);
        pairs.push([id, segmentId(on)]);
        continue;
      }
      node.legs.push({
        on: describeIface(hi.iface),
        addrs: right,
        name: "right",
      });
      edge(id, iface(hi.host, hi.iface), "binding", right);
    }
    for (const app of [...(kr.left.apps ?? []), ...(kr.right.apps ?? [])]) {
      const described = describeApp(app);
      const appId = `app:${kr.name}|${described.name}`;
      add({
        id: appId,
        kind: "app",
        name: described.name,
        notes: described.notes,
      });
      edge(appId, id, "runs");
    }
    node.routes = [
      ...(kr.right.routes ?? []).map((r) => ({
        dst: r.dst.to_string(),
        via: r.via ? r.via.to_s() : "(the side's own link)",
      })),
      ...(kernelRoutes.get(id) ?? []),
    ].filter((r, i, all) =>
      all.findIndex((o) => o.dst === r.dst && o.via === r.via) === i
    );
  }

  for (const ref of groupRefs) {
    const node = nodes.get(ref.node);
    const group = groupRules.get(ref.group);
    if (node === undefined || group === undefined) continue;
    for (const rule of group.rules) {
      node.rules.push(`${ref.group}: ${describeRule(rule, ref.out)}`);
    }
    for (const line of group.defaults) node.rules.push(`${ref.group}: ${line}`);
  }

  const groups = groupNodes(nodes, edges, pairs);
  return {
    name: net.name,
    nodes: [...nodes.values()],
    edges,
    groups,
  };
}

/** The boxes: a router with the segments only it is on, the workloads and
 * interfaces there; a kernel router's transit segments join it with the
 * routers on their other end, its apps join it. Hosts stay outside. */
function groupNodes(
  nodes: Map<string, Node>,
  edges: readonly GraphEdge[],
  pairs: readonly [string, string][],
): GraphGroup[] {
  const parent = new Map<string, string>();
  const find = (id: string): string => {
    const up = parent.get(id) ?? id;
    if (up === id) return id;
    const root = find(up);
    parent.set(id, root);
    return root;
  };
  const union = (a: string, b: string) => {
    const ra = find(a), rb = find(b);
    if (ra !== rb) parent.set(rb, ra);
  };

  const attached = new Map<string, Set<string>>(); // segment → routers, netns
  for (const e of edges) {
    if (e.role !== "link") continue;
    const kind = nodes.get(e.source)?.kind;
    if (kind !== "router" && kind !== "netns") continue;
    const set = attached.get(e.target) ?? new Set<string>();
    set.add(e.source);
    attached.set(e.target, set);
  }
  // A segment only one router is on.
  for (const [seg, set] of attached) {
    if (set.size === 1) union([...set][0]!, seg);
  }
  // A kernel router's transit segments: everything on them is one thing.
  for (const [netns, seg] of pairs) {
    for (const other of attached.get(seg) ?? []) union(netns, other);
    union(netns, seg);
  }
  // Apps with their namespace; workloads and interfaces with their segment,
  // when that is boxed and they are on nothing else.
  const grouped = (id: string) => {
    const root = find(id);
    return [...nodes.keys()].some((other) =>
      other !== id && find(other) === root
    );
  };
  for (const e of edges) {
    if (e.role === "runs") union(e.target, e.source);
  }
  const neighbours = (id: string, role: GraphEdgeRole) =>
    edges.filter((e) => e.role === role && (e.source === id || e.target === id))
      .map((e) => (e.source === id ? e.target : e.source));
  for (const node of nodes.values()) {
    const around = node.kind === "workload"
      ? neighbours(node.id, "link")
      : node.kind === "interface"
      ? neighbours(node.id, "binding")
      : [];
    if (around.length === 0) continue;
    const roots = new Set(around.map(find));
    if (roots.size === 1 && grouped(around[0]!)) union(around[0]!, node.id);
  }

  const members = new Map<string, Node[]>();
  for (const node of nodes.values()) {
    if (node.kind === "host") continue;
    const list = members.get(find(node.id)) ?? [];
    list.push(node);
    members.set(find(node.id), list);
  }
  const out: GraphGroup[] = [];
  for (const [root, list] of members) {
    if (list.length < 2) continue;
    // Named after its router: the one that is not a tunnel's -upstream.
    const routers = list.filter((n) => n.kind === "router").map((n) => n.name);
    const name = routers.find((n) => !n.endsWith("-upstream")) ?? routers[0] ??
      list.find((n) => n.kind === "netns")?.name ?? list[0]!.name;
    const id = `group:${root}`;
    out.push({ id, name });
    for (const node of list) node.group = id;
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}
