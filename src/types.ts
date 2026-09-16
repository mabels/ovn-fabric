// types.ts — the shapes a topology config declares: hosts, collision
// domains, routers/router endpoints, kernel routers, and services. No
// topology data lives here. This file defines the SHAPE; define.ts
// declares the FACTS.

import type { TransitNetwork } from "./addressing.ts";
import type { IPv4, IPv6 } from "./ip.ts";

// ── host / chassis ─────────────────────────────────────────────────
// Where a given router's OVN/OVS configuration is actually applied. A
// given deployment may run everything on one chassis, but the model
// does not assume that — each router endpoint declares the Host its
// interfaces live on, so a future topology with multiple chassis is a
// config change, not a redesign.

export type AccessMethod =
  | { method: "ssh"; user: string }
  | { method: "local" }; // the generator's own host — no SSH needed

// ── host address: fqdn and/or real IPv4/IPv6 ────────────────────────
// A host can be reachable at more than one of these simultaneously — an
// FQDN for humans/SSH, real IPv4/IPv6 for OVN's own Geneve tunnel
// endpoint (see OvnHostConfig.encapIp below). These are genuinely
// independent concerns that can differ (SSH via a management name,
// tunnel traffic via a dedicated network) — not three equivalent ways
// to spell the same thing. At least one field must be given; validated
// at construction (sshHost/localHost), not statically enforceable
// without a much less readable union type.
export interface HostAddress {
  readonly fqdn?: string;
  readonly ipv4?: IPv4;
  readonly ipv6?: IPv6;
}

function primaryHostAddress(address: HostAddress): string {
  if (address.fqdn) return address.fqdn;
  if (address.ipv4) return address.ipv4.to_s();
  if (address.ipv6) return address.ipv6.to_s();
  throw new Error(
    "HostAddress requires at least one of fqdn/ipv4/ipv6",
  );
}

// ── OVN cluster membership ──────────────────────────────────────────
// `defineNetwork` is one OVN cluster (one shared control plane, ADR
// 0003), not one host — every Host participating in it is a chassis,
// and exactly one role in the cluster is "central" (runs ovn-central:
// northd + the NB/SB databases). Every other chassis runs only
// ovn-host/ovn-controller, pointed at the central chassis's SB DB
// remotely instead of a local one — this is the real, previously
// undone TODO from the legacy generator's requiredPackages doc comment
// (2026-07-19): today every Host gets its own full independent stack,
// so N hosts under one defineNetwork silently become N uncoordinated
// clusters, not one shared one.
export type OvnRole =
  | { readonly kind: "central" }
  | { readonly kind: "chassis" };

export interface OvnHostConfig {
  readonly role: OvnRole;
  /** Real IP used as this chassis's Geneve tunnel endpoint
   * (external-ids:ovn-encap-ip). Defaults to this Host's own
   * address.ipv4 ?? address.ipv6 when omitted — only needed as an
   * explicit override when tunnel traffic and SSH/generator
   * reachability use different networks. Never an FQDN: the SB DB's
   * own Chassis.encaps.ip column is a real address, not a hostname —
   * confirmed against a live 3-chassis cluster, 2026-08-09. */
  readonly encapIp?: IPv4 | IPv6;
  /** Defaults to "geneve" — OVN's modern default, and what every real
   * chassis tested against so far actually uses. */
  readonly encapType?: "geneve" | "stt" | "vxlan";
}

// ── OVN cluster-wide (NB_Global) options ────────────────────────────
// Set once per cluster, not per chassis — these live on the NB
// database itself. See ADR 0003 for where each of these came from
// (OVN's real NB_Global schema, not this project's invention).
export interface OvnClusterOptions {
  /** Encrypt every inter-chassis Geneve tunnel via IPsec (ESP, IKE via
   * libreswan/ovn-ipsec). Off by default — no real deployment uses
   * this yet; identified against a live cluster but not yet verified
   * end to end (ADR 0003, "Open questions"). */
  readonly ipsec?: boolean;
  /** OUI-ish prefix OVN uses when auto-generating MAC addresses for
   * ports declared with dynamic addressing. Unused by this project
   * today — every router port declares an explicit MAC. */
  readonly macPrefix?: string;
  /** Batches similar logical switches into shared flow tables — a real
   * scale optimization, irrelevant at today's segment counts. */
  readonly useLogicalDpGroups?: boolean;
  readonly northd?: {
    readonly probeInterval?: number;
    readonly threads?: number;
  };
}

// ── monitoring: IPFIX flow export ──────────────────────────────────
// Host-level, not per-Segment/Uplink, and deliberately scoped to
// br-int only: that's OVN's own integration bridge, which already
// carries every logical packet for every segment/uplink resolved onto
// this chassis (ovn-controller creates and owns br-int itself — this
// generator never creates it, only points IPFIX at it). Sampling
// there once captures the whole chassis' traffic; sampling per-segment
// bridge instead would mean N separate IPFIX declarations reporting
// overlapping/duplicate data for anything that also crosses br-int.
export interface IpfixExport {
  /** "<collector-ip>:<port>" — passed straight to `ovs-vsctl create
   * IPFIX targets=...`. E.g. an Akvorado inlet's NodePort listener. */
  readonly target: string;
  /** 1 = every packet, no sampling. N = export 1 in every N packets.
   * Omit to leave OVS's own IPFIX table default in place (unsampled). */
  readonly sampling?: number;
  /** Seconds of inactivity before a cached flow is force-expired and
   * exported. Omit for OVS's own default. */
  readonly cacheActiveTimeout?: number;
  readonly cacheMaxFlows?: number;
}

export interface HostMonitoring {
  readonly ipfix?: IpfixExport;
}

/** The OS a Host runs (2026-08-23) — what the deployer needs to install
 * the topology's dependencies in the right package form: OVS/OVN for a
 * chassis, iproute2/iptables for a kernel router's netns, dhcpcd or
 * isc-dhcp-client for a dhcp-client app, wireguard-tools, the zerotier
 * curl installer, docker. `name` is the distro, `version` its release
 * ("24.04" for Ubuntu, "12" for Debian). Required to know HOW to
 * install; no deinstall ever happens (2026-08-23). */
export interface HostOs {
  readonly name: "ubuntu" | "debian";
  readonly version: string;
}

export interface Host {
  readonly name: string;
  readonly address: HostAddress;
  /** What the generator actually connects to — derived once from
   * `address` (fqdn, else ipv4, else ipv6) rather than re-picked
   * ad hoc everywhere a connection string is needed. */
  readonly connectAddress: string;
  readonly access: AccessMethod;
  /** The distro/version the deployer installs dependencies for — see
   * HostOs. Undefined = no dependencies can be installed (the host's
   * own setup is the operator's responsibility). */
  readonly os?: HostOs;
  readonly monitoring?: HostMonitoring;
  /** Undefined means this Host is not part of the OVN cluster at all
   * (e.g. a pure bastion/management host with no chassis role) — no
   * ovn-central/ovn-host gets installed there. See OvnHostConfig. */
  readonly ovn?: OvnHostConfig;
}

