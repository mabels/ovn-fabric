// src/ir.ts — toIR(NetworkDefinition): the DESIRED-state half of the IR
// this project has been building toward since ADR 0002 (docs/adr/
// 0002-intermediate-representation.md). The reconciler package (Python,
// this repo's sibling `reconciler`/`ladops` work) already produces this
// exact {id, kind, key, data} envelope shape from LIVE router state —
// this is the same shape, produced from topology.ts's DECLARED state
// instead, so the two sides can eventually be diffed against each
// other (ADR 0002 § Decision, point 5 — a plain function over the
// union of both key sets, not an "engine").
//
// Plain TypeScript objects, not ArkType schemas: the reconciler side
// never needed schema validation either (ladops/reconciler are all
// plain dicts, no runtime validation library at all) — this data is
// internal, produced by this project's own generator, not adversarial
// input. ArkType (ADR 0002 § Decision, point 4; § "Type stability
// across the TypeScript/Python boundary") is a real, separate follow-up
// for when cross-language type generation is actually needed, not a
// prerequisite for toIR() to exist and be useful.
//
// Scope, deliberately: only what the CURRENT primitives (CollisionDomain,
// Router/RouterEndpoint, Host — see types.ts) can produce a DESIRED-state
// fact for. Kernel-side NAT is covered as of 2026-08-22: a kernel
// router's `kernel.*.masq` services (RouterEndpointService, types.ts)
// become an IMPLEMENTATION-ABSTRACT `security.group` node
// (securityGroupToIR below) carrying the POSTROUTING MASQUERADE rules
// for its real-world-facing interface. Real-interface discovery
// (DHCP/static) still has no home in this shape yet — not because it's
// forgotten, but because there's nothing yet to extract it FROM. Static
// routes (RoutingDomain, see computeRoutes/computeInterconnectRoutes
// below) and IPv6 RA/SLAAC (see RouterEndpointService, types.ts,
// resolved below) both ARE covered. toIR() walks allCollisionDomains/
// allRouters/allHosts.

import { fnv1a32, macFromAddrsOrName, macFromV4 } from "./addressing.ts";
import type { IPv4, IPv6 } from "./ip.ts";
import {
  CollisionDomain,
  type Host,
  type InterfaceKind,
  type InterfaceParent,
  type KernelRouter,
  type OvnRouterEndpoint,
  type Resolver,
  type Router,
  type RouterEndpointService,
  type RoutingDomain,
  type SecurityGroup,
  type Service,
  type XdpIface,
  type XdpMode,
} from "./types.ts";
import type { NetworkDefinition } from "./define.ts";
import type {
  BridgeNode,
  InfraHostNode,
  Ipv6RaConfigs,
  IRNode,
  KernelAppContainerNode,
  KernelRouterNode,
  OvnLrNode,
  OvnLrpNode,
  OvnLsInterface,
  OvnLsNode,
  OvnNatRule,
  RouteNode,
  SecurityGroupNode,
  ServiceRef,
} from "./protocol.ts";

// The IR node types are the ones INFERRED from the protocol schema
// (src/protocol.ts) — not a loose `{key, data: Record<string, unknown>}`
// envelope: every builder below declares the exact node type it returns,
// so emitting something the protocol doesn't declare fails to compile.
export type { IRNode } from "./protocol.ts";

// The platform's control-plane leg + resolved global resolver — what a
// workload attachment on that domain inherits when it declares none
// (2026-09-19).
type PlatformControlPlaneResolver = {
  readonly collisionDomain: CollisionDomain;
  readonly resolver?: Resolver;
};

// RouterEndpoint.ipaddrs is a flat (IPv4 | IPv6)[], one entry per
// address (see RouterEndpoint, types.ts). to_string() (not to_s()) is
// the one that includes the prefix length.
function addrStrings(ipaddrs: OvnRouterEndpoint["ipaddrs"]): string[] {
  return ipaddrs.map((addr) => addr.to_string());
}

// ── infra.host ───────────────────────────────────────────────────────
// Matches the reconciler side's own infra.host kind (reconciler/host/
// reconcile.py) — but NOT the same data shape. The live side reports
// OBSERVED facts (uname -a, a reconcile timestamp) that have no
// "desired" analog — you can't declare what `uname -a` should say. The
// desired side instead reports the one thing that genuinely IS a
// declared intent: whether this host should run ovn-central or be a
// plain chassis (see OvnHostConfig, types.ts). A diff between the two
// sides' infra.host nodes is therefore only ever comparing key
// presence (does this host exist at all), never comparing `data`
// field-for-field — the two `data` shapes are deliberately incomparable.
// encapIp resolution matches OvnHostConfig.encapIp's own documented
// default (types.ts): explicit override, else address.ipv4, else
// address.ipv6 — never address.fqdn (the SB DB's Chassis.encaps.ip
// column is a real address, not a hostname, confirmed live 2026-08-09).
// undefined only when a host declares nothing but an fqdn — the
// shell-conversion step is what actually needs this to exist, so it's
// left optional here rather than throwing at IR-build time.
function resolveEncapIp(host: Host): string | undefined {
  return (host.ovn?.encapIp ?? host.address.ipv4 ?? host.address.ipv6)?.to_s();
}

// The ABSTRACT dependencies this Host needs (2026-08-23), derived from
// what the topology puts on it — ABSTRACT names, deliberately not
// distro package names, so the IR stays OS-agnostic: every host gets the
// base tooling (`arp`/net-tools, `ping`, `traceroute`, `curl`,
// `python`/python-is-python3 — the
// everyday debugging commands, and curl is also what the zerotier
// installer needs), an OVN-cluster host needs `ovn`+`ovs`, any kernel
// router's netns needs `ip`+`iptables`, and the apps need their own
// (dhclient/dhcpcd, wireguard, zerotier+jq, docker). `jq` is not
// optional for zerotier: the generated wire script parses
// `zerotier-cli -j listnetworks` with it to learn the mesh device name
// (deployer/ir_common.py's _zerotier_wire_script), and without it that
// script dies with "jq: not found" and the mesh device is never
// configured (confirmed live, 2026-10-20). The deployer maps each
// abstract dependency to the host OS's concrete package/install form;
// there is deliberately NO deinstall.
function hostDependencies(host: Host, network: NetworkDefinition): string[] {
  const deps = new Set<string>(["arp", "ping", "traceroute", "curl", "python"]);
  if (host.ovn) {
    deps.add("ovn");
    deps.add("ovs");
  }
  const kernelRouters = network.allKernelRouters.filter(
    (kr) => kr.host.name === host.name,
  );
  if (kernelRouters.length > 0) {
    deps.add("ip");
    deps.add("iptables");
    for (const kr of kernelRouters) {
      for (const side of [kr.left, kr.right]) {
        for (const app of side.apps ?? []) {
          if (app.kind === "dhcp-client") {
            deps.add(app.style);
          } else if (app.kind === "wireguard") {
            deps.add("wireguard");
          } else if (app.kind === "zerotier") {
            deps.add("zerotier");
            deps.add("jq");
          } else if (app.kind === "docker") {
            deps.add("docker");
          }
        }
      }
    }
  }
  // Host-level docker workloads (`net.service` -> kernel.app.container)
  // are containers too: the host needs the docker engine even when it
  // has no kernel router apps.
  for (const service of serviceNodes(network)) {
    if (service.data.host === `host:${host.name}`) {
      deps.add("docker");
    }
  }
  return [...deps];
}

