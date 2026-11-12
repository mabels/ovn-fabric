// src/addressing.ts — the fold rules the router/kernel model needs:
// transitNetwork() (an explicit transit block -> its two endpoint
// addresses) plus the small deterministic helpers (fnv1a32, macFromName)
// the generator folds repeated names into.
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
import fnv1aIdentity from "@sindresorhus/fnv1a";
const hash = fnv1aModule as unknown as (s: string, h?: number) => number;
import type { IPv4, IPv6 } from "./ip.ts";
import { type Addr, IpNet, type LogicalIP } from "./ipnet.ts";

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
  readonly ipv4?: Addr;
  readonly ipv6?: Addr;
}

export interface TransitNetwork {
  readonly left: TransitEndpoint;
  readonly right: TransitEndpoint;
}

/** A transit's two ends: the first and the last address of each family
 * (`"first"` / `"last"`, ADR 0011) — of a net
 * (`transitNetwork(net.ipNet({ ipv4, ipv6 }))`), or of the two parsed
 * prefixes. */
export function transitNetwork(
  net?: IPv4 | IpNet,
  ipv6Prefix?: IPv6,
): TransitNetwork {
  if (net === undefined && ipv6Prefix === undefined) {
    return { left: {}, right: {} };
  }
  if (!(net instanceof IpNet)) {
    net = new IpNet({
      ...(net ? { ipv4: net.to_string() } : {}),
      ...(ipv6Prefix ? { ipv6: ipv6Prefix.to_string() } : {}),
    });
  } else if (ipv6Prefix !== undefined) {
    throw new Error("transitNetwork(net): the net carries its own ipv6");
  }
  const ends = (l: LogicalIP): TransitEndpoint => ({
    ipv4: net.ipv4 ? l.asIpv4() : undefined,
    ipv6: net.ipv6 ? l.asIpv6() : undefined,
  });
  return { left: ends(net.host("first")), right: ends(net.host("last")) };
}

// ── deterministic short-name hash ───────────────────────────────────
// One stable 32-bit hash of a name, folded into generated interface/MAC
// names (shortIfaceName, vethShortId, app names — src/ir.ts) so the
// same input always yields the same name across runs and hosts.
export function fnv1a32(s: string): number {
  return hash(s);
}

// ── deterministic identity hash (64-bit FNV-1a) ─────────────────────
// fnv1a32 above stays the right tool for COSMETIC short names
// (bridge/veth/container), where a collision is visible and merely
// renames an interface. This one is for facts that are a MERGE KEY — an
// endpoint's identity (deriveEndpointName, router-endpoints.ts) — where a
// 32-bit birthday bound (~65k inputs) is too weak: a collision there
// silently aliases two distinct ports.
//
// Delegated to @sindresorhus/fnv1a rather than hand-rolled: pure ESM,
// zero dependencies, no `node:` builtins (its only global is
// `globalThis.TextEncoder`), and — the point — the bit size is an
// option, so widening the identity (128/256) later is a one-line change
// here, not a new algorithm. Synchronous, so the topology-facing API
// (defineNetwork/defineOvnRouter) never has to `await` just to derive an
// id. Returns BigInt, hence the hex conversion below. UTF-8 bytes, so
// the digest is reproducible from the same string.
const FNV1A_IDENTITY_SIZE = 64;

/** Fixed-width (16 lowercase hex chars at size 64) so an id's own length
 * never depends on its input — see deriveEndpointName
 * (router-endpoints.ts). */
export function fnv1a64(s: string): string {
  return fnv1aIdentity(s, { size: FNV1A_IDENTITY_SIZE })
    .toString(16)
    .padStart(FNV1A_IDENTITY_SIZE / 4, "0");
}

// ── a MAC from a name ───────────────────────────────────────────────
// A locally-administered MAC (02:…) hashed from a stable, config-derived
// name — for a port that has no IPv4 to fold one from (a DHCP wan's cable)
// and must not get a random one. An address-derived MAC is the runtime's
// (ADR 0011 §5): it folds the resolved IPv4.
export function macFromName(name: string): string {
  const hex = fnv1a64(name).slice(0, 10);
  return `02:${hex.slice(0, 2)}:${hex.slice(2, 4)}:${hex.slice(4, 6)}:${
    hex.slice(6, 8)
  }:${hex.slice(8, 10)}`;
}
