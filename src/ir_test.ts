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
  // buildKernelRouterEndpoint from the ovnRouter() name — the LONG leg
  // names exceed IFNAMSIZ, so the IR resolves the IFNAMSIZ-safe
  // `shortName` (readable when it fits, else a deterministic 6-hex
  // fnv1a32) and the deployer builds the real pair from it.
  const shortId = fnv1a32("router-wan").toString(16).padStart(8, "0").slice(
    0,
    6,
  );
  assertEquals(left.data.ifaces, [
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
  const transitLs = nodes["ls:transit-router-wan"];
  assertEquals(transitLs.data.interfaces, [
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
    net.ovnRouter("router-wan", (router) => {
      router.routingDomains = [domain];
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
    });
  });

  const nodes = toIR(network);

  // OVN side of the transit link: transit addresses only — the WAN
  // 192.168.132.93/24 must not be here, it lives on the kernel router's
  // own `right` (asserted by the sibling test above). And no ipv6_ra
  // configs: the kernel.* services were split off, never reaching the
  // OVN endpoint's RA handling.
  const leftLrp = nodes["ovnrouter:router-wan|lrp:left"];
  assertEquals(leftLrp.data.addresses, [
    "10.12.80.1/28",
    "fd00::10:12:80:1/124",
  ]);
  assertEquals(leftLrp.data.ipv6RaConfigs, undefined);

  // Both default routes point at the kernel router's transit side, even
  // though the v4 one was declared with a literal ISP `via`.
  assertEquals(
    nodes["ovnrouter:router-wan|route:0.0.0.0/0"].data.nexthop,
    "10.12.80.14",
  );
  assertEquals(
    nodes["ovnrouter:router-wan|route:::/0"].data.nexthop,
    "fd00::10:12:80:f",
  );

  // The kernel.* masq services are a SHORTCUT that expands (via
  // net.securityGroup()) to a group named `masq-<router>` attached to
  // the kernel router's RIGHT (WAN) side, emitted as an
  // implementation-abstract `security.group` node carrying the rules.
  const right = nodes["kernelrouter:router-wan|side:right"];
  assertEquals(right.data.securityGroup, "masq-router-wan");
  const left = nodes["kernelrouter:router-wan|side:left"];
  assertEquals(left.data.securityGroup, undefined);
  assertEquals(nodes["securitygroup:masq-router-wan"].data.rules, [
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
    net.ovnRouter("router-wan", (router) => {
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
    });
  });

  const nodes = toIR(network);
  // The explicit group is attached, exactly as built (ipv4-only masq);
  // the services' ipv6 masq never made it in.
  assertEquals(
    nodes["kernelrouter:router-wan|side:right"].data.securityGroup,
    "wan-out",
  );
  assertEquals(nodes["securitygroup:wan-out"].data.rules, [
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
      net.ovnRouter("router-wan", (router) => {
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
      });
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
  });
  assertEquals(network.allSecurityGroups.length, 1);
  assertEquals(network.allSecurityGroups[0].name, "g1");
});

// `kernel.app.*` services resolve to KernelApp descriptors carried on
// the kernel router's right (WAN) side — independent of the security-
// group shortcut — and never reach the OVN endpoint's RA handling.
Deno.test("kernelRouterEndpoint: kernel.app services resolve to app descriptors on the right side", () => {
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
    });
  });

  const nodes = toIR(network);
  assertEquals(nodes["kernelrouter:router-wan|side:right"].data.apps, [
    { kind: "dhcp-client", style: "dhcpcd" },
  ]);
  assertEquals(nodes["kernelrouter:router-wan|side:left"].data.apps, undefined);
  // The kernel.app service was split off, never becoming an RA config.
  assertEquals(
    nodes["ovnrouter:router-wan|lrp:left"].data.ipv6RaConfigs,
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
    net.ovnRouter("router-wan", (router) => {
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
            ip: "10.200.0.2/24",
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
    });
  });

  const nodes = toIR(network);
  // The veth prefix is a short `ve-<hash>` derived from the container
  // name (same fnv1a32 rule shortIfaceName applies).
  assertEquals(nodes["kernelrouter:router-wan|side:right"].data.apps, [
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
    net.ovnRouter("router-wan", (router) => {
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
    });
  });

  const nodes = toIR(network);
  assertEquals(nodes["kernelrouter:router-wan|side:right"].data.apps, [
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
    net.ovnRouter("router-mullvad-de", (router) => {
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
        tunnel: {
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
        },
        routes: [{ dst: IPv4.parse("0.0.0.0/0") }],
        services: [{ kind: "kernel.ipv4.masq" }, { kind: "kernel.ipv6.masq" }],
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
    });
    // A neighbor participant on the backbone, in the neighbor domain.
    net.ovnRouter("router-neighbor", (router) => {
      router.routingDomains = [neighborRoute];
      router.left = router.ovnRouterEndpoint({
        l2Segment: net.collisionDomain("neighbor"),
        ipaddrs: [IPv4.parse("192.168.130.1/24")],
      });
      router.right = router.ovnRouterEndpoint({
        l2Segment: backbone,
        ipaddrs: [IPv4.parse("172.22.0.130/16")],
      });
    });
    // A voda participant on the backbone (so the tunnel's right side
    // learns the voda default; the tunnel's left default must NOT leak).
    net.ovnRouter("router-voda", (router) => {
      router.routingDomains = [vodaRoute];
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
    });
  });

  const nodes = toIR(network);
  // neighbor defaults out the tunnel (rewritten to its backbone addr).
  assertEquals(
    nodes["ovnrouter:router-neighbor|route:0.0.0.0/0"].data,
    { nexthop: "172.22.0.140", masq: false, domain: "Neighbor-defaultRoute" },
  );
  // No leakage: the tunnel default stays inside Neighbor-defaultRoute,
  // so voda (only in Voda-defaultRoute) keeps its OWN literal default,
  // and the tunnel router (participating in Voda via its right side)
  // LEARNS the voda default rewritten to voda's backbone.
  assertEquals(
    nodes["ovnrouter:router-voda|route:0.0.0.0/0"].data,
    { nexthop: "192.168.132.1", masq: false, domain: "Voda-defaultRoute" },
  );
  assertEquals(
    nodes["ovnrouter:router-mullvad-de|route:0.0.0.0/0"].data,
    { nexthop: "172.22.12.80", masq: false, domain: "Voda-defaultRoute" },
  );
  // The netns backroutes are scoped to the tunnel router's OWN domain:
  // only the neighbor subnet comes back into the netns (no home/voda
  // leakage) — the whole point of the separate domains.
  assertEquals(
    nodes["kernelrouter:router-mullvad-de|side:left"].data.routes,
    [{ dst: "192.168.130.0/24", via: "10.12.81.1" }],
  );
});
