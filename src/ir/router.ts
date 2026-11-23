// src/ir/router.ts — ovn.lr / ovn.lrp: OVN logical routers, their SNAT groups
// and their ports.

import type { NetworkDefinition } from "../define.ts";
import { IPv4, IPv6 } from "../ip.ts";
import { selectors } from "../ipnet.ts";
import type { AddressIndex } from "../ir-addresses.ts";
import type { Ipv6RaConfigs, OvnLrNode, OvnLrpNode } from "../protocol.ts";
import type {
  FilterRule,
  OvnRouterEndpoint,
  OvnSnatRule,
  Router,
  RouterEndpointService,
  SecurityGroup,
} from "../types.ts";
import { lsId } from "./names.ts";
import {
  type PlatformControlPlaneResolver,
  resolveServiceRefs,
} from "./workloads.ts";

// ── ovn.lr ─────────────────────────────────────────────────────────
// The Logical_Router itself — one node per Router, carrying its own OVN
// options as the FINAL Logical_Router.options (protocol.ts's
// OvnLrOptions — the field names are OVN's own keys). Router.options is already
// fully resolved (defaults filled in at defineOvnRouter() time,
// types.ts's resolveOvnRouterOptions), so this is pure serialization —
// same "the IR carries the final fact" boundary as resolveMac /
// resolveIpv6RaConfigs below. The deployer only applies each pair.
// OVN SNAT: `{kind:"ovn.ipv4.masq"}` / `ovn.ipv6.masq` on an endpoint means
// "masquerade this router's OTHER endpoint subnets to THIS endpoint's own
// address of that family". TypeScript renders the concrete `snat` rules
// into a security.group; the IR stores the group + one LR reference.
export function computeOvnSecurityGroups(network: NetworkDefinition): {
  byRouter: Map<string, SecurityGroup<"ovn">>;
} {
  const byRouter = new Map<string, SecurityGroup<"ovn">>();
  for (const router of network.allRouters) {
    const rules: (OvnSnatRule | FilterRule)[] = [];
    const name = `masq-${router.name}-ovn`;
    for (const endpoint of router.endpoints) {
      for (const service of endpoint.services ?? []) {
        const denyInput = service.kind === "ovn.ipv4.block-ingress" ||
          service.kind === "ovn.ipv6.block-ingress";
        if (denyInput) {
          const isV4 = service.kind === "ovn.ipv4.block-ingress";
          const protectedIps = new Set<string>();
          const externalNet = endpoint.ipaddrs.find((a) => a.is_ipv4() === isV4)
            ?.network().to_string();
          for (const other of router.endpoints) {
            if (other.name === endpoint.name) continue;
            for (const addr of other.ipaddrs) {
              if (addr.is_ipv4() !== isV4) continue;
              const net = addr.network().to_string();
              if (net !== externalNet) protectedIps.add(net);
            }
          }
          if (protectedIps.size === 0) continue;
          const destinations = [...protectedIps].map((net) =>
            isV4 ? IPv4.parse(net) : IPv6.parse(net)
          );
          // Every rule is stateful: an explicit outbound allow commits
          // the control plane's own connections so their replies are
          // ESTABLISHED. The inbound deny then only has to drop new
          // connections; established replies hit the group's global
          // conntrack allow.
          rules.push({
            kind: "filter",
            direction: "output",
            action: "allow",
            priority: 200,
          });
          for (const allow of service.allow ?? []) {
            const sources = selectors(allow.sources).filter(
              (a) => a.is_ipv4() === isV4,
            );
            rules.push({
              kind: "filter",
              direction: "input",
              action: "allow",
              ...(sources.length > 0 ? { sources } : {}),
              destinations,
              ...(allow.protocol ? { protocol: allow.protocol } : {}),
              ...(allow.ports
                ? { ports: allow.ports.map((p) => ({ ...p })) }
                : {}),
              priority: 200,
            });
          }
          rules.push({
            kind: "filter",
            direction: "input",
            action: "deny",
            excludeSources: destinations,
            destinations,
            priority: 100,
          });
          continue;
        }
        if (
          service.kind !== "ovn.ipv4.masq" && service.kind !== "ovn.ipv6.masq"
        ) continue;
        const isV4 = service.kind === "ovn.ipv4.masq";
        const external = endpoint.ipaddrs.find((a) => a.is_ipv4() === isV4);
        if (external === undefined) {
          throw new Error(
            `router "${router.name}" endpoint "${endpoint.name}": ` +
              `${service.kind} needs a local IPv${isV4 ? 4 : 6} address ` +
              "to use as the SNAT external IP",
          );
        }
        const externalNet = external.network().to_string();
        const logicalIps = new Set<string>();
        for (const other of router.endpoints) {
          if (other.name === endpoint.name) continue;
          for (const addr of other.ipaddrs) {
            if (addr.is_ipv4() !== isV4) continue;
            const logical = addr.network().to_string();
            if (logical !== externalNet) logicalIps.add(logical);
          }
        }
        const logicals = service.sources
          ? service.sources.map((a) => a.to_string())
          : [...logicalIps];
        if (logicals.length === 0) continue;
        rules.push({
          kind: "snat",
          family: isV4 ? "ipv4" : "ipv6",
          externalIp: external,
          logicalIps: logicals.map((s) => isV4 ? IPv4.parse(s) : IPv6.parse(s)),
          ...(endpoint.gatewayChassis ? { gatewayPort: endpoint.name } : {}),
        });
      }
    }
    if (rules.length > 0) {
      byRouter.set(router.name, { name, render: "ovn", rules });
    }
  }
  return { byRouter };
}

