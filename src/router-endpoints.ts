// src/router-endpoints.ts — the endpoint builders: turn a router
// endpoint INPUT (ovn spec / kernel builder / tunnel spec) into the stored
// OvnRouterEndpoint, registering any KernelRouter/collision-domain it
// implies. Split out of define.ts (2026-09-08); state stays in
// NetworkBuilder, reached through the small RouterEndpointContext below.

import { fnv1a32, fnv1a64 } from "./addressing.ts";
import { IPv4, IPv6 } from "./ip.ts";
import {
  endpointBuilder,
  type KernelEndpointBuilderFn,
  normalizeKernelEndpoint,
  type OvnEndpointFn,
  type RouterBuilder,
  type RouterBuildResult,
} from "./builders.ts";
import type { SecurityGroupBuilder } from "./security-group.ts";
import type {
  CollisionDomain,
  Host,
  InterfaceKind,
  KernelApp,
  KernelRouter,
  KernelRouterSide,
  OvnRouterEndpoint,
  OvnRouterEndpointSpec,
  Router,
  RouterEndpointRoute,
  RoutingDomain,
  SecurityGroup,
  TunnelRouterEndpoint,
} from "./types.ts";

/** The slice of NetworkBuilder the endpoint builders need — state and
 * registration stay on the builder, this just names what they touch. */
export interface RouterEndpointContext {
  collisionDomain(name: string): CollisionDomain;
  defineOvnRouter(
    name: string,
    build: (router: RouterBuilder) => RouterBuildResult,
  ): Router;
  kernelRouter(
    name: string,
    endpoints: {
      readonly host: Host;
      readonly left: KernelRouterSide;
      readonly right: KernelRouterSide;
      readonly transitDomain: CollisionDomain;
      readonly transitPeerAddrs: readonly (IPv4 | IPv6)[];
      readonly upstreamPeerAddrs: readonly (IPv4 | IPv6)[];
      readonly routingDomains?: readonly RoutingDomain[];
    },
  ): KernelRouter;
  securityGroup(
    name: string,
    build: (group: SecurityGroupBuilder) => void,
  ): SecurityGroup;
  securityGroupDeclared(group: SecurityGroup): boolean;
}

/** Which builder produced an endpoint — part of its identity (see
 * deriveEndpointName below), so a kernel/tunnel endpoint never shares a
 * name with a plain ovn one that happens to sit on the same derived
 * transit domain. */
export type EndpointRole = "ovn" | "kernel" | "tunnel";

/** The endpoint's stable identity — see EndpointBase.name (types.ts).
 * Folded ONLY from immutable, identity-defining configuration: the
 * enclosing router, the role that produced it, and the collision domain
 * it binds. Deliberately NOT from addresses, mac, ifaces, services, routes
 * or routingDomains: those are STATE of an already-identified port, and
 * folding them in would turn every address/service edit into a
 * delete+create at reconcile time. An explicit `name` (the input half of
 * the contract, EndpointDefinition) wins outright. */
export function deriveEndpointName(
  endpoint: { readonly name?: string },
  routerName: string,
  role: EndpointRole,
  attachmentDomain: string,
): string {
  if (endpoint.name) return endpoint.name;
  return `lrp-${fnv1a64(`${routerName}\n${role}\n${attachmentDomain}`)}`;
}

/** Tags a plain input object as the `kind: "ovn"` RouterEndpoint —
 * no real transformation beyond stamping the derived `name`, just keeps
 * `kind: "ovn"` from ever being
 * hand-typed at a net.ovnRouter() call site. Private: only reachable
 * as `router.ovnRouterEndpoint()` inside an ovnRouter() callback
 * (2026-08-12) — matches kernelRouterEndpoint() below, which
 * genuinely needs the callback's own RouterBuilder for
 * routingDomains; this one carries no such need but stays alongside
 * it for symmetry rather than being reachable a different way. */