export function sshHost(
  name: string,
  address: HostAddress,
  user: string,
  os?: HostOs,
  ovn?: OvnHostConfig,
  monitoring?: HostMonitoring,
): Host {
  return {
    name,
    address,
    connectAddress: primaryHostAddress(address),
    access: { method: "ssh", user },
    ...(os ? { os } : {}),
    ...(ovn ? { ovn } : {}),
    ...(monitoring ? { monitoring } : {}),
  };
}

export function localHost(
  name: string,
  os?: HostOs,
  ovn?: OvnHostConfig,
  monitoring?: HostMonitoring,
): Host {
  return {
    name,
    address: { fqdn: "localhost" },
    connectAddress: "127.0.0.1",
    access: { method: "local" },
    ...(os ? { os } : {}),
    ...(ovn ? { ovn } : {}),
    ...(monitoring ? { monitoring } : {}),
  };
}

// ── collision domain: a bare OVN logical switch ─────────────────────
// The L2-only primitive underneath Uplink/Segment — those are a
// SUPERSET (a collision domain plus addressing, uplink selection,
// routes, NAT: real L3 concerns, deliberately out of scope here). A
// collision domain is just a name plus whichever real interfaces have
// been attached to it — matches OVN's own minimal Logical_Switch shape
// (everything else — ports, ACLs, DHCP options — hangs off it as
// separate, related objects, not fields on the switch itself).
//
// Deliberately NOT Host-scoped, unlike Uplink/Segment: a real OVN
// logical switch is a cluster-wide object, not owned by any one
// chassis — confirmed live (ADR 0003): sw-test's two ports lived on
// two different chassis, at two different physical sites, with no
// single chassis "owning" the switch itself.
//
// addInterface(), not an `if` field: physical attachment is additive,
// not a fixed one-shot property of the domain — a real logical switch
// can carry more than one localnet port (e.g. redundant physical
// attachment across chassis), and there's no reason to special-case
// "the first/only one" as a constructor field while every subsequent
// one needs a different mechanism. A class with mutable internal state
// (not a plain readonly interface), matching how ManualUplink already
// holds mutable state (`switchTo`) elsewhere in this file — a bare
// data literal can't expose a method.
export class CollisionDomain {
  readonly name: string;
  private readonly interfaces: InterfaceKind[] = [];

  constructor(name: string) {
    this.name = name;
  }

  /** Returns the same `iface` it was given — so a caller can register
   * it and get a handle back in one expression, e.g.
   * `ifaces: [{ host, iface: domain.addInterface({...}) }]`, rather
   * than needing a separate statement before the call that uses it. */
  addInterface(iface: InterfaceKind): InterfaceKind {
    this.interfaces.push(iface);
    return iface;
  }

  get allInterfaces(): readonly InterfaceKind[] {
    return this.interfaces;
  }
}

// ── security groups: not designed yet ───────────────────────────────
// A dummy placeholder, not a real mechanism — see ADR 0002's "Firewall
// / security-group subschema" (net.securitygroup/net.sgattachment) for
// the intended real shape. Exists so RouterEndpoint (below) has
// somewhere to hold "this will need a security group eventually"
// without pretending the real mechanism exists yet, and without
// silently omitting the field and forgetting the gap is there. Also:
// as of OVN 26.03.0, whether ACLs even attach to a router port
// directly (vs. only a Logical_Switch/Port_Group) hasn't been verified
// against the live cluster (ADR 0003) — this may end up belonging on
// CollisionDomain instead of RouterEndpoint once that's checked.
export interface SecurityGroupRef {
  readonly name: string;
}

/** One rule inside a security group — generic vocabulary, deliberately
 * implementation-abstract (see SecurityGroup's own doc comment): a rule
 * is expressed in terms any backend can translate to its own concrete
 * command, not in iptables/nft/OVN-ACL syntax. Today the only kind is
 * `masq` (masquerade egress from the attached interface, per address
 * family); future kernel services (docker containers, wireguard, ...)
 * extend this same union. */
export type SecurityGroupRule = {
  readonly family: "ipv4" | "ipv6";
  readonly kind: "masq";
};

/** A fully-resolved security group — name plus its rules. Built via
 * net.securityGroup(name, build) (define.ts — see SecurityGroupBuilder,
 * same file), never assembled by hand: the builder is the one place a
 * rule's abstract shape gets turned into the concrete SecurityGroupRule
 * this type carries. Serialized straight into the implementation-
 * abstract `security.group` IR node (src/ir.ts's securityGroupToIR).
 * The `.masq` kernel services are a SHORTCUT that expands to one of
 * these: an explicit `securityGroup` on a kernelRouterEndpoint() wins
 * outright (and the masq services are ignored), otherwise the shortcut
 * builds `{ name: "masq-<router>", rules: [<one per masq family>] }`
 * through the very same builder. */
export interface SecurityGroup {
  readonly name: string;
  readonly rules: readonly SecurityGroupRule[];
}

/** A fully-resolved kernel-netns APPLICATION — the `kernel.app.*`
 * service kinds (RouterEndpointService, types.ts) resolved once, at
 * declaration time (buildKernelRouterEndpoint, define.ts), into the
 * descriptor the deployer turns into the actual service script. The
 * `kernel.app.` prefix is dropped here (`kernel.app.dhcp-client` ->
 * `kind: "dhcp-client"`). Every app runs as a systemd unit
 * (Restart=always) supervising the process inside the netns; a running
 * app daemon holds its netns alive past `ip netns delete`, so the
 * deployer's delete pass MUST stop or release it first. */
