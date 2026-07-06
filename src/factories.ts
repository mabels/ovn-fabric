// src/factories.ts — sparse-input factories for Uplink/Segment.
//
// net.uplink(name, uplinkVlan({ ... })) reads as: "this uplink is realized as
// a VLAN; here are the few facts that vary." Every factory here takes a
// sparse, optional-heavy input and returns an UplinkBuilder — a function
// of `slot` (the transfer-link /28 block index, see addressing.ts
// transferNet()) producing a FULLY RESOLVED object with no optional
// fields left unset. `slot` is deliberately NOT something the caller
// supplies in config/topology.ts: NetworkBuilder.uplink() (define.ts)
// assigns slots sequentially in declaration order and calls this
// function itself, so a config never has to think about slot allocation
// or its 0-4095 range at all — and two uplinks can never collide on the
// same slot, since each is assigned exactly once by NetworkBuilder.
//
// Addresses are computed from `id` via addressing.ts's fold rules
// unless the caller asks for "dhcp"/"slaac" discovery instead, in which
// case the corresponding stack's address is left to be learned at
// runtime (see ADR 0001 — the netns owns that, not this generator).
//
// This is the layer config/topology.ts actually calls. define.ts's
// NetworkBuilder.uplink()/segment() accept the UplinkBuilder/Segment
// these factories produce — they do not know about "vlan" vs "physical"
// vs sparse input at all.

import { segmentNet, transferNet } from "./addressing.ts";
import type {
  Addresses,
  Backdoor,
  DhcpClient,
  Discovery,
  Host,
  InterfaceKind,
  Nat,
  StaticIpv4,
  Uplink,
  WireguardInterfaceConfig,
} from "./types.ts";

/** A factory result still waiting on its assigned transfer-link
 * slot(s). Takes an ALLOCATOR, not a bare slot number: most uplinks
 * consume exactly one slot (their own front-door transfer link), but
 * an uplink with a `backdoor` (see types.ts) needs a SECOND, entirely
 * separate slot for it — sharing the front-door's /28 was tried and is
 * broken (see Backdoor's doc comment). Calling allocSlot() again for
 * the backdoor, from the same sequential pool NetworkBuilder already
 * guarantees uniqueness over, is what keeps the two from ever
 * colliding.
 *
 * Also receives `name` — the SAME string given to net.uplink(name,
 * ...) — so a factory that needs a human-facing identifier of its own
 * (e.g. uplinkWireguard's real interface/conf-file name) can reuse it
 * instead of asking for a second, redundant one. Most factories ignore
 * it entirely (JS/TS both allow a callback to declare fewer params
 * than its type permits), which is why this was added as a second
 * parameter rather than changed in place — every existing builder
 * above keeps working unmodified. */
export type UplinkBuilder = (
  allocSlot: () => number,
  name: string,
) => Omit<Uplink, "name">;
import {
  FixedUplink,
  segmentId,
  type Segment,
  uplinkId,
  type UplinkSelector,
} from "./types.ts";

// ── shared per-stack address-or-discovery input ─────────────────────
// Each stack is either omitted (defaults to "dhcp"/"slaac" — the common
// case for a real WAN uplink, which has no fixed address to compute),
// or explicitly "dhcp"/"slaac" (same as omitted, written out for
// clarity), or a literal already-known IPAddress-parseable string for
// cases like a cable/DSL uplink's historically-stable lease.

interface AddressSpec {
  readonly ipv4?: "dhcp" | string;
  readonly ipv6?: "slaac" | string;
}

function resolveDiscovery(
  spec: AddressSpec | undefined,
  client?: DhcpClient,
  static4?: StaticIpv4,
): Discovery {
  // client === "static" implies ipv4 "static" outright — the caller
  // doesn't also have to remember to set addresses.ipv4: "static"
  // separately for it to take effect (see emitIpv4Discovery,
  // generate-netns.ts).
  const ipv4 = client === "static"
    ? "static"
    : spec?.ipv4 === undefined || spec.ipv4 === "dhcp"
    ? "dhcp"
    : "static";
  const ipv6 = spec?.ipv6 === undefined || spec.ipv6 === "slaac"
    ? "slaac"
    : "static";
  return { ipv4, ipv6, client, static4 };
}

// ── vlan() ───────────────────────────────────────────────────────────

