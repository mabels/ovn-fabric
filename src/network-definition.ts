// src/network-definition.ts — the value defineNetwork() hands back: the
// FACTS a topology config declared, fully resolved. src/ir.ts walks this
// (allCollisionDomains/allRouters/allHosts/allKernelRouters) to build the
// desired-state IR.

import type { CollisionDomain } from "./types.ts";
import type {
  Host,
  KernelRouter,
  OvnClusterOptions,
  Router,
  RoutingDomain,
  SecurityGroup,
} from "./types.ts";

export interface NetworkDefinition {
  readonly name: string;
  /** Every Host declared via sshHost()/localHost() — not just the ones
   * referenced by a router endpoint. A pure-central chassis (ADR 0003:
   * runs only ovn-central, hosts no router of its own) would otherwise
   * be unreachable from a NetworkDefinition at all. */
  readonly allHosts: readonly Host[];
  /** Every bare collision domain declared via net.collisionDomain()/
   * net.backbone() — L2 only, see CollisionDomain (types.ts). */
  readonly allCollisionDomains: readonly CollisionDomain[];
  /** The one collision domain declared via net.backbone(), if any —
   * OVN's own internal backbone switch (sw-backbone in real captured
   * data), made explicit instead of silently auto-created. */
  readonly backbone?: CollisionDomain;
  /** Every router declared via net.defineOvnRouter() — see Router
   * (types.ts). Includes the sub-routers a tunnelRouterEndpoint creates
   * internally (flattened from Router.subRouters). */
  readonly allRouters: readonly Router[];
  /** Every KernelRouter created by net.kernelRouterEndpoint() (see
   * KernelRouter, types.ts) — never declared directly, always as a side
   * effect of that method. Real Linux netns instances, not OVN
   * Logical_Routers — src/ir.ts's toIR() emits one `kernel.router` node
   * per side. */
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
