// src/addressing.ts — the three fold rules, exactly once each, exposed
// as NetId factories.
//
// Fold operation = string construction. Decimal identifiers are placed
// directly into the address text, so the result stays human-readable
// and visually corresponds to its IPv4 counterpart (fd00:10:80::128:1
// reads as "segment 128", the same way 10.80.128.1 does). IPAddress.parse()
// is called ONLY AFTER the string is fully built — it validates the
// result and returns a real IPAddress for everything downstream
// (containment checks, to_string(), comparisons). There is no integer
// arithmetic on Crunchy here: that produces hex-folded results (segment
// 128 -> "80") which breaks the human-readability property these rules
// exist for.

// Deno's npm-CJS interop doesn't type this import as callable directly
// (its .d.ts's `export default` doesn't resolve the way a real ESM
// default would) — confirmed live, 2026-08-12: `import hash from
// "fnv1a"` type-checks as the whole module namespace, not the function
// itself. The runtime value IS the callable function either way (CJS
// `module.exports = hash`), so a cast through `unknown` is honest here,
// not a workaround for a real type mismatch.
import fnv1aModule from "fnv1a";
const hash = fnv1aModule as unknown as (s: string, h?: number) => number;
import { IPv4, IPv6 } from "./ip.ts";
import {
  type NetId,
  type SegmentGateway,
  type SegmentId,
  segmentId,
  type UplinkId,
  uplinkId,
} from "./types.ts";

function makeNetId(
  ipv4Str: string,
  ipv6Str: string,
  rawId: number,
  vlan: number | undefined,
): NetId {
  const ipv4 = IPv4.parse(ipv4Str);
  const ipv6 = IPv6.parse(ipv6Str);
  return {
    ipv4,
    ipv6,
    id: () => rawId,
    vlan: () => vlan,
  };
}

// ── backbone: one shared /16, every identifier gets its own /28 block ──
//
// IPv4: 10.80.0.0/16, 4096 /28 blocks (same partition scheme as
// transferNet's 10.99.0.0/16 — see that function's comment for the
// general approach). block_base = identifier << 4. Within a block,
// SEGMENTS use the low half (host offsets 1-7) and UPLINKS use the
// high half (block_base | 0x8, host offsets 1-7 within that half) —
// so a segment and an uplink that happen to share the same numeric
// slot can never collide, both halves fit in the same /28 without
// overlapping.
//
// IPv6 has no equivalent scarcity (128 bits is enormous) — it keeps
// directly folding the real identifier into the network portion, with
// a literal 0/8 marker group distinguishing segment vs uplink, so the
// human-readability property (you can read an id straight out of the
// address) is preserved on the v6 side even though v4 has to use the
// less-readable shifted-block scheme to fit in 32 bits.
//
// No physical VLAN on either — the backbone is OVN-internal transit,
// not wired to any single segment's or uplink's physical VLAN. vlan()
// is always undefined.

function backboneBlockAddress(blockBase: number, host: number): string {
  if (host < 1 || host > 7) {
    throw new RangeError(`backbone host out of range (1-7): ${host}`);
  }
  const third = (blockBase >> 8) & 0xff;
  const fourth = (blockBase & 0xff) + host;
  return `10.80.${third}.${fourth}`;
}

export function segmentBackboneNet(
  segment: SegmentId | number,
  host: number,
): NetId {
  const id = typeof segment === "number" ? segmentId(segment) : segment;
  const blockBase = id << 4;
  return makeNetId(
    `${backboneBlockAddress(blockBase, host)}/16`,
    `fd00:10:80::0:${id}:${host}/64`,
    id,
    undefined,
  );
}