interface VlanUplinkInput {
  readonly id: string | number;
  readonly vlanParent: string;
  /** Override the VLAN tag if it should differ from `id`. Defaults to id. */
  readonly vlan?: number;
  /** Override the kernel subinterface name — see InterfaceKind
   * (types.ts). Only needed if `${vlanParent}.${vlanId}` is already in
   * use for something else. */
  readonly ifaceName?: string;
  readonly nat?: Nat;
  readonly host: Host;
  readonly addresses?: AddressSpec;
  /** Which program acquires the IPv4 lease when addresses.ipv4 is
   * "dhcp" (or omitted, same default). Defaults to "dhclient" — see
   * DhcpClient (types.ts). Set to "static" (with `static4` below) for
   * a real interface whose address is fixed/known rather than leased. */
  readonly client?: DhcpClient;
  /** Only consulted when client === "static" — see StaticIpv4 (types.ts). */
  readonly static4?: StaticIpv4;
}

export function uplinkVlan(input: VlanUplinkInput): UplinkBuilder {
  const id = uplinkId(
    typeof input.id === "string" ? Number.parseInt(input.id, 10) : input.id,
  );

  return (allocSlot: () => number) => {
    const slot = allocSlot();
    const ifc: InterfaceKind = {
      kind: "vlan",
      vlanParent: input.vlanParent,
      vlanId: input.vlan ?? id,
      ifaceName: input.ifaceName,
    };

    const addresses: Addresses = [
      transferNet(id, slot, 1), // OVN side
      transferNet(id, slot, 2), // netns side
    ];

    return {
      slot,
      addresses,
      if: ifc,
      nat: input.nat,
      discovery: resolveDiscovery(input.addresses, input.client, input.static4),
      host: input.host,
    };
  };
}

// ── physical() ───────────────────────────────────────────────────────
// The raw case: a real, untagged interface (e.g. { name: "ens19" }).
// No VLAN math, no transfer-link addressing — used for an uplink
// (or segment) that's wired directly, no 802.1Q tagging involved.

interface PhysicalUplinkInput {
  readonly id: string | number;
  readonly name: string;
  readonly nat?: Nat;
  readonly host: Host;
  readonly addresses?: AddressSpec;
  /** Which program acquires the IPv4 lease when addresses.ipv4 is
   * "dhcp" (or omitted, same default). Defaults to "dhclient" — see
   * DhcpClient (types.ts). Set to "static" (with `static4` below) for
   * a real interface whose address is fixed/known rather than leased. */
  readonly client?: DhcpClient;
  /** Only consulted when client === "static" — see StaticIpv4 (types.ts). */
  readonly static4?: StaticIpv4;
}

export function uplinkPhysical(input: PhysicalUplinkInput): UplinkBuilder {
  const id = uplinkId(
    typeof input.id === "string" ? Number.parseInt(input.id, 10) : input.id,
  );

  return (allocSlot: () => number) => {
    const slot = allocSlot();
    const ifc: InterfaceKind = { kind: "physical", name: input.name };

    const addresses: Addresses = [
      transferNet(id, slot, 1),
      transferNet(id, slot, 2),
    ];

    return {
      slot,
      addresses,
      if: ifc,
      nat: input.nat,
      discovery: resolveDiscovery(input.addresses, input.client, input.static4),
      host: input.host,
    };
  };
}

// ── dummy() ──────────────────────────────────────────────────────────
// A placeholder uplink with no real backing interface — see
// InterfaceKind's "dummy" variant (types.ts). Lets the OVN
// router/transfer-link/backbone-join chain be built and pinged
// end-to-end before the real mechanism (e.g. a WireGuard tunnel)
// exists. No `name`/`vlanParent` to supply — the generator derives and
// creates the actual kernel interface itself, at a slot-based name.

interface DummyUplinkInput {
  readonly id: string | number;
  readonly nat?: Nat;
  readonly host: Host;
  readonly addresses?: AddressSpec;
  readonly client?: DhcpClient;
  /** Borrow real egress from another, already-working uplink — see
   * Backdoor (types.ts). Needed for any VPN-like uplink (WireGuard,
   * ZeroTier, ...): the dummy/tunnel interface itself carries no real
   * traffic path, so something else has to get this uplink's own netns
   * onto the real internet. `via` must already be declared (e.g.
   * isp-primary), earlier in the same defineNetwork call. */
  readonly backdoor?: { readonly via: Uplink };
}