function hostToIR(
  host: Host,
  dependencies: readonly string[],
): InfraHostNode {
  const id = `host:${host.name}`;
  const ipfix = host.monitoring?.ipfix;
  return {
    id,
    kind: "infra.host",
    key: { host: host.name },
    data: {
      connectAddress: host.connectAddress,
      ovnRole: host.ovn?.role.kind,
      encapIp: resolveEncapIp(host),
      // OS always resolved — when the config leaves it unset, assume
      // Ubuntu (2026-08-23), so the deployer always knows the install
      // form; `...host.os` lets a partial override win field by field.
      os: { name: "ubuntu", version: "26.04", ...host.os },
      dependencies: dependencies.length > 0 ? dependencies : undefined,
      // WHERE to export flows (types.ts IpfixExport -> protocol IpfixData)
      // — the per-leg WHETHER rides each ovn.ls interface instead (see
      // collisionDomainToIR's `flow`). `target` becomes `targets`, the
      // OVS IPFIX column name; the API's singular reads better in config.
      ipfix: ipfix
        ? {
          targets: ipfix.target,
          sampling: ipfix.sampling,
          cacheActiveTimeout: ipfix.cacheActiveTimeout,
          cacheMaxFlows: ipfix.cacheMaxFlows,
        }
        : undefined,
    },
  };
}

// ── kernel.router ──────────────────────────────────────────────────
// A KernelRouter's real Linux netns (types.ts) — NOT an OVN kind at
// all, unlike everything else this module emits (confirmed live,
// 2026-08-12: `ip netns exec ns-uplink-voda-avm ip a` — a real netns
// with two real interfaces, no OVN LRP involved). THREE nodes per
// instance, all `kind: "kernel.router"`: one router-level node (`key:
// {name}`, no `side`) carrying just the host reference, plus one node
// PER SIDE (`key: {name, side}`, same shape convention as ovn.lrp —
// `data` fields flat, not nested under a left/right wrapper). `host` is
// still repeated on the per-side nodes too (not removed — both sides
// genuinely do live in the same netns/host, and the router-level node
// is additive, not a replacement for that).
function kernelRouterToIR(router: KernelRouter): KernelRouterNode {
  const id = `kernelrouter:${router.name}`;
  return {
    id,
    kind: "kernel.router",
    key: { name: router.name },
    data: { host: `host:${router.host.name}` },
  };
}

// Which (Router, endpoint) is THIS KernelRouter's own OVN twin — the
// endpoint whose l2Segment IS this KernelRouter's own transitDomain
// (types.ts's own doc comment on that field). Always defined in
// practice (both builders pair the KernelRouter with an OVN twin), but
// returns undefined — shouldn't happen, not assumed — when no Router
// endpoint actually references that transitDomain.
function transitPeer(
  kernelRouter: KernelRouter,
  routers: readonly Router[],
):
  | { readonly router: Router; readonly endpoint: OvnRouterEndpoint }
  | undefined {
  for (const router of routers) {
    for (const endpoint of router.endpoints) {
      if (
        endpoint.kind === "ovn" &&
        endpoint.l2Segment.name === kernelRouter.transitDomain.name
      ) {
        return { router, endpoint };
      }
    }
  }
  return undefined;
}

// The kernel netns's own route BACK into the OVN mesh, for its `left`
// (transit-facing) side only — everything already computed as a route
// FOR its OVN twin (computeRoutes/computeInterconnectRoutes above,
// merged into `routeNodes` by the time toIR() calls this), mirrored
// with the SAME dst but nexthop = transitPeerAddrs (the OVN twin's own
// address on the shared transit domain — the only way back into OVN
// from inside this netns). Without this, the netns has no route to
// anything but its own directly-connected subnets — confirmed live,
// 2026-08-13: router-voda-avm-v2 got its own default route via
// kernel-0's transit-facing address, but kernel-0 itself had no route
// back to home/management's own subnets at all, nothing told it those
// exist.
//
// Excludes any route whose nexthop is THIS KernelRouter's OWN address
// (kernelRouterEndpoint()'s via-filled anchor route, see that method's
// own doc comment) — that one already points AT this kernel router
// (i.e. it's the reason this function's caller exists at all), mirroring
// it back onto itself would be circular. Also excludes any dst the
// kernel router's OWN `right` side already applies (KernelRouterSide.
// routes with a `via` — the real-world-facing routes, e.g. the WAN's
// default) — applying a route on both sides would give the netns two
// conflicting default routes (2026-08-18: the mirrored
// `0.0.0.0/0 via <transit-peer>` next to the right side's real
// `0.0.0.0/0 via <ISP>`). Family-matched: an IPv4 dst only ever gets an
// IPv4 nexthop from transitPeerAddrs, same for IPv6.
function kernelRouterBackRoutes(
  kernelRouter: KernelRouter,
  routers: readonly Router[],
  routeNodes: readonly RouteNode[],
): Array<{ dst: string; via: string }> {
  const peer = transitPeer(kernelRouter, routers);
  if (!peer) return [];
  // .to_s() (bare, no prefix length), not addrStrings()'s .to_string() —
  // a route node's own `nexthop` is always stored bare (routeToIR
  // above: `nexthop: nexthop.to_s()`), so comparing against it needs
  // the same format or this exclusion silently never matches anything.
  const ownAddrs = new Set(kernelRouter.left.ipaddrs.map((a) => a.to_s()));
  const peerAddrs = kernelRouter.transitPeerAddrs ?? [];
  // Scope the mirrored routes to the KernelRouter's OWN RoutingDomains
  // (the domain its left endpoint anchors — e.g. a tunnelRouterEndpoint's
  // Neighbor-defaultRoute). Every route node carries `data.domain`, so
  // this keeps each tunnel's netns knowing only ITS OWN segment's
  // subnets (mullvad-de -> 192.168.130.0/24, mullvad-us -> 192.168.131.0/24)
  // instead of the whole mesh — the whole point of having two separate
  // domains (2026-08-23).
  const ownDomains = new Set(
    (kernelRouter.routingDomains ?? []).map((d) => d.name),
  );
  const rightApplied = new Set(
    (kernelRouter.right.routes ?? [])
      .filter((r) => r.via)
      .map((r) => r.dst.to_string()),
  );
  const routes: Array<{ dst: string; via: string }> = [];
  for (const node of routeNodes) {
    if (node.key.ovnrouter !== peer.router.name) continue;
    if (!ownDomains.has(node.data.domain)) continue;
    const nexthop = node.data.nexthop;
    if (ownAddrs.has(nexthop)) continue;
    const prefix = node.key.prefix;
    if (rightApplied.has(prefix)) continue;
    const via = peerAddrs.find((a) => a.is_ipv4() === !prefix.includes(":"));
    if (!via) continue;
    routes.push({ dst: prefix, via: via.to_s() });
  }
  return routes;
}

