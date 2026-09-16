// src/addressing.ts — the fold rules the router/kernel model needs:
// transitNetwork() (an explicit transit block -> its two endpoint
// addresses) plus the small deterministic helpers (fnv1a32, macFromV4)
// the generator folds repeated names/addresses into.
//
// Fold operation = string construction. Decimal identifiers are placed
// directly into the address text, so the result stays human-readable.
// IPAddress.parse() is called ONLY AFTER the string is fully built — it
// validates the result and returns a real IPAddress for everything
// downstream (containment checks, to_string(), comparisons). There is
// no integer arithmetic on Crunchy here: that produces hex-folded
// results (segment 128 -> "80") which breaks the human-readability
// property these rules exist for.

// Deno's npm-CJS interop doesn't type this import as callable directly
// (its .d.ts's `export default` doesn't resolve the way a real ESM
// default would) — confirmed live, 2026-08-12: `import hash from
// "fnv1a"` type-checks as the whole module namespace, not the function
// itself. The runtime value IS the callable function either way (CJS
// `module.exports = hash`), so a cast through `unknown` is honest here,
// not a workaround for a real type mismatch.
import fnv1aModule from "fnv1a";
const hash = fnv1aModule as unknown as (s: string, h?: number) => number;
import type { IPv4, IPv6 } from "./ip.ts";

// ── transit network: an OVN<->kernel-netns point-to-point link ──────
// The kernelRouterEndpoint()/transit-CollisionDomain design (2026-08-12
// discussion) — the config author supplies the transit block explicitly
// (a full network prefix, e.g. IPv4.parse("10.99.0.0/30")), not a slot:
// auto-allocation is deferred, not designed away (see that discussion).
// This just folds ONE given network per family into its two
// point-to-point endpoint addresses — `.first()`/`.last()` (ip.ts), not
// `.network()`/`.broadcast()`: for IPv4 that's the two actually-usable
// host addresses of the block (10.99.0.0/30 -> .1/.2, skipping the
// network/broadcast addresses); for IPv6, which has no reserved
// broadcast, `.first()` is the network's own zero-host address and
// `.last()` its highest — both genuinely usable there. `left`/`right`
// are deliberately unlabeled beyond that (not "ovnSide"/"kernelSide") —
// which literal side is OVN vs kernel is the CALLER's concern
// (kernelRouterEndpoint() decides), not something this pure fold has
// any basis to assert.
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
  // Numeric: the address's own value (host_address, a Crunchy — prefix
  // independent), rendered as the 32-bit hex that folds straight into
  // the four MAC bytes. No string parsing, no per-octet loop.
  const hex = ipv4.host_address.toString(16).padStart(8, "0");
  return `00:00:${hex.slice(0, 2)}:${hex.slice(2, 4)}:${hex.slice(4, 6)}:${
    hex.slice(6, 8)
  }`;
}

// ── deterministic short-name hash ───────────────────────────────────
// One stable 32-bit hash of a name, folded into generated interface/MAC
// names (shortIfaceName, vethShortId, app names — src/ir.ts) so the
// same input always yields the same name across runs and hosts.
export function fnv1a32(s: string): number {
  return hash(s);
}