export function uplinkBackboneNet(
  uplink: UplinkId | number,
  slot: number,
  host: number,
): NetId {
  // `slot` (not the real uplink id) drives the IPv4 block-shift, same
  // as transferNet — uplink ids like 1280 are far too large to use
  // directly in <<4 arithmetic (1280<<4 = 20480, producing a
  // meaningless-looking but technically-valid address in the wrong
  // place: 10.80.80.x). `slot` MUST be the same small sequential index
  // (0,1,2,...) NetworkBuilder already assigns for the transfer link
  // (see define.ts), so an uplink's backbone leg and transfer leg use
  // consistent, small, collision-free numbering. The real uplink id is
  // still used for the IPv6 fold, which has no such size constraint.
  const id = typeof uplink === "number" ? uplinkId(uplink) : uplink;
  if (slot < 0 || slot > 4095) {
    throw new RangeError(
      `uplinkBackboneNet: slot out of range (0-4095): ${slot}`,
    );
  }
  const blockBase = (slot << 4) | 0x8;
  return makeNetId(
    `${backboneBlockAddress(blockBase, host)}/16`,
    `fd00:10:80::8:${id}:${host}/64`,
    id,
    undefined,
  );
}

// ── client segments: one network PER SEGMENT, identifier folds into the
//    NETWORK portion ──
// fd00:192:168:<segment>::<host>/64  <->  192.168.<segment>.<host>/24
//
// vlan() returns the segment id itself when the segment's physical VLAN
// tag matches its numeric id (true for every segment built so far —
// segment 128 is VLAN 128, etc.). Pass an explicit vlan to override
// (e.g. segment 128 is untagged on the wire, so its real VLAN concept
// doesn't apply the same way — see untaggedOnWire on Segment).

// Extract the host-id embedded in an explicit gateway address, so it
// can be "transferred" over to fold the OTHER family's address when
// that family has no suffix/literal of its own (see segmentNet below).
// Deliberately text-based (split + parseInt base 10), NOT a numeric
// IPAddress operation: the host-id is a human-readable DECIMAL label
// placed directly into the address text (see this file's header
// comment), not a real integer derived from the address's binary
// value — e.g. IPv6 "::10" is meant to read as "host 10", even though
// as a real hex group it's worth 16. `IPAddress.sub()`/`.add()` operate
// on the true binary value and would silently give the wrong number
// here; text extraction (on to_s()'s bare, prefix-free rendering)
// preserves the label as typed.
function extractSuffixFromIpv4Literal(addr: IPv4): number {
  const parts = addr.to_s().split(".");
  const last = parts[parts.length - 1];
  const n = Number.parseInt(last, 10);
  if (Number.isNaN(n)) {
    throw new Error(
      `segmentNet: could not extract a host-id from ipv4 literal "${addr.to_s()}"`,
    );
  }
  return n;
}

function extractSuffixFromIpv6Literal(addr: IPv6): number {
  const groups = addr.to_s().split(":").filter((g) => g.length > 0);
  const last = groups[groups.length - 1];
  const n = Number.parseInt(last, 10);
  if (Number.isNaN(n)) {
    throw new Error(
      `segmentNet: could not extract a host-id from ipv6 literal "${addr.to_s()}"`,
    );
  }
  return n;
}

