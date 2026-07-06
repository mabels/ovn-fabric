// types.ts — distinct identity types and the switchable-uplink mechanism.
// No topology data lives here. This file defines the SHAPE; model.ts
// declares the FACTS.

import { IPAddress } from "npm:ipaddress@0.2.6";

// ── distinct identity types ──────────────────────────────────────
// Branded types: structurally still numbers at runtime, but the type
// checker will not let a SegmentId be passed where an UplinkId is
// expected, or vice versa. Every bug from tonight's session was
// "correct math, applied to the wrong identifier" — a checker that
// treats number as number cannot catch that; two distinct branded
// types can.

type Brand<T, B extends string> = T & { readonly __brand: B };

export type SegmentId = Brand<number, "SegmentId">;
export type UplinkId = Brand<number, "UplinkId">;

export function segmentId(n: number): SegmentId {
  if (n < 0 || n > 255) {
    throw new RangeError(`SegmentId out of range (0-255): ${n}`);
  }
  return n as SegmentId;
}

export function uplinkId(n: number): UplinkId {
  if (n < 0 || n > 65535) {
    throw new RangeError(`UplinkId out of range: ${n}`);
  }
  return n as UplinkId;
}

// ── host / chassis ─────────────────────────────────────────────────
// Where a given segment's or uplink's OVN/OVS configuration is actually
// applied. A given deployment may run everything on one chassis, but the
// model does not assume that — each Segment/Uplink declares which Host
// it runs on, so a future topology with multiple chassis is a config
// change, not a redesign.

export type AccessMethod =
  | { method: "ssh"; user: string }
  | { method: "local" }; // the generator's own host — no SSH needed

export interface Host {
  readonly name: string;
  readonly address: string; // hostname or IP the generator connects to
  readonly access: AccessMethod;
}

export function sshHost(name: string, address: string, user: string): Host {
  return { name, address, access: { method: "ssh", user } };
}

export function localHost(name: string): Host {
  return { name, address: "127.0.0.1", access: { method: "local" } };
}

// ── NetId: the identity every segment/uplink/transfer-link carries ──
// Backed by the `ipaddress` library (not std — see ADR discussion: std
// libraries lack reliable prefix-notation parsing and the 128-bit
// arithmetic IPv6 fold rules need). id() returns the raw numeric
// identifier this NetId was derived from (a segment or uplink number);
// vlan() returns the physical VLAN tag if one applies, or undefined —
// note this is a DERIVED convenience, distinct from whether the thing
// holding this NetId actually has an `if: { kind: "vlan", ... }` — a
// NetId can report a vlan() number purely because of its fold rule
// while the real physical attachment (see InterfaceKind below) is
// something else entirely (a WireGuard interface, a plain port). Don't
// use vlan() to decide physical wiring; use the owning Uplink/Segment's
// `if` field for that.
//
// NetId instances are produced by factory functions in addressing.ts
// (segmentNet(), uplinkNet(), transferNet()), not constructed directly
// here — this interface only defines the shape every factory must
// satisfy. The fold operation itself is string construction (see
// addressing.ts header comment) — IPAddress.parse() is called only
// after the address string is fully built.

export interface NetId {
  readonly ipv4: IPAddress;
  readonly ipv6: IPAddress;
  id(): number;
  vlan(): number | undefined;
}

/**
 * An address PAIR — the thing config/topology.ts actually declares per
 * Uplink/Segment via `addresses: [...]`. Most things have exactly one
 * NetId; the array form exists for cases like a transfer link, which
 * conceptually carries both its OVN-side and netns-side identity.
 */
export type Addresses = readonly NetId[];

// ── physical realization ─────────────────────────────────────────
// HOW a Segment/Uplink actually attaches to a real wire. Deliberately
// separate from addressing: a NetId's vlan() can return a number purely
// from its fold rule while the real interface here is something else
// entirely (WireGuard, a bridge port with no VLAN at all). This is the
// split that was missing before tonight's correction — conflating
// "has an address" with "is a VLAN" broke as soon as WireGuard needed
// modelling, since a WireGuard tunnel has addresses but is not a VLAN.

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