export type KernelApp =
  | { readonly kind: "dhcp-client"; readonly style: "dhclient" | "dhcpcd" }
  | {
    readonly kind: "wireguard";
    /** The real kernel interface name AND the .conf's basename
     * (mirrors the `kernel.app.wireguard` service's `ifaceName`). */
    readonly ifaceName: string;
    /** The full wg-quick conf content, written verbatim. */
    readonly config: WireguardInterfaceConfig;
    /** The address families to MASQUERADE out the tunnel interface —
     * resolved from the tunnelRouterEndpoint's `kernel.*.masq` services.
     * Masq belongs to the TUNNEL iface here, NOT to the upstream leg
     * (the security-group shortcut targets the side's real iface, which
     * would be wrong for a tunnel router). */
    readonly masq: readonly ("ipv4" | "ipv6")[];
  }
  | {
    readonly kind: "zerotier";
    /** The ZeroTier network ID to join (mirrors the
     * `kernel.app.zerotier` service's `networkId`). */
    readonly networkId: string;
    /** This tunnel's OWN dedicated ZeroTier home directory. */
    readonly instanceDir: string;
    /** The address families to MASQUERADE out the tunnel interface —
     * the real iface name is only known at RUNTIME (ZeroTier names it),
     * so the wire script adds/removes these rules on the captured
     * interface. */
    readonly masq: readonly ("ipv4" | "ipv6")[];
    /** Via-less routes the tunnel carries out the mesh — the tunnel
     * endpoint's declared `routes` (e.g. the ztnet supernet
     * 192.168.0.0/16). The interface name is only known at RUNTIME, so
     * the wire script adds each as `ip route add <dst> dev "$var"` once
     * ZeroTier names it (2026-08-30). */
    readonly routes?: readonly {
      readonly dst: string;
      readonly via?: string;
    }[];
  }
  | {
    readonly kind: "docker";
    /** The image to run — resolved by buildKernelRouterEndpoint from the
     * service's optional `image`, defaulting to `ovn-fabric-<router>` when
     * omitted (2026-08-31). */
    readonly image: string;
    /** Optional: build the image on first use if missing (packages
     * installed at image-build time; `from` picks apk vs apt). */
    readonly build?: {
      readonly from: string;
      readonly packages?: readonly string[];
      readonly dockerfile?: string;
    };
    /** Whether the container OWNS the router's interfaces (the transit veth
     * leg + this side's real interface moved into its netns, running `cmd`
     * against the real one — the "container is the router" mode,
     * dhcpcd-in-docker) is decided on the TS side when the IR interfaces are
     * derived, and the IR carries the OUTCOME structurally, with NO flag: an
     * interface-owning docker has NO veth addressing (`ip`/`routerIp`
     * absent — it owns the real interface, not a veth), a veth-injection
     * client always has it. The deployer branches on that concrete IR fact,
     * never on a TS concept (2026-08-31). */
    /** The container name — always resolved by
     * buildKernelRouterEndpoint (the service's `name` prefixed with the
     * router name, `<router>-<name>`, default `<router>-docker`), so
     * delete can `docker rm -f` exactly what create started. */
    readonly name: string;
    /** The container's command — trailing args of `docker run`, split
     * into tokens by buildKernelRouterEndpoint from the service's
     * `cmd` string. */
    readonly cmd?: readonly string[];
    /** The container's interface address, e.g. "10.200.0.2/24" — the
     * container's end of the veth injected into the router netns. */
    readonly ip?: string;
    /** The router end of that veth — the subnet's first host, e.g.
     * "10.200.0.1/24", resolved by buildKernelRouterEndpoint. */
    readonly routerIp?: string;
    /** The SHORT veth prefix for this app's veth pair, computed from
     * the container name (`ve-<hash>`, src/ir.ts's kernelRouterSideToIR)
     * — the router-netns end is `veth-<vethName>`-free, the pair is
     * `ve-<hash>` / `ve-<hash>-c`, both ≤ IFNAMSIZ. */
    readonly vethName?: string;
  };

// ── Router: connects exactly two collision domains ──────────────────
// The L3 primitive underneath Uplink/Segment (which are becoming a
// superset built on top of this + CollisionDomain, see ADR 0003) — one
// OVN Logical_Router, with exactly two Logical_Router_Ports (`left`/
// `right`), each bound into a different CollisionDomain. Real OVN
// routers can have more than two ports; this project's actual deployed
// topology never uses that — every router today is a bridge between
// exactly its own segment/uplink and the shared backbone (confirmed:
// router-home's real LRPs are lrp-home + lrp-home-bb, nothing else) —
// so N-way routers are a deliberate non-goal here, not an oversight.
// 3+-way connectivity is achieved by chaining through a shared
// CollisionDomain (the backbone), not by one router with many legs.
//
// Deliberately no `routes` field yet — static routes are a
// GLOBAL-topology concern (which hops does traffic between two
// arbitrary domains actually cross), not something to hand-declare
// per Router. See net.reachability() (define.ts) for the intended
// direction: declare "domain A needs to reach domain B," compute the
// path (and its return path) by walking the graph of CollisionDomains
// connected by Routers, instead of manually restating the same fact at
// every hop along the way.
//
// No `mac` field on RouterEndpoint: every existing case (segments,
// uplinks, backbone joins) derives it from the endpoint's own IPv4 via
// macFromV4 (addressing.ts) at emission time, never stores one
// separately — RouterEndpoint follows the same convention rather than
// reintroducing a redundant field.
/** One real (host, interface) pair — see RouterEndpoint.ifaces. */
export interface HostInterface {
  readonly host: Host;
  readonly iface: InterfaceKind;
}

/** The INPUT (topology-definition) half of the endpoint identity
 * contract, shared by every endpoint implementation: `name` is OPTIONAL.
 * An author may pin one, but it is normally derived from configuration by
 * the builder (deriveEndpointName, router-endpoints.ts). Optional (not
 * absent) precisely because a KernelRouterEndpoint/TunnelRouterEndpoint
 * INPUT cannot know the enclosing router's derived transit domain, yet
 * still must conform to the same contract as the OVN endpoint it resolves
 * into. */
export interface EndpointDefinition {
  /** Optional explicit port name — overrides the derived one. */
  readonly name?: string;
}

/** The INTERNAL half of the same contract: `name` is REQUIRED. Every
 * endpoint a Router actually stores (Router.left/right, and the future
 * endpoints[]) satisfies this, whichever builder produced it. */
export interface EndpointBase extends EndpointDefinition {
  /** The endpoint's OVN port name AND its identity: `lrp-<fnv1a64>`
   * unless an explicit `name` was given. This is the ovn.lrp key/name
   * across the TS/Python boundary — the reconciler keys a real port by
   * its own name (reconciler/ovn/reconcile.py), so this must be stable
   * under endpoint reordering and derivable from configuration, never
   * positional (no array index) and never from mutable state. */
  readonly name: string;
}

// ── RouterEndpoint: OVN-side or kernel-side, discriminated by `kind` ──
// Everything a router endpoint needs REGARDLESS of which world its
// OTHER side touches (an OVN CollisionDomain, or — not yet built, see
// the netnsBridge/KernelRouterEndpoint design discussion, 2026-08-12 —
// a Linux kernel netns) lives on the shared INPUT base below. `kind` lets
// generation code (src/ir.ts's toIR()) branch to a different emission
// strategy per side, instead of a structural ("does it have l2Segment")
// check — same discriminated-union pattern InterfaceKind already uses
// in this file.
export interface RouterEndpointInputBase<
  S = RouterEndpointService,
