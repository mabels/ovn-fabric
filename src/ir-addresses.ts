// src/ir-addresses.ts — addresses into the IR as references (ADR 0011).
//
// The generator passes Addr values (src/ipnet.ts) around; toIR() turns them
// into the IR's logical form through one AddressIndex:
//
//   own(addrs, domain, owner)  a carrier (router port, kernel side, NIC, a
//                              docker link) — its LogicalIPs, and the
//                              collision domain's net from their prefixes
//   ref(addr, line, domains)   a next hop / SNAT address — an AddrRef to
//                              whoever carries `addr`
//   nameserver(addr, line)     an AddrRef, or the address as written when
//                              no collision domain of the fabric holds it
//   finish()                   fills every reference (carriers may be
//                              emitted after the line that refers to them)
//                              and returns one `ip.net` node per domain
//
// A reference finds its owner by the Addr itself (the same object the
// carrier holds), else — for a pinned one — by its number on the domain.
// A pinned address no carrier holds (an upstream router's `.1`) is owned by
// the line that writes it and keeps its `host`. An allocated address nobody
// carries is an error: nothing could ever hold its number.

import type { IPv4, IPv6 } from "./ip.ts";
import {
  type Addr,
  type AllocatorStrategy,
  checkHostBits,
  type Family,
  type HostRef,
} from "./ipnet.ts";
import type { AddrRefValue, IpNetNode, LogicalIPValue } from "./protocol.ts";

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

interface NetFamily {
  readonly prefix: IPv4 | IPv6;
  readonly allocator: AllocatorStrategy;
}

interface Carrier {
  readonly owner: string;
  readonly domain: string;
}

interface PendingRef {
  readonly addr: Addr;
  readonly line: string;
  readonly domains: readonly string[];
  readonly fill: (value: AddrRefValue | string) => void;
  /** A nameserver may be "not our world"; a next hop may not. */
  readonly foreignAsText: boolean;
}

export function ipNetId(domain: string): string {
  return `ipnet:${domain}`;
}

const FAMILIES: readonly Family[] = ["ipv4", "ipv6"];

export class AddressIndex {
  readonly #nets = new Map<
    string,
    Map<Family, NetFamily>
  >();
  readonly #carriers = new Map<Addr, Carrier>();
  /** `<domain>|<address>` of every pinned carried address → its carrier. */
  readonly #pinned = new Map<string, Carrier>();
  /** `<address>` → carriers on any domain (nameservers). */
  readonly #byValue = new Map<string, Carrier[]>();
  readonly #pending: PendingRef[] = [];
  readonly #logical: { value: Mutable<LogicalIPValue>; fams: Family[] }[] = [];
  readonly #refAddr = new WeakMap<object, Addr>();

  /** A carrier's addresses as LogicalIPs: one per name (v4 and v6 of one
   * LogicalIP together), the first owned by `owner`, more by `owner#n`. */
  own(
    addrs: readonly Addr[],
    domain: string,
    owner: string,
  ): LogicalIPValue[] {
    const groups: Addr[][] = [];
    for (const a of addrs) {
      this.#addNet(domain, a, owner);
      const g = groups.find((g) => g[0]!.name === a.name);
      if (g) g.push(a);
      else groups.push([a]);
    }
    return groups.map((group, i) => {
      const o = i === 0 ? owner : `${owner}#${i + 1}`;
      for (const a of group) this.#carry(a, { owner: o, domain });
      const fams = FAMILIES.filter((f) => group.some((a) => a.family === f));
      const value: Mutable<LogicalIPValue> = {
        net: ipNetId(domain),
        owner: o,
        ...hostOf(group, fams),
      };
      this.#logical.push({ value, fams });
      return value;
    });
  }

  /** One address of a carrier, as a single LogicalIP (a docker link end). */
  ownOne(addr: Addr, domain: string, owner: string): LogicalIPValue {
    return this.own([addr], domain, owner)[0]!;
  }

