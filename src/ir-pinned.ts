// src/ir-pinned.ts — the IR's pinned addresses as text, for tests and
// debugging (ADR 0011). Not a resolver: the runtime resolves (Python, its
// address metadata). This only substitutes what the IR already fixes — a
// pinned `host` on its net — and writes an allocated address as
// `auto:<owner>`, so a test can still read "which port is the next hop".
//
//   LogicalIP list (addresses, ipaddrs)  → ["192.168.128.1/24", "…::1/64"]
//   one LogicalIP (a docker link end)     → "10.200.7.2/24"
//   AddrRef (nexthop, via, externalIp,
//            a nameserver)                → "172.22.0.3" (bare)

import { IPAddress } from "jsr:@adviser/ipaddress@0.2.12";
import type { AddrRefValue, IRNode, LogicalIPValue } from "./protocol.ts";

type Family = "ipv4" | "ipv6";
type HostRef = number | "first" | "last";
type Json = unknown;

/** `T` with every address as text: a LogicalIP list becomes its address
 * strings, one LogicalIP or an AddrRef one string. */
export type Pinned<T> = T extends AddrRefValue | LogicalIPValue ? string
  : T extends readonly (infer I)[]
    ? readonly (I extends AddrRefValue | LogicalIPValue ? string : Pinned<I>)[]
  : T extends object ? { readonly [K in keyof T]: Pinned<T[K]> }
  : T;

export type PinnedNode = Pinned<Exclude<IRNode, { kind: "ip.net" }>>;

interface LogicalIP {
  readonly net: string;
  readonly owner: string;
  readonly host?: HostRef | { ipv4?: HostRef; ipv6?: HostRef };
  readonly families?: readonly Family[];
}

interface AddrRef {
  readonly net: string;
  readonly owner: string;
  readonly family: Family;
  readonly host?: HostRef;
}

const isRef = (v: Json): v is AddrRef =>
  typeof v === "object" && v !== null && "net" in v && "owner" in v &&
  "family" in v;
const isLogical = (v: Json): v is LogicalIP =>
  typeof v === "object" && v !== null && "net" in v && "owner" in v &&
  !("family" in v);

export function resolvePinned(
  ir: Readonly<Record<string, IRNode>> | readonly IRNode[],
): Record<string, PinnedNode> {
  const nodes = Array.isArray(ir) ? ir : Object.values(ir);
  const nets = new Map<string, Partial<Record<Family, string>>>();
  const owners = new Map<string, LogicalIP>();
  for (const n of nodes) {
    if (n.kind === "ip.net") nets.set(n.id, n.data);
  }
  const collect = (v: Json): void => {
    if (Array.isArray(v)) return v.forEach(collect);
    if (typeof v !== "object" || v === null) return;
    if (isLogical(v)) owners.set(`${v.net}|${v.owner}`, v);
    for (const x of Object.values(v)) collect(x);
  };
  for (const n of nodes) collect(n.data);

  const families = (ip: LogicalIP): Family[] => {
    if (ip.families) return [...ip.families];
    if (typeof ip.host === "object") {
      return (["ipv4", "ipv6"] as const).filter((f) =>
        (ip.host as Record<string, unknown>)[f] !== undefined
      );
    }
    const net = nets.get(ip.net) ?? {};
    return (["ipv4", "ipv6"] as const).filter((f) => net[f] !== undefined);
  };
  const hostOf = (ip: LogicalIP, f: Family): HostRef | undefined =>
    typeof ip.host === "object" ? ip.host[f] : ip.host;
  const address = (
    netId: string,
    f: Family,
    host: HostRef | undefined,
    owner: string,
  ): IPAddress | string => {
    if (host === undefined) return `auto:${owner}`;
    const prefix = nets.get(netId)?.[f];
    if (prefix === undefined) throw new Error(`${netId}: no ${f}`);
    const p = IPAddress.parse(prefix)!;
    const a = host === "first"
      ? p.first()
      : host === "last"
      ? p.last()
      : p.host(host);
    if (!a) throw new Error(`${netId}: host ${host}`);
    return a;
  };
  const withPrefix = (a: IPAddress | string) =>
    typeof a === "string" ? a : a.to_string();
  const bare = (a: IPAddress | string) => typeof a === "string" ? a : a.to_s();

  const list = (ip: LogicalIP): string[] =>
    families(ip).map((f) =>
      withPrefix(address(ip.net, f, hostOf(ip, f), ip.owner))
    );
  const ref = (r: AddrRef): string => {
    if (r.host !== undefined) {
      return bare(address(r.net, r.family, r.host, r.owner));
    }
    const ip = owners.get(`${r.net}|${r.owner}`);
    if (!ip) throw new Error(`${r.owner}: no such LogicalIP on ${r.net}`);
    return bare(address(r.net, r.family, hostOf(ip, r.family), r.owner));
  };
  const walk = (v: Json): Json => {
    if (Array.isArray(v)) {
      if (v.length > 0 && v.every(isLogical)) return v.flatMap(list);
      return v.map(walk);
    }
    if (isRef(v)) return ref(v);
    if (isLogical(v)) {
      const [one, ...more] = list(v);
      if (more.length > 0) throw new Error(`${v.owner}: expected one address`);
      return one;
    }
    if (typeof v !== "object" || v === null) return v;
    return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, walk(x)]));
  };
  return Object.fromEntries(
    nodes.filter((n) => n.kind !== "ip.net").map((n) => [
      n.id,
      { ...n, data: walk(n.data) } as PinnedNode,
    ]),
  );
}