export function buildOvnRouterEndpoint(
  input: Omit<OvnRouterEndpointSpec, "kind"> | OvnEndpointFn,
  context: { readonly routerName: string; readonly role: EndpointRole },
): OvnRouterEndpoint {
  const spec = typeof input === "function" ? input(endpointBuilder()) : input;
  const name = deriveEndpointName(
    spec,
    context.routerName,
    context.role,
    spec.l2Segment.name,
  );
  // `name` LAST: an input spec may carry an explicit (optional) `name`,
  // but the resolved endpoint's name is always the derived/pinned value —
  // a spread of `spec` after it would otherwise write `name: undefined`
  // back when the spec left it unset.
  const base = { ...spec, kind: "ovn" as const, name };
  // Inject THIS endpoint into every service entry (EndpointService<T> =
  // T & { endpoint }) so the generator can read the endpoint's
  // l2Segment/ipaddrs/routes per service (2026-09-08). The endpoint
  // referenced is the un-wrapped one — its own fields are identical.
  const endpoint = base as unknown as OvnRouterEndpoint;
  if (!base.services) return endpoint;
  return {
    ...base,
    services: base.services.map((service) => {
      const entry = { ...service, endpoint };
      // Attaching a reusable workload records the ENDPOINT-REFERENCE on
      // the service — the wired entry itself ({ ...service, endpoint }) —
      // so the service points back at the endpoints that attach it.
      if (entry.kind === "service.attach") {
        entry.srvRef.endpointRefs.push(entry);
      }
      return entry;
    }),
  } as OvnRouterEndpoint;
}
/** The entry point where an OVN<->kernel transit link actually gets
   * created ("that was the entrypoint where we created the transit" —
   * 2026-08-12 design discussion). Creates a fresh, auto-named transit
   * CollisionDomain (`transit-<routerName>` — `router-voda-avm-v2` ->
   * a `ls:transit-router-voda-avm-v2` IR node, whose bridge/bridge-
   * mapping/localnet-port names the deployer derives from the same
   * name) AND a KernelRouter named after the enclosing
   * net.ovnRouter() (a `kernel.router` IR node named
   * `router-voda-avm-v2`, whose netns the deployer derives as
   * `ns-router-voda-avm-v2`), then returns the OVN side of the transit
   * link as a plain
   * OvnRouterEndpoint, ready to drop straight into net.ovnRouter()'s
   * left/right.
   *
   * `input.transit` is a TransitNetwork — always built by calling
   * transitNetwork(ipv4, ipv6) (addressing.ts) in the topology itself,
   * never assembled by hand, so it's always a valid transit pair here.
   * `.left` becomes the OVN side's own address (folded into the
   * returned OvnRouterEndpoint below); `.right` becomes the
   * KernelRouter's OWN transit-facing side (confirmed live, 2026-08-12:
   * `ip netns exec ns-uplink-voda-avm ip a` — veth-krn-0, the kernel
   * side of the SAME veth pair). `input.ipaddrs` becomes the
   * KernelRouter's real-world-facing side (confirmed live: ens18.1280,
   * the actual WAN interface in that same capture) — no real iface
   * binding for either KernelRouter side yet, that's its own next step
   * (KernelRouterSide, types.ts). No l2Segment on the input at all:
   * that's a purely OVN concept, and this input describes the KERNEL
   * side — it has no business naming an OVN domain to bind to.
   * Everything else (`Omit<KernelRouterEndpoint, "kind">` minus `host`/
   * `transit`/`ifaces`, consumed below — ipaddrs, mac, gatewayChassis,
   * securityGroup, services, routes) carries straight through to the
   * returned OvnRouterEndpoint, symmetric with ovnRouterEndpoint()
   * above taking `Omit<OvnRouterEndpoint, "kind">` — EXCEPT `services`
   * (split: the `kernel.*` kinds are consumed here into the
   * KernelRouter's security group, only the `ipv6.*` kinds reach the
   * OVN endpoint) and `securityGroup` (consumed here — attached to the
   * KernelRouter's `right`, masq shortcut expansion, see below). `ifaces`
   * is the exception that lands ONLY on the paired KernelRouter's `right`
   * (KernelRouterSide.ifaces, types.ts — the real-world-facing
   * interface the deployer moves into the netns). The transit domain
   * still gets its localnet port/gateway-chassis pin/bridge binding/
   * bridge-mapping from the returned OVN endpoint's OWN ifaces, which
   * are the transit veth (constructed explicitly here, 2026-08-18). `routes` in
   * particular matters here: the returned endpoint is a real
   * OvnRouterEndpoint, and it's frequently the RoutingDomain anchor
   * (e.g. a real ISP default route) despite being kernel-backed, so it
   * needs the same `.routes` a hand-declared OvnRouterEndpoint would
   * (RouterEndpointRoute, types.ts) — EXCEPT that a route's `via` is
   * ALWAYS rewritten to the paired KernelRouter's transit-facing
   * address here, regardless of whether the config author declared one.
   * The OVN side of a transit link has exactly ONE reachable peer — the
   * paired KernelRouter — so a literal `via` (e.g. the real ISP gateway
   * 192.168.132.1 in the voda-avm config) is a fact about the kernel
   * netns's WAN side, not about anything OVN can ARP on the transit
   * link: keeping it made the OVN logical router believe
   * 192.168.132.0/24 was directly connected on the transit port and
   * emit ARP requests for its WAN gateway out the transit veth
   * (confirmed live, 2026-08-21). This also supersedes the "via-less
   * means handled elsewhere" reading a client-facing endpoint gives a
   * route (RouterEndpointRoute's own doc comment: "e.g. SLAAC/RA on a
   * client-facing segment... or an existing less-specific route already
   * covering it there") — there is no SLAAC/RA on a transit link, so no
   * route declared here may be left for computeRoutes (src/ir.ts) to
   * skip. The literal `via` is NOT lost: `input.routes` lands
   * unmodified on the KernelRouter's own `right` side (below), where
   * the kernel netns applies it as its own real default gateway.
   *

   * `routingDomains` isn't part of `input` — it's whatever the
   * enclosing ovnRouter() callback's `router.routingDomains` is set to
   * AT THE TIME this runs (read live off the RouterBuilder by
   * ovnRouter() above, not an argument a config author passes here
   * directly), stamped onto the KernelRouter this creates so its own
   * routes are gated by the same RoutingDomain-membership rule an
   * OVN-side route already is (src/ir.ts's kernelRouterSideToIR). */
