// src/ir_test.ts — covers toIR()'s shortIfaceName derivation, moved
// here from deployer/ir_to_shell.py's own _bridge_name/_fnv1a_32
// (2026-08-12 — see ir.ts's own shortIfaceName() doc comment for why:
// the generator computes the FINAL name a real kernel object needs,
// the translator only ever applies an already-resolved fact).

import { assertEquals, assertNotEquals, assertThrows } from "jsr:@std/assert@1";
import { fnv1a32, transitNetwork } from "./addressing.ts";
import { defineNetwork } from "./define.ts";
import { toIR } from "./ir.ts";
import type { IRNode } from "./ir.ts";
import { IPv4, IPv6 } from "./ip.ts";
import type { CollisionDomain, Service } from "./types.ts";

// shortIfaceName now lives on each interface entry (`iface.shortName`),
// not on the ovn.ls node's own `data` (2026-08-12 — see
// collisionDomainToIR's own doc comment, src/ir.ts) — so exercising it
// needs a domain with at least one real-world-bound interface, not a
// bare collision domain.
// Fails loudly if the IR node the test asked for is missing, instead of
// letting a later property access blow up with a TypeError.
function node(nodes: Record<string, IRNode>, key: string): IRNode {
  const found = nodes[key];
  if (!found) throw new Error(`missing IR node: ${key}`);
  return found;
}

function lsShortIfaceName(
  nodes: Record<string, IRNode>,
  domain: string,
): string {
  const interfaces = node(nodes, `ls:${domain}`).data["interfaces"] as Array<
    { iface: Record<string, unknown> }
  >;
  const first = interfaces[0];
  if (!first) throw new Error(`no interfaces on ls:${domain}`);
  return first.iface["shortName"] as string;
}