// Resolution rules, per family (v4 shown; v6 is the mirror image):
//   1. `ipv4` given -> use it EXACTLY as given (already parsed/
//      validated by the caller via IPv4.parse(...) — no
//      segment-default-prefix substitution here, unlike the old
//      string-based API this replaced).
//   2. else `suffix` given -> fold it into the standard v4 pattern.
//   3. else `ipv6` given -> "transfer": extract the host-id out of the
//      ipv6 literal and fold THAT into the standard v4 pattern, so a
//      caller supplying only one family's literal doesn't also have
//      to spell out a redundant suffix.
//   4. else -> throw: nothing at all was given for this family.
export function segmentNet(
  segment: SegmentId | number,
  gateway: SegmentGateway,
): NetId {
  const id = typeof segment === "number" ? segmentId(segment) : segment;

  let ipv4: IPv4;
  if (gateway.ipv4 !== undefined) {
    ipv4 = gateway.ipv4;
  } else if (gateway.suffix !== undefined) {
    ipv4 = IPv4.parse(`192.168.${id}.${gateway.suffix}/24`);
  } else if (gateway.ipv6 !== undefined) {
    const transferred = extractSuffixFromIpv6Literal(gateway.ipv6);
    ipv4 = IPv4.parse(`192.168.${id}.${transferred}/24`);
  } else {
    throw new Error(
      `segmentNet: segment ${id} needs one of "suffix", "ipv4", or "ipv6"`,
    );
  }

  let ipv6: IPv6;
  if (gateway.ipv6 !== undefined) {
    ipv6 = gateway.ipv6;
  } else if (gateway.suffix6 !== undefined || gateway.suffix !== undefined) {
    const suffix6 = gateway.suffix6 ?? gateway.suffix as number;
    ipv6 = IPv6.parse(`fd00:192:168:${id}::${suffix6}/64`);
  } else if (gateway.ipv4 !== undefined) {
    const transferred = extractSuffixFromIpv4Literal(gateway.ipv4);
    ipv6 = IPv6.parse(`fd00:192:168:${id}::${transferred}/64`);
  } else {
    throw new Error(
      `segmentNet: segment ${id} needs one of "suffix", "suffix6", or "ipv4"/"ipv6"`,
    );
  }

  return {
    ipv4,
    ipv6,
    id: () => id,
    vlan: () => id,
  };
}

// ── transfer links: one network PER UPLINK, identifier folds into the
//    NETWORK portion ──
// fd00:10:99:<uplink>::<host>/124  <->  10.99.<third>.<fourth>/28
//
// IPv4: 10.99.0.0/16 holds exactly 4096 /28 blocks (16 addresses each).
// `slot` (0-4095) selects which block — slot N is the Nth /28 in
// sequence: slot 0 = 10.99.0.0/28, slot 1 = 10.99.0.16/28, ...,
// slot 128 = 10.99.8.0/28, etc. `slot` is NOT the uplink's own id
// (uplink ids like 1280/1281/1282 exceed 4095 and aren't sequential
// from 0) — it's a small sequential index the caller assigns, one per
// uplink, distinct from the uplink's real identity. See define.ts,
// where NetworkBuilder assigns slots automatically in declaration
// order so config/topology.ts never has to think about this at all.
//
// IPv6 still folds the uplink's real id (not slot) into the network
// portion, since IPv6 has no equivalent address-space scarcity forcing
// a slot scheme — fd00:10:99:1280::/124 is perfectly fine on its own.
//
// No physical VLAN on the OVN side of a transfer link — vlan() is
// undefined here; the REAL uplink (see uplinkNet below) is what carries
// the physical VLAN tag.

export function transferNet(
  uplink: UplinkId | number,
  slot: number,
  host: number,
): NetId {
  const id = typeof uplink === "number" ? uplinkId(uplink) : uplink;
  if (slot < 0 || slot > 4095) {
    throw new RangeError(`transfer slot out of range (0-4095): ${slot}`);
  }
  if (host < 1 || host > 14) {
    throw new RangeError(`transfer host out of range (1-14): ${host}`);
  }
  const blockBase = slot * 16; // 0-65520, fits in 16 bits
  const third = (blockBase >> 8) & 0xff;
  const fourth = (blockBase & 0xff) + host;
  return makeNetId(
    `10.99.${third}.${fourth}/28`,
    `fd00:10:99:${id}::${host}/124`,
    id,
    undefined,
  );
}

// ── uplink's real-world physical identity ──────────────────────────
// The uplink's own VLAN (e.g. 1280 for an uplink named isp-primary) — this is the ONE
// place vlan() reflects a real physical tag tied 1:1 to the uplink
// numeric id, since every uplink built so far uses vlanId === id.
// No IPv4/IPv6 addressing of its own (the uplink's real address is
// whatever the ISP hands out dynamically) — ipv4/ipv6 here are the
// transfer link's OVN-side address, reused, so every NetId still
// satisfies the same shape.