export function buildKernelRouterEndpoint(
  ctx: RouterEndpointContext,
  input: KernelEndpointBuilderFn,
  routingDomains: readonly RoutingDomain[] | undefined,
  routerName: string,
): OvnRouterEndpoint {
  input = normalizeKernelEndpoint(input);
  const {
    transit: link,
    host,
    ipaddrs,
    ifaces,
    services,
    securityGroup,
    routes: inputRoutes,
    ...rest
  } = input;
  const transitDomain = ctx.collisionDomain(`transit-${routerName}`);
  const ovnSideAddrs = [link.left.ipv4, link.left.ipv6]
    .filter((a): a is IPv4 | IPv6 => Boolean(a));
  const kernelSideAddrs = [link.right.ipv4, link.right.ipv6]
    .filter((a): a is IPv4 | IPv6 => Boolean(a));

  // Kernel-side services (the `kernel.*` kinds of RouterEndpointService
  // — today `kernel.ipv4.masq`/`kernel.ipv6.masq`, later docker/
  // wireguard...) apply INSIDE the KernelRouter's netns, never to the
  // OVN twin's LRP — so they're split off here before `...rest`
  // reaches buildOvnRouterEndpoint, where resolveIpv6RaConfigs
  // (src/ir.ts) would throw on a kind it doesn't know. The OVN
  // endpoint keeps only the `ipv6.*` services.
  const kernelServices = services?.filter((s) => s.kind.startsWith("kernel."));
  const ovnServices = services?.filter((s) => !s.kind.startsWith("kernel."));
  // `.masq` is a SHORTCUT for a self-defined security group: it
  // expands (through net.securityGroup() — the SAME builder a config
  // author would call explicitly) to a group named `masq-<router>`
  // containing one MASQUERADE rule per declared masq family. An
  // EXPLICIT `securityGroup` on the endpoint wins outright — masq is
  // then IGNORED (the author takes responsibility for the group's own
  // content, so no rules are derived from the services) — and must
  // have been declared via net.securityGroup() in this same
  // defineNetwork call, same "register + fail fast" split as every
  // other cross-reference here.
  if (
    securityGroup &&
    !ctx.securityGroupDeclared(securityGroup)
  ) {
    throw new Error(
      `kernelRouterEndpoint: security group "${securityGroup.name}" was ` +
        `not declared via net.securityGroup() in this defineNetwork call`,
    );
  }
  const masqKinds = (kernelServices ?? []).filter((s) =>
    s.kind === "kernel.ipv4.masq" || s.kind === "kernel.ipv6.masq"
  );
  const securityGroupDef = securityGroup
    ? securityGroup
    : masqKinds.length > 0
    ? ctx.securityGroup(`masq-${routerName}`, (group) => {
      for (const s of masqKinds) {
        group.masq(s.kind === "kernel.ipv4.masq" ? "ipv4" : "ipv6");
      }
    })
    : undefined;
  // `kernel.app.*` services resolve into app descriptors running INSIDE
  // the netns on the right side's real interface — independent of the
  // security-group shortcut above (an explicit `securityGroup` does
  // NOT suppress them; only the masq services it overrides).
  const appServices = (kernelServices ?? []).filter((s) =>
    s.kind.startsWith("kernel.app.")
  );
  const apps: KernelApp[] = [];
  for (const s of appServices) {
    if (s.kind === "kernel.app.dhcp-client") {
      // dhcpcd is expressed as a GENERIC docker app that OWNS the
      // router's interfaces (the container-owns-the-interfaces mode,
      // signaled by omitting veth addressing) — image defaulted to the
      // router name, cmd dhcpcd, and a build that bakes dhcpcd into the
      // image (2026-08-31). dhclient stays a plain in-netns client.
      if (s.style === "dhcpcd") {
        // The container OWNS the router's interfaces (the "container IS the
        // router" mode): it has NO veth addressing — the IR signals "owns
        // interfaces" by omitting `ip`/`routerIp` (the deployer detects it
        // structurally, never a TS flag, 2026-08-31).
        apps.push({
          kind: "docker",
          image: `ovn-fabric-${routerName}`,
          name: `${routerName}-dhcpcd`,
          cmd: ["/sbin/dhcpcd"],
          build: { from: "alpine:latest", packages: ["dhcpcd"] },
        });
      } else {
        apps.push({ kind: "dhcp-client", style: s.style });
      }
    } else if (s.kind === "kernel.app.docker") {
      // The container name is resolved HERE — the service's `name`
      // PREFIXED with the router name (so it's globally unique and
      // delete can `docker rm -f` exactly what create started), the
      // `cmd` string split into `docker run` trailing args, and the
      // container's veth addresses resolved: the container gets
      // `ip`, the router end of the veth is the subnet's first host.
      // `ip` is OPTIONAL — when omitted, a deterministic per-router
      // slot is derived (10.200.<fnv1a32(routerName) % 256>.2/24) so
      // the config stays concise, same "derived at declaration time,
      // never at runtime" reasoning as every other address here.
      const cmd = typeof s.cmd === "string"
        ? s.cmd.trim().split(/\s+/).filter((t) => t.length > 0)
        : s.cmd;
      const containerIp =
        (s.ipaddrs && s.ipaddrs.length > 0 ? s.ipaddrs[0] : undefined) ??
          IPv4.parse(`10.200.${fnv1a32(routerName) % 256}.2/24`);
      const routerIp = containerIp.network().first();
      apps.push({
        kind: "docker",
        // `image` is optional — defaults to the router name (2026-08-31).
        image: s.image ?? `ovn-fabric-${routerName}`,
        ...(s.build ? { build: s.build } : {}),
        name: s.name ? `${routerName}-${s.name}` : `${routerName}-docker`,
        ...(cmd && cmd.length > 0 ? { cmd } : {}),
        ip: containerIp.to_string(),
        routerIp: routerIp.to_string(),
      });
    }
  }

  // `routes` also lands on the KernelRouter's own right side (the
  // real-world-facing one, see KernelRouterSide's own doc comment) —
  // it's a physical fact about that real device's own routing table,
  // not just the OVN logical router's — UNMODIFIED (ovnSideRoutes
  // above is a separate, via-rewritten copy for the OVN side only;
  // the kernel netns's own real gateway, e.g. the actual ISP address,
  // is a different fact that belongs on the kernel side of the
  // transit link, exactly where the unmodified copy keeps it).
  const ovnSideRoutes = inputRoutes?.map((route) => {
    // The OVN side of a transit link has exactly ONE reachable peer —
    // the paired KernelRouter — so every route declared on this
    // endpoint is rewritten to point at the kernel router's OWN
    // transit-facing address, regardless of whether the config author
    // declared a literal `via` (e.g. the real ISP gateway). A literal
    // `via` is a fact about the kernel netns's WAN side, not about
    // anything OVN can reach on the transit link (confirmed live,
    // 2026-08-21: keeping 192.168.132.1 made the OVN logical router
    // ARP for its WAN gateway out the transit veth).
    const via = route.dst.is_ipv4() ? link.right.ipv4 : link.right.ipv6;
    if (!via) {
      throw new Error(
        `kernelRouterEndpoint: route to ${route.dst.to_string()} has ` +
          `no matching-family kernel-side address on the transit link ` +
          `to route via`,
      );
    }
    return { ...route, via };
  });

  // The transit link's own veth pair — the OVN-side endpoint on the
  // transit network (.first() side) carries it EXPLICITLY, so the
  // transit ovn.ls keeps its bridge binding (localnet port, gateway
  // chassis, ovn-bridge-mappings) from the endpoint's ifaces, the
  // same source every other domain uses — no ir.ts backfill (2026-08-
  // 18). LONG names here on purpose — the IFNAMSIZ-safe shortening is
  // the lower layers' job (src/ir.ts's kernelRouterSideToIR and
  // collisionDomainToIR, same as bridges' shortName).
  const transitVeth: InterfaceKind = {
    kind: "veth",
    ifaceName: `veth-krn-${routerName}`,
    peerName: `veth-ovn-${routerName}`,
  };

  const kernelDomainMembership = input.routingDomains ?? routingDomains;
  ctx.kernelRouter(routerName, {
    host,
    left: {
      ipaddrs: kernelSideAddrs,
      // The transit link's own veth pair, constructed implicitly from
      // the enclosing ovnRouter()'s name — no config surface for it
      // (2026-08-18). `ifaceName` is the leg that lives in this
      // kernel router's netns (addresses/routes bind to it),
      // `peerName` the root-side leg the transit domain's bridge
      // attaches to.
      ifaces: [{ host, iface: transitVeth }],
    },
    right: {
      ipaddrs,
      ...(input.routes ? { routes: input.routes } : {}),
      ...(ifaces ? { ifaces } : {}),
      ...(apps.length > 0 ? { apps } : {}),
      ...(securityGroupDef ? { securityGroup: securityGroupDef } : {}),
    },
    transitDomain,
    transitPeerAddrs: ovnSideAddrs,
    // A plain kernelRouterEndpoint() has no upstream leg — its real
    // default lives in `right`'s own routes — so the upstream peers are
    // always present but EMPTY (only tunnelRouterEndpoint() fills them).
    upstreamPeerAddrs: [],
    // The endpoint's OWN per-endpoint routingDomains override the
    // router-level ones stamped by ovnRouter() (2026-08-23) — an
    // endpoint-level membership gates only THIS side's routes.
    ...(kernelDomainMembership
      ? { routingDomains: kernelDomainMembership }
      : {}),
  });

  return buildOvnRouterEndpoint({
    ...rest,
    ...(ovnSideRoutes ? { routes: ovnSideRoutes } : {}),
    l2Segment: transitDomain,
    ...(ovnServices && ovnServices.length > 0 ? { services: ovnServices } : {}),
    // Only the transit-side addresses live on the OVN port: `ipaddrs`
    // (the real-world-facing ones, e.g. 192.168.132.93/24) belong to
    // the kernel netns's OWN interface (KernelRouterSide.right above),
    // not to the OVN side of the transit link. Putting them here made
    // OVN treat the WAN subnet as directly connected on the transit
    // port and ARP for the WAN gateway out the transit veth
    // (confirmed live, 2026-08-21).
    ipaddrs: ovnSideAddrs,
    ifaces: [{ host, iface: transitVeth }],
  }, { routerName, role: "kernel" });
}