> extends EndpointDefinition {
  /** A router port's own addresses — plain parsed IPv4/IPv6 values, one
   * array entry per address (IPv4.parse(...), IPv6.parse(...)). A
   * router endpoint has no segment/uplink identity for a fold rule to
   * derive an address from, so its addresses are simply declared. */
  readonly ipaddrs: readonly (IPv4 | IPv6)[];
  /** Explicit MAC override. Required when ipaddrs has no IPv4 for
   * macFromV4() to fold (e.g. an endpoint whose real address isn't
   * declared yet, or is DHCP-assigned) — undefined otherwise means
   * "derive it from ipaddrs's IPv4." */
  readonly mac?: string;
  /** Real physical/tunnel attachment(s) for this endpoint's side of the
   * collision domain — a segment's localnet port needs one (it bridges
   * OVN's virtual world onto a real NIC/VLAN/tunnel), a router's
   * backbone-facing port doesn't (OVN-internal transit never touches
   * real hardware — northd compiles it straight into OpenFlow on
   * br-int). Optional and plural for exactly that reason: not every
   * endpoint has one, and a domain's physical presence isn't
   * necessarily one interface on one chassis (redundant attachment
   * across chassis is a real case, not just tolerated). */
  readonly ifaces?: readonly HostInterface[];
  /** Pins this endpoint's LRP to a specific chassis (OVN's native
   * gateway-chassis mechanism — real captured data already carries
   * this on every LRP, confirmed live 2026-08-09) instead of being
   * fully distributed. Typically only the endpoint that needs
   * NAT/external egress sets this, not both. Undefined means "fully
   * distributed, no pinning" — OVN's own default. */
  readonly gatewayChassis?: Host;
  /** Not designed yet — see SecurityGroupRef above. */
  readonly securityGroup?: SecurityGroupRef;
  /** IPv6 RA/SLAAC behavior on this endpoint's LRP — see
   * RouterEndpointService above. Undefined/empty means neither
   * ipv6_ra_configs key gets set (OVN's own default: no RA at all). The
   * array also carries ServiceAttachments (`endpoint.attachTo(sv, {...})`)
   * — a workload's NIC on this endpoint's segment (2026-09-08).
   *
   * INTERNAL: the EndpointBuilder (define.ts) injects `endpoint` into every
   * entry — see EndpointService — so each service carries a reference to
   * the endpoint it's attached to (its l2Segment/ipaddrs/routes), which the
   * generator (src/ir.ts) reads to wire the service's NIC. The author writes
   * the plain kind; `endpoint` is added by the builder, never by hand. */
  readonly services?: readonly S[];
  /** Routes this endpoint is the ANCHOR for — see RouterEndpointRoute
   * below. Declaring a route here IS what makes this (router, side)
   * the anchor; nothing infers it from address containment anymore.
   * Only takes effect for routers that also declare a shared
   * Router.routingDomains membership (see RoutingDomain below) — a
   * route declared here with no domain membership on this router
   * reaches no one but this router itself. */
  readonly routes?: readonly RouterEndpointRoute[];
  /** The RoutingDomains this endpoint's DECLARED ROUTES participate in
   * (2026-08-23) — per-endpoint membership that OVERRIDES the router-
   * level Router.routingDomains for THIS endpoint when set. A router can
   * legitimately anchor one domain from its left and participate in
   * another from its right (a tunnelRouterEndpoint does exactly that:
   * the left anchors Neighbor-defaultRoute, the right joins
   * Voda-defaultRoute). Undefined = inherit the router's own
   * routingDomains (the pre-2026-08-23 behavior). */
  readonly routingDomains?: readonly RoutingDomain[];
}

/** The RESOLVED (stored) endpoint base — RouterEndpointInputBase plus
 * the one thing that cannot be optional on it: the stable,
 * configuration-derived `name` (the merge key). Every endpoint a Router
 * stores satisfies EndpointBase; the INPUT shapes
 * (KernelRouterEndpoint/TunnelRouterEndpoint/OvnRouterEndpointSpec) only
 * satisfy EndpointDefinition, since the builder computes their name with
 * the enclosing router's name and the endpoint's role. */
export interface RouterEndpointBase<
  S = RouterEndpointService,
> extends RouterEndpointInputBase<S> {
  readonly name: string;
}

/** Today's ONLY concrete shape — adds the one field that's actually
 * OVN-specific: which CollisionDomain (Logical_Switch) this LRP binds
 * into. */
export interface OvnRouterEndpoint
  extends RouterEndpointBase<EndpointService<RouterEndpointService>> {
  readonly kind: "ovn";
  readonly l2Segment: CollisionDomain;
}

/** The input shape for NetworkBuilder.kernelRouterEndpoint() (define.ts)
 * — Meno's own design idea, 2026-08-12: describes the real-world
 * (WAN-facing) side of an OVN<->kernel transit link. `transit` is
 * always built by calling transitNetwork(ipv4, ipv6) (addressing.ts) in
 * the topology itself, never assembled by hand. `host`: which real
 * chassis the netns this eventually creates (KernelRouter, below)
 * actually runs on — required, since nothing else in scope at a
 * net.kernelRouterEndpoint() call site supplies it. NAT
 * (`kernel.*.masq` services) and apps still land here; real-interface
 * discovery is not modeled yet beyond "create the netns, assign it
 * these addresses" (see KernelRouter's own doc comment). */
export interface KernelRouterEndpoint extends RouterEndpointInputBase {
  readonly kind: "kernel";
  readonly host: Host;
  readonly transit: TransitNetwork;
  /** An EXPLICIT, fully-built security group (net.securityGroup(),
   * define.ts) to attach to this endpoint's real-world-facing
   * interface — overrides the base `SecurityGroupRef` shape, since a
   * kernel endpoint carries the whole group, not a bare ref. When set,
   * the `kernel.*.masq` services are IGNORED (the author owns the
   * group's content); when absent, the masq shortcut builds
   * `masq-<router>` implicitly. Must have been declared via
   * net.securityGroup() in the same defineNetwork call. */
  readonly securityGroup?: SecurityGroup;
}

/** The input shape for NetworkBuilder.tunnelRouterEndpoint() (define.ts)
 * — the generic "ANY TUNNEL" pattern (WireGuard today, ZeroTier later;
 * see InterfaceKind's "wireguard"/"zerotier" variants): a kernel netns
 * with a MESH transit on one side (left — same as
 * kernelRouterEndpoint()'s `transit`, connecting the netns to the OVN
 * mesh), an UPSTREAM transit on the other (right — the tunnel's own
 * endpoint handshake/keepalive UDP must reach the real internet via a
 * mundane path, never through the tunnel itself, so the netns's default
 * route goes out this leg), and the tunnel interface in the middle. The
 * tunnel egress is the ANCHOR's default: the `routes` field carries the
 * via-less `0.0.0.0/0`/`::/0` the routing domain rewrites to other
 * participants.
 *
 * Deliberately NOT extending RouterEndpointBase: this endpoint has no
 * `ipaddrs` of its own (every address is derived from the two transits
 * at build time), and it's consumed immediately by
 * tunnelRouterEndpoint() into a plain OvnRouterEndpoint — the tunnel
 * itself is never an IR endpoint, only the netns + wireguard app it
 * builds. */
export interface TunnelRouterEndpoint extends EndpointDefinition {
  readonly kind: "tunnel";
  readonly host: Host;
  /** The mesh-side transit (left, veth-krn-* style) — same mechanics as
   * kernelRouterEndpoint()'s `transit`. */
  readonly transit: TransitNetwork;
  /** The upstream-side transit (right, veth-bdk-* style) — back toward
   * the physical path the tunnel endpoint is reached through. */
  readonly upstream: TransitNetwork;
  // NOTE: the tunnel WORKLOAD is no longer a `tunnel` field — it's a
  // `wireguard`/`zerotier` service in `services[]` below (define.ts maps it
  // onto the generic kernel.app.service).
  /** The upstream-peer router's BACKBONE-facing port (2026-08-23): the
   * tunnel netns's upstream/backdoor leg terminates on an OVN router
   * (`<router>-upstream`) that tunnelRouterEndpoint defines INTERNALLY —
   * it carries the backdoor LRP on `upstream` AND this backbone port, so
   * the tunnel's endpoint traffic enters the mesh (whose default routes
   * it on to the physical WAN, e.g. via voda-avm) instead of dying in
   * the root namespace. */
  readonly upstreamBackbone: {
    readonly l2Segment: CollisionDomain;
    readonly ipaddrs: readonly (IPv4 | IPv6)[];
  };
  /** The RoutingDomains the INTERNAL upstream-peer router joins
   * (2026-08-23) — it needs the mesh's default route (learned via the
   * domain anchor rewrite) to forward the tunnel's endpoint traffic out
   * its backbone leg to the physical WAN (e.g. Voda-defaultRoute →
   * voda-avm). Typically the same domain as the enclosing router's
   * backbone/right side. */
  readonly upstreamDomains?: readonly RoutingDomain[];
  /** The anchor's default route(s) — via-less, so computeRoutes rewrites
   * them to the domain's other participants. */
  readonly routes?: readonly RouterEndpointRoute[];
  /** Per-endpoint routing domain membership (2026-08-23) — stamped onto
   * the returned OVN endpoint AND the KernelRouter. */
  readonly routingDomains?: readonly RoutingDomain[];
  /** The OVN-side services (ipv6.*) and the kernel masq services (whose
   * families the wireguard app masquerades out the tunnel). */
  readonly services?: readonly RouterEndpointService[];
}