// Addresses are already fully resolved facts (KernelRouterSide.ipaddrs)
// — no real iface binding yet (deliberately deferred, see
// KernelRouterSide's own doc comment, types.ts), so a dummy device
// stands in as the thing addresses/routes actually bind to (deployer/
// ir_common.py's own _emit_kernel_router_create) — assigning these to
// a REAL device is its own next step. `routes`: declared entries with a
// `via` (a route with none means "handled elsewhere," e.g. IPv6 RA/DHCP
// on the real segment, not yet modeled — same semantic computeRoutes
// above already uses for a route's own anchor) PLUS, on `left` only,
// whatever kernelRouterBackRoutes above mirrors — nothing for the
// deployer to apply otherwise, so those are dropped here rather than
// emitted as a route with no nexthop. Declared routes are ALSO gated on
// router.routingDomains being non-empty — a route only takes effect if
// this KernelRouter is actually a participant of some declared
// RoutingDomain, the same rule that already gates every OVN-side route
// (RouterEndpointRoute's own doc comment, types.ts) — `routingDomains`
// is only ever populated with real, net.routingDomain()-registered
// references (validated at defineOvnRouter() call time), so non-empty already
// implies membership, no separate intersection with
// network.allRoutingDomains needed. Back-routes need no separate gate:
// `routeNodes` is already empty for a router with no RoutingDomain
// membership (computeRoutes/computeInterconnectRoutes are themselves
// scoped that way), so there's nothing to mirror in that case either.
//
// `ifaces` — KernelRouterSide.ifaces, populated for `right` by
// buildKernelRouterEndpoint (define.ts, 2026-08-18) — is carried onto
// the side node so the deployer creates/moves the real interface into
// the netns and binds addresses/routes to IT instead of a dummy stand-
// in; a side with no `ifaces` still gets the dummy (deployer/
// ir_common.py's own _emit_kernel_router_create). `iface` stays
// structurally unmodeled in the cross-language protocol, same as
// ovn.ls's own entries (see protocol.ts's OvnLsInterface).
function kernelRouterSideToIR(
  router: KernelRouter,
  side: "left" | "right",
  routers: readonly Router[],
  routeNodes: readonly RouteNode[],
): KernelRouterNode {
  const id = `kernelrouter:${router.name}|side:${side}`;
  const inDomain = router.routingDomains &&
    router.routingDomains.length > 0;
  const declaredRoutes = inDomain
    ? router[side].routes
      ?.filter((r) => r.via)
      .map((r) => ({ dst: r.dst.to_string(), via: r.via!.to_s() }))
    : undefined;
  const backRoutes = side === "left"
    ? kernelRouterBackRoutes(router, routers, routeNodes)
    : [];
  const routes = [...(declaredRoutes ?? []), ...backRoutes];
  // `ifaces` — KernelRouterSide.ifaces — mirrored to the side node. A
  // "veth" entry additionally carries a `shortName` (the same
  // convention every other interface's `iface` uses, e.g. ovn.ls's
  // bridges): buildKernelRouterEndpoint emits LONG leg names
  // (`veth-krn-<routerName>`), which exceed IFNAMSIZ for a real
  // device name, so the readable-when-it-fits/else-deterministic-hash
  // suffix is resolved HERE and the deployer builds the real pair
  // (`veth-krn-<shortName>`/`veth-ovn-<shortName>`) from it
  // (deployer/ir_common.py's _emit_kernel_router_create) — same
  // "generator computes the final name" boundary as shortIfaceName.
  // It also carries the netns-side leg's `mac`, resolved HERE for the
  // same reason an LRP's is (resolveMac): deterministic from this
  // side's own addresses (macFromAddrsOrName, addressing.ts), so the
  // OVN router's MAC_Binding for them survives the leg being recreated
  // instead of pointing at the previous leg's random MAC (2026-10-11).
  const ifaces = router[side].ifaces?.map((hi) => {
    const iface = hi.iface;
    // An AF_XDP port is owned by OVS's userspace datapath; a kernel
    // router's netns cannot take it. Same for a VLAN riding one.
    if (iface.kind === "xdp") {
      throw new Error(
        `kernel router ${router.name} side ${side}: an "xdp" interface is ` +
          `OVS-owned and cannot be moved into a netns`,
      );
    }
    if (iface.kind === "vlan") {
      const vlanParent = resolveParent(iface.vlanParent);
      if (vlanParent.kind === "xdp") {
        throw new Error(
          `kernel router ${router.name} side ${side}: VLAN ${iface.vlanId} ` +
            `rides an "xdp" parent, but kernel-router VLANs need a ` +
            `physical parent`,
        );
      }
      return { host: hi.host.name, iface: { ...iface, vlanParent } };
    }
    if (iface.kind !== "veth") return { host: hi.host.name, iface };
    return {
      host: hi.host.name,
      iface: {
        ...iface,
        shortName: vethShortId(iface.ifaceName),
        mac: macFromAddrsOrName(router[side].ipaddrs, iface.ifaceName),
      },
    };
  });
  return {
    id,
    kind: "kernel.router",
    key: { name: router.name, side },
    data: {
      host: `host:${router.host.name}`,
      ipaddrs: addrStrings(router[side].ipaddrs),
      routes: routes.length > 0 ? routes : undefined,
      ifaces: ifaces && ifaces.length > 0 ? ifaces : undefined,
      // The tunnel router's UPSTREAM peer address (tunnelRouterEndpoint(),
      // define.ts) — the OTHER end of the upstream veth (right side only):
      // the deployer brings that peer leg UP and assigns it this address
      // in the root namespace (the tunnel netns's default route points at
      // it via the right side's routes; without a live peer the leg stays
      // LOWERLAYERDOWN and the default is `linkdown`, confirmed live
      // 2026-08-23).
      upstreamPeerAddrs: side === "right"
        ? addrStrings(router.upstreamPeerAddrs)
        : undefined,
      // The security group attached to THIS side's interface (right
      // side only in practice — set by buildKernelRouterEndpoint,
      // define.ts). Carried here as the generic `security.group` node's
      // name (`masq-<router>` for the masq shortcut): the attachment
      // point lives on the side node, the rules live in the group node
      // itself.
      securityGroup: router[side].securityGroup?.name,
      // Applications running inside the netns on this side's real
      // interface (right side only in practice — the `kernel.app.*`
      // services resolved in buildKernelRouterEndpoint, define.ts). The
      // deployer turns each into its `ip netns exec` service script,
      // and stops/releases it on delete.
      apps: router[side].apps?.map((app) => {
        // A VETH-CLIENT docker app needs SHORT, collision-free veth device
        // names (IFNAMSIZ ≤ 15): `ve-<hash>` / `ve-<hash>-c` derived from
        // the container name — same rule shortIfaceName applies everywhere
        // else in this file. An interface-OWNING docker (no veth addressing,
        // e.g. dhcpcd-in-docker) gets NO veth fields — its "owns the
        // interfaces" is exactly the ABSENCE of the veth machinery.
        if (app.kind === "docker" && app.ip) {
          const hash = fnv1a32(app.name).toString(16).padStart(8, "0");
          return { ...app, vethName: `ve-${hash}` };
        }
        return app;
      }),
    },
  };
}