/** The generic "ANY TUNNEL" router (2026-08-23 design discussion) —
 * WireGuard today, ZeroTier later: a kernel netns with a MESH transit
 * on one side (left — same mechanics as kernelRouterEndpoint()'s
 * `transit`), an UPSTREAM transit on the other (right — the tunnel's
 * own endpoint UDP reaches the real internet via this leg, never
 * through the tunnel itself), and the tunnel interface in the middle
 * (created by the `kernel.app.wireguard` service). The tunnel egress
 * is the ANCHOR's default: `routes` carries the via-less
 * `0.0.0.0/0`/`::/0` that computeRoutes rewrites to the domain's
 * other participants (each learns `0.0.0.0/0 via <this router's
 * backbone address>`); the netns's OWN default route goes out the
 * upstream leg, via the upstream peer.
 *
 * Per-endpoint routingDomains (2026-08-23): the returned OVN endpoint
 * and the KernelRouter are both stamped from `input.routingDomains`
 * — a tunnel router anchors one domain from its LEFT (the tunnel's
 * default) and participates in another from its RIGHT (e.g.
 * Voda-defaultRoute on the backbone side). */
export function buildTunnelRouterEndpoint(
  ctx: RouterEndpointContext,
  input: Omit<TunnelRouterEndpoint, "kind">,
  routerName: string,
  subRouters: Router[],
): OvnRouterEndpoint {
  const {
    transit: meshLink,
    upstream: upLink,
    upstreamBackbone,
    upstreamDomains,
    host,
    services,
    routes,
    routingDomains,
    ...rest
  } = input;
  const transitDomain = ctx.collisionDomain(`transit-${routerName}`);
  const backdoorDomain = ctx.collisionDomain(`backdoor-${routerName}`);
  const ovnSideAddrs = [meshLink.left.ipv4, meshLink.left.ipv6]
    .filter((a): a is IPv4 | IPv6 => Boolean(a));
  const kernelSideAddrs = [meshLink.right.ipv4, meshLink.right.ipv6]
    .filter((a): a is IPv4 | IPv6 => Boolean(a));
  const upstreamKernelAddrs = [upLink.right.ipv4, upLink.right.ipv6]
    .filter((a): a is IPv4 | IPv6 => Boolean(a));
  const upstreamPeerAddrs = [upLink.left.ipv4, upLink.left.ipv6]
    .filter((a): a is IPv4 | IPv6 => Boolean(a));

  // The tunnel WORKLOAD is a `wireguard`/`zerotier` entry in services[]
  // (the config-side shortcut) — define.ts maps it to the kernel app; the
  // IR never sees wireguard/zerotier (2026-09-08). The egress masq now
  // lives ON the workload (masq), not as separate kernel.*.masq services.
  const workload = (services ?? []).find(
    (s) => s.kind === "wireguard" || s.kind === "zerotier",
  );
  if (workload === undefined) {
    throw new Error(
      `tunnelRouterEndpoint "${routerName}": services[] must include a ` +
        `wireguard or zerotier workload`,
    );
  }
  const masq = workload.masq ?? [];
  const ovnServices = (services ?? []).filter(
    (s) => s.kind === "ipv6.slaac" || s.kind === "ipv6.ra",
  );
  const apps: KernelApp[] = [
    workload.kind === "wireguard"
      ? {
        kind: "wireguard",
        ifaceName: workload.ifaceName,
        config: workload.config,
        masq,
      }
      : {
        kind: "zerotier",
        networkId: workload.networkId,
        // `instanceDir` is optional on the tunnel — derived from the
        // router name when omitted (types.ts), so a config author only
        // sets it when the default location is wrong.
        instanceDir: workload.instanceDir ??
          `/var/lib/zerotier-one-${routerName}`,
        masq,
        // The tunnel's via-less declared routes (e.g. the ztnet mesh
        // supernet 192.168.0.0/16) are the routes the zerotier netns
        // must push OUT the tunnel once ZeroTier names the interface —
        // carried here (not on the kernel router's own routes, which
        // would target the upstream veth) so the wire script applies
        // them over the runtime interface (2026-08-30).
        routes: (routes ?? [])
          .filter((r) => !r.via)
          .map((r) => ({ dst: r.dst.to_string() })),
      },
  ];

  // The OVN side: rewrite each via-less route (the tunnel's egress
  // supernet, e.g. 192.168.0.0/16) to point at the KERNEL side of the
  // mesh transit (`meshLink.right` — the address the kernel netns's
  // veth owns on the transit L2). The OVN logical router (R2) ARPs for
  // that address on the transit and forwards the supernet INTO the
  // netns, which then egresses it over the tunnel. Without it R2 has
  // no route for the supernet at all — computeRoutes drops via-less
  // routes for the anchor (confirmed missing live, 2026-08-30). Via
  // routes are left as the author declared them.
  const ovnSideRoutes = (routes ?? []).map((route) => {
    if (route.via) return route;
    const via = route.dst.is_ipv4() ? meshLink.right.ipv4 : meshLink.right.ipv6;
    if (!via) {
      throw new Error(
        `tunnelRouterEndpoint: via-less route to ${route.dst.to_string()} has no ` +
          `matching-family kernel-side transit address to forward into the netns`,
      );
    }
    return { ...route, via };
  });

  // The netns's OWN default route goes out the upstream leg, via the
  // upstream peer — that's how the tunnel's endpoint UDP escapes
  // (wg-quick's fwmark policy routing keeps it out of the tunnel).
  const upstreamRoutes: RouterEndpointRoute[] = [];
  if (upLink.left.ipv4) {
    upstreamRoutes.push({
      dst: IPv4.parse("0.0.0.0/0"),
      via: upLink.left.ipv4,
    });
  }
  if (upLink.left.ipv6) {
    upstreamRoutes.push({
      dst: IPv6.parse("::/0"),
      via: upLink.left.ipv6,
    });
  }

  const transitVeth: InterfaceKind = {
    kind: "veth",
    ifaceName: `veth-krn-${routerName}`,
    peerName: `veth-ovn-${routerName}`,
  };
  const upstreamVeth: InterfaceKind = {
    kind: "veth",
    ifaceName: `veth-up-${routerName}`,
    peerName: `veth-up-${routerName}-peer`,
  };

  // The SECOND router the tunnel needs (2026-08-23, "defaultendpoint-to-
  // wg, backbone-leg"): an OVN router `<router>-upstream` that
  // terminates the tunnel's upstream/backdoor leg (the backdoor LRP,
  // the netns's default gateway) and carries a backbone leg out to the
  // mesh — whose default routes the tunnel's endpoint UDP on to the
  // physical WAN (e.g. voda-avm). The tunnel netns's upstream veth
  // binds to the backdoor domain's localnet bridge, and the internal
  // router's backdoor port carries that same veth for the bridge
  // binding (mirror of how the mesh transit binds its own veth).
  const upstreamPeer = ctx.defineOvnRouter(
    `${routerName}-upstream`,
    () => ({
      routingDomains: upstreamDomains ?? [],
      endpoints: [
        buildOvnRouterEndpoint({
          l2Segment: backdoorDomain,
          ipaddrs: upstreamPeerAddrs,
          ifaces: [{ host, iface: upstreamVeth }],
        }, { routerName: `${routerName}-upstream`, role: "ovn" }),
        buildOvnRouterEndpoint({
          l2Segment: upstreamBackbone.l2Segment,
          ipaddrs: upstreamBackbone.ipaddrs,
        }, { routerName: `${routerName}-upstream`, role: "ovn" }),
      ],
    }),
  );
  subRouters.push(upstreamPeer);

  ctx.kernelRouter(routerName, {
    host,
    left: {
      ipaddrs: kernelSideAddrs,
      ifaces: [{ host, iface: transitVeth }],
    },
    right: {
      ipaddrs: upstreamKernelAddrs,
      ...(upstreamRoutes.length > 0 ? { routes: upstreamRoutes } : {}),
      ifaces: [{ host, iface: upstreamVeth }],
      ...(apps.length > 0 ? { apps } : {}),
    },
    transitDomain,
    transitPeerAddrs: ovnSideAddrs,
    upstreamPeerAddrs,
    ...(routingDomains ? { routingDomains } : {}),
  });

  return buildOvnRouterEndpoint({
    ...rest,
    ...(ovnSideRoutes ? { routes: ovnSideRoutes } : {}),
    ...(routingDomains ? { routingDomains } : {}),
    l2Segment: transitDomain,
    ...(ovnServices && ovnServices.length > 0 ? { services: ovnServices } : {}),
    ipaddrs: ovnSideAddrs,
    ifaces: [{ host, iface: transitVeth }],
  }, { routerName, role: "tunnel" });
}