/** The [Interface] stanza plus its one [Peer] — everything
 * emitWireguardInterface (generate-netns.ts) needs to reconstruct a
 * wg-quick conf byte-for-byte. See InterfaceKind's "wireguard" variant
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
   * route already existed — the Backdoor below, typically — while
   * every OTHER packet gets diverted into the tunnel), and re-deriving
   * that by hand would just duplicate a battle-tested implementation.
   * Confirmed live, 2026-07-06: this is genuinely how it behaves when
   * a default route already exists in the netns before `wg-quick up`
   * runs.
   *
   * `config` (see WireguardInterfaceConfig below) is declared directly
   * in config/topology.ts and written verbatim to
   * /etc/wireguard/<ifaceName>.conf on the target host by the
   * generator (see emitWireguardInterface, generate-netns.ts) — INCLUDING
   * the PrivateKey. This is a DELIBERATE, EXPLICIT exception to this
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
   * basename on disk — defaults to this uplink's OWN name (see
   * uplinkWireguard, factories.ts, which threads UplinkBuilder's
   * `name` parameter through), overridable when that default doesn't
   * fit: it exceeds IFNAMSIZ (15 usable characters), or would rename
   * an already-running real interface unnecessarily. */
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
   * the real interface name is NOT known at generation time — it gets
   * captured into a shell variable at RUNTIME by emitZerotierInterface
   * (generate-netns.ts), the same way `$CHASSIS` is already resolved
   * live in the generated script rather than computed here (see
   * generate-ovn.ts header comment). NOT yet verified against a live
   * host (unlike "wireguard", which went through several rounds of
   * live correction) — treat the exact zerotier-one/zerotier-cli
   * invocations this produces as a first draft to test and iterate on,
   * same as wireguard's own history.
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
     * one truly load-bearing file). Defaults to
     * `/var/lib/zerotier-one-uplink-<uplink name>` — see
     * uplinkZerotier, factories.ts. */
    instanceDir: string;
  }
  /** A placeholder Linux dummy interface — no real backing device, no
   * real-world connectivity. Stands in for an uplink whose real
   * mechanism (e.g. a WireGuard tunnel) isn't built yet, so the rest of
   * the chain (OVN router, transfer link, backbone join, back-routes)
   * can be wired up and tested end-to-end first. No `name` field, on
   * purpose: unlike "vlan"/"physical", a dummy interface has no
   * pre-existing real-world name to preserve — the generator derives
   * and creates it itself, at a slot-based name (see generate-netns.ts,
   * dummyIface()), the same IFNAMSIZ-safe convention already used for
   * every other uplink-owned kernel interface (veth-ovn-N, veth-krn-N,
   * br-up-N). See WireGuard design discussion, 2026-07-06. */
  | { kind: "dummy" };

// ── NAT ────────────────────────────────────────────────────────────
// Per-stack, since a segment/uplink might need v4 masquerade but not
// v6 (the common case once real delegated IPv6 prefixes exist — see
// ADR 0001 consequence notes on DHCPv6-PD vs NAT66).

export type NatRule = { readonly kind: "masq" };

export interface Nat {
  readonly ipv4?: readonly NatRule[];
  readonly ipv6?: readonly NatRule[];
}

// ── discovery ──────────────────────────────────────────────────────
// HOW this Uplink/Segment's real-world address is learned, per stack.
// "static" means the NetId's address IS the real address, nothing to
// discover. This is what determines which mechanism runs inside an
// uplink's netns (see ADR 0001 — dhclient supervision, SLAAC accept_ra
// handling, etc.) — addressing.ts and define.ts do not need to know
// about discovery; it's read by the (not yet built) generation layer.

/** Which real userspace program acquires this uplink's IPv4 lease when
 * discovery.ipv4 is "dhcp". Defaults to "dhclient" (the only client
 * used so far, and the one confirmed live). "dhcpcd" is the same
 * "dhcp" discovery KIND, a different PROGRAM doing it — same
 * idempotent "already running? no-op : start" shape, different
 * command line (see generate-netns.ts, emitIpv4Discovery). "static"
 * is different again: not a program at all, just "configure this
 * fixed address and gateway directly" — added 2026-07-06 for a real
 * uplink whose real-world address is known and stable (e.g. a
 * reserved LAN IP on the ISP router) rather than DHCP-leased. Kept as
 * its own field rather than folded into the ipv4 union so a future
 * WireGuard uplink — not "dhcp" at all, its own InterfaceKind branch
 * entirely (see WireGuard design discussion, 2026-07-06) — never has
 * to touch this dance. */
export type DhcpClient = "dhclient" | "dhcpcd" | "static";

/** The fixed address+prefix and default gateway to configure directly
 * on a real interface when Discovery.client is "static" — see
 * emitStaticIpv4 (generate-netns.ts). Only consulted then; every other
 * client ignores it. */
export interface StaticIpv4 {
  /** e.g. "192.0.2.93/24" */
  readonly address: string;
  /** e.g. "192.0.2.1" */
  readonly gateway: string;
}