function networkWithBoundDomain(domainName: string) {
  return defineNetwork("test-net", (net) => {
    const domain = net.collisionDomain(domainName);
    const other = net.collisionDomain(`${domainName}-other`);
    const host = net.localHost("chassis-1");
    const r1 = net.defineOvnRouter(`router-${domainName}`, (router) => {
      router.left = router.ovnRouterEndpoint({
        l2Segment: domain,
        ipaddrs: [IPv4.parse("192.168.1.1/24")],
        ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: other,
        ipaddrs: [IPv4.parse("192.168.2.1/24")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
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

// buildKernelRouterEndpoint puts the kernelRouterEndpoint() input's
// `ifaces` ONLY on the KernelRouter's OWN `right` (KernelRouterSide.
// ifaces, types.ts, 2026-08-18) — the real-world-facing interface the
// deployer creates/moves into the netns. The transit domain keeps its
// bridge binding (localnet port/gateway-chassis pin/bridge-mapping)
// through the returned OVN endpoint's OWN ifaces, which are the transit
// veth (constructed explicitly in define.ts, SHORT leg names so the
// deployer's _emit_iface_bindings_create attaches the actual devices).
Deno.test("kernelRouterSideToIR: right carries the WAN ifaces, transit ovn.ls carries the veth", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
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
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  const right = node(nodes, "kernelrouter:router-wan|side:right");
  assertEquals(right.data["ipaddrs"], ["192.168.132.93/24"]);
  assertEquals(right.data["ifaces"], [
    {
      host: "chassis-1",
      iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
    },
  ]);

  const left = node(nodes, "kernelrouter:router-wan|side:left");
  // The transit leg's own veth pair, constructed implicitly in
  // buildKernelRouterEndpoint from the ovnRouter() name — the LONG leg
  // names exceed IFNAMSIZ, so the IR resolves the IFNAMSIZ-safe
  // `shortName` (readable when it fits, else a deterministic 6-hex
  // fnv1a32) and the deployer builds the real pair from it.
  const shortId = fnv1a32("router-wan").toString(16).padStart(8, "0").slice(
    0,
    6,
  );
  assertEquals(left.data["ifaces"], [
    {
      host: "chassis-1",
      iface: {
        kind: "veth",
        ifaceName: "veth-krn-router-wan",
        peerName: "veth-ovn-router-wan",
        shortName: shortId,
      },
    },
  ]);

  // The transit domain's ovn.ls keeps its bridge binding through the
  // OVN endpoint's OWN ifaces — the transit veth with SHORT leg names
  // (so the deployer attaches the actual created devices), plus the
  // bridge `shortName` (readable/hash, same rule shortIfaceName applies
  // everywhere; "br-transit-router-wan" exceeds IFNAMSIZ).
  const transitLs = node(nodes, "ls:transit-router-wan");
  assertEquals(transitLs.data["interfaces"], [
    {
      host: "chassis-1",
      iface: {
        kind: "veth",
        ifaceName: `veth-krn-${shortId}`,
        peerName: `veth-ovn-${shortId}`,
        shortName: `br-${
          fnv1a32("transit-router-wan").toString(16).padStart(8, "0")
        }`,
      },
    },
  ]);
});

// buildKernelRouterEndpoint must NOT put the WAN-facing `ipaddrs` on the
// OVN side of the transit link (they belong on the KernelRouter's own
// `right`, the kernel netns's real interface), and it must rewrite every
// declared route's `via` to the paired KernelRouter's transit-side
// address — a literal `via` (e.g. the real ISP gateway) is only
// reachable from the kernel netns, never from OVN's side of the transit
// link (confirmed live, 2026-08-21: keeping 192.168.132.1 on the OVN
// router made it ARP for its WAN gateway out the transit veth).
Deno.test("kernelRouterEndpoint: OVN side carries only transit addrs and routes via the kernel router", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const domain = net.routingDomain("test-domain");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [IPv4.parse("192.168.132.93/24")],
        routes: [
          { dst: IPv4.parse("0.0.0.0/0"), via: IPv4.parse("192.168.132.1") },
          { dst: IPv6.parse("::/0") },
        ],
        services: [
          { kind: "kernel.ipv4.masq" },
          { kind: "kernel.ipv6.masq" },
        ],
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [domain] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);

  // OVN side of the transit link: transit addresses only — the WAN
  // 192.168.132.93/24 must not be here, it lives on the kernel router's
  // own `right` (asserted by the sibling test above). And no ipv6_ra
  // configs: the kernel.* services were split off, never reaching the
  // OVN endpoint's RA handling.
  const leftLrp = node(nodes, "ovnrouter:router-wan|lrp:left");
  assertEquals(leftLrp.data["addresses"], [
    "10.12.80.1/28",
    "fd00::10:12:80:1/124",
  ]);
  assertEquals(leftLrp.data["ipv6RaConfigs"], undefined);

  // Both default routes point at the kernel router's transit side, even
  // though the v4 one was declared with a literal ISP `via`.
  assertEquals(
    node(nodes, "ovnrouter:router-wan|route:0.0.0.0/0").data["nexthop"],
    "10.12.80.14",
  );
  assertEquals(
    node(nodes, "ovnrouter:router-wan|route:::/0").data["nexthop"],
    "fd00::10:12:80:f",
  );

  // The kernel.* masq services are a SHORTCUT that expands (via
  // net.securityGroup()) to a group named `masq-<router>` attached to
  // the kernel router's RIGHT (WAN) side, emitted as an
  // implementation-abstract `security.group` node carrying the rules.
  const right = node(nodes, "kernelrouter:router-wan|side:right");
  assertEquals(right.data["securityGroup"], "masq-router-wan");
  const left = node(nodes, "kernelrouter:router-wan|side:left");
  assertEquals(left.data["securityGroup"], undefined);
  assertEquals(node(nodes, "securitygroup:masq-router-wan").data["rules"], [
    { family: "ipv4", kind: "masq" },
    { family: "ipv6", kind: "masq" },
  ]);
});

// An explicit securityGroup on a kernelRouterEndpoint() WINS — the
// `kernel.*.masq` services are then IGNORED (no rules derived from
// them), and the side attaches the explicitly-declared group instead.
Deno.test("kernelRouterEndpoint: explicit security group wins, masq services are ignored", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const out = net.securityGroup("wan-out", (g) => g.masq("ipv4"));
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [IPv4.parse("192.168.132.93/24")],
        services: [
          { kind: "kernel.ipv4.masq" },
          { kind: "kernel.ipv6.masq" },
        ],
        securityGroup: out,
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  // The explicit group is attached, exactly as built (ipv4-only masq);
  // the services' ipv6 masq never made it in.
  assertEquals(
    node(nodes, "kernelrouter:router-wan|side:right").data["securityGroup"],
    "wan-out",
  );
  assertEquals(node(nodes, "securitygroup:wan-out").data["rules"], [
    { family: "ipv4", kind: "masq" },
  ]);
  assertEquals(nodes["securitygroup:masq-router-wan"], undefined);
});

// An unregistered group (never returned by net.securityGroup()) fails
// fast instead of silently attaching something no other object can
// resolve. Same register + fail-fast split every other builder uses.
Deno.test("kernelRouterEndpoint: unregistered security group is rejected", () => {
  const foreign = {
    name: "wan-out",
    rules: [{ family: "ipv4" as const, kind: "masq" as const }],
  };
  let threw = false;
  try {
    defineNetwork("test-net", (net) => {
      const host = net.localHost("chassis-1");
      const backbone = net.collisionDomain("backbone");
      const r1 = net.defineOvnRouter("router-wan", (router) => {
        router.left = router.kernelRouterEndpoint({
          host,
          transit: transitNetwork(
            IPv4.parse("10.12.80.1/28"),
            IPv6.parse("fd00::10:12:80:1/124"),
          ),
          ipaddrs: [IPv4.parse("192.168.132.93/24")],
          securityGroup: foreign,
          ifaces: [
            { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
          ],
        });
        router.right = router.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.12.80/16")],
        });
        return { routingDomains: [] };
      });
      return { routers: [r1] };
    });
  } catch {
    threw = true;
  }
  assertEquals(threw, true);
});

// net.securityGroup() itself: the builder accumulates masq()/rule()
// entries into the resolved SecurityGroup, registers it on the network,
// and rejects a duplicate name — same register + fail-fast split as
// routingDomain()/collisionDomain().
Deno.test("securityGroup builder: accumulates rules, registers once, rejects duplicates", () => {
  const network = defineNetwork("test-net", (net) => {
    const g = net.securityGroup("g1", (group) => {
      group.masq("ipv4").masq("ipv6").rule({ family: "ipv6", kind: "masq" });
    });
    assertEquals(g.rules, [
      { family: "ipv4", kind: "masq" },
      { family: "ipv6", kind: "masq" },
      { family: "ipv6", kind: "masq" },
    ]);
    let threw = false;
    try {
      net.securityGroup("g1", (group) => group.masq("ipv4"));
    } catch {
      threw = true;
    }
    assertEquals(threw, true);
    return { routers: [] };
  });
  assertEquals(network.allSecurityGroups.length, 1);
  const firstGroup = network.allSecurityGroups[0];
  if (!firstGroup) throw new Error("expected one security group");
  assertEquals(firstGroup.name, "g1");
});

// `kernel.app.*` services resolve to KernelApp descriptors carried on
// the kernel router's right (WAN) side — independent of the security-
// group shortcut — and never reach the OVN endpoint's RA handling.
Deno.test("kernelRouterEndpoint: kernel.app services resolve to app descriptors on the right side", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [],
        services: [{ kind: "kernel.app.dhcp-client", style: "dhcpcd" }],
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  // dhcpcd is expressed as a generic docker app that OWNS the router's
  // interfaces, with an auto-build for the dhcpcd image.
  assertEquals(node(nodes, "kernelrouter:router-wan|side:right").data["apps"], [
    {
      kind: "docker",
      image: "ovn-fabric-router-wan",
      name: "router-wan-dhcpcd",
      cmd: ["/sbin/dhcpcd"],
      build: { from: "alpine:latest", packages: ["dhcpcd"] },
      // Interface-OWNING: NO veth fields at all — "owns the interfaces" is
      // exactly the ABSENCE of the veth machinery (no ip/routerIp/vethName).
    },
  ]);
  assertEquals(
    node(nodes, "kernelrouter:router-wan|side:left").data["apps"],
    undefined,
  );
  // The kernel.app service was split off, never becoming an RA config.
  assertEquals(
    node(nodes, "ovnrouter:router-wan|lrp:left").data["ipv6RaConfigs"],
    undefined,
  );
});

// `kernel.app.docker` resolves with the router name PREFIXED onto the
// container name (`<router>-<name>`, default `<router>-docker`), the
// `cmd` string split into `docker run` trailing args, the veth
// injection addresses (`ip` = the container's end, `routerIp` = the
// subnet's first host) — and the deployer's veth names are stamped in
// IR (`ve-<hash>`).
Deno.test("kernelRouterEndpoint: kernel.app.docker resolves router-prefixed name and veth addressing", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [],
        services: [
          {
            kind: "kernel.app.docker",
            name: "test-docker",
            image: "ubuntu",
            cmd: "sleep 86400",
            ipaddrs: [IPv4.parse("10.200.0.2/24")],
          },
        ],
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  // The veth prefix is a short `ve-<hash>` derived from the container
  // name (same fnv1a32 rule shortIfaceName applies).
  assertEquals(node(nodes, "kernelrouter:router-wan|side:right").data["apps"], [
    {
      kind: "docker",
      image: "ubuntu",
      name: "router-wan-test-docker",
      cmd: ["sleep", "86400"],
      ip: "10.200.0.2/24",
      routerIp: "10.200.0.1/24",
      vethName: `ve-${
        fnv1a32("router-wan-test-docker").toString(16).padStart(8, "0")
      }`,
    },
  ]);
});

// A docker service without `ip` gets a deterministic per-router slot
// (10.200.<fnv1a32(routerName) % 256>.2/24) so the config stays concise.
Deno.test("kernelRouterEndpoint: kernel.app.docker without ip gets a deterministic default slot", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [],
        services: [{ kind: "kernel.app.docker", image: "ubuntu" }],
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  assertEquals(node(nodes, "kernelrouter:router-wan|side:right").data["apps"], [
    {
      kind: "docker",
      image: "ubuntu",
      name: "router-wan-docker",
      ip: `10.200.${fnv1a32("router-wan") % 256}.2/24`,
      routerIp: `10.200.${fnv1a32("router-wan") % 256}.1/24`,
      vethName: `ve-${
        fnv1a32("router-wan-docker").toString(16).padStart(8, "0")
      }`,
    },
  ]);
});