  /** A reference to the carrier of `addr` — filled in by finish(). For a
   * pinned address no carrier holds, `domains` are where it may lie (the
   * collision domains of the line that writes it). */
  ref(addr: Addr, line: string, domains: readonly string[]): AddrRefValue {
    const value = {} as Mutable<AddrRefValue>;
    this.#refAddr.set(value, addr);
    this.#pending.push({
      addr,
      line,
      domains,
      foreignAsText: false,
      fill: (v) => Object.assign(value, v),
    });
    return value;
  }

  /** A nameserver: a reference, or — when no collision domain of the
   * fabric holds it — the address as written. `set` receives the value. */
  nameserver(
    addr: Addr,
    line: string,
    set: (value: AddrRefValue | string) => void,
  ): void {
    this.#pending.push({
      addr,
      line,
      domains: [],
      foreignAsText: true,
      fill: set,
    });
  }

  /** The Addr a reference was made from — for the generator's own
   * comparisons ("is this next hop my own address"). */
  addrOf(ref: AddrRefValue): Addr | undefined {
    return this.#refAddr.get(ref);
  }

  /** Fill every reference; the `ip.net` nodes, sorted by domain. */
  finish(): IpNetNode[] {
    for (const [domain, fams] of this.#nets) {
      checkHostBits(
        fams.get("ipv4")?.prefix as IPv4 | undefined,
        fams.get("ipv6")?.prefix as IPv6 | undefined,
        domain,
      );
    }
    for (const p of this.#pending) p.fill(this.#resolve(p));
    for (const { value, fams } of this.#logical) {
      const domainFams = FAMILIES.filter((f) =>
        this.#nets.get(value.net.slice("ipnet:".length))?.has(f)
      );
      if (fams.length !== domainFams.length) value.families = fams;
    }
    return [...this.#nets.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([domain, fams]) => ({
        id: ipNetId(domain),
        kind: "ip.net" as const,
        key: { domain },
        data: {
          ...(fams.has("ipv4")
            ? { ipv4: fams.get("ipv4")!.prefix.to_string() }
            : {}),
          ...(fams.has("ipv6")
            ? { ipv6: fams.get("ipv6")!.prefix.to_string() }
            : {}),
          allocator: {
            ipv4: fams.get("ipv4")?.allocator ?? "low-high",
            ipv6: fams.get("ipv6")?.allocator ?? "low-high",
          },
        },
      }));
  }

  #resolve(p: PendingRef): AddrRefValue | string {
    const a = p.addr;
    const carried = this.#carriers.get(a);
    if (carried) return refTo(carried, a.family);
    const value = a.pinned()?.to_s();
    if (value === undefined) {
      throw new Error(
        `${p.line}: refers to an allocated address on ` +
          `${a.network().to_string()} that no port or NIC carries`,
      );
    }
    for (const d of p.domains) {
      const c = this.#pinned.get(`${d}|${value}`);
      if (c) return refTo(c, a.family);
    }
    if (p.foreignAsText) {
      const hits = this.#byValue.get(value) ?? [];
      if (hits.length === 1) return refTo(hits[0]!, a.family);
      if (hits.length > 1) {
        throw new Error(
          `${p.line}: ${value} is carried on several collision domains (${
            hits.map((h) => h.domain).join(", ")
          }) — name which one`,
        );
      }
      return value; // not our world
    }
    // Pinned, carried by nothing of the fabric: the line owns it.
    const pinned = a.pinned()!;
    const domain = p.domains.find((d) =>
      this.#nets.get(d)?.get(a.family)?.prefix.includes(pinned)
    );
    if (domain === undefined) {
      throw new Error(
        `${p.line}: ${value} lies on none of its collision domains (${
          p.domains.join(", ") || "none"
        })`,
      );
    }
    return {
      net: ipNetId(domain),
      owner: p.line,
      family: a.family,
      host: hostNumberIn(pinned, this.#nets.get(domain)!.get(a.family)!.prefix),
    };
  }

  #carry(a: Addr, c: Carrier): void {
    this.#carriers.set(a, c);
    const value = a.pinned()?.to_s();
    if (value === undefined) return;
    const key = `${c.domain}|${value}`;
    const prev = this.#pinned.get(key);
    if (prev !== undefined && prev.owner !== c.owner) {
      throw new Error(
        `${c.domain}: ${value} is pinned by both ${prev.owner} and ${c.owner}`,
      );
    }
    this.#pinned.set(key, c);
    const list = this.#byValue.get(value) ?? [];
    list.push(c);
    this.#byValue.set(value, list);
  }

  #addNet(domain: string, a: Addr, who: string): void {
    const fams = this.#nets.get(domain) ?? new Map<Family, NetFamily>();
    const prefix = a.network();
    const allocator = a.net.allocator[a.family];
    const prev = fams.get(a.family);
    if (
      prev !== undefined &&
      (prev.prefix.to_string() !== prefix.to_string() ||
        prev.allocator !== allocator)
    ) {
      throw new Error(
        `${domain}: addressed with two ${a.family} nets — ` +
          `${prev.prefix.to_string()} (${prev.allocator}) and ` +
          `${prefix.to_string()} (${allocator}) (${who})`,
      );
    }
    fams.set(a.family, { prefix, allocator });
    this.#nets.set(domain, fams);
  }
}

function refTo(c: Carrier, family: Family): AddrRefValue {
  return { net: ipNetId(c.domain), owner: c.owner, family };
}

/** `host` of a LogicalIP: absent when allocated, one value when every
 * family has the same, else one per family. */
function hostOf(
  group: readonly Addr[],
  fams: readonly Family[],
): { host?: LogicalIPValue["host"] } {
  const refs = new Map<Family, HostRef | undefined>(
    group.map((a) => [a.family, a.host]),
  );
  const values = fams.map((f) => refs.get(f));
  if (values.every((v) => v === undefined)) return {};
  if (values.some((v) => v === undefined)) {
    throw new Error(
      `${group[0]!.network().to_string()}: an address pinned in one family ` +
        `and allocated in the other`,
    );
  }
  if (values.every((v) => v === values[0])) return { host: values[0]! };
  return {
    host: Object.fromEntries(fams.map((f) => [f, refs.get(f)!])) as {
      ipv4?: HostRef;
      ipv6?: HostRef;
    },
  };
}

/** The host number of `addr` within `prefix` (a next hop is written
 * without a prefix length). */
function hostNumberIn(addr: IPv4 | IPv6, prefix: IPv4 | IPv6): number {
  const inNet = addr.change_prefix(prefix.prefix.num);
  if (inNet === null) {
    throw new Error(`${addr.to_s()}: not in ${prefix.to_string()}`);
  }
  return Number(inNet.host_number());
}
