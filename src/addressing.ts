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

import { IPAddress } from "npm:ipaddress@0.2.6";
import {
  type NetId,
  segmentId,
  type SegmentId,
  uplinkId,
  type UplinkId,
} from "./types.ts";

function makeNetId(
  ipv4Str: string,
  ipv6Str: string,
  rawId: number,
  vlan: number | undefined,
): NetId {
  const ipv4 = IPAddress.parse(ipv4Str);
  const ipv6 = IPAddress.parse(ipv6Str);
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
    throw new RangeError(`uplinkBackboneNet: slot out of range (0-4095): ${slot}`);
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

export function segmentNet(
  segment: SegmentId | number,
  host: number,
  vlan?: number,
): NetId {
  const id = typeof segment === "number" ? segmentId(segment) : segment;
  return makeNetId(
    `192.168.${id}.${host}/24`,
    `fd00:192:168:${id}::${host}/64`,
    id,
    vlan ?? id,
  );
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

// ── MAC address derivation ────────────────────────────────────────
// Folds an IPv4 address's four octets directly into a MAC, prefixed
// with 00:00 — the convention already used by hand throughout this
// project (e.g. 192.168.128.2 -> 00:00:c0:a8:80:02, verified against a
// live deployment). Locally-administered OUI space
// (00:00:xx is not a real vendor block) is fine for this use — these
// MACs only need to be unique within OVN's logical topology, never
// routed on a real physical LAN segment.

export function macFromV4(ipv4: IPAddress): string {
  const octets = ipv4.to_s().split("/")[0].split(".").map((s) =>
    Number.parseInt(s, 10)
  );
  if (octets.length !== 4 || octets.some((o) => Number.isNaN(o))) {
    throw new Error(`macFromV4: not a valid IPv4 address: ${ipv4.to_s()}`);
  }
  const hex = octets.map((o) => o.toString(16).padStart(2, "0"));
  return `00:00:${hex.join(":")}`;
}