// kernelRouterEndpoint now accepts a builder FUNCTION (not just an object),
// exposing endpoint.buildAppDocker — a method that only exists on kernel
// endpoints. The docker app's `image` defaults to the router name, and the
// `build` section is carried through (2026-08-31).
Deno.test("kernelRouterEndpoint: builder function + buildAppDocker, image defaults to router name", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint((endpoint) => {
        endpoint.host = host;
        endpoint.transit = transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        );
        endpoint.ipaddrs = [IPv4.parse("192.168.140.93/24")];
        endpoint.ifaces = [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ];
        endpoint.buildAppDocker("dhcpcd", (app) => {
          app.cmd("/sbin/dhcpcd");
          app.build({ from: "alpine:latest", packages: ["dhcpcd"] });
        });
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  assertEquals(node(nodes, "kernelrouter:router-wan|side:right").data["apps"], [
    {
      kind: "docker",
      image: "ovn-fabric-router-wan",
      build: { from: "alpine:latest", packages: ["dhcpcd"] },
      name: "router-wan-dhcpcd",
      cmd: ["/sbin/dhcpcd"],
      ip: `10.200.${fnv1a32("router-wan") % 256}.2/24`,
      routerIp: `10.200.${fnv1a32("router-wan") % 256}.1/24`,
      vethName: `ve-${
        fnv1a32("router-wan-dhcpcd").toString(16).padStart(8, "0")
      }`,
    },
  ]);
});