export function uplinkDummy(input: DummyUplinkInput): UplinkBuilder {
  const id = uplinkId(
    typeof input.id === "string" ? Number.parseInt(input.id, 10) : input.id,
  );

  return (allocSlot: () => number) => {
    const slot = allocSlot();
    const ifc: InterfaceKind = { kind: "dummy" };

    const addresses: Addresses = [
      transferNet(id, slot, 1),
      transferNet(id, slot, 2),
    ];

    // Backdoor gets its OWN slot — allocSlot() called a second time,
    // from the same sequential pool as every uplink's front-door
    // transfer link, so its /28 (IPv4) can never overlap with this
    // uplink's own `addresses` above (see Backdoor's doc comment,
    // types.ts, for why that overlap is a real bug and not just
    // untidy). host=3/4 here, not 1/2: transferNet's IPv6 fold keys
    // only on (id, host), NOT slot, so reusing host 1/2 under the SAME
    // id would fold to the IDENTICAL IPv6 addresses as this uplink's
    // own front-door transfer link above, even though slot keeps the
    // IPv4 side distinct. 3/4 keeps both stacks distinct.
    const backdoor: Backdoor | undefined = input.backdoor === undefined
      ? undefined
      : (() => {
        const bdSlot = allocSlot();
        return {
          via: input.backdoor!.via,
          slot: bdSlot,
          addresses: [
            transferNet(id, bdSlot, 3),
            transferNet(id, bdSlot, 4),
          ],
        };
      })();

    return {
      slot,
      addresses,
      if: ifc,
      nat: input.nat,
      // Defaults to "static"/"static" (no discovery) unlike
      // uplinkVlan/uplinkPhysical's "dhcp" default — a dummy interface
      // has no real ISP behind it, so dhclient/dhcpcd would just hang
      // forever waiting for a lease that never comes. Still overridable
      // via `addresses`, in case a future test wants to exercise the
      // discovery path against a dummy on purpose.
      discovery: resolveDiscovery(
        input.addresses ?? { ipv4: "static", ipv6: "static" },
        input.client,
      ),
      backdoor,
      host: input.host,
    };
  };
}

// ── wireguard() ──────────────────────────────────────────────────────
// A real WireGuard tunnel — see InterfaceKind's "wireguard" variant
// (types.ts) for why this shells out to wg-quick against an
// already-on-disk conf file rather than embedding key material here.
// Almost always paired with `backdoor` (the tunnel's own
// handshake/keepalive traffic needs a mundane path to the real
// internet, same as uplinkDummy() — see Backdoor, types.ts), but not
// required: an uplink whose netns can already reach the internet some
// other way (e.g. it inherited a route) wouldn't need one.

interface WireguardUplinkInput {
  readonly id: string | number;
  /** Override the real kernel interface name AND the .conf's basename
   * on disk (e.g. "mull-fra" -> /etc/wireguard/mull-fra.conf). Defaults
   * to this uplink's OWN name (the string given to net.uplink(name,
   * ...)) — redundant to repeat that in most cases, since it's already
   * unique per uplink. Only needed when that default doesn't fit: it
   * exceeds IFNAMSIZ (15 usable characters), or a real interface under
   * a different name already exists and renaming it would mean
   * bouncing a live tunnel unnecessarily. */
  readonly ifaceName?: string;
  /** The full wg-quick conf, declared directly here — see
   * WireguardInterfaceConfig and InterfaceKind's "wireguard" variant
   * (types.ts) for why the PrivateKey lives in plain sight in this
   * file rather than behind an env var, for this uplink specifically. */
  readonly config: WireguardInterfaceConfig;
  readonly nat?: Nat;
  readonly host: Host;
  /** Bootstrap egress for the tunnel's own setup/keepalive traffic —
   * see Backdoor (types.ts) and emitBackdoorNat (generate-netns.ts),
   * which scopes the resulting NAT rule to the backdoor's OWN transit
   * address (not any segment subnet) whenever the owning uplink has a
   * real interface of its own, as this one does. */
  readonly backdoor?: { readonly via: Uplink };
}