/** One side of a KernelRouter's own netns — an IP assignment, plus
 * (right side only, in practice — see NetworkBuilder.kernelRouterEndpoint(),
 * define.ts) whichever RouterEndpointRoute entries the config author
 * declared for the real-world-facing side. A route with no `via` means
 * exactly what it means everywhere else RouterEndpointRoute is read
 * (src/ir.ts's computeRoutes): "this side needs no literal route of its
 * own for this — handled elsewhere," e.g. IPv6 RA/DHCP on the real
 * segment, not yet modeled here — src/ir.ts's kernelRouterSideToIR
 * drops those entirely rather than emitting a route with no nexthop to
 * apply. `ifaces` carries the real physical attachment(s) this side's
 * addresses/routes actually bind to (same shape as RouterEndpointBase.
 * ifaces — see its own doc comment above) — the deployer creates/moves
 * the interface into the netns instead of a dummy stand-in
 * (deployer/ir_to_shell.py's own _emit_kernel_router_create). Only
 * populated for `right` today (via NetworkBuilder.kernelRouterEndpoint(),
 * which copies the kernelRouterEndpoint() input's `ifaces` onto this
 * side in ADDITION to the OVN-side endpoint's own copy, 2026-08-18 —
 * the endpoint keeps its ifaces so the transit domain still gets its
 * localnet port/bridge binding/bridge-mapping); the `left` side's real
 * iface binding is still its own next step. */
export interface KernelRouterSide {
  readonly ipaddrs: readonly (IPv4 | IPv6)[];
  readonly routes?: readonly RouterEndpointRoute[];
  /** Real physical/tunnel attachment(s) for this side — mirrors
   * RouterEndpointBase.ifaces (types.ts): `host` is the chassis the
   * interface actually exists on (the KernelRouter's own host in
   * practice), `iface` is the InterfaceKind to create/move into this
   * router's netns. A side with no `ifaces` still gets a dummy device
   * from the deployer so its addresses/routes have something to bind
   * to. */
  readonly ifaces?: readonly HostInterface[];
  /** An APPLICATION running inside this side's netns, on this side's
   * real interface (right side only in practice — set by
   * buildKernelRouterEndpoint, define.ts, from the `kernel.app.*`
   * service kinds; see KernelApp). Resolved once at declaration time;
   * the deployer turns each into the actual `ip netns exec` service
   * script — and MUST stop/release it on delete, since a running app
   * daemon holds the netns alive past `ip netns delete`. */
  readonly apps?: readonly KernelApp[];
  /** The FULLY-RESOLVED security group attached to this side's real
   * interface (right side only in practice) — an object reference, same
   * "no string that could drift" discipline as l2Segment/routingDomains.
   * Computed once, in buildKernelRouterEndpoint (define.ts): an explicit
   * `securityGroup` on the kernelRouterEndpoint() input wins outright
   * (the `kernel.*.masq` services are then IGNORED), otherwise the masq
   * shortcut builds `{ name: "masq-<router>", rules: [...] }` through
   * net.securityGroup() — see SecurityGroup, types.ts. Serialized into
   * the implementation-abstract `security.group` IR node (src/ir.ts's
   * securityGroupToIR); this side node is the ATTACHMENT point. */
  readonly securityGroup?: SecurityGroup;
}

/** A router whose two ports are real Linux interfaces inside ONE netns
 * running on `host` — NOT an OVN Logical_Router at all (contrast
 * Router, below). Confirmed live, 2026-08-12 (`ip netns exec
 * ns-uplink-voda-avm ip a`): `left` is the transit-facing side (the
 * veth pair's own kernel-side leg — veth-krn-0, 10.99.0.2/28 in that
 * capture, matching transitNetwork()'s `.right`, addressing.ts);
 * `right` is the real-world-facing side (ens18.1280 in that capture,
 * the actual WAN interface). Built via
 * NetworkBuilder.kernelRouterEndpoint() (define.ts), not declared
 * directly — see that method's own doc comment for why. src/ir.ts's
 * toIR() emits a `kernel.router` IR node per instance: creates the real
 * netns and assigns it these addresses — no real iface binding yet,
 * see KernelRouterSide's own doc comment. */
export interface KernelRouter {
  readonly name: string;
  readonly host: Host;
  readonly left: KernelRouterSide;
  readonly right: KernelRouterSide;
  /** The transit CollisionDomain this KernelRouter's `left` shares with
   * its OVN twin (kernelRouterEndpoint()'s own transitDomain, define.ts)
   * — an object reference, not a name, same "no string that could drift
   * out of sync" reasoning as l2Segment/gatewayChassis elsewhere in this
   * file. Lets src/ir.ts's toIR() find which Router endpoint is THIS
   * KernelRouter's other half (matching endpoint.l2Segment === this),
   * so it can mirror that router's own already-computed routes onto
   * this KernelRouter's `left` — the kernel netns otherwise has no way
   * to route back INTO the OVN mesh at all (confirmed live, 2026-08-13:
   * router-voda-avm-v2 got its own default route via kernel-0's
   * transit-facing address, but kernel-0 itself had no route back to
   * home/management's own subnets — nothing told it those exist).
   * Always set: both builders (kernelRouterEndpoint()/
   * tunnelRouterEndpoint()) create the KernelRouter as an OVN twin, so
   * there is always a transit domain to name. */
  readonly transitDomain: CollisionDomain;
  /** The OVN twin's OWN address on `transitDomain` (kernelRouterEndpoint()'s
   * own `ovnSideAddrs`) — the nexthop for every route mirrored onto
   * `left` per transitDomain's own doc comment above. NOT the same as
   * `left.ipaddrs` (this KernelRouter's OWN address on that same
   * domain) — this is the address on the OTHER end of that same wire.
   * Always set, like transitDomain above. */
  readonly transitPeerAddrs: readonly (IPv4 | IPv6)[];
  /** The tunnel router's UPSTREAM transit peer (tunnelRouterEndpoint(),
   * define.ts) — the OTHER end of the upstream wire (e.g. the physical-
   * path router the tunnel netns borrows egress from): the nexthop for
   * the netns's default route out the upstream leg, which is how the
   * tunnel's own endpoint UDP reaches the real internet (wg-quick's
   * fwmark policy routing keeps it out of the tunnel). Populated by
   * tunnelRouterEndpoint(); EMPTY for a plain kernelRouterEndpoint() —
   * it has no upstream leg, its real default lives in `right`'s own
   * routes (KernelRouterSide.routes). Always present, possibly empty. */
  readonly upstreamPeerAddrs: readonly (IPv4 | IPv6)[];
  /** Same field, same meaning as Router.routingDomains below — a
   * KernelRouter's own routes (KernelRouterSide.routes) only apply if
   * it's actually a participant of some declared RoutingDomain, same
   * rule that already gates every OVN-side route (src/ir.ts's
   * computeRoutes). Set once, at declaration time (NetworkBuilder.
   * defineOvnRouter() stamps the router's returned routingDomains onto the
   * KernelRouter it creates, when the endpoint didn't carry its own
   * per-endpoint routingDomains, 2026-09-08) — never a second,
   * independently-stated copy that could drift from the owning Router's
   * own routingDomains. */
  readonly routingDomains?: readonly RoutingDomain[];
}