// ── security.group ───────────────────────────────────────────────────
// The IR counterpart of a security group (SecurityGroup, types.ts) —
// deliberately IMPLEMENTATION-ABSTRACT: a named set of rules that some
// object (today a KernelRouterSide, later an ovn.lrp, a docker
// container, ...) attaches to via its own `securityGroup` reference.
// Nothing about this node is kernel- or OVN-specific: the name is just
// a label, and the rules are the FINAL resolved facts in a generic
// vocabulary (`kind: "masq"` = masquerade, `family` = which address
// family), which each implementation maps to its own concrete command
// (the deployer turns `masq` into an iptables MASQUERADE inside a
// kernel netns; an OVN backend would translate the same rule to its own
// NAT/ACL). The group is BUILT once, at declaration time
// (net.securityGroup(), define.ts — including the implicit `masq-<router>`
// shortcut expansion); this function is pure serialization, it derives
// nothing.
function securityGroupToIR(group: SecurityGroup): SecurityGroupNode {
  return {
    id: `securitygroup:${group.name}`,
    kind: "security.group",
    key: { name: group.name },
    data: { rules: group.rules },
  };
}

// IFNAMSIZ (16) minus the NUL terminator — a real OVS bridge is a real
// kernel netdev, capped at 15 usable characters. Confirmed live,
// 2026-08-10: "br-voda-modem-v2" (16 chars) failed with ofproto
// "Invalid argument" on a real container while "br-voda-avm-v2" (14
// chars) right next to it succeeded.
const IFNAMSIZ_MAX = 15;

// The IFNAMSIZ-safe suffix for a veth pair's legs, resolved HERE (the
// generator layer — deliberately NOT in define.ts, 2026-08-18): readable
// when the long `veth-krn-<name>` leg fits, else a deterministic 6-hex
// fnv1a32 of the name. The same id feeds both legs so the pair stays
// matched, and it's shared by kernelRouterSideToIR (the netns-side
// copy, via `shortName`) and collisionDomainToIR (the transit ovn.ls
// copy, which renames the legs directly so the bridge binding attaches
// the actual created devices).
function vethShortId(ifaceName: string): string {
  const name = ifaceName.replace(/^veth-krn-/, "");
  return ifaceName.length <= IFNAMSIZ_MAX
    ? name
    : fnv1a32(name).toString(16).padStart(8, "0").slice(0, 6);
}

// Readable (`${prefix}${name}`) whenever it fits; only falls back to a
// short deterministic hash (fnv1a32, addressing.ts) when it doesn't.
// Resolved HERE, not in deployer/ir_common.py (the translator) — same
// boundary reasoning as mac/gatewayChassis/routes throughout this file:
// the generator computes the FINAL name a real kernel object needs, the
// translator only ever applies an already-resolved fact. Moved here
// 2026-08-12 — this used to live translator-side as deployer/
// ir_common.py's own `_bridge_name`/`_fnv1a_32`.
function shortIfaceName(prefix: string, name: string): string {
  const candidate = `${prefix}${name}`;
  if (candidate.length <= IFNAMSIZ_MAX) return candidate;
  return `${prefix}${fnv1a32(name).toString(16).padStart(8, "0")}`;
}

// A host-local bridge for a VLAN shared by several kernel routers: Linux
// allows only one 8021q device per (parent, VLAN id), even across netns.
function transitBridgeName(parent: string, vlanId: number): string {
  return shortIfaceName("br-upl-", `${parent}:${vlanId}`);
}

// ── ovn.ls (NEW — not in ADR 0002's original node-kind table) ────────
// A bare OVN Logical_Switch — CollisionDomain's direct IR counterpart.
// Not host-scoped (see CollisionDomain's own doc comment, types.ts —
// confirmed live, ADR 0003: a real logical switch is cluster-wide, no
// single chassis owns it), so `key` carries no host at all, unlike
// almost every other kind in this project so far.
//
// `interfaces` is derived from every Router's endpoints that reference
// this domain (RouterEndpoint.ifaces), not from CollisionDomain's own
// addInterface()/allInterfaces — net.router() doesn't currently call
// addInterface() when it registers an endpoint's ifaces (a real,
// currently-unresolved gap between the two mechanisms, not something
// papered over here: relying on RouterEndpoint.ifaces directly sidesteps
// it rather than pretending it's already reconciled).
//
// `shortName`: the real OVS bridge name this domain binds to on any
// chassis with a bindable interface (see deployer/ir_common.py's
// _emit_iface_bindings_create/_delete) — see shortIfaceName() above.
// Carried on EACH interface entry, not as its own data field: `iface`
// is already the one place a real-world-binding fact lives per (host,
// interface) pair, and every entry for this domain resolves to the
// same value (deterministic from domain.name), so there's nothing lost
// repeating it there instead of hoisting it out.
// Whether flow export is OFF for one endpoint binding (types.ts
// RouterEndpointInputBase.flow). Explicit wins both ways; otherwise a
// TRANSIT link defaults OFF — a kernel/tunnel router's transit (and its
// tunnel backdoor) is an internal mesh veth, not segment traffic worth
// sampling, so its bridge stays out of IPFIX unless the author opts in
// with `flow: true`. Segment legs (vlan/physical) default ON.
// ── interface resolution ─────────────────────────────────────────────
// The DSL leaves AF_XDP's `mode`/`nRxq` optional and accepts a bare
// string as a VLAN parent (every pre-XDP topology). The IR carries the
// FINAL fact, so both are resolved here — same boundary as shortName,
// mac and the LRP options above.
const XDP_MODE_DEFAULT: XdpMode = "best-effort";
const XDP_NRXQ_DEFAULT = 1;

type ResolvedXdp = {
  kind: "xdp";
  name: string;
  mode: XdpMode;
  nRxq: number;
};
type ResolvedParent = { kind: "physical"; name: string } | ResolvedXdp;
type ResolvedIface =
  | Exclude<InterfaceKind, { kind: "vlan" } | { kind: "xdp" }>
  | (Omit<Extract<InterfaceKind, { kind: "vlan" }>, "vlanParent"> & {
    vlanParent: ResolvedParent;
  })
  | ResolvedXdp;

