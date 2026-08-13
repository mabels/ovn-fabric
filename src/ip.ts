// src/ip.ts — IPv4/IPv6: family-branded wrappers around IPAddress.
//
// IPAddress.parse() accepts EITHER family for any syntactically valid
// string — nothing in its own type stops a v6 literal from reaching a
// v4-only field (or vice versa); the mismatch would only surface much
// later, when the generated shell script runs `ip addr add`/`ip route
// add` against a real interface. Since config/topology.ts is itself
// TypeScript, it can call IPv4.parse()/IPv6.parse() directly at
// declaration time instead of handing this library a bare string —
// that gets a config author both a compile-time family check (an IPv6
// literal doesn't structurally satisfy an `IPv4`-typed field) and an
// immediate runtime one (parse() throws right there if the string
// itself is the wrong family), rather than a mismatch discovered on a
// live host.
//
// EXTENDS IPAddress rather than wraps it — to_s()/to_string()/
// includes()/etc. all come along for free, no per-method delegation to
// maintain here. The one place this leaks: IPAddress's own instance
// methods that hand back a new address (network(), broadcast(),
// first(), last(), ...) hardcode `new IPAddress(...)` internally, not
// `new this.constructor(...)` — so called on an IPv4, they still
// return a plain base-class IPAddress, not an IPv4. network()/first()/
// last() are the ones this codebase actually calls through the wrapper
// (first()/last() added for addressing.ts's transitNetwork() — a
// transit link's two endpoint addresses, folded from the network's own
// first/last usable address), so those are the ones overridden below
// to re-assert the family; add another override here if a caller ever
// needs broadcast() too.
//
// The private constructor is what makes IPv4/IPv6 nominal types
// (TypeScript would otherwise accept ANY object with the right shape,
// including a bare IPv6 — defeating the entire point) — the only way
// to obtain an instance is parse()/from(), both of which check
// is_ipv4()/is_ipv6() and throw on a family mismatch.
//
// Deliberately no Host-vs-Network role split (e.g. "must have an
// explicit prefix") on top of this — a bare address defaulting to
// /32-/128 is perfectly valid for a gateway/interface address, and the
// one place a missing prefix is a real bug (a route prefix silently
// narrowing to a single host) is better solved once real block-based
// address ALLOCATION exists (segments/uplinks carved out of a shared
// supernet automatically) rather than layered onto this wrapper now.

import { IPAddress } from "npm:ipaddress@0.2.6";

export class IPv4 extends IPAddress {
  private constructor(source: IPAddress) {
    super(source);
  }

  static override parse(input: string): IPv4 {
    const addr = IPAddress.parse(input);
    if (!addr.is_ipv4()) {
      throw new Error(`IPv4.parse: "${input}" is not a valid IPv4 address`);
    }
    return new IPv4(addr);
  }

  /** Wrap an IPAddress already known to be IPv4 (e.g. derived via
   * `.network()` from another IPv4) without re-parsing/re-validating a
   * string — still checks the family, just skips the text round-trip. */
  static from(addr: IPAddress): IPv4 {
    if (!addr.is_ipv4()) {
      throw new Error(
        `IPv4.from: "${addr.to_s()}" is not a valid IPv4 address`,
      );
    }
    return new IPv4(addr);
  }

  override network(): IPv4 {
    return IPv4.from(super.network());
  }

  override first(): IPv4 {
    return IPv4.from(super.first());
  }

  override last(): IPv4 {
    return IPv4.from(super.last());
  }
}

export class IPv6 extends IPAddress {
  private constructor(source: IPAddress) {
    super(source);
  }

  static override parse(input: string): IPv6 {
    const addr = IPAddress.parse(input);
    if (!addr.is_ipv6()) {
      throw new Error(`IPv6.parse: "${input}" is not a valid IPv6 address`);
    }
    return new IPv6(addr);
  }

  /** Wrap an IPAddress already known to be IPv6 — see IPv4.from(). */
  static from(addr: IPAddress): IPv6 {
    if (!addr.is_ipv6()) {
      throw new Error(
        `IPv6.from: "${addr.to_s()}" is not a valid IPv6 address`,
      );
    }
    return new IPv6(addr);
  }

  override network(): IPv6 {
    return IPv6.from(super.network());
  }

  override first(): IPv6 {
    return IPv6.from(super.first());
  }

  override last(): IPv6 {
    return IPv6.from(super.last());
  }
}