// Per-endpoint routingDomains (2026-08-23): a router can anchor one
// domain from its LEFT and participate in another from its RIGHT — the
// left's via-less default stays inside its own domain, while the right
// joins a different one as a plain participant (the tunnelRouterEndpoint
// pattern).
Deno.test("tunnelRouterEndpoint: per-endpoint routingDomains keep the anchor's default in its own domain", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const neighborRoute = net.routingDomain("Neighbor-defaultRoute");
    const vodaRoute = net.routingDomain("Voda-defaultRoute");
    const r1 = net.defineOvnRouter("router-mullvad-de", (router) => {
      router.left = router.tunnelRouterEndpoint({
        routingDomains: [neighborRoute],
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.81.1/28"),
          IPv6.parse("fd00::10:12:81:1/124"),
        ),
        upstream: transitNetwork(
          IPv4.parse("10.12.82.1/28"),
          IPv6.parse("fd00::10:12:82:1/124"),
        ),
        routes: [{ dst: IPv4.parse("0.0.0.0/0") }],
        services: [
          {
            kind: "wireguard",
            ifaceName: "mullvad-de",
            config: {
              privateKey: "k",
              address: "10.64.56.207/32",
              peer: {
                publicKey: "p",
                allowedIps: "0.0.0.0/0",
                endpoint: "1.2.3.4:51820",
              },
            },
            masq: ["ipv4", "ipv6"],
          },
        ],
        upstreamBackbone: {
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.150/16")],
        },
      });
      router.right = router.ovnRouterEndpoint({
        routingDomains: [vodaRoute],
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.0.140/16")],
      });
      return { routingDomains: [] };
    });
    // A neighbor participant on the backbone, in the neighbor domain.
    const r2 = net.defineOvnRouter("router-neighbor", (router) => {
      router.left = router.ovnRouterEndpoint({
        l2Segment: net.collisionDomain("neighbor"),
        ipaddrs: [IPv4.parse("192.168.130.1/24")],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.0.130/16")],
      });
      return { routingDomains: [neighborRoute] };
    });
    // A voda participant on the backbone (so the tunnel's right side
    // learns the voda default; the tunnel's left default must NOT leak).
    const r3 = net.defineOvnRouter("router-voda", (router) => {
      router.left = router.ovnRouterEndpoint({
        l2Segment: net.collisionDomain("voda"),
        ipaddrs: [IPv4.parse("192.168.132.1/24")],
        routes: [
          { dst: IPv4.parse("0.0.0.0/0"), via: IPv4.parse("192.168.132.1") },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [vodaRoute] };
    });
    return { routers: [r1, r2, r3] };
  });

  const nodes = toIR(network);
  // neighbor defaults out the tunnel (rewritten to its backbone addr).
  assertEquals(
    node(nodes, "ovnrouter:router-neighbor|route:0.0.0.0/0").data,
    { nexthop: "172.22.0.140", masq: false, domain: "Neighbor-defaultRoute" },
  );
  // No leakage: the tunnel default stays inside Neighbor-defaultRoute,
  // so voda (only in Voda-defaultRoute) keeps its OWN literal default.
  // The tunnel router, although it participates in Voda via its right
  // side, keeps its OWN tunnel egress (0.0.0.0/0 -> its netns) instead
  // of overwriting it with voda's learned default (2026-08-30).
  assertEquals(
    node(nodes, "ovnrouter:router-voda|route:0.0.0.0/0").data,
    { nexthop: "192.168.132.1", masq: false, domain: "Voda-defaultRoute" },
  );
  assertEquals(
    node(nodes, "ovnrouter:router-mullvad-de|route:0.0.0.0/0").data,
    { nexthop: "10.12.81.14", masq: false, domain: "Neighbor-defaultRoute" },
  );
  // The netns backroutes are scoped to the tunnel router's OWN domain:
  // only the neighbor subnet comes back into the netns (no home/voda
  // leakage) — the whole point of the separate domains.
  assertEquals(
    node(nodes, "kernelrouter:router-mullvad-de|side:left").data["routes"],
    [{ dst: "192.168.130.0/24", via: "10.12.81.1" }],
  );
});