function resolveXdp(
  iface: Pick<XdpIface, "name" | "mode" | "nRxq">,
): ResolvedXdp {
  return {
    kind: "xdp",
    name: iface.name,
    mode: iface.mode ?? XDP_MODE_DEFAULT,
    nRxq: iface.nRxq ?? XDP_NRXQ_DEFAULT,
  };
}

function resolveParent(parent: InterfaceParent): ResolvedParent {
  if (typeof parent === "string") return { kind: "physical", name: parent };
  if (parent.kind === "xdp") return resolveXdp(parent);
  return { kind: "physical", name: parent.name };
}

function resolveIface(iface: InterfaceKind): ResolvedIface {
  if (iface.kind === "vlan") {
    return { ...iface, vlanParent: resolveParent(iface.vlanParent) };
  }
  if (iface.kind === "xdp") return resolveXdp(iface);
  return iface;
}

function collisionDomainToIR(
  domain: CollisionDomain,
): OvnLsNode | BridgeNode {
  if (domain.hostOnly) {
    // Host-local L2 segment: a bridge and its own physical ports. No
    // OVN logical switch/localnet/mapping is emitted for it. A domain
    // may already be named as its bridge (`br-...`); otherwise apply the
    // usual `br-` prefix.
    const shortName = domain.name.startsWith("br-")
      ? domain.name
      : shortIfaceName("br-", domain.name);
    return {
      id: `bridge:${shortName}`,
      kind: "bridge",
      key: { name: shortName },
      data: {
        host: `host:${domain.host!.name}`,
        ports: domain.hostPorts.map((port) => ({
          host: port.host.name,
          iface: { ...resolveIface(port.iface), bridge: shortName },
        })),
      },
    };
  }
  const shortName = shortIfaceName("br-", domain.name);
  const interfaces: OvnLsInterface[] = [];
  for (const port of domain.ovnPorts) {
    if (port.host === undefined || port.iface === undefined) continue;
    if (port.iface.kind !== "veth") {
      interfaces.push({
        host: port.host.name,
        iface: {
          ...resolveIface(port.iface),
          bridge: shortName,
          ...(port.flowOff ? { flow: false } : {}),
        },
      });
      continue;
    }
    // A veth on a transit domain: buildKernelRouterEndpoint stamps LONG
    // leg names; the deployer's binding emitter attaches `peerName` to
    // the bridge, so shorten both legs HERE to the same id
    // kernelRouterSideToIR resolves for the netns-side pair.
    const id = vethShortId(port.iface.ifaceName);
    interfaces.push({
      host: port.host.name,
      iface: {
        ...port.iface,
        ifaceName: `veth-krn-${id}`,
        peerName: `veth-ovn-${id}`,
        bridge: shortName,
        ...(port.flowOff ? { flow: false } : {}),
      },
    });
  }
  return {
    id: `ls:${domain.name}`,
    kind: "ovn.ls",
    key: { name: domain.name },
    data: { interfaces },
  };
}

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
// address of that family". One `lr-nat-add ... snat external logical` row
// per logical prefix; the deployer owns the imperative side.
function computeOvnNats(network: NetworkDefinition): Map<string, OvnNatRule[]> {
  const byRouter = new Map<string, OvnNatRule[]>();
  for (const router of network.allRouters) {
    const rules: OvnNatRule[] = [];
    for (const endpoint of router.endpoints) {
      for (const service of endpoint.services ?? []) {
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
        const seen = new Set<string>();
        for (const other of router.endpoints) {
          if (other.name === endpoint.name) continue;
          for (const addr of other.ipaddrs) {
            if (addr.is_ipv4() !== isV4) continue;
            const logical = addr.network().to_string();
            if (logical === externalNet || seen.has(logical)) continue;
            seen.add(logical);
            rules.push({
              type: "snat",
              family: isV4 ? "ipv4" : "ipv6",
              externalIp: external.to_s(),
              logicalIp: logical,
            });
          }
        }
      }
    }
    if (rules.length > 0) {
      rules.sort((a, b) =>
        `${a.externalIp}|${a.logicalIp}`.localeCompare(
          `${b.externalIp}|${b.logicalIp}`,
        )
      );
      byRouter.set(router.name, rules);
    }
  }
  return byRouter;
}

function routerToIR(
  router: Router,
  nats?: readonly OvnNatRule[],
): OvnLrNode {
  return {
    id: `ovnrouter:${router.name}`,
    kind: "ovn.lr",
    key: { name: router.name },
    data: {
      options: {
        mac_binding_age_threshold: router.options.macBindingAgeThreshold,
      },
      ...(nats && nats.length > 0 ? { nats: [...nats] } : {}),
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
// Resolved HERE, not left for the shell-conversion step to guess: the
// IR is the desired-state boundary between this project's TypeScript
// half and its Python half (deployer/ir_common.py) — it should carry
// the FINAL mac, not push "how do I fold one" across that boundary
// (macFromV4 only exists on this side). Same reasoning as
// resolveEncapIp above. Matches RouterEndpoint.mac's own doc comment
// (types.ts): explicit override wins outright, else fold the first
// IPv4 in ipaddrs — required to exist as one or the other, since an
// LRP with no mac at all can't be created.
function resolveMac(lrp: string, endpoint: OvnRouterEndpoint): string {
  if (endpoint.mac) return endpoint.mac;
  const v4 = endpoint.ipaddrs.find((addr): addr is IPv4 => addr.is_ipv4());
  if (!v4) {
    throw new Error(
      `${lrp}: no IPv4 address to derive a MAC from, and no explicit ` +
        `RouterEndpoint.mac override`,
    );
  }
  return macFromV4(v4);
}

// Folds RouterEndpoint.services into the single ipv6_ra_configs smap
// OVN actually has (see RouterEndpointService, types.ts, for why these
// are two independently-toggleable services rather than one boolean).
// Resolved HERE, not in deployer/ir_common.py, same boundary reasoning
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
      service.kind === "ovn.ipv4.masq" || service.kind === "ovn.ipv6.masq"
    ) {
      continue;
    }
    // Config-side workload shortcuts (wireguard/zerotier) are consumed by
    // buildTunnelRouterEndpoint and never reach an OVN endpoint's services
    // (they'd be a config error if they did) — skip so the exhaustiveness
    // check below stays meaningful (2026-09-08).
    if (service.kind === "wireguard" || service.kind === "zerotier") {
      continue;
    }
    const unknownKind: never = service;
    throw new Error(
      `unknown RouterEndpointService kind: ${JSON.stringify(unknownKind)}`,
    );
  }
  return configs;
}

