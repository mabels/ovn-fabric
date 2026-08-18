// src/ir_test.ts — covers toIR()'s shortIfaceName derivation, moved
// here from deployer/ir_to_shell.py's own _bridge_name/_fnv1a_32
// (2026-08-12 — see ir.ts's own shortIfaceName() doc comment for why:
// the generator computes the FINAL name a real kernel object needs,
// the translator only ever applies an already-resolved fact).

import { assertEquals, assertNotEquals } from "jsr:@std/assert@1";
import { fnv1a32, transitNetwork } from "./addressing.ts";
import { defineNetwork } from "./define.ts";
import { toIR } from "./ir.ts";
import type { IRNode } from "./ir.ts";
import { IPv4, IPv6 } from "./ip.ts";

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

// buildKernelRouterEndpoint copies the kernelRouterEndpoint() input's
// `ifaces` onto the KernelRouter's OWN `right` (KernelRouterSide.ifaces,
// types.ts, 2026-08-18) IN ADDITION to the OVN-side endpoint's own copy
// — so the `right` per-side kernel.router node carries the real
// interface the deployer creates/moves into the netns (see deployer/
// ir_to_shell.py's _emit_kernel_router_create), while the transit
// domain's ovn.ls keeps its bridge binding via the endpoint's copy (the
// localnet port/bridge binding/bridge-mapping must survive).
Deno.test("kernelRouterSideToIR: right carries the ifaces, transit ovn.ls keeps its binding", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    net.ovnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [IPv4.parse("192.168.132.93/24")],
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
    });
  });

  const nodes = toIR(network);
  const right = nodes["kernelrouter:router-wan|side:right"];
  assertEquals(right.data.ipaddrs, ["192.168.132.93/24"]);
  assertEquals(right.data.ifaces, [
    {
      host: "chassis-1",
      iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
    },
  ]);

  const left = nodes["kernelrouter:router-wan|side:left"];
  // The transit leg's own veth pair, constructed implicitly in
  // buildKernelRouterEndpoint from the ovnRouter() name (long names —
  // the IFNAMSIZ-safe shortening is the lower layers' job).
  assertEquals(left.data.ifaces, [
    {
      host: "chassis-1",
      iface: {
        kind: "veth",
        ifaceName: "veth-krn-router-wan",
        peerName: "veth-ovn-router-wan",
      },
    },
  ]);

  // The transit domain's ovn.ls keeps its bridge binding through the
  // endpoint's own ifaces copy — and is named after the enclosing
  // ovnRouter (`transit-<routerName>`, 2026-08-18). "br-transit-router-
  // wan" exceeds IFNAMSIZ, so the bridge shortName is the deterministic
  // fnv1a32 fallback (same rule shortIfaceName applies everywhere).
  const transitLs = nodes["ls:transit-router-wan"];
  assertEquals(transitLs.data.interfaces, [
    {
      host: "chassis-1",
      iface: {
        kind: "vlan",
        vlanParent: "eth0",
        vlanId: 2280,
        shortName: `br-${
          fnv1a32("transit-router-wan").toString(16).padStart(8, "0")
        }`,
      },
    },
  ]);
});