export type RouterEndpoint =
  | OvnRouterEndpoint
  | KernelRouterEndpoint
  | TunnelRouterEndpoint;

// ── RouterEndpoint services: IPv6 RA/SLAAC + kernel-side services ──
// IPv6 RA is split into its two REAL, independently meaningful OVN
// behaviors instead of one flag toggling both together, because they
// genuinely differ (confirmed live, the ipv6_ra_configs history/
// upstream-bug comment):
//   - "ipv6.slaac" sets ipv6_ra_configs:address_mode=slaac — this alone
//     already makes OVN answer solicited Router Solicitations (the
//     lr_in_nd_ra_options/lr_in_nd_ra_response responder), even with no
//     other option set.
//   - "ipv6.ra" sets ipv6_ra_configs:send_periodic=true (+ optional
//     min/max interval overrides) — genuinely UNSOLICITED, self-timer-
//     driven RA, pinctrl-injected, which needed a real upstream OVN fix
//     (ovn-org/ovn#313) before it worked at all on a DGP/patch port.
// Both live in ipv6_ra_configs (a single OVSDB smap column), but they're
// independently useful (e.g. "ipv6.slaac" alone for solicited-only, no
// periodic chatter), so RouterEndpoint models them as two composable
// services instead of one boolean that can't express that.
//
// The `kernel.*` kinds are the OPPOSITE world — services that apply
// INSIDE a KernelRouter's netns, never to an OVN LRP. Two families:
//   - `kernel.ipv4.masq`/`kernel.ipv6.masq`: a SHORTCUT that expands
//     (through net.securityGroup(), define.ts) to a security group named
//     `masq-<router>` carrying the matching POSTROUTING MASQUERADE rules
//     for the netns's real-world-facing interface.
//   - `kernel.app.*`: an APPLICATION running inside the netns on that
//     interface — a DHCP client (`kernel.app.dhcp-client`) today, docker
//     containers/wireguard later. Resolved into a KernelApp descriptor
//     (same file) in buildKernelRouterEndpoint, carried on the
//     KernelRouterSide that runs it, and turned into the actual
//     `ip netns exec` service script by the deployer — including the
//     release/kill on delete (a DHCP client is a daemon that holds the
//     netns alive, so it MUST be stopped before `ip netns delete`).
// Split off from the OVN `ipv6.*` kinds in buildKernelRouterEndpoint so
// the OVN endpoint never sees them — resolveIpv6RaConfigs (src/ir.ts)
// throws on a kind it doesn't know.
export type RouterEndpointService =
  | { readonly kind: "ipv6.slaac" }
  | {
    readonly kind: "ipv6.ra";
    /** Seconds. Omit for OVN's own RFC 4861 default. */
    readonly minInterval?: number;
    /** Seconds. Omit for OVN's own RFC 4861 default. */
    readonly maxInterval?: number;
  }
  | { readonly kind: "kernel.ipv4.masq" }
  | { readonly kind: "kernel.ipv6.masq" }
  | {
    readonly kind: "kernel.app.dhcp-client";
    readonly style: "dhclient" | "dhcpcd";
  }
  | {
    readonly kind: "kernel.app.wireguard";
    /** The real kernel interface name AND the .conf's basename on disk
     * (/etc/wireguard/<ifaceName>.conf) — see InterfaceKind's
     * "wireguard" variant, whose config this mirrors. */
    readonly ifaceName: string;
    /** The full wg-quick conf content (privateKey/address/peer) —
     * written verbatim, PrivateKey included, per the documented
     * deliberate exception to this project's credential policy. */
    readonly config: WireguardInterfaceConfig;
  }
  | {
    readonly kind: "kernel.app.zerotier";
    /** The ZeroTier network ID to join (16 hex chars) — see
     * InterfaceKind's "zerotier" variant. */
    readonly networkId: string;
    /** This tunnel's OWN dedicated ZeroTier home directory (identity/
     * per-network state) — see InterfaceKind's "zerotier" variant. */
    readonly instanceDir: string;
  }
  | {
    readonly kind: "kernel.app.docker";
    /** The image to run, e.g. "ubuntu". OPTIONAL — defaults to the
     * router name (`ovn-fabric-<router>`) at resolve time (2026-08-31). */
    readonly image?: string;
    /** Optional: build the image on first use if missing — `from` is the
     * base image (determines apk vs apt), `packages` the ones installed
     * at image-build time, `dockerfile` the full escape hatch. */
    readonly build?: {
      readonly from: string;
      readonly packages?: readonly string[];
      readonly dockerfile?: string;
    };
    /** The container name — PREFIXED with the router name at resolve
     * time (`<router>-<name>`), so it's globally unique and delete can
     * `docker rm -f` exactly what create started. Defaults to
     * `<router>-docker` when omitted. */
    readonly name?: string;
    /** The command the container runs, e.g. "sleep 86400" or
     * ["sleep", "86400"] — the trailing args of `docker run`
     * (normalized to tokens at resolve time). */
    readonly cmd?: string | readonly string[];
    /** The container's addresses on the attached network (a segment, or a
     * kernel router's services segment) — parsed IPv4/IPv6 with prefixes,
     * same as an endpoint's `ipaddrs`. For the veth-injection mode the
     * FIRST entry is the container's interface address and the peer end is
     * that subnet's first host (`.1`); for a container bound to a segment
     * they are its addresses on that segment. */
    readonly ipaddrs?: readonly (IPv4 | IPv6)[];
    /** The container's OWN routes (e.g. its default via the segment
     * gateway). Omitted/empty → a default route (per family on the
     * endpoint) is added via the endpoint's address (the segment/router
     * gateway) at resolve time (2026-09-08). */
    readonly routes?: readonly RouterEndpointRoute[];
  }
  | {
    /** An ATTACHMENT: bind a reusable workload (`srvRef`, a net.service) to
     * this endpoint's segment — one NIC on this L2 (2026-09-08). */
    readonly kind: "service.attach";
    readonly srvRef: Service;
    readonly ipaddrs: readonly (IPv4 | IPv6)[];
    readonly routes?: readonly RouterEndpointRoute[];
    readonly primary?: boolean;
  }
  // CONFIG-SIDE WORKLOAD SHORTCUTS — kept in the topology; define.ts maps
  // them onto the generic kernel.app.container / kernel.app.service (the IR
  // knows nothing about wireguard/zerotier, 2026-09-08).
  | {
    readonly kind: "wireguard";
    readonly ifaceName: string;
    readonly config: Extract<
      InterfaceKind,
      { kind: "wireguard" }
    >["config"];
    readonly masq?: readonly ("ipv4" | "ipv6")[];
  }
  | {
    readonly kind: "zerotier";
    readonly networkId: string;
    readonly instanceDir?: string;
    readonly masq?: readonly ("ipv4" | "ipv6")[];
    readonly routes?: readonly RouterEndpointRoute[];
  };

