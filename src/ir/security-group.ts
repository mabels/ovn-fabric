// src/ir/security-group.ts — security.group: a security group's rules, expanded
// per address family.

import type { IPv4, IPv6 } from "../ip.ts";
import { Addr, selectors } from "../ipnet.ts";
import type { AddressIndex } from "../ir-addresses.ts";
import { NetworkValue, type SecurityGroupNode } from "../protocol.ts";
import type { FilterRule, SecurityGroup } from "../types.ts";

// ── security.group ───────────────────────────────────────────────────
// The IR counterpart of a security group (SecurityGroup, types.ts) —
// deliberately IMPLEMENTATION-ABSTRACT: a named set of rules that some
// object (today a KernelRouterSide, later an ovn.lrp, a docker
// container, ...) attaches to via its own `securityGroup` reference.
// Nothing about this node is kernel- or OVN-specific: the name is just
// a label, and the rules are the FINAL resolved facts in a generic
// vocabulary (`kind: "masq"` = masquerade, `family` = which address
// family), which each implementation maps to its own concrete command
// (the deployer turns `masq` into an iptables MASQUERADE inside a
// kernel netns; an OVN backend would translate the same rule to its own
// NAT/ACL). The group is BUILT once, at declaration time
// (net.securityGroup(), define.ts — including the implicit `masq-<router>`
// shortcut expansion); this function is pure serialization, it derives
// nothing.
function expandFilterRule(
  rule: FilterRule,
  addrs: AddressIndex,
  line: string,
): Record<string, unknown>[] {
  const src = selectors(rule.sources);
  const dst = selectors(rule.destinations);
  const exclude = selectors(rule.excludeSources);
  // A written prefix or a net's prefix as text; an address of the fabric as
  // a reference to its carrier (ADR 0011 §3b).
  const out = (a: IPv4 | IPv6 | Addr, field: string) =>
    a instanceof Addr ? addrs.ref(a, `${line}|${field}`, []) : a.to_string();
  const families = new Set<"ipv4" | "ipv6">();
  for (const a of [...src, ...dst]) families.add(a.is_ipv4() ? "ipv4" : "ipv6");
  if (families.size === 0) {
    families.add("ipv4");
    families.add("ipv6");
  }
  const srcFams = new Set(src.map((a) => (a.is_ipv4() ? "ipv4" : "ipv6")));
  const dstFams = new Set(dst.map((a) => (a.is_ipv4() ? "ipv4" : "ipv6")));
  if (srcFams.size > 0 && dstFams.size > 0) {
    const shared = [...srcFams].some((f) => dstFams.has(f));
    if (!shared) {
      throw new Error(
        "filter rule: sources and destinations have no address family in common",
      );
    }
  }
  return [...families].map((family) => {
    if (rule.protocol === "icmp" && family === "ipv6") {
      throw new Error("filter rule: icmp requires IPv4 selectors");
    }
    if (rule.protocol === "icmpv6" && family === "ipv4") {
      throw new Error("filter rule: icmpv6 requires IPv6 selectors");
    }
    return {
      kind: "filter",
      direction: rule.direction,
      action: rule.action,
      family,
      ...(src.length > 0
        ? {
          sources: src.filter((a) => a.is_ipv4() === (family === "ipv4"))
            .map((a) => out(a, "sources")),
        }
        : {}),
      ...(exclude.length > 0
        ? {
          excludeSources: exclude
            .filter((a) => a.is_ipv4() === (family === "ipv4"))
            .map((a) => out(a, "excludeSources")),
        }
        : {}),
      ...(dst.length > 0
        ? {
          destinations: dst.filter((a) => a.is_ipv4() === (family === "ipv4"))
            .map((a) => out(a, "destinations")),
        }
        : {}),
      ...(rule.protocol ? { protocol: rule.protocol } : {}),
      ...(rule.ports ? { ports: rule.ports.map((p) => ({ ...p })) } : {}),
      priority: rule.priority ?? 100,
    };
  });
}

export function securityGroupToIR(
  group: SecurityGroup,
  addrs: AddressIndex,
): SecurityGroupNode {
  const rules: Record<string, unknown>[] = [];
  for (const rule of group.rules) {
    if (rule.kind === "masq") {
      rules.push({ ...rule });
    } else if (rule.kind === "filter") {
      rules.push(
        ...expandFilterRule(rule, addrs, `securitygroup:${group.name}`),
      );
    } else {
      // The outgoing port's own address, by reference (ADR 0011 §3b).
      const externalIp = addrs.ref(
        rule.externalIp,
        `securitygroup:${group.name}|snat:${rule.family}|externalIp`,
        [],
      );
      const logicalIps = rule.logicalIps.map((a) => a.to_string());
      for (const logicalIp of logicalIps) {
        if (NetworkValue(logicalIp) instanceof Error) {
          throw new Error(
            `security group "${group.name}": bad logicalIp ${logicalIp}`,
          );
        }
      }
      rules.push({ ...rule, externalIp, logicalIps });
    }
  }
  return {
    id: `securitygroup:${group.name}`,
    kind: "security.group",
    key: { name: group.name },
    data: { rules } as unknown as SecurityGroupNode["data"],
  };
}