export function routerToIR(
  router: Router,
  securityGroups: readonly { render: "kernel" | "ovn"; name: string }[] = [],
): OvnLrNode {
  return {
    id: `ovnrouter:${router.name}`,
    kind: "ovn.lr",
    key: { name: router.name },
    data: {
      options: {
        mac_binding_age_threshold: router.options.macBindingAgeThreshold,
      },
      ...(securityGroups.length > 0
        ? {
          securityGroups: securityGroups.map((g) => ({
            render: g.render,
            name: g.name,
          })),
        }
        : {}),
    },
  };
}

// ── ovn.lrp ────────────────────────────────────────────────────────
// Matches ADR 0002's existing ovn.lrp kind exactly (already produced
// real, on the reconciler side, from real Logical_Router_Port data —
// see reconciler/ovn/reconcile.py) — this is the DESIRED-state half of
// the same fact. Key extends the ADR's `ovnrouter:<scope>|lrp` with the
// endpoint's own `name` (EndpointBase.name, types.ts) — the SAME identity
// the reconciler keys a live port by (`ovnrouter:<router>|lrp:<port
// name>`), which is what lets the two sides be merged at all. `name` is
// derived from configuration (or pinned by the author), never positional,
// so reordering endpoints cannot re-key a port.
// The MAC: only an explicit RouterEndpoint.mac reaches the IR (ADR 0011
// §5). Otherwise the runtime folds it from the resolved IPv4 — TypeScript
// does not know an allocated address's number.
function resolveMac(endpoint: OvnRouterEndpoint): string | undefined {
  // Only an explicit one (ADR 0011 §5): a derived MAC is the fold of the
  // resolved IPv4, which only the runtime knows.
  return endpoint.mac;
}

// Folds RouterEndpoint.services into the single ipv6_ra_configs smap
// OVN actually has (see RouterEndpointService, types.ts, for why these
// are two independently-toggleable services rather than one boolean).
// Resolved HERE, not in fabric/emit.py, same boundary reasoning
// as resolveMac/resolveEncapIp above: the IR carries the FINAL key/value
// pairs to set, not "here's a services list, go figure out the ovn-nbctl
// options syntax." Undefined (not an empty object) when no service is
// declared, so the deployer can tell "nothing to set" apart from "set
// zero keys" without inspecting emptiness itself.
// A RouterEndpointService is a kernel-side one (`kernel.*`) iff its
// `kind` says so — used as a type predicate so the exhaustive-throw
// branch below can narrow the union all the way to `never` (a plain
// `kind.startsWith()` can't).
function isKernelService(
  service: RouterEndpointService,
): service is Extract<
  RouterEndpointService,
  { readonly kind: `kernel.${string}` }
