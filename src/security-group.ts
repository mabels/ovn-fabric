// src/security-group.ts — the per-declaration rule API handed to
// net.securityGroup()'s build callback. Separate from define.ts because
// it's pure accumulation with no NetworkBuilder state.

import type { SecurityGroup, SecurityGroupRule } from "./types.ts";

/** The per-declaration rule API handed to net.securityGroup()'s build
 * callback — the ONE place a rule's abstract shape gets turned into the
 * concrete SecurityGroupRule a SecurityGroup carries. Today it only knows
 * `masq(family)`; future kernel services (docker containers, wireguard,
 * ...) add their own methods here, and the implementation-abstract
 * IR/deployer side consumes them unchanged. */
export class SecurityGroupBuilder {
  private readonly rules: SecurityGroupRule[] = [];

  constructor(private readonly name: string) {}

  /** Add a masquerade rule for one address family — the expansion of the
   * `kernel.ipv4.masq`/`kernel.ipv6.masq` service shortcut (see
   * buildKernelRouterEndpoint). Chainable. */
  masq(family: "ipv4" | "ipv6"): this {
    this.rules.push({ family, kind: "masq" });
    return this;
  }

  /** Add an already-fully-shaped rule — for the day a service isn't a
   * simple masq. Chainable. */
  rule(rule: SecurityGroupRule): this {
    this.rules.push(rule);
    return this;
  }

  /** The resolved, immutable group — name + a snapshot of the rules
   * accumulated so far. */
  build(): SecurityGroup {
    return { name: this.name, rules: [...this.rules] };
  }
}
