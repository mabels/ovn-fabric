// src/ir_test.ts — covers toIR()'s shortIfaceName derivation, moved
// here from deployer/ir_to_shell.py's own _bridge_name/_fnv1a_32
// (2026-08-12 — see ir.ts's own shortIfaceName() doc comment for why:
// the generator computes the FINAL name a real kernel object needs,
// the translator only ever applies an already-resolved fact).

import { assertEquals, assertNotEquals } from "jsr:@std/assert@1";
import { defineNetwork } from "./define.ts";
import { toIR } from "./ir.ts";
import type { IRNode } from "./ir.ts";
import { IPv4 } from "./ip.ts";

// shortIfaceName now lives on each interface entry (`iface.shortName`),
// not on the ovn.ls node's own `data` (2026-08-12 — see
// collisionDomainToIR's own doc comment, src/ir.ts) — so exercising it
// needs a domain with at least one real-world-bound interface, not a
// bare collision domain.
function lsShortIfaceName(
  nodes: Record<string, IRNode>,
  domain: string,
): string {
  const interfaces = nodes[`ls:${domain}`].data.interfaces as Array<
    { iface: Record<string, unknown> }
  >;
  return interfaces[0].iface.shortName as string;
}

function networkWithBoundDomain(domainName: string) {
  return defineNetwork("test-net", (net) => {
    const domain = net.collisionDomain(domainName);
    const other = net.collisionDomain(`${domainName}-other`);
    const host = net.localHost("chassis-1");
    net.ovnRouter(`router-${domainName}`, (router) => {
      router.left = router.ovnRouterEndpoint({
        l2Segment: domain,
        ipaddrs: [IPv4.parse("192.168.1.1/24")],
        ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: other,
        ipaddrs: [IPv4.parse("192.168.2.1/24")],
      });
    });
  });
}

Deno.test("collisionDomainToIR: short domain name -> readable br-<name>, no hashing", () => {
  const network = networkWithBoundDomain("home");
  const nodes = toIR(network);
  assertEquals(lsShortIfaceName(nodes, "home"), "br-home");
});

Deno.test("collisionDomainToIR: long domain name falls back to a short deterministic hash", () => {
  // Regression: "br-voda-modem-v2" (16 chars) really failed on a live
  // container with ofproto "Invalid argument" — IFNAMSIZ is 15 usable
  // characters. A domain name long enough to blow that budget must
  // still produce a short, valid bridge name, deterministically (the
  // same long name always yields the same bridge, so a second
  // create/delete pass still targets the same real object).
  const longName = "voda-modem-v2-extremely-long-domain-name";
  const network = networkWithBoundDomain(longName);

  const short = lsShortIfaceName(toIR(network), longName);
  assertEquals(short.startsWith("br-"), true);
  assertEquals(
    short.length <= 15,
    true,
    `shortIfaceName too long for IFNAMSIZ: ${short}`,
  );
  assertNotEquals(short, `br-${longName}`);

  // Same input, same output.
  assertEquals(lsShortIfaceName(toIR(network), longName), short);
});
