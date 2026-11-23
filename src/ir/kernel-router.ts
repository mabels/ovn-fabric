// src/ir/kernel-router.ts — kernel.router: a kernel router's netns and its
// sides.

import { fnv1a32 } from "../addressing.ts";
import { type Addr, prefixOf, toAddr } from "../ipnet.ts";
import type { AddressIndex } from "../ir-addresses.ts";
import type { AddrRefValue, KernelRouterNode, RouteNode } from "../protocol.ts";
import type { KernelRouter, OvnRouterEndpoint, Router } from "../types.ts";
import { resolveParent } from "./interfaces.ts";
import { lsId, vethShortId } from "./names.ts";

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
export function kernelRouterToIR(router: KernelRouter): KernelRouterNode {
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
  addrs: AddressIndex,
): Array<{ dst: string; via: AddrRefValue }> {
  const peer = transitPeer(kernelRouter, routers);
  if (!peer) return [];
  const domain = lsId(kernelRouter.transitDomain);
  const line = `kernelrouter:${kernelRouter.name}|side:left`;
  // By identity: a route node's nexthop is a reference to an Addr.
  const ownAddrs = new Set<Addr>(kernelRouter.left.ipaddrs);
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
      .map((r) => prefixOf(r.dst).to_string()),
  );
  const routes: Array<{ dst: string; via: AddrRefValue }> = [];
  for (const node of routeNodes) {
    if (node.key.ovnrouter !== peer.router.name) continue;
    if (!ownDomains.has(node.data.domain)) continue;
    const nexthop = addrs.addrOf(node.data.nexthop);
    if (nexthop !== undefined && ownAddrs.has(nexthop)) continue;
    const prefix = node.key.prefix;
    if (rightApplied.has(prefix)) continue;
    const via = peerAddrs.find((a) => a.is_ipv4() === !prefix.includes(":"));
    if (!via) continue;
    routes.push({
      dst: prefix,
      via: addrs.ref(via, `${line}|route:${prefix}|via`, [domain]),
    });
  }
  // The twin's own networks (its other legs): what a NAT gateway
  // masquerades onto internet-core comes back from there (ADR 0009).
  const have = new Set(routes.map((r) => r.dst));
  for (const endpoint of peer.router.endpoints) {
    if (endpoint === peer.endpoint) continue;
    for (const addr of endpoint.ipaddrs) {
      const dst = addr.network().to_string();
      if (have.has(dst) || rightApplied.has(dst)) continue;
      const via = peerAddrs.find((a) => a.is_ipv4() === addr.is_ipv4());
      if (!via) continue;
      routes.push({
        dst,
        via: addrs.ref(via, `${line}|route:${dst}|via`, [domain]),
      });
      have.add(dst);
    }
  }
  return routes;
}

// Addresses are already fully resolved facts (KernelRouterSide.ipaddrs)
// — no real iface binding yet (deliberately deferred, see
// KernelRouterSide's own doc comment, types.ts), so a dummy device
// stands in as the thing addresses/routes actually bind to (fabric/
// emit.py's own _emit_kernel_router_create) — assigning these to
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
// in; a side with no `ifaces` still gets the dummy (fabric/
// emit.py's own _emit_kernel_router_create). `iface` stays
// structurally unmodeled in the cross-language protocol, same as
// ovn.ls's own entries (see protocol.ts's OvnLsInterface).
export function kernelRouterSideToIR(
  router: KernelRouter,
  side: "left" | "right",
  routers: readonly Router[],
  routeNodes: readonly RouteNode[],
  addrs: AddressIndex,
): KernelRouterNode {
  const id = `kernelrouter:${router.name}|side:${side}`;
  // Its collision domain: the transit switch on the left; on the right the
  // switch its leg is plugged into, else the side itself (a WAN's LAN,
  // which only this side is on).
  const domain = side === "left"
    ? lsId(router.transitDomain)
    : router.upstreamDomain
    ? lsId(router.upstreamDomain)
    : id;
  const ipaddrs = addrs.own(router[side].ipaddrs, domain, id);
  const inDomain = router.routingDomains &&
    router.routingDomains.length > 0;
  const declaredRoutes = inDomain
    ? router[side].routes
      ?.filter((r) => r.via)
      .map((r) => {
        const dst = prefixOf(r.dst).to_string();
        return {
          dst,
          via: addrs.ref(toAddr(r.via!), `${id}|route:${dst}|via`, [domain]),
        };
      })
    : undefined;
  const backRoutes = side === "left"
    ? kernelRouterBackRoutes(router, routers, routeNodes, addrs)
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
  // (fabric/emit.py's _emit_kernel_router_create) — same
  // "generator computes the final name" boundary as shortIfaceName.
  // No `mac`: the netns-side leg's MAC is derived from its resolved
  // address by the runtime (ADR 0011 §5), so the OVN router's MAC_Binding
  // still survives the leg being recreated.
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
      },
    };
  });
  return {
    id,
    kind: "kernel.router",
    key: { name: router.name, side },
    data: {
      host: `host:${router.host.name}`,
      ipaddrs,
      routes: routes.length > 0 ? routes : undefined,
      ifaces: ifaces && ifaces.length > 0 ? ifaces : undefined,
      // The security group attached to THIS side's interface (right
      // side only in practice — set by buildKernelRouterEndpoint,
      // define.ts). Carried here as the generic `security.group` node's
      // name (`masq-<router>` for the masq shortcut): the attachment
      // point lives on the side node, the rules live in the group node
      // itself.
      ...(router[side].securityGroups && router[side].securityGroups.length > 0
        ? {
          securityGroups: router[side].securityGroups.map((g) => ({
            render: g.render,
            name: g.name,
          })),
        }
        : {}),
      // Applications running inside the netns on this side's real
      // interface (right side only in practice — the `kernel.app.*`
      // services resolved in buildKernelRouterEndpoint, define.ts). The
      // deployer turns each into its `ip netns exec` service script,
      // and stops/releases it on delete.
      apps: router[side].apps?.map((app) => {
        const withRefs = app.kind === "wireguard" || app.kind === "zerotier" ||
            (app.kind === "docker" && app.securityGroups)
          ? {
            ...app,
            securityGroups: (app.securityGroups ?? []).map((g) => ({
              render: g.render,
              name: g.name,
            })),
          }
          : app;
        // A VETH-CLIENT docker app needs SHORT, collision-free veth device
        // names (IFNAMSIZ ≤ 15): `ve-<hash>` / `ve-<hash>-c` derived from
        // the container name — same rule shortIfaceName applies everywhere
        // else in this file. An interface-OWNING docker (no veth addressing,
        // e.g. dhcpcd-in-docker) gets NO veth fields — its "owns the
        // interfaces" is exactly the ABSENCE of the veth machinery.
        if (app.kind === "docker" && app.ip) {
          const hash = fnv1a32(app.name).toString(16).padStart(8, "0");
          // The container-to-netns link is a collision domain of its own.
          const link = `${id}|app:${app.name}`;
          const { ip, routerIp, ...rest } = withRefs as typeof app;
          return {
            ...rest,
            ip: addrs.ownOne(ip!, link, `${link}|container`),
            ...(routerIp
              ? { routerIp: addrs.ownOne(routerIp, link, `${link}|router`) }
              : {}),
            vethName: `ve-${hash}`,
          };
        }
        // A docker app without veth addressing carries no address at all.
        return withRefs as unknown as NonNullable<
          KernelRouterNode["data"]["apps"]
        >[number];
      }),
    },
  };
}