// A zerotier tunnel's via-less declared routes (the ztnet mesh supernet)
// ride on the zerotier app so the deployer's wire script can push them
// out the RUNTIME-named interface — NOT on the kernel router's own
// routes, which target the upstream veth (2026-08-30).
Deno.test("tunnelRouterEndpoint: zerotier carries via-less tunnel routes onto its app", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const zt = net.routingDomain("Zerotier-route");
    const r1 = net.defineOvnRouter("router-zerotier", (router) => {
      router.left = router.tunnelRouterEndpoint({
        routingDomains: [zt],
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.85.1/28"),
          IPv6.parse("fd00::10:12:85:1/124"),
        ),
        upstream: transitNetwork(
          IPv4.parse("10.12.86.1/28"),
          IPv6.parse("fd00::10:12:86:1/124"),
        ),
        routes: [{ dst: IPv4.parse("192.168.0.0/16") }],
        services: [
          {
            kind: "zerotier",
            networkId: "02cfbec15c2319ff",
            instanceDir: "/var/lib/zerotier-one-uplink-zerotier",
          },
        ],
        upstreamBackbone: {
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.152/16")],
        },
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.0.142/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });
  const nodes = toIR(network);
  // The netns wire script gets the via-less supernet to egress the tunnel.
  const right = node(nodes, "kernelrouter:router-zerotier|side:right").data as {
    apps?: Array<{ kind: string; routes?: Array<{ dst: string }> }>;
  };
  const app = right.apps?.find((a) => a.kind === "zerotier");
  assertEquals(app?.routes, [{ dst: "192.168.0.0/16" }]);
  // R2 (the OVN logical router) ALSO gets a route to forward the supernet
  // INTO its netns via the kernel-side transit address (10.12.85.14) —
  // otherwise it has no way to send ztnet traffic to the tunnel
  // (2026-08-30).
  assertEquals(
    node(nodes, "ovnrouter:router-zerotier|route:192.168.0.0/16").data,
    { nexthop: "10.12.85.14", masq: false, domain: "Zerotier-route" },
  );
});

