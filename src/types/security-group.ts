import type { IPv4, IPv6 } from "../ip.ts";
import type { Addr, Selector } from "../ipnet.ts";

// ── security groups: not designed yet ───────────────────────────────
// A dummy placeholder, not a real mechanism — see ADR 0002's "Firewall
// / security-group subschema" (net.securitygroup/net.sgattachment) for
// the intended real shape. Exists so RouterEndpoint (below) has
// somewhere to hold "this will need a security group eventually"
// without pretending the real mechanism exists yet, and without
// silently omitting the field and forgetting the gap is there. Also:
// as of OVN 26.03.0, whether ACLs even attach to a router port
// directly (vs. only a Logical_Switch/Port_Group) hasn't been verified
// against the live cluster (ADR 0003) — this may end up belonging on
// CollisionDomain instead of RouterEndpoint once that's checked.
export interface SecurityGroupRef {
  readonly name: string;
}

export type SecurityGroupRender = "kernel" | "ovn";

/** Kernel netns rule: MASQUERADE egress out the attached interface. */
export interface KernelMasqRule {
  readonly family: "ipv4" | "ipv6";
  readonly kind: "masq";
}

/** OVN logical-router rule: SNAT to an external address. Rendered by
 * TypeScript; the IR receives only this concrete form, never a
 * placeholder. */
export interface OvnSnatRule {
  readonly family: "ipv4" | "ipv6";
  readonly kind: "snat";
  readonly externalIp: Addr;
  readonly logicalIps: readonly (IPv4 | IPv6)[];
  /** The endpoint it goes out of, when that one is a gateway port: a router
   * with several of them must be told which (`--gateway-port`). */
  readonly gatewayPort?: string;
}

export type FilterDirection = "input" | "output" | "forward";
export type FilterAction = "allow" | "deny" | "reject";
export type Address = IPv4 | IPv6;

/** A generic L3/L4 filter rule. Family is NOT authored: it is inferred
 * from the union of `sources`/`destinations`, then expanded into one
 * concrete per-family IR rule. */
export interface FilterRule {
  readonly kind: "filter";
  readonly direction: FilterDirection;
  readonly action: FilterAction;
  /** Written prefixes, nets, or addresses of the fabric (ADR 0011 §3b). */
  readonly sources?: readonly Selector[];
  /** Sources explicitly NOT matched (OVN: `ip4.src != {...}`). */
  readonly excludeSources?: readonly Selector[];
  readonly destinations?: readonly Selector[];
  readonly protocol?: "tcp" | "udp" | "icmp" | "icmpv6";
  readonly ports?: readonly { readonly from: number; readonly to: number }[];
  readonly priority?: number;
}

export interface DenyInputAllow {
  readonly protocol?: "tcp" | "udp";
  readonly ports?: readonly { readonly from: number; readonly to: number }[];
  readonly sources?: readonly Selector[];
}

export type SecurityGroupRule<R extends SecurityGroupRender> = R extends
  "kernel" ? KernelMasqRule | FilterRule : OvnSnatRule | FilterRule;

/** A rendered group. Its `rules` are concrete and backend-specific;
 * the IR only ever sees this. */
export interface SecurityGroup<
  R extends SecurityGroupRender = SecurityGroupRender,
> {
  readonly name: string;
  readonly render: R;
  readonly rules: readonly SecurityGroupRule<R>[];
}

/** What a consumer carries: backend + name + the rendered rules to emit
 * into the `security.group` node. Only {render,name} reaches the IR. */
export interface SecurityGroupRef<
  R extends SecurityGroupRender = SecurityGroupRender,
> {
  readonly render: R;
  readonly name: string;
  readonly rules: readonly SecurityGroupRule<R>[];
}

/** A reusable, typed template. `P` is the reference's placeholder
 * environment: `ref(values)` requires every field at compile time and
 * the callback receives them as real IPv4/IPv6 instances. Nothing about
 * placeholders survives into the IR. */
export interface SecurityGroupTemplate<
  P extends object,
  R extends SecurityGroupRender,
> {
  readonly name: string;
  readonly render: R;
  ref(values: P): SecurityGroupRef<R>;
}