export interface Discovery {
  readonly ipv4?: "static" | "dhcp";
  readonly ipv6?: "static" | "slaac";
  /** Defaults to "dhclient" when ipv4 is "dhcp" and no client is
   * given; otherwise (ipv4 "static" with no explicit client — e.g. a
   * backdoor's merged dummy interface, see Backdoor below) nothing
   * runs here at all. */
  readonly client?: DhcpClient;
  /** Only consulted when client === "static". See StaticIpv4. */
  readonly static4?: StaticIpv4;
}

// ── backdoor: borrowed egress for a VPN-like uplink ─────────────────
// Any uplink with no real interface of its own (dummy today; WireGuard,
// ZeroTier, Tailscale, ... tomorrow — anything tunnel-shaped) still
// needs a mundane, unencrypted path to the real internet: something has
// to carry the tunnel's own setup/keepalive traffic, separate from
// whatever the tunnel itself eventually carries. A backdoor is exactly
// that: a second, dedicated transfer-link-shaped connection from this
// uplink's OWN netns into an ALREADY-real uplink's router (`via`),
// borrowing its egress instead of duplicating one.
//
// Deliberately generic — this is not a WireGuard-specific concept, it's
// what ANY VPN-shaped uplink needs (originally built by hand for one
// specific VPN uplink borrowing a plain uplink's egress, then
// generalized here).
//
// `addresses`/`slot` here are the backdoor's OWN dedicated /28 — NOT
// the owning uplink's own `addresses` (that's its front-door transfer
// link to ITS OWN router). Drawing them from a genuinely separate slot
// is required, not optional: sharing the front-door's /28 (both links'
// addresses inside the SAME subnet, on two different netns interfaces)
// was tried and is broken — Linux ends up with two equally-specific
// connected routes for the one prefix, on two different devices, and
// which one actually wins is unreliable, not a real design. Confirmed
// live, this session — a ping "worked" against the shared-subnet
// version, but for the wrong reason, not because the intended path
// (through `via`'s router) was actually the one carrying it.
export interface Backdoor {
  /** The real, already-working uplink this borrows egress from (e.g.
   * isp-primary). Must already be declared — see NetworkBuilder.uplink(). */
  readonly via: Uplink;
  /** This backdoor's own transfer-link addresses (OVN-side, netns-side)
   * — drawn from the same global slot sequence as every other transfer
   * link (see NetworkBuilder), so it can never collide with one. */
  readonly addresses: Addresses;
  /** The slot this backdoor consumed — used to derive its own
   * IFNAMSIZ-safe kernel interface/bridge names, same convention as
   * uplinkTransferBridge()/vethOvn()/vethNetns() (generate-netns.ts). */
  readonly slot: number;
}

// ── Uplink ───────────────────────────────────────────────────────────

export interface Uplink {
  /** Unique per network — also used directly as the prefix for every
   * generated OVN object name (sw-<name>, router-<name>, lrp-<name>,
   * ...). Uniqueness is enforced by NetworkBuilder (see define.ts). */
  readonly name: string;
  /** The small sequential index NetworkBuilder assigned this uplink
   * (0-4095), used for BOTH the transfer-link IPv4 block (transferNet)
   * and the backbone-leg IPv4 block (uplinkBackboneNet) — kept on the
   * resolved object so tier-2 generation can recover it without
   * re-deriving it from an already-computed address. */
  readonly slot: number;
  readonly addresses: Addresses;
  readonly if: InterfaceKind;
  readonly nat?: Nat;
  readonly discovery?: Discovery;
  /** Borrowed egress for a VPN-like uplink with no real interface of
   * its own — see Backdoor above. Undefined for every uplink that has
   * real connectivity itself (a VLAN uplink, a physical NIC, a working
   * VPN tunnel, ...). */
  readonly backdoor?: Backdoor;
  readonly host: Host;
}

// ── switchable uplink selection ───────────────────────────────────
// A segment does not hold a fixed Uplink reference. It holds an
// UplinkSelector — something that can be asked "which uplink right
// now" — so the generator can support failover/manual-switch later
// without changing the Segment type or any derivation logic that
// consumes it. Three selector strategies are provided; all of them
// satisfy the same interface, so emit-time code only ever calls
// `.resolve()` and never needs to know which strategy is in play.

export interface UplinkSelector {
  resolve(): Uplink;
}

/** Always the same uplink. The common case, and tonight's actual need. */
export class FixedUplink implements UplinkSelector {
  constructor(private readonly uplink: Uplink) {}
  resolve(): Uplink {
    return this.uplink;
  }
}