// instanceDir on a zerotier tunnel is OPTIONAL — when omitted it's
// derived from the router name (/var/lib/zerotier-one-<router>) so a
// config author only sets it when the default is wrong (2026-08-30).
Deno.test("tunnelRouterEndpoint: zerotier instanceDir is derived from the router name when omitted", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-zt", (router) => {
      router.left = router.tunnelRouterEndpoint({
        host,
        transit: transitNetwork(IPv4.parse("10.12.85.1/28")),
        upstream: transitNetwork(IPv4.parse("10.12.86.1/28")),
        services: [{ kind: "zerotier", networkId: "02cfbec15c2319ff" }],
        upstreamBackbone: {
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.152/16")],
        },
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.0.142/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });
  const nodes = toIR(network);
  const right = node(nodes, "kernelrouter:router-zt|side:right").data as {
    apps?: Array<{ kind: string; instanceDir?: string }>;
  };
  const app = right.apps?.find((a) => a.kind === "zerotier");
  assertEquals(app?.instanceDir, "/var/lib/zerotier-one-router-zt");
});

// hostToIR carries the host's ABSTRACT OS dependencies (ovn/ovs for an
// OVN-cluster host, ip/iptables for a kernel router, plus the app-level
// ones) and the resolved OS (assume Ubuntu when unset) — the deployer
// maps each abstract dep to the distro's package form (2026-08-23).
Deno.test("hostToIR: carries abstract OS dependencies and resolved OS", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost(
      "chassis-1",
      undefined,
      { role: { kind: "chassis" } },
    );
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-wan", (router) => {
      router.left = router.kernelRouterEndpoint({
        host,
        transit: transitNetwork(
          IPv4.parse("10.12.80.1/28"),
          IPv6.parse("fd00::10:12:80:1/124"),
        ),
        ipaddrs: [IPv4.parse("192.168.132.93/24")],
        services: [
          { kind: "kernel.app.dhcp-client", style: "dhclient" },
        ],
        ifaces: [
          { host, iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 } },
        ],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.12.80/16")],
      });
      return { routingDomains: [] };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  const hostNode = node(nodes, "host:chassis-1");
  assertEquals(hostNode.data["dependencies"], [
    "ovn",
    "ovs",
    "ip",
    "iptables",
    "dhclient",
  ]);
  // os not set -> assume Ubuntu (2026-08-23).
  assertEquals(hostNode.data["os"], { name: "ubuntu", version: "26.04" });
});

// defineOvnRouter object form (OvnRouterSpec): routingDomains is declared
// up-front and left/right are kind-tagged endpoint specs resolved by the
// builder — a fully declarative alternative to the builder-function form
// (2026-09-08).
Deno.test("defineOvnRouter object form: declarative OvnRouterSpec resolves left/right", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    const d = net.routingDomain("Route-D");
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", {
          routingDomains: [d],
          left: {
            kind: "ovn",
            l2Segment: a,
            ipaddrs: [IPv4.parse("192.168.1.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
          },
          right: {
            kind: "ovn",
            l2Segment: b,
            ipaddrs: [IPv4.parse("192.168.2.1/24")],
          },
        }),
      ],
    };
  });
  assertEquals(network.allRouters.length, 1);
  const r = network.allRouters[0];
  if (!r) throw new Error("expected one router");
  assertEquals(r.name, "router-x");
  assertEquals(r.routingDomains?.[0]?.name, "Route-D");
  assertEquals(r.left.l2Segment.name, "seg-a");
  assertEquals(r.right.l2Segment.name, "seg-b");
  const firstAddr = r.left.ipaddrs[0];
  if (!firstAddr) throw new Error("expected one left ipaddr");
  assertEquals(firstAddr.to_string(), "192.168.1.1/24");
});

