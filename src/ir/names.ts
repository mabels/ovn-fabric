// src/ir/names.ts — Shared ids and IFNAMSIZ-safe short interface/bridge names.

import { fnv1a32 } from "../addressing.ts";
import type { CollisionDomain, Router } from "../types.ts";

// Addresses go into the IR as references (ADR 0011, src/ir-addresses.ts):
// a carrier's own as LogicalIPs on its collision domain, everyone else's
// as an AddrRef to the carrier. The collision domain of an OVN leg is its
// logical switch; the legs of a router are where its next hops can be.
export function lsId(domain: CollisionDomain): string {
  return `ls:${domain.name}`;
}

export function routerLegs(router: Router): string[] {
  return router.endpoints.map((e) => lsId(e.l2Segment));
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
export function vethShortId(ifaceName: string): string {
  const name = ifaceName.replace(/^veth-krn-/, "");
  return ifaceName.length <= IFNAMSIZ_MAX
    ? name
    : fnv1a32(name).toString(16).padStart(8, "0").slice(0, 6);
}

// Readable (`${prefix}${name}`) whenever it fits; only falls back to a
// short deterministic hash (fnv1a32, addressing.ts) when it doesn't.
// Resolved HERE, not in fabric/emit.py (the translator) — same
// boundary reasoning as mac/gatewayChassis/routes throughout this file:
// the generator computes the FINAL name a real kernel object needs, the
// translator only ever applies an already-resolved fact. Moved here
// 2026-08-12 — this used to live translator-side as fabric/
// emit.py's own `_bridge_name`/`_fnv1a_32`.
export function shortIfaceName(prefix: string, name: string): string {
  const candidate = `${prefix}${name}`;
  if (candidate.length <= IFNAMSIZ_MAX) return candidate;
  return `${prefix}${fnv1a32(name).toString(16).padStart(8, "0")}`;
}

// A host-local bridge for a VLAN shared by several kernel routers: Linux
// allows only one 8021q device per (parent, VLAN id), even across netns.
export function transitBridgeName(parent: string, vlanId: number): string {
  return shortIfaceName("br-upl-", `${parent}:${vlanId}`);
}