function routerEndpointToIR(
  router: Router,
  endpoint: OvnRouterEndpoint,
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
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
      addresses: addrStrings(endpoint.ipaddrs),
      mac: resolveMac(endpoint.name, endpoint),
      gatewayChassis: endpoint.gatewayChassis
        ? `host:${endpoint.gatewayChassis.name}`
        : undefined,
      ipv6RaConfigs: resolveIpv6RaConfigs(endpoint.services),
      serviceRefs: resolveServiceRefs(
        router,
        endpoint,
        dstsByDomain,
        platformCp,
      ),
    },
  };
}

function isDefaultDst(dst: IPv4 | IPv6): boolean {
  const s = dst.to_string();
  return s === "0.0.0.0/0" || s === "::/0";
}

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
        ? { dst: r.dst.to_string(), metric: r.metric }
        : { dst: r.dst.to_string() }
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
  attachmentAddrs: readonly (IPv4 | IPv6)[],
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
  attachmentMetric?: number,
): { dst: string; via: string; metric?: number }[] {
  const domains = endpoint.routingDomains ?? router.routingDomains ?? [];
  const out: { dst: string; via: string; metric?: number }[] = [];
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
        via: gateway.to_s(),
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
function resolveServiceRefs(
  router: Router,
  endpoint: OvnRouterEndpoint,
  dstsByDomain: ReadonlyMap<string, ReadonlySet<string>>,
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
  return refs.map((ref) => {
    const routes = ref.routes && ref.routes.length > 0
      ? ref.routes.map((r) => {
        if (r.via && !viaIsOnSegment(endpoint, r.via)) {
          throw new Error(
            `service "${ref.srvRef.name}" on ${router.name} ` +
              `(${endpoint.name}): route ` +
              `${r.dst.to_string()} via ${r.via.to_s()} — the via is not on ` +
              `segment "${endpoint.l2Segment.name}" (${
                endpoint.ipaddrs.map((a) => a.to_string()).join(", ")
              }); a container next-hop must be reachable on its own L2`,
          );
        }
        // Route metric wins, else this ATTACHMENT's metric (the NIC-level
        // default), else unset — the generator fills the execution-order
        // fallback in assignFallbackRouteMetrics below (2026-09-19).
        const metric = r.metric ?? ref.metric;
        return {
          dst: r.dst.to_string(),
          ...(r.via ? { via: r.via.to_s() } : {}),
          ...(metric !== undefined ? { metric } : {}),
        };
      })
      : workloadDomainRoutes(
        router,
        endpoint,
        ref.ipaddrs,
        dstsByDomain,
        ref.metric,
      );
    // Resolver precedence (innermost wins): the attachment's own, else the
    // platform's global control-plane resolver (2026-09-19).
    const resolver = ref.resolver ??
      (onControlPlane ? platformCp?.resolver : undefined);
    return {
      service: `kernel.app.container:${ref.srvRef.name}`,
      // The in-container NIC name — resolved HERE (generator side), not by
      // the deployer: the segment name when it fits IFNAMSIZ, else the fnv1a
      // short form (shortIfaceName, same rule as every other short name).
      name: shortIfaceName("", endpoint.l2Segment.name),
      // The NIC's MAC — deterministic (macFromAddrsOrName, addressing.ts)
      // for the same reason as a kernel router's veth leg: the container
      // is recreated on every `up`, and the segment's OVN router would
      // otherwise keep a MAC_Binding for the previous NIC (2026-10-11).
      mac: macFromAddrsOrName(
        ref.ipaddrs,
        `${ref.srvRef.name}|${endpoint.l2Segment.name}`,
      ),
      ...(ref.ipaddrs.length > 0
        ? { ipaddrs: ref.ipaddrs.map((a) => a.to_string()) }
        : {}),
      ...(routes.length > 0 ? { routes: orderRoutes(routes) } : {}),
      ...(resolver
        ? {
          resolver: { nameservers: resolver.nameservers.map((n) => n.to_s()) },
        }
        : {}),
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
function orderRoutes(
  routes: readonly { dst: string; via?: string; metric?: number }[],
): { dst: string; via?: string; metric?: number }[] {
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

// ── ipv4.route / ipv6.route ──────────────────────────────────────────
// Matches ADR 0002's own node-kind table exactly (`ovnrouter:<scope>|
// route:<prefix>` — the shape fits Router/RoutingDomain).
//
// The ANCHOR — whichever (router, side) a route's `via` is actually
// reachable from — is DECLARED, not inferred: it's simply whichever
// RouterEndpoint carries the route in its own `.routes` array (see
// RouterEndpointRoute, types.ts). An earlier design searched every
// router's every endpoint for one whose declared subnet happened to
// contain `via` — removed 2026-08-12 as unnecessary indirection once
// routes moved onto the endpoint directly: the config author already
// knows router-voda-avm-v2's WAN-facing `left` is the real egress, so
// there's nothing left to search for. Every OTHER router in the SAME
// RoutingDomain gets `via` rewritten to the anchor's own address on
// whichever CollisionDomain the two routers actually share — not `via`
// itself, which usually isn't even reachable from anywhere but the
// anchor's own segment. Confirmed by hand against a real 3-router
// constellation (2026-08-11, live design discussion) before writing
// this: home/management both resolve to "default via voda-avm's OWN
// backbone address," not "via the real ISP gateway" — only voda-avm's
// own route gets the literal ISP address.
//
// No domain shared between a given router and the anchor, or the
// family just doesn't match anything reachable — that router simply
// gets no route from that entry. Not an error: a declared route is one
// possible SOURCE of reachability among several (SLAAC/RA on a segment
// being the other one already real in this project, see
// the legacy generator's ipv6_ra_configs) — a source that doesn't resolve
// for a given router/family just isn't the one supplying it there.

interface Anchor {
  readonly router: Router;
  readonly endpoint: OvnRouterEndpoint;
}

// A real type guard (not just a boolean check) — narrows `b` to the
// SAME branded family as `a`, so a caller like anchorAddressSharedWith
// below can call the family-specific `.includes()` without an unsafe
// cast. Plain boolean, not a type guard: a `b is T` predicate here
// narrows FINE on the true branch but incorrectly narrows the false
// branch to `never` whenever both arguments already share the same
// declared union type (confirmed live, 2026-08-12, trying exactly that
// on routeToIR's validation below) — and `.includes()` never actually
// needed narrowed arguments in the first place (its inherited base
// IPAddress signature already accepts either family).
function isSameFamily(a: IPv4 | IPv6, b: IPv4 | IPv6): boolean {
  return a.is_ipv4() === b.is_ipv4();
}

// Two endpoints "share a CollisionDomain" only when BOTH are OVN-side —
// a KernelRouterEndpoint has no l2Segment at all, so it never
// participates in collision-domain-based routing (RoutingDomain/
// computeRoutes/computeInterconnectRoutes are all OVN-world concepts
// today; a kernel endpoint's own reachability, once built, will need
// its own mechanism, not this one).
function sharesL2Segment(a: OvnRouterEndpoint, b: OvnRouterEndpoint): boolean {
  return a.kind === "ovn" && b.kind === "ovn" &&
    a.l2Segment.name === b.l2Segment.name;
}

// The anchor's OWN address, of `dst`'s family, on whichever endpoint
// `router` actually shares a CollisionDomain with — i.e. NOT the anchor
// endpoint itself (that's what `via` already sits on), but one of the
// anchor router's OTHER endpoints, the one facing the rest of the cluster.
// With an endpoint array there may be several such endpoints; the first
// (in the router's own sorted order) that both shares a domain with
// `router` and carries an address of `dst`'s family wins.
function anchorAddressSharedWith(
  router: Router,
  anchor: Anchor,
  dst: IPv4 | IPv6,
): (IPv4 | IPv6) | undefined {
  for (const candidate of anchor.router.endpoints) {
    if (candidate === anchor.endpoint) continue;
    const shared = router.endpoints.some((e) => sharesL2Segment(e, candidate));
    if (!shared) continue;
    const addr = candidate.ipaddrs.find((a) => isSameFamily(a, dst));
    if (addr) return addr;
  }
  return undefined;
}

// `domain` names WHICH RoutingDomain produced this route (net.
// routingDomain()'s own name) — every route, from either mechanism
// below, always belongs to exactly one. Confirmed live, 2026-08-12:
// without this, deployer/ir_common.py had no way to group a
// router's routes into labeled blocks in the generated script — every
// route landed in one flat, unlabeled dump instead of being grouped
// with its router and its source, unlike every other section of the
// script.
function routeToIR(
  router: Router,
  dst: IPv4 | IPv6,
  nexthop: IPv4 | IPv6,
  masq: boolean,
  domain: string,
  metric: number,
): RouteNode {
  // Every route this module builds funnels through here — the ONE
  // place that guarantees dst and nexthop never mismatch family. A
  // mismatch can only originate from a config author's own
  // RouterEndpointRoute (dst/via independently typed IPv4|IPv6, nothing
  // stops declaring one of each by mistake) — computeInterconnectRoutes
  // below can't produce one itself (it always pairs same-family
  // addresses within one domain's own participants), but validating
  // here, once, covers every caller instead of trusting each one
  // individually.
  if (!isSameFamily(dst, nexthop)) {
    throw new Error(
      `router "${router.name}": route to ${dst.to_string()} has a ` +
        `next-hop of the wrong address family (${nexthop.to_s()}) — ` +
        `dst and via/nexthop must both be IPv4 or both be IPv6`,
    );
  }
  const prefix = dst.to_string();
  const id = `ovnrouter:${router.name}|route:${domain}|${prefix}`;
  return {
    id,
    kind: dst.is_ipv4() ? "ipv4.route" : "ipv6.route",
    key: { ovnrouter: router.name, prefix },
    data: { nexthop: nexthop.to_s(), masq, domain, metric },
  };
}

// A router participates in `domain` if its router-level membership names
// it OR any of its endpoints does (per-endpoint routingDomains,
// 2026-08-23 — a tunnelRouterEndpoint participates in Neighbor-defaultRoute
// via its tunnel endpoint and Voda-defaultRoute via its backbone endpoint,
// neither on the router itself).
function isDomainParticipant(router: Router, domain: RoutingDomain): boolean {
  return router.routingDomains?.includes(domain) === true ||
    router.endpoints.some((e) => e.routingDomains?.includes(domain) === true);
}

function computeRoutes(network: NetworkDefinition): RouteNode[] {
  const nodes: RouteNode[] = [];
  // Anchor-outer (2026-08-23): routing-domain membership is PER ENDPOINT
  // now — an endpoint's declared routes participate in its OWN
  // routingDomains (falling back to the router-level ones), so a router
  // can anchor one domain from one endpoint and participate in another
  // from another (a tunnelRouterEndpoint does exactly that).
  //
  // Every router's OWN declared route prefixes (across all endpoints,
  // via-less or via) — a router's own route to a prefix wins over one it
  // would otherwise LEARN as a participant of another domain's anchor.
  // Without this a full-tunnel router (e.g. mullvad-de anchors 0.0.0.0/0
  // via-less for Neighbor, but its backbone endpoint also joins
  // Voda-defaultRoute) gets voda's default written over its OWN tunnel
  // egress — same node id, last-write-wins (hit live 2026-08-30).
  const ownPrefixes = new Map<string, Set<string>>();
  for (const r of network.allRouters) {
    const set = new Set<string>();
    for (const endpoint of r.endpoints) {
      for (const rt of endpoint.routes ?? []) set.add(rt.dst.to_string());
    }
    ownPrefixes.set(r.name, set);
  }
  for (const anchorRouter of network.allRouters) {
    for (const anchorEndpoint of anchorRouter.endpoints) {
      const anchor: Anchor = { router: anchorRouter, endpoint: anchorEndpoint };
      const routes = anchorEndpoint.routes ?? [];
      if (routes.length === 0) continue;
      const endpointDomains = anchorEndpoint.routingDomains ??
        anchorRouter.routingDomains ??
        [];
      for (const domain of endpointDomains) {
        // A router is a participant of `domain` if the router-level
        // membership names it OR any of its endpoints does.
        const participants = network.allRouters.filter((r) =>
          isDomainParticipant(r, domain)
        );
        for (const route of routes) {
          const masq = route.with === "masq";
          for (const router of participants) {
            if (router.name === anchorRouter.name) {
              // No `via` here means "the anchor needs no literal route
              // of its own for this — handled elsewhere on its own
              // endpoint" (e.g. SLAAC/RA, a less-specific default, or
              // the tunnel egress inside a tunnelRouterEndpoint's
              // netns). That's a statement about the ANCHOR only, not
              // about whether OTHER participants should still learn to
              // route toward it — they always should (confirmed live,
              // 2026-08-12), so this `continue` is scoped to the
              // anchor's own branch, not the whole route entry.
              if (!route.via) continue;
              nodes.push(
                routeToIR(
                  router,
                  route.dst,
                  route.via,
                  masq,
                  domain.name,
                  domain.metric ?? 400,
                ),
              );
              continue;
            }
            const nexthop = anchorAddressSharedWith(router, anchor, route.dst);
            if (!nexthop) continue; // no shared domain / family with the anchor
            // A router's OWN declared route to this prefix is its own
            // authoritative egress — don't overwrite it with a learned one
            // (e.g. a full-tunnel router must keep its tunnel egress, not
            // take another domain's default, 2026-08-30).
            if (ownPrefixes.get(router.name)?.has(route.dst.to_string())) {
              continue;
            }
            nodes.push(
              routeToIR(
                router,
                route.dst,
                nexthop,
                masq,
                domain.name,
                domain.metric ?? 400,
              ),
            );
          }
        }
      }
    }
  }
  // Domain routes at the ROUTER layer (2026-09-16): every participant of a
  // domain also learns each NON-default dst the domain declares
  // (RoutingDomain.routes), routed via whichever participant OWNS it (an
  // endpoint subnet containing the dst) on a CollisionDomain the two share.
  // Defaults (0.0.0.0/0, ::/0) are deliberately excluded: they have no
  // owning participant, and the anchor loop above already covers them. This
  // deliberately overlaps computeInterconnectRoutes — same node id, same
  // next hop — because both describe "reach a peer's own subnet"; it only
  // adds anything for a domain dst that is NOT a participant's subnet.
  for (const domain of network.allRoutingDomains) {
    const participants = network.allRouters.filter((r) =>
      isDomainParticipant(r, domain)
    );
    // EXPLICIT overrides only — a domain's CALCULATED routes are the
    // anchor/interconnect routes already produced above, so distributing
    // them again here would just duplicate those node ids.
    for (const route of domain.routes ?? []) {
      if (isDefaultDst(route.dst)) continue;
      const dst = route.dst.to_string();
      for (const router of participants) {
        if (ownPrefixes.get(router.name)?.has(dst)) continue;
        const ownsDst = (
          candidate: Router,
        ): OvnRouterEndpoint | undefined =>
          candidate.endpoints.find((e) =>
            e.ipaddrs.some((a) =>
              a.is_ipv4() === route.dst.is_ipv4() &&
              a.network().to_string() === dst
            )
          );
        if (ownsDst(router)) continue; // already on-link for this dst
        const owner = participants.find((p) =>
          p.name !== router.name && ownsDst(p) !== undefined
        );
        if (!owner) continue;
        const ownerEndpoint = ownsDst(owner);
        if (!ownerEndpoint) continue;
        const nexthop = anchorAddressSharedWith(
          router,
          { router: owner, endpoint: ownerEndpoint },
          route.dst,
        );
        if (!nexthop) continue;
        nodes.push(
          routeToIR(
            router,
            route.dst,
            nexthop,
            false,
            domain.name,
            route.metric ?? domain.metric ?? 400,
          ),
        );
      }
    }
  }
  return nodes;
}

// ── interconnect routes, scoped to a RoutingDomain's own participants ─
// "Interconnect only exists if a routing domain exists" (confirmed
// live, 2026-08-12, after the first cut of this function generated a
// route between EVERY pair of routers sharing the backbone — home,
// neighbor, usa, management, voda-avm, voda-modem, starlink all meshed
// together regardless of RoutingDomain membership. That's wrong: these
// are separate sites/tenants sharing physical hardware, and giving
// every one of them a route into every other one's LAN is a real
// privacy/security break, not just noise. The original hand-derived
// 3-router example (home/management/voda-avm, all three actually IN
// Voda-defaultRoute) never implied anything about neighbor/usa, who
// aren't in that domain at all.)
//
// So: routers only get mutual peer routes to each other if they're
// BOTH members of the SAME RoutingDomain — same scope computeRoutes
// already uses for `participants`. This still runs "any ways", i.e.
// unconditionally on whether that domain's own `route.via` entries
// resolve for a given family (see computeRoutes's `!via`
// skip, e.g. IPv6 default via SLAAC/RA) — a domain's participants
// still need direct routes to each other's own subnets regardless of
// whether the domain also has an external via-route.
//
// For every ordered pair of distinct participants (r1, r2) that share
// a CollisionDomain: r1 learns a route to each of r2's OTHER endpoints'
// subnets, next-hop r2's own address on the SHARED domain (not r2's
// address on a different endpoint — that's not reachable from r1 at
// all). Symmetric — running the same pair the other way round produces
// r2's own route back to r1. "Other endpoints" (plural now that a router
// owns an array): every endpoint of r2 except the one sharing the
// domain, which for the two-endpoint case is exactly the old single
// "other side".
function computeInterconnectRoutes(
  network: NetworkDefinition,
): RouteNode[] {
  const nodes: RouteNode[] = [];
  for (const domain of network.allRoutingDomains) {
    const participants = network.allRouters.filter((r) =>
      isDomainParticipant(r, domain)
    );
    for (const r1 of participants) {
      for (const r2 of participants) {
        if (r1.name === r2.name) continue;
        for (const r1side of r1.endpoints) {
          for (const r2side of r2.endpoints) {
            if (!sharesL2Segment(r1side, r2side)) continue;
            for (const nexthop of r2side.ipaddrs) {
              for (const other of r2.endpoints) {
                if (other === r2side) continue;
                for (const peerAddr of other.ipaddrs) {
                  if (!isSameFamily(nexthop, peerAddr)) continue;
                  nodes.push(
                    routeToIR(
                      r1,
                      peerAddr.network(),
                      nexthop,
                      false,
                      domain.name,
                      domain.metric ?? 400,
                    ),
                  );
                }
              }
            }
          }
        }
      }
    }
  }
  return nodes;
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
function viaIsOnSegment(
  endpoint: OvnRouterEndpoint,
  via: IPv4 | IPv6,
): boolean {
  for (const addr of endpoint.ipaddrs) {
    if (addr.is_ipv4() !== via.is_ipv4()) continue;
    if (addr.network().includes(via)) return true;
  }
  return false;
}

// One `kernel.app.container` node per referenced workload — the image/cmd/build
// plus the host it runs on. Networks are NOT here: they live on the
// endpoints that reference this service (OvnLrpData.serviceRefs,
// resolveServiceRefs above), so the container's NICs are DERIVED from the
// references at emit time (2026-09-08).
function serviceNodes(network: NetworkDefinition): KernelAppContainerNode[] {
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

function assignFallbackRouteMetrics(nodes: Record<string, IRNode>): void {
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

export function toIR(network: NetworkDefinition): Record<string, IRNode> {
  const nodes: Record<string, IRNode> = {};

  // Route nodes FIRST (2026-09-16): a RoutingDomain's CALCULATED destinations
  // are the prefixes its participants actually resolved (anchors +
  // interconnect), and a workload inherits them (workloadDomainRoutes) — so
  // they must exist before the endpoints are emitted. Insertion order is
  // irrelevant here; the final map is sorted by id below.
  const interconnectRoutes = computeInterconnectRoutes(network);
  const domainRoutes = computeRoutes(network);
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

  const natsByRouter = computeOvnNats(network);
  for (const router of network.allRouters) {
    const routerNode = routerToIR(router, natsByRouter.get(router.name));
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
        macFromAddrsOrName([], `${node.key.name}-${node.key.side}`);
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
    const node = securityGroupToIR(group);
    nodes[node.id] = node;
  }

  // Resolve any route metric the topology left implicit, so the IR always
  // carries a concrete value (2026-09-19).
  assignFallbackRouteMetrics(nodes);

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