// ── Service: a reusable WORKLOAD, and its network attachments ──────────
// A `service` (net.service(), define.ts) is WHAT runs — an image + command
// + optional build — declared ONCE and network-free. It is bound to
// networks by ATTACHING it at router endpoints: each attachment
// (endpoint.attachTo(sv, {...})) is one NIC on that endpoint's segment.
// One service attached at N endpoints = one container with N NICs (the
// k8s pod model, 2026-09-08).
export interface Service {
  readonly name: string;
  readonly image: string;
  readonly cmd?: readonly string[];
  readonly build?: {
    readonly from: string;
    readonly packages?: readonly string[];
    readonly dockerfile?: string;
  };
  /** ENDPOINT-REFERENCES — the `service.attach` entries that attach this
   * service (each is the endpoint's `{ ...service, endpoint }` tuple, so it
   * carries the endpoint AND the attachment's addressing), so the service
   * knows which endpoints reference it. Filled in by the endpoint builders
   * at attach time; mutable on purpose — populated during defineNetwork,
   * not authored (2026-09-08). */
  readonly endpointRefs: EndpointService<
    Extract<RouterEndpointService, { kind: "service.attach" }>
  >[];
}

/** INTERNAL — the EndpointBuilder injects `endpoint` into every `services[]`
 * entry, so each service references the endpoint it's attached to. The
 * generator reads `svc.endpoint` for the segment + addresses + routes to
 * wire the service's NIC (a `wireguard`/`zerotier` service uses the
 * endpoint's; `docker` carries its own). Added by the builder (define.ts),
 * never written by hand (2026-09-08). */
export type EndpointService<T> = T & {
  readonly endpoint: OvnRouterEndpoint;
};

/** The AUTHOR-facing endpoint spec (before the builder injects `endpoint`
 * into each service): same as OvnRouterEndpoint but `services` is the plain
 * union. define.ts's buildOvnRouterEndpoint turns a spec into the stored
 * OvnRouterEndpoint by injecting the endpoint into every service. `id` is
 * omitted too — it's derived by the builder, never authored (an author who
 * wants a specific name sets `name` instead). */
export type OvnRouterEndpointSpec =
  & Omit<OvnRouterEndpoint, "services" | "name">
  & {
    /** Optional explicit port name — see EndpointDefinition. */
    readonly name?: string;
    readonly services?: readonly RouterEndpointService[];
  };

/** One route entry declared directly on the RouterEndpoint that IS the
 * anchor for it — `dst` reachable via `via`, optionally NAT'd. Moved
 * here from an earlier design where routes lived inside RoutingDomain
 * itself and the anchor was INFERRED by searching every router's every
 * endpoint for one whose declared subnet happened to contain `via`
 * (confirmed live, 2026-08-12: that inference is unnecessary — the
 * config author already knows which endpoint is the real egress, e.g.
 * router-voda-avm-v2's WAN-facing `left`, so declaring the route right
 * there removes a whole class of "which router owns this subnet"
 * ambiguity instead of resolving it algorithmically). The anchor is now
 * simply "whichever (router, side) this array lives on" — see
 * computeRoutes, src/ir.ts, which no longer searches for it at all.
 * `with: "masq"` (source NAT) is only ever meaningful here, at the
 * anchor — never replicated onto routers that merely relay toward it.
 *
 * `via` is OPTIONAL on the ANCHOR side only — omitting it means "the
 * anchor needs no literal route of its own here, it's handled some
 * other way on its own side" (e.g. SLAAC/RA on a client-facing segment
 * for an IPv6 default, or an existing less-specific route already
 * covering it there — see RouterEndpointService above). It does NOT
 * mean "no route at all": every OTHER router in the same RoutingDomain
 * still gets a route to `dst`, rewritten to the anchor's own address on
 * whichever CollisionDomain they share with it, exactly as when `via`
 * IS given (confirmed live, 2026-08-12: "you need to add that route to
 * all hops so that all packets will be transmitted to [the anchor's own
 * side]" — computeRoutes() (src/ir.ts) only skips the ANCHOR's own
 * emission when `via` is absent, every other participant is
 * unaffected). */
export interface RouterEndpointRoute {
  readonly dst: IPv4 | IPv6;
  readonly via?: IPv4 | IPv6;
  readonly with?: "masq";
}

/** A named group of routers that should all learn about each other's
 * anchor routes — declared once via net.routingDomain(), then
 * referenced by any number of routers' Router.routingDomains. Purely a
 * membership tag now, no routes of its own (see RouterEndpointRoute
 * above for where those actually live): every participant router's own
 * RouterEndpoint.routes get propagated to every OTHER participant,
 * rewritten to the anchor's own address on whichever CollisionDomain
 * the two routers actually share (src/ir.ts's computeRoutes) — and the
 * SAME participant set also gets direct peer-to-peer routes to each
 * other's own subnets regardless of whether any `routes` entry exists
 * at all (src/ir.ts's computeInterconnectRoutes — "interconnect only
 * exists if a routing domain exists," confirmed live 2026-08-12). A
 * router with no shared domain to a given anchor, or where `via`'s
 * family doesn't resolve at all (e.g. an IPv6-only route on a router
 * whose own connectivity there is SLAAC/RA-derived, not static), simply
 * gets no route from that entry — not an error, just a different route
 * source owning that family for that router (confirmed live design
 * discussion, 2026-08-11: "if you don't know the next hop you don't
 * apply anything"). */
export interface RoutingDomain {
  readonly name: string;
}

export interface Router {
  readonly name: string;
  // Always the OVN side — even a kernelRouterEndpoint()/tunnelRouterEndpoint()
  // input is consumed into a plain OvnRouterEndpoint by its builder (the
  // KernelRouter/TunnelRouterEndpoint shapes are INPUT types, never a
  // stored Router's own endpoint).
  readonly left: OvnRouterEndpoint;
  readonly right: OvnRouterEndpoint;
  /** RoutingDomains (net.routingDomain()) this router participates in
   * — object references, not names, matching every other cross-
   * reference in this file (l2Segment, gatewayChassis, ...), not a
   * string that could typo/drift out of sync with what was actually
   * declared. See RoutingDomain's own doc comment for how a domain's
   * routes actually resolve per-router. */
  readonly routingDomains?: readonly RoutingDomain[];
  /** Routers this router IMPLIES (derived, not author-declared) — e.g. a
   * tunnelRouterEndpoint's internally-created `<name>-upstream` peer.
   * Always an array (possibly empty). The config author lists only the
   * TOP-level router in defineNetwork()'s returned routers[]; define.ts
   * flattens each router's subRouters when assembling allRouters, so a
   * tunnel's upstream peer never needs listing (2026-09-08). */
  readonly subRouters: readonly Router[];
}

