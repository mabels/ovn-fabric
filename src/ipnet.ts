// src/ipnet.ts — nets and logical addresses (ADR 0011).
//
// A net (`net.ipNet(…)`) is configuration only: the prefixes of a collision
// domain plus the strategy its allocator uses. A LogicalIP is one name's
// address on it — pinned with `host`, or (later) allocated by the runtime.
//
// The generator passes Addr values around (one family of a LogicalIP); the
// IR gets references (src/ir.ts) — numbers only for pinned addresses.

import { IPAddress } from "jsr:@adviser/ipaddress@0.2.12";
import { IPv4, IPv6 } from "./ip.ts";

export type Family = "ipv4" | "ipv6";
export type AllocatorStrategy = "low-high" | "high-low" | "random";
export type Allocator =
  | AllocatorStrategy
  | { readonly ipv4?: AllocatorStrategy; readonly ipv6?: AllocatorStrategy };

export type HostRef = number | "first" | "last";
export type Host =
  | HostRef
  | { readonly ipv4: HostRef }
  | { readonly ipv6: HostRef }
  | { readonly ipv4: HostRef; readonly ipv6: HostRef };

export interface IpNetSpec {
  readonly ipv4?: string;
  readonly ipv6?: string;
  readonly allocator?: Allocator;
}

/** A prefix as the DSL takes it: a net, or (still accepted) a parsed
 * address with its prefix length. */
export type Prefix = IPv4 | IPv6 | IpNet;

/** The one family `p` has — a route destination is a single prefix. */
export function prefixOf(p: Prefix): IPv4 | IPv6 {
  if (!(p instanceof IpNet)) return p;
  const [only, ...more] = p.prefixes();
  if (only === undefined || more.length > 0) {
    throw new Error(
      `prefix ${p.toString()}: expected exactly one family, got ${
        p.families().join(", ") || "none"
      }`,
    );
  }
  return only;
}

/** The prefixes and allocator of a collision domain. Compared by value
 * (`id`), never by object. */
export class IpNet {
  readonly ipv4?: IPv4;
  readonly ipv6?: IPv6;
  readonly allocator: {
    readonly ipv4: AllocatorStrategy;
    readonly ipv6: AllocatorStrategy;
  };

  constructor(spec: string | IpNetSpec) {
    const s: IpNetSpec = typeof spec === "string" ? fromString(spec) : spec;
    if (s.ipv4 === undefined && s.ipv6 === undefined) {
      throw new Error("net.ipNet(): needs an ipv4 or an ipv6 prefix");
    }
    // The network, not the written address: "10.12.80.1/28" and
    // "10.12.80.0/28" are the same net.
    if (s.ipv4 !== undefined) this.ipv4 = IPv4.parse(s.ipv4).network();
    if (s.ipv6 !== undefined) this.ipv6 = IPv6.parse(s.ipv6).network();
    checkHostBits(this.ipv4, this.ipv6, "net.ipNet()");
    const a = s.allocator ?? "low-high";
    this.allocator = typeof a === "string"
      ? { ipv4: a, ipv6: a }
      : { ipv4: a.ipv4 ?? "low-high", ipv6: a.ipv6 ?? "low-high" };
  }

  families(): Family[] {
    return [
      ...(this.ipv4 ? ["ipv4" as const] : []),
      ...(this.ipv6 ? ["ipv6" as const] : []),
    ];
  }

  prefixes(): (IPv4 | IPv6)[] {
    return [this.ipv4, this.ipv6].filter((p): p is IPv4 | IPv6 =>
      p !== undefined
    );
  }

  /** `ipnet:<v4>|<v6>|<allocator v4>,<allocator v6>` — the value the net is
   * compared by. */
  id(): string {
    return `ipnet:${this.ipv4?.to_string() ?? ""}|${
      this.ipv6?.to_string() ?? ""
    }|${this.allocator.ipv4},${this.allocator.ipv6}`;
  }

  toString(): string {
    return this.prefixes().map((p) => p.to_string()).join(" ");
  }

  /** The pinned address `host` on this net. */
  host(host: Host): LogicalIP {
    return new LogicalIP(this, host);
  }

  /** An address on this net the runtime allocates (no `host`). */
  allocated(): LogicalIP {
    return new LogicalIP(this);
  }
}

/** One host number for both families (ADR 0011 §5) needs the IPv6 prefix to
 * have at least as many host bits as the IPv4 one (a /24 needs a /120 or
 * larger) — else a number valid in IPv4 has no IPv6 partner. */
export function checkHostBits(
  ipv4: IPv4 | undefined,
  ipv6: IPv6 | undefined,
  where: string,
): void {
  if (ipv4 === undefined || ipv6 === undefined) return;
  const v4 = 32 - ipv4.prefix.num;
  const v6 = 128 - ipv6.prefix.num;
  if (v6 < v4) {
    throw new Error(
      `${where}: ${ipv6.to_string()} has fewer host bits (${v6}) than ` +
        `${ipv4.to_string()} (${v4}) — one host number for both families ` +
        "needs at least as many in IPv6",
    );
  }
}

function fromString(s: string): IpNetSpec {
  const addr = IPAddress.parse(s);
  if (!addr) throw new Error(`net.ipNet("${s}"): not a prefix`);
  return addr.is_ipv4() ? { ipv4: s } : { ipv6: s };
}

/** One name's address on a net, in every family it has: pinned with
 * `host`, or — without — allocated by the runtime (ADR 0011). TypeScript
 * never knows an allocated number; it knows the net, so prefix and family. */
export class LogicalIP {
  readonly #addrs = new Map<Family, Addr>();