export function uplinkWireguard(input: WireguardUplinkInput): UplinkBuilder {
  const id = uplinkId(
    typeof input.id === "string" ? Number.parseInt(input.id, 10) : input.id,
  );

  return (allocSlot: () => number, name: string) => {
    const slot = allocSlot();
    const ifc: InterfaceKind = {
      kind: "wireguard",
      ifaceName: input.ifaceName ?? name,
      config: input.config,
    };

    const addresses: Addresses = [
      transferNet(id, slot, 1),
      transferNet(id, slot, 2),
    ];

    // Same separate-slot reasoning as uplinkDummy()'s backdoor above —
    // see Backdoor's doc comment (types.ts) for why sharing a /28 with
    // this uplink's own front-door transfer link is broken, not just
    // untidy.
    const backdoor: Backdoor | undefined = input.backdoor === undefined
      ? undefined
      : (() => {
        const bdSlot = allocSlot();
        return {
          via: input.backdoor!.via,
          slot: bdSlot,
          addresses: [
            transferNet(id, bdSlot, 3),
            transferNet(id, bdSlot, 4),
          ],
        };
      })();

    return {
      slot,
      addresses,
      if: ifc,
      nat: input.nat,
      // wg-quick manages the tunnel's own address (and, via its
      // fwmark/policy-routing dance, its own default route) entirely —
      // nothing for dhclient/dhcpcd/static to do on this interface.
      discovery: { ipv4: "static", ipv6: "static" },
      backdoor,
      host: input.host,
    };
  };
}

// ── segment factories ─────────────────────────────────────────────
// Mirror the uplink factories above, but produce a Segment-shaped
// result (adds `uplink`, uses segmentNet() instead of transferNet()
// since a segment has a client-facing gateway address, not a
// transfer-link pair).

/** input.uplink -> UplinkSelector | undefined. Omitting uplink entirely
 * means "no egress yet" (see Segment.uplink, types.ts) — a deliberate,
 * representable state, not an oversight. */
function resolveUplinkSelector(
  uplink: Uplink | UplinkSelector | undefined,
): UplinkSelector | undefined {
  if (uplink === undefined) return undefined;
  return "resolve" in uplink ? uplink : new FixedUplink(uplink);
}

interface SegmentPhysicalInput {
  readonly id: string | number;
  readonly name: string;
  readonly uplink?: Uplink | UplinkSelector;
  readonly nat?: Nat;
  /** Advertise RA/SLAAC for this segment's IPv6 prefix. Defaults to true. */
  readonly slaac?: boolean;
  /** The last octet/host-id OVN's own gateway answers on within this
   * segment's /24 (e.g. 2 -> 192.168.<id>.2). Defaults to 2, matching
   * the usual "existing router keeps .1, OVN answers on .2, both
   * coexist" pattern. Override to 1 once the old router at .1 is
   * decommissioned and OVN should take over that address instead. */
  readonly gatewayHost?: number;
  readonly host: Host;
}

export function segmentPhysical(
  input: SegmentPhysicalInput,
): Omit<Segment, "name"> {
  const id = segmentId(
    typeof input.id === "string" ? Number.parseInt(input.id, 10) : input.id,
  );
  return {
    addresses: [segmentNet(id, input.gatewayHost ?? 2)],
    if: { kind: "physical", name: input.name },
    uplink: resolveUplinkSelector(input.uplink),
    nat: input.nat,
    slaac: input.slaac ?? true,
    host: input.host,
  };
}

interface SegmentVlanInput {
  readonly id: string | number;
  readonly vlanParent: string;
  /** Override the VLAN tag if it should differ from `id`. Defaults to id. */
  readonly vlan?: number;
  /** Override the kernel subinterface name — see InterfaceKind
   * (types.ts). Only needed if `${vlanParent}.${vlanId}` is already in
   * use for something else (e.g. VLAN 129 already carries this host's
   * own management IP on `ens18.129` outside OVS). */
  readonly ifaceName?: string;
  readonly uplink?: Uplink | UplinkSelector;
  readonly nat?: Nat;
  /** Advertise RA/SLAAC for this segment's IPv6 prefix. Defaults to true. */
  readonly slaac?: boolean;
  /** The last octet/host-id OVN's own gateway answers on within this
   * segment's /24 (e.g. 2 -> 192.168.<id>.2). Defaults to 2, matching
   * the usual "existing router keeps .1, OVN answers on .2, both
   * coexist" pattern. Override to 1 once the old router at .1 is
   * decommissioned and OVN should take over that address instead. */
  readonly gatewayHost?: number;
  readonly host: Host;
}

export function segmentVlan(input: SegmentVlanInput): Omit<Segment, "name"> {
  const id = segmentId(
    typeof input.id === "string" ? Number.parseInt(input.id, 10) : input.id,
  );
  return {
    addresses: [segmentNet(id, input.gatewayHost ?? 2)],
    if: {
      kind: "vlan",
      vlanParent: input.vlanParent,
      vlanId: input.vlan ?? id,
      ifaceName: input.ifaceName,
    },
    uplink: resolveUplinkSelector(input.uplink),
    nat: input.nat,
    slaac: input.slaac ?? true,
    host: input.host,
  };
}