// A Service attached at an OVN endpoint (ep.attachTo) becomes a
// kernel.container node with a FACE (NIC) on that endpoint's segment.
Deno.test("net.service + ep.attachTo -> kernel.service node + endpoint serviceRef", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    const dns = net.service("dns", (svc) => {
      svc.image = "ovn-fabric-dns";
      svc.cmd = ["/usr/sbin/dnsmasq", "--no-daemon"];
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          router.left = router.ovnRouterEndpoint((ep) => ({
            l2Segment: a,
            ipaddrs: [IPv4.parse("192.168.1.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
            services: [
              ep.attachTo(dns, { ipaddrs: [IPv4.parse("192.168.1.53/24")] }),
            ],
          }));
          router.right = router.ovnRouterEndpoint({
            l2Segment: b,
            ipaddrs: [IPv4.parse("192.168.2.1/24")],
          });
          return { routingDomains: [] };
        }),
      ],
    };
  });
  const nodes = toIR(network);
  // The workload is its OWN node (no network baked in)…
  assertEquals(node(nodes, "kernel.app.container:dns").data, {
    host: "host:chassis-1",
    image: "ovn-fabric-dns",
    cmd: ["/usr/sbin/dnsmasq", "--no-daemon"],
  });
  // …and the ENDPOINT references it (its NIC on that segment).
  assertEquals(node(nodes, "ovnrouter:router-x|lrp:left").data["serviceRefs"], [
    {
      service: "kernel.app.container:dns",
      name: "seg-a",
      ipaddrs: ["192.168.1.53/24"],
    },
  ]);
});

// A container's route via must be on the face's segment — an off-segment
// next-hop is unreachable and must throw.
Deno.test("attached service route via must be on the endpoint's segment", () => {
  const build = (via: string) =>
    defineNetwork("test-net", (net) => {
      const host = net.localHost("chassis-1");
      const a = net.collisionDomain("seg-a");
      const b = net.collisionDomain("seg-b");
      const dns = net.service("dns", (svc) => {
        svc.image = "x";
      });
      return {
        hosts: [host],
        routers: [
          net.defineOvnRouter("router-x", (router) => {
            router.left = router.ovnRouterEndpoint((ep) => ({
              l2Segment: a,
              ipaddrs: [IPv4.parse("192.168.1.1/24")],
              ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
              services: [
                ep.attachTo(dns, {
                  ipaddrs: [IPv4.parse("192.168.1.53/24")],
                  routes: [{
                    dst: IPv4.parse("0.0.0.0/0"),
                    via: IPv4.parse(via),
                  }],
                }),
              ],
            }));
            router.right = router.ovnRouterEndpoint({
              l2Segment: b,
              ipaddrs: [IPv4.parse("192.168.2.1/24")],
            });
            return { routingDomains: [] };
          }),
        ],
      };
    });
  toIR(build("192.168.1.1")); // on-segment via is fine
  assertThrows(() => toIR(build("10.9.9.1"))); // off-segment -> error
});

// Multiple DISTINCT services, each attached to its own segment AND the
// shared control plane -> one kernel.service node per service; the
// control-plane endpoint references BOTH.
Deno.test("multiple services attached across endpoints -> one service node each", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg128 = net.collisionDomain("home-v2");
    const seg129 = net.collisionDomain("management-v2");
    const cp = net.collisionDomain("control-plane");
    const backbone = net.collisionDomain("backbone");
    const dnsFor = (seg: number) =>
      net.service(`dns-${seg}`, (svc) => {
        svc.image = "ovn-fabric-dns";
      });
    const dns128 = dnsFor(128);
    const dns129 = dnsFor(129);
    const seg = (
      router: string,
      l2: CollisionDomain,
      svc: Service,
      v4: string,
    ) =>
      net.defineOvnRouter(router, (r) => {
        r.left = r.ovnRouterEndpoint((ep) => ({
          l2Segment: l2,
          ipaddrs: [IPv4.parse(v4)],
          ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
          services: [ep.attachTo(svc, { ipaddrs: [IPv4.parse(v4)] })],
        }));
        r.right = r.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.9/16")],
        });
        return { routingDomains: [] };
      });
    return {
      hosts: [host],
      collisionDomains: [backbone],
      routers: [
        seg("router-home-v2", seg128, dns128, "192.168.128.5/24"),
        seg("router-management-v2", seg129, dns129, "192.168.129.5/24"),
        net.defineOvnRouter("router-control-plane-v2", (r) => {
          r.left = r.ovnRouterEndpoint((ep) => ({
            l2Segment: cp,
            ipaddrs: [IPv4.parse("10.43.0.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
            services: [
              ep.attachTo(dns128, {
                ipaddrs: [IPv4.parse("10.43.0.128/24")],
                primary: true,
              }),
              ep.attachTo(dns129, {
                ipaddrs: [IPv4.parse("10.43.0.129/24")],
                primary: true,
              }),
            ],
          }));
          r.right = r.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.0.2/16")],
          });
          return { routingDomains: [] };
        }),
      ],
    };
  });
  const nodes = toIR(network);
  assertEquals(
    Object.keys(nodes).filter((k) => k.startsWith("kernel.app.container:"))
      .sort(),
    [
      "kernel.app.container:dns-128",
      "kernel.app.container:dns-129",
    ],
  );
  assertEquals(
    node(nodes, "ovnrouter:router-home-v2|lrp:left").data[
      "serviceRefs"
    ],
    [
      {
        service: "kernel.app.container:dns-128",
        name: "home-v2",
        ipaddrs: ["192.168.128.5/24"],
      },
    ],
  );
  const cpRefs = (node(nodes, "ovnrouter:router-control-plane-v2|lrp:left")
    .data as {
      serviceRefs?: { service: string }[];
    }).serviceRefs ?? [];
  assertEquals(cpRefs.map((r) => r.service).sort(), [
    "kernel.app.container:dns-128",
    "kernel.app.container:dns-129",
  ]);
});