> {
  return service.kind.startsWith("kernel.");
}

function resolveIpv6RaConfigs(
  services:
    | readonly RouterEndpointService[]
    | undefined,
): Ipv6RaConfigs | undefined {
  if (!services || services.length === 0) return undefined;
  const configs: {
    -readonly [Key in keyof Ipv6RaConfigs]: Ipv6RaConfigs[Key];
  } = {};
  for (const service of services) {
    // A service ATTACHMENT (endpoint.attachTo) is a workload NIC, not an
    // LRP config — a workload attachment is emitted as a serviceRef, skip here.
    if (service.kind === "service.attach") continue;
    if (service.kind === "ipv6.slaac") {
      configs.address_mode = "slaac";
      continue;
    }
    if (service.kind === "ipv6.ra") {
      configs.send_periodic = "true";
      if (service.minInterval) {
        configs.min_interval = String(service.minInterval);
      }
      if (service.maxInterval) {
        configs.max_interval = String(service.maxInterval);
      }
      continue;
    }
    // Exhaustive on purpose, not "anything that isn't slaac must be
    // ra" — confirmed live, 2026-08-12: a services entry with a kind
    // outside the OVN `ipv6.*` pair silently got treated as "ipv6.ra"
    // and set send_periodic=true on an endpoint that never asked for
    // it — a real runtime catch beats silent misbehavior.
    // A `kernel.*` service reaching an OVN endpoint is now LEGITIMATE: a
    // `kernel.app.docker` listed on a plain ovnRouterEndpoint's services[]
    // is a container the leg's host exposes on that segment (emitted as a
    // kernel.app.container node, see resolveServiceRefs/serviceNodes) — it produces
    // no RA config here, so skip it. Kernel-router services are otherwise
    // still split off in buildKernelRouterEndpoint before the OVN endpoint
    // is built.
    if (isKernelService(service)) {
      continue;
    }
    if (
      service.kind === "ovn.ipv4.masq" || service.kind === "ovn.ipv6.masq" ||
      service.kind === "ovn.ipv4.block-ingress" ||
      service.kind === "ovn.ipv6.block-ingress"
    ) {
      continue;
    }
    // Config-side workload shortcuts (wireguard/zerotier) are consumed by
    // buildTunnelRouterEndpoint and never reach an OVN endpoint's services
    // (they'd be a config error if they did) — skip so the exhaustiveness
    // check below stays meaningful (2026-09-08).
    if (
      service.kind === "wireguard" || service.kind === "zerotier" ||
      service.kind === "tailscale"
    ) {
      continue;
    }
    const unknownKind: never = service;
    throw new Error(
      `unknown RouterEndpointService kind: ${JSON.stringify(unknownKind)}`,
    );
  }
  return configs;
}

export function routerEndpointToIR(
  router: Router,
  endpoint: OvnRouterEndpoint,
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
  addrs: AddressIndex,
  platformCp?: PlatformControlPlaneResolver,
): OvnLrpNode {
  const scope = `ovnrouter:${router.name}`;
  const id = `${scope}|lrp:${endpoint.name}`;
  return {
    id,
    kind: "ovn.lrp",
    key: { ovnrouter: router.name, name: endpoint.name },
    data: {
      // `ls:<name>`/`host:<name>` — the referenced ovn.ls/infra.host
      // node's own id (hostToIR/collisionDomainToIR above), not the
      // bare name, so every cross-node reference in the IR is
      // consistently an id, not a mix of ids and bare strings.
      l2Segment: `ls:${endpoint.l2Segment.name}`,
      addresses: addrs.own(endpoint.ipaddrs, lsId(endpoint.l2Segment), id),
      mac: resolveMac(endpoint),
      gatewayChassis: endpoint.gatewayChassis
        ? `host:${endpoint.gatewayChassis.name}`
        : undefined,
      ipv6RaConfigs: resolveIpv6RaConfigs(endpoint.services),
      serviceRefs: resolveServiceRefs(
        router,
        endpoint,
        dstsByDomain,
        addrs,
        platformCp,
      ),
    },
  };
}