/** The [Peer] stanza of a wg-quick conf — see InterfaceKind's
 * "wireguard" variant below. */
export interface WireguardPeer {
  readonly publicKey: string;
  /** "host:port" */
  readonly endpoint: string;
  /** wg-quick's own comma-joined syntax, e.g. "0.0.0.0/0,::0/0" —
   * stored as one string (not parsed here) since this generator never
   * needs to reason about individual prefixes, only reproduce the
   * conf file verbatim. */
  readonly allowedIps: string;
  readonly persistentKeepalive?: number;
}

/** The [Interface] stanza plus its one [Peer] — everything the deployer
 * needs to reconstruct a wg-quick conf byte-for-byte. See InterfaceKind's
 * "wireguard" variant
 * for why the PrivateKey lives here, in a git-tracked file, rather
 * than behind an env var/secret manager as this project's credentials
 * normally would. */
export interface WireguardInterfaceConfig {
  readonly privateKey: string;
  /** wg-quick's own comma-joined syntax, e.g.
   * "10.64.56.207/32,fc00:bbbb:bbbb:bb01::1:38ce/128" */
  readonly address: string;
  readonly listenPort?: number;
  readonly dns?: string;
  readonly peer: WireguardPeer;
}

export type InterfaceKind =
  | {
    kind: "vlan";
    vlanParent: string;
    vlanId: number;
    /** Override the kernel subinterface name — defaults to
     * `${vlanParent}.${vlanId}` when omitted. Needed whenever that
     * default name is already in use for something else on the same
     * VLAN tag (e.g. VLAN 129 already carries this host's own
     * management IP on `ens18.129` via netplan/networkd — a second,
     * differently-named vlan subinterface for the same tag can safely
     * coexist and feed an OVS bridge without touching the existing
     * one; Linux delivers matching-tagged frames to every registered
     * vlan netdevice for a given (parent, vlanId) pair, not just one). */
    ifaceName?: string;
  }
  | { kind: "physical"; name: string } // a real, untagged NIC/port
  | { kind: "bridge-port"; bridge: string; port: string }
  /** A real WireGuard tunnel, managed via `wg-quick` rather than
   * hand-rolled `wg setconf`/`ip link` calls — wg-quick's own fwmark +
   * policy-routing dance is exactly what's needed here (the tunnel's
   * own handshake/keepalive UDP packets must keep leaving via whatever
   * route already existed — its kernel netns's upstream leg, typically —
   * while every OTHER packet gets diverted into the tunnel), and re-deriving
   * that by hand would just duplicate a battle-tested implementation.
   * Confirmed live, 2026-07-06: this is genuinely how it behaves when
   * a default route already exists in the netns before `wg-quick up`
   * runs.
   *
   * `config` (see WireguardInterfaceConfig below) is declared directly
   * in the topology and written verbatim to
   * /etc/wireguard/<ifaceName>.conf on the target host by the deployer —
   * INCLUDING the PrivateKey. This is a DELIBERATE, EXPLICIT exception to this
   * project's usual "never hard-code credentials, use env vars/secret
   * managers" policy, made 2026-07-06 after being asked to confirm:
   * the tradeoff (a real credential living in a git-tracked source
   * file) was accepted on purpose, not overlooked. Treat topology.ts
   * — and any generated script built from it — with the same care as
   * any other credential-bearing file (this repo's git-safety rules
   * around scanning for secrets before any commit/push still apply in
   * full).
   *
   * `ifaceName` is the real kernel interface name AND the .conf's
   * basename on disk, overridable when the chosen name doesn't fit:
   * it exceeds IFNAMSIZ (15 usable characters), or would rename an
   * already-running real interface unnecessarily. */
  | {
    kind: "wireguard";
    ifaceName: string;
    config: WireguardInterfaceConfig;
  }
  /** A real ZeroTier client confined to this uplink's own network
   * namespace — same overall shape as "wireguard" above (a real tunnel
   * this generator stands up and supervises inside the netns), but
   * ZeroTier is a persistent userspace DAEMON (no in-kernel device the
   * way WireGuard has), and it names its own resulting interface
   * itself rather than accepting one from the caller. Because of that,
   * the real interface name is NOT known at generation time — the
   * deployer's zerotier service script captures it into a shell
   * variable at RUNTIME, the same way `$CHASSIS` is already resolved
   * live in the generated script rather than computed here. NOT yet
   * verified against a live host (unlike "wireguard", which went
   * through several rounds of live correction) — treat the exact
   * zerotier-one/zerotier-cli invocations this produces as a first
   * draft to test and iterate on, same as wireguard's own history.
   *
   * `authorization` is deliberately NOT modeled here: joining a
   * network only gets this node as far as an unauthorized member —
   * approving it is a controller-side admin action on a DIFFERENT
   * system, outside anything this generator runs or has credentials
   * for. */
  | {
    kind: "zerotier";
    /** The ZeroTier network ID to join (16 hex chars), e.g.
     * "02cfbec15c2319ff"). */
    networkId: string;
    /** This uplink's OWN dedicated ZeroTier home directory —
     * identity.secret/identity.public and all per-network state live
     * there, deliberately NOT shared with any host-level zerotier-one
     * instance (or any other zerotier-kind uplink). Persistent across
     * reboots/netns recreation on purpose: losing this directory means
     * losing this node's ZT identity, which means losing its
     * controller authorization too (see the "moving a ZeroTier
     * installation" discussion, this session — identity.secret is the
     * one truly load-bearing file). OPTIONAL — when omitted,
     * buildTunnelRouterEndpoint (define.ts) derives
     * `/var/lib/zerotier-one-<router name>` from the enclosing router's
     * name. */
    instanceDir?: string;
  }
  /** A placeholder Linux dummy interface — no real backing device, no
   * real-world connectivity. Stands in for a real mechanism (e.g. a
   * WireGuard tunnel) that isn't built yet, so the rest of the chain
   * can be wired up and tested end-to-end first. No `name` field, on
   * purpose: unlike "vlan"/"physical", a dummy interface has no
   * pre-existing real-world name to preserve — the deployer derives
   * and creates it itself, IFNAMSIZ-safe. */
  | { kind: "dummy" }
  /** A veth pair owned by a KernelRouter's netns — the real wiring of an
   * OVN<->kernel transit link (2026-08-18): `ifaceName` is the netns-
   * side leg (created in root, then moved into the kernel router's
   * netns, where that side's addresses/routes bind to it), `peerName`
   * is the root-side leg (attached to the transit domain's OVS bridge).
   * Constructed implicitly by NetworkBuilder.kernelRouterEndpoint()
   * (define.ts) from the enclosing defineOvnRouter()'s LONG name — the
   * IFNAMSIZ-safe shortening is the lower layers' job (src/ir.ts, same
   * as bridges' shortName) — never declared by config authors. */
  | {
    kind: "veth";
    ifaceName: string;
    peerName: string;
  };