// The EndpointBuilder injects the endpoint into every services[] entry
// (EndpointService<T> = T & { endpoint }), so the generator can read the
// endpoint's l2Segment/ipaddrs/routes per service.
Deno.test("endpoint builder injects `endpoint` into every service entry", () => {
  const network = defineNetwork("t", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          router.left = router.ovnRouterEndpoint((_ep) => ({
            l2Segment: a,
            ipaddrs: [IPv4.parse("192.168.1.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
            services: [{ kind: "ipv6.slaac" }],
          }));
          router.right = router.ovnRouterEndpoint({
            l2Segment: b,
            ipaddrs: [IPv4.parse("192.168.2.1/24")],
          });
          return { routingDomains: [] };
        }),
      ],
    };
  });
  const firstRouter = network.allRouters[0];
  if (!firstRouter) throw new Error("expected one router");
  const left = firstRouter.left;
  const svc = left.services?.[0] as unknown as {
    kind: string;
    endpoint: {
      l2Segment: { name: string };
      ipaddrs: { to_string(): string }[];
    };
  };
  assertEquals(svc.kind, "ipv6.slaac");
  assertEquals(svc.endpoint.l2Segment.name, "seg-a");
  assertEquals(svc.endpoint.ipaddrs[0]?.to_string(), "192.168.1.1/24");
});

// Attaching a reusable service records an ENDPOINT REFERENCE on the
// service itself (its counterpart to the endpoint's service.attach), one
// per endpoint it's attached to.
Deno.test("attachTo records endpointRefs on the service", () => {
  let dns!: Service;
  defineNetwork("t", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    const cp = net.collisionDomain("control-plane");
    dns = net.service("dns", (svc) => {
      svc.image = "x";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (router) => {
          router.left = router.ovnRouterEndpoint((ep) => ({
            l2Segment: a,
            ipaddrs: [IPv4.parse("192.168.1.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
            services: [
              ep.attachTo(dns, { ipaddrs: [IPv4.parse("192.168.1.5/24")] }),
            ],
          }));
          router.right = router.ovnRouterEndpoint({
            l2Segment: b,
            ipaddrs: [IPv4.parse("192.168.2.1/24")],
          });
          return { routingDomains: [] };
        }),
        net.defineOvnRouter("router-cp", (router) => {
          router.left = router.ovnRouterEndpoint((ep) => ({
            l2Segment: cp,
            ipaddrs: [IPv4.parse("10.43.0.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
            services: [
              ep.attachTo(dns, {
                ipaddrs: [IPv4.parse("10.43.0.5/24")],
                primary: true,
              }),
            ],
          }));
          router.right = router.ovnRouterEndpoint({
            l2Segment: b,
            ipaddrs: [IPv4.parse("172.22.0.2/16")],
          });
          return { routingDomains: [] };
        }),
      ],
    };
  });
  assertEquals(
    dns.endpointRefs.map((r) => r.endpoint.l2Segment.name).sort(),
    ["control-plane", "seg-a"],
  );
  assertEquals(
    dns.endpointRefs.find((r) => r.primary)?.endpoint.l2Segment.name,
    "control-plane",
  );
});