  constructor(readonly net: IpNet, readonly host?: Host) {
    // A pinned number is checked where it is written.
    for (const f of this.families()) {
      const addr = new Addr(this, f);
      addr.pinned();
      this.#addrs.set(f, addr);
    }
  }

  /** The families this address has: every family of the net for a plain
   * or absent host, exactly the listed ones for an object. */
  families(): Family[] {
    if (typeof this.host === "object") {
      return (["ipv4", "ipv6"] as const).filter((f) =>
        f in (this.host as object)
      );
    }
    return this.net.families();
  }

  /** Its addresses, one per family — an endpoint's `ipaddrs`. */
  ipaddrs(): Addr[] {
    return this.families().map((f) => this.as(f));
  }

  /** Its IPv4 alone — a next hop. */
  asIpv4(): Addr {
    return this.as("ipv4");
  }

  /** Its IPv6 alone — a next hop. */
  asIpv6(): Addr {
    return this.as("ipv6");
  }

  as(family: Family): Addr {
    const a = this.#addrs.get(family);
    if (a === undefined) {
      throw new Error(`${this.net.toString()}: this address has no ${family}`);
    }
    return a;
  }

  /** The host of one family, undefined when the runtime allocates it. */
  hostRef(family: Family): HostRef | undefined {
    if (this.host === undefined || typeof this.host !== "object") {
      return this.host;
    }
    return (this.host as { ipv4?: HostRef; ipv6?: HostRef })[family];
  }
}

/** One family of a LogicalIP — what the generator passes around where it
 * used an IPv4/IPv6 value. It knows its net, so `network()` and `is_ipv4()`
 * work for an allocated address too; the number exists only when pinned. */
export class Addr {
  constructor(readonly name: LogicalIP, readonly family: Family) {}

  /** A written address (`IPv4.parse("192.168.128.1/24")`): pinned, on the
   * net its prefix length gives. */
  static from(ip: IPv4 | IPv6): Addr {
    const net = new IpNet(
      ip.is_ipv4() ? { ipv4: ip.to_string() } : { ipv6: ip.to_string() },
    );
    const n = Number(ip.host_number());
    return new LogicalIP(net, n).as(ip.is_ipv4() ? "ipv4" : "ipv6");
  }

  get net(): IpNet {
    return this.name.net;
  }

  get host(): HostRef | undefined {
    return this.name.hostRef(this.family);
  }

  is_ipv4(): boolean {
    return this.family === "ipv4";
  }

  /** The net's prefix in this family — known whether or not the number is. */
  network(): IPv4 | IPv6 {
    const p = this.net[this.family];
    if (p === undefined) {
      throw new Error(`${this.net.toString()}: has no ${this.family}`);
    }
    return p;
  }

  /** The concrete address, with its prefix — only for a pinned one. */
  pinned(): IPv4 | IPv6 | undefined {
    const ref = this.host;
    if (ref === undefined) return undefined;
    const prefix = this.network();
    const out = ref === "first"
      ? prefix.first()
      : ref === "last"
      ? prefix.last()
      : hostNumber(prefix, ref);
    return this.family === "ipv4" ? IPv4.from(out) : IPv6.from(out);
  }

  /** For messages and the graph: the number when pinned, else the net. */
  toString(): string {
    return this.pinned()?.to_string() ?? `${this.network().to_string()}#auto`;
  }
}

/** One net from the families of `addrs` (a leg's addresses → the net its
 * collision domain is addressed with). Two addresses of one family on
 * different prefixes are an error. */
export function netOf(addrs: readonly Addr[], who: string): IpNet {
  const spec: { ipv4?: string; ipv6?: string } = {};
  for (const a of addrs) {
    const p = a.network().to_string();
    const prev = spec[a.family];
    if (prev !== undefined && prev !== p) {
      throw new Error(`${who}: two ${a.family} prefixes, ${prev} and ${p}`);
    }
    spec[a.family] = p;
  }
  return new IpNet(spec);
}

/** What a filter rule matches (ADR 0011 §3b): a written prefix, a net (its
 * prefixes — configuration, exact), or an address of the fabric (`host(n)`,
 * an allocated leg — resolved by the runtime). */
export type Selector = IPv4 | IPv6 | IpNet | LogicalIP | Addr;

/** Selectors flattened to one family each: a net into its prefixes, a
 * LogicalIP into its addresses. */
export function selectors(
  list: readonly Selector[] | undefined,
): (IPv4 | IPv6 | Addr)[] {
  return (list ?? []).flatMap((s) =>
    s instanceof IpNet
      ? s.prefixes()
      : s instanceof LogicalIP
      ? s.ipaddrs()
      : [s]
  );
}

/** An address as the DSL takes it: a LogicalIP's Addr, or a written one. */
export type AddrLike = Addr | IPv4 | IPv6;

export function toAddr(a: AddrLike): Addr {
  return a instanceof Addr ? a : Addr.from(a);
}

export function toAddrs(list: readonly AddrLike[] | undefined): Addr[] {
  return (list ?? []).map(toAddr);
}

/** The network base of `prefix` plus host number `n` (numeric: host 10 is
 * `.10` and `::a`), keeping the prefix length. Host 0 and anything past the
 * net are errors. */
function hostNumber(prefix: IPv4 | IPv6, n: number): IPAddress {
  // Host 0 is the network itself — except in a single-address net (a next
  // hop written without a prefix, `/32` / `/128`), where it is the address.
  const single = prefix.prefix.num === (prefix.is_ipv4() ? 32 : 128);
  if (!Number.isInteger(n) || n < 0 || (n === 0 && !single)) {
    throw new Error(`${prefix.to_string()}: host ${n} is not a host number`);
  }
  const addr = prefix.host(n);
  if (addr === null) {
    throw new Error(`${prefix.to_string()}: host ${n} is outside the net`);
  }
  return addr;
}