export function uplinkNet(
  uplink: UplinkId | number,
  slot: number,
): NetId {
  const id = typeof uplink === "number" ? uplinkId(uplink) : uplink;
  const transfer = transferNet(id, slot, 1);
  return makeNetId(
    transfer.ipv4.to_string(),
    transfer.ipv6.to_string(),
    id,
    id,
  );
}

// ── transit network: an OVN<->kernel-netns point-to-point link ──────
// The kernelRouterEndpoint()/transit-CollisionDomain design (2026-08-12
// discussion) — unlike transferNet() above, the config author supplies
// the transit block explicitly (a full network prefix, e.g. IPv4.parse
// ("10.99.0.0/30")), not a slot: auto-allocation is deferred, not
// designed away (see that discussion). This just folds ONE given
// network per family into its two point-to-point endpoint addresses —
// `.first()`/`.last()` (ip.ts), not `.network()`/`.broadcast()`: for
// IPv4 that's the two actually-usable host addresses of the block
// (10.99.0.0/30 -> .1/.2, skipping the network/broadcast addresses);
// for IPv6, which has no reserved broadcast, `.first()` is the
// network's own zero-host address and `.last()` its highest — both
// genuinely usable there. `left`/`right` are deliberately unlabeled
// beyond that (not "ovnSide"/"kernelSide") — which literal side is OVN
// vs kernel is the CALLER's concern (kernelRouterEndpoint() decides),
// not something this pure fold has any basis to assert.
export interface TransitEndpoint {
  readonly ipv4?: IPv4;
  readonly ipv6?: IPv6;
}

export interface TransitNetwork {
  readonly left: TransitEndpoint;
  readonly right: TransitEndpoint;
}

export function transitNetwork(ipv4?: IPv4, ipv6?: IPv6): TransitNetwork {
  return {
    left: {
      ipv4: ipv4?.first(),
      ipv6: ipv6?.first(),
    },
    right: {
      ipv4: ipv4?.last(),
      ipv6: ipv6?.last(),
    },
  };
}

// ── MAC address derivation ────────────────────────────────────────
// Folds an IPv4 address's four octets directly into a MAC, prefixed
// with 00:00 — the convention already used by hand throughout this
// project (e.g. 192.168.128.2 -> 00:00:c0:a8:80:02, verified against a
// live deployment). Locally-administered OUI space
// (00:00:xx is not a real vendor block) is fine for this use — these
// MACs only need to be unique within OVN's logical topology, never
// routed on a real physical LAN segment.

export function macFromV4(ipv4: IPv4): string {
  const octets = ipv4.to_s().split("/")[0].split(".").map((s) =>
    Number.parseInt(s, 10)
  );
  if (octets.length !== 4 || octets.some((o) => Number.isNaN(o))) {
    throw new Error(`macFromV4: not a valid IPv4 address: ${ipv4.to_s()}`);
  }
  const hex = octets.map((o) => o.toString(16).padStart(2, "0"));
  return `00:00:${hex.join(":")}`;
}

// ── MAC derivation for real (vlan) attachments ──────────────────────
// Unlike macFromV4 above, needs no address to exist at all yet — closes
// the exact gap macFromV4 couldn't (a DHCP/transfer-link endpoint whose
// real address isn't known at config time). `macSuffix` is network-wide
// (see defineNetwork's own options — explicit, or fnv1a32(networkName)
// when omitted) and folds into the upper 4 bytes; `vlanId` (0 when this
// attachment isn't a vlan at all) folds into the lower 2.
export function fnv1a32(s: string): number {
  return hash(s);
}

export function macFromVlan(
  macSuffix: number,
  vlanId: number | undefined,
): string {
  const hi = (macSuffix >>> 0).toString(16).padStart(8, "0");
  const lo = ((vlanId ?? 0) & 0xffff).toString(16).padStart(4, "0");
  return (hi + lo).match(/../g)!.join(":");
}