/**
 * Picks the first uplink in priority order whose `isAvailable` callback
 * returns true. `isAvailable` is injected, not hardcoded — at generation
 * time it might always return true (no live-state check, "as designed"
 * output); at a future runtime-aware stage it could call a data-source
 * plugin (see ADR 0001 §5) to check a real lease/handshake state.
 */
export class PriorityUplink implements UplinkSelector {
  constructor(
    private readonly candidates: readonly Uplink[],
    private readonly isAvailable: (u: Uplink) => boolean = () => true,
  ) {
    if (candidates.length === 0) {
      throw new Error("PriorityUplink requires at least one candidate");
    }
  }
  resolve(): Uplink {
    const found = this.candidates.find((u) => this.isAvailable(u));
    return found ?? this.candidates[0];
  }
}

/** Explicit manual override — for an operator-driven "switch to X now". */
export class ManualUplink implements UplinkSelector {
  private current: Uplink;
  constructor(initial: Uplink) {
    this.current = initial;
  }
  resolve(): Uplink {
    return this.current;
  }
  switchTo(uplink: Uplink): void {
    this.current = uplink;
  }
}

// ── extra routes: a MORE-SPECIFIC route via a SECONDARY uplink ──────
// A segment's primary `uplink` (below) gets the default route
// (0.0.0.0/0 / ::/0) plus NAT — that's its one general-purpose
// internet egress. An ExtraRoute is a completely separate, additional
// backbone join to a DIFFERENT uplink, carrying only a specific prefix
// — e.g. routing a private supernet (192.168.0.0/16) into a VPN-mesh
// uplink (ZeroTier, a second WireGuard peer, ...) so traffic to OTHER
// sites in that mesh goes there, while everything else still leaves
// via the segment's normal uplink. Deliberately separate from `uplink`
// rather than trying to extend UplinkSelector to return multiple
// uplinks with per-uplink route scoping — a segment can have zero,
// one, or several of these, each independent, each getting its own
// backbone join (see emitBackboneJoin, generate-ovn.ts) distinctly
// named from the primary join so multiple simultaneous joins for the
// same segment never collide.
export interface ExtraRoute {
  /** e.g. "192.168.0.0/16". Passed straight to `ovn-nbctl
   * lr-route-add` — no fold/derivation, this is a literal prefix the
   * caller declares. */
  readonly prefix: string;
  /** IPv6 equivalent, if this route needs one too. Omit for a v4-only
   * extra route (the common case for a private-supernet-shaped
   * route). */
  readonly prefix6?: string;
  /** Already resolved to a selector by the factory (segmentPhysical/
   * segmentVlan), same normalization as Segment.uplink — the caller in
   * config/topology.ts may pass a plain Uplink or any UplinkSelector,
   * see resolveUplinkSelector (factories.ts). */
  readonly uplink: UplinkSelector;
}

// ── Segment ──────────────────────────────────────────────────────────

export interface Segment {
  /** Unique per network — also used directly as the prefix for every
   * generated OVN object name (sw-<name>, router-<name>, lrp-<name>,
   * ...). Uniqueness is enforced by NetworkBuilder (see define.ts). */
  readonly name: string;
  readonly addresses: Addresses;
  readonly if: InterfaceKind;
  /** Undefined means "no egress yet" — deliberately, not a bug: a
   * segment meant to eventually exit via an uplink that doesn't exist
   * yet (e.g. a VPN WireGuard tunnel not built out) should have NO
   * backbone join, NO route, and NO NAT generated for it at all, not
   * be silently routed out whichever uplink happens to be declared —
   * confirmed live: two VPN-bound segments were provisionally pointed
   * at the general default uplink and got MASQUERADEd out alongside
   * another segment, defeating the whole point of routing them
   * through a separate VPN egress later. See emitSegmentBackboneJoin
   * (generate-ovn.ts), which returns no lines at all when this is
   * undefined. */
  readonly uplink?: UplinkSelector;
  /** Zero or more additional, more-specific routes via a SECONDARY
   * uplink — see ExtraRoute above. Independent of `uplink`; a segment
   * can have a primary uplink, extra routes, both, or neither. */
  readonly extraRoutes?: readonly ExtraRoute[];
  readonly nat?: Nat;
  /** Whether OVN advertises RA/SLAAC for this segment's IPv6 prefix so
   * clients self-configure a global address (see generate-ovn.ts). */
  readonly slaac: boolean;
  readonly host: Host;
}
