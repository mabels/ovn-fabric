// src/ir_test.ts — covers toIR()'s shortIfaceName derivation, moved
// here from deployer/ir_common.py's own _bridge_name/_fnv1a_32
// (2026-08-12 — see ir.ts's own shortIfaceName() doc comment for why:
// the generator computes the FINAL name a real kernel object needs,
// the translator only ever applies an already-resolved fact).

import { assertEquals, assertNotEquals, assertThrows } from "jsr:@std/assert@1";
import {
  fnv1a32,
  fnv1a64,
  macFromAddrsOrName,
  transitNetwork,
} from "./addressing.ts";
import { defineNetwork } from "./define.ts";
import { toIR } from "./ir.ts";
import type { IRNode } from "./ir.ts";
import type {
  InfraHostNode,
  KernelAppContainerNode,
  KernelRouterNode,
  OvnLrNode,
  OvnLrpNode,
  OvnLsNode,
  RouteNode,
  SecurityGroupNode,
} from "./protocol.ts";
import { IPv4, IPv6 } from "./ip.ts";
import { deriveEndpointName } from "./router-endpoints.ts";
import type {
  CollisionDomain,
  DnsProvider,
  OvnRouterEndpoint,
  Platform,
  Publish,
  Router,
  Service,
} from "./types.ts";

// Endpoints are an ARRAY now, addressed by their own `name` rather than
// by position — tests pick one out by the CollisionDomain name it binds.
function endpointOn(router: Router, domainName: string): OvnRouterEndpoint {
  const found = router.endpoints.find((e) => e.l2Segment.name === domainName);
  if (!found) {
    throw new Error(
      `router "${router.name}" has no endpoint on domain "${domainName}"`,
    );
  }
  return found;
}

// shortIfaceName now lives on each interface entry (`iface.shortName`),
// not on the ovn.ls node's own `data` (2026-08-12 — see
// collisionDomainToIR's own doc comment, src/ir.ts) — so exercising it
// needs a domain with at least one real-world-bound interface, not a
// bare collision domain.
// The node type an id names — every IR id starts with (or contains) a
// marker for its kind, so `node(nodes, "ls:home")` is an OvnLsNode and its
// `.data.interfaces` is typed, with no cast at the call site.
type NodeForId<Id extends string> = Id extends `host:${string}` ? InfraHostNode
  : Id extends `ls:${string}` ? OvnLsNode
  : Id extends `${string}|lrp:${string}` ? OvnLrpNode
  : Id extends `${string}|route:${string}` ? RouteNode
  : Id extends `kernelrouter:${string}` ? KernelRouterNode
  : Id extends `securitygroup:${string}` ? SecurityGroupNode
  : Id extends `kernel.app.container:${string}` ? KernelAppContainerNode
  : Id extends `ovnrouter:${string}` ? OvnLrNode
  : IRNode;

// The same rule at runtime, most specific marker first.
const KIND_MARKERS: readonly (readonly [RegExp, readonly IRNode["kind"][]])[] =
  [
    [/^host:/, ["infra.host"]],
    [/^ls:/, ["ovn.ls"]],
    [/\|lrp:/, ["ovn.lrp"]],
    [/\|route:/, ["ipv4.route", "ipv6.route"]],
    [/^kernelrouter:/, ["kernel.router"]],
    [/^securitygroup:/, ["security.group"]],
    [/^kernel\.app\.container:/, ["kernel.app.container"]],
    [/^ovnrouter:/, ["ovn.lr"]],
  ];

function isNodeForId<Id extends string>(
  found: IRNode,
  id: Id,
): found is IRNode & NodeForId<Id> {
  const marker = KIND_MARKERS.find(([pattern]) => pattern.test(id));
  return marker === undefined || marker[1].includes(found.kind);
}

// Fails loudly if the IR node the test asked for is missing (or is not
// the kind its id says), instead of letting a later property access blow
// up with a TypeError.
function node<Id extends string>(
  nodes: Record<string, IRNode>,
  id: Id,
): IRNode & NodeForId<Id> {
  const found = nodes[id];
  if (!found) throw new Error(`missing IR node: ${id}`);
  if (!isNodeForId(found, id)) {
    throw new Error(`IR node ${id} has unexpected kind "${found.kind}"`);
  }
  return found;
}

function lsShortIfaceName(
  nodes: Record<string, IRNode>,
  domain: string,
): string {
  const first = node(nodes, `ls:${domain}`).data.interfaces[0];
  if (!first) throw new Error(`no interfaces on ls:${domain}`);
  return first.iface.shortName;
}

function networkWithBoundDomain(domainName: string) {
  return defineNetwork("test-net", (net) => {
    const domain = net.collisionDomain(domainName);
    const other = net.collisionDomain(`${domainName}-other`);
    const host = net.localHost("chassis-1");
    const r1 = net.defineOvnRouter(`router-${domainName}`, (router) => {
      return {
        routingDomains: [],
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: domain,
            ipaddrs: [IPv4.parse("192.168.1.1/24")],
            ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
          }),
          router.ovnRouterEndpoint({
            l2Segment: other,
            ipaddrs: [IPv4.parse("192.168.2.1/24")],
          }),
        ],
      };
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint({
            host,
            transit: transitNetwork(
              IPv4.parse("10.12.80.1/28"),
              IPv6.parse("fd00::10:12:80:1/124"),
            ),
            ipaddrs: [IPv4.parse("192.168.132.93/24")],
            ifaces: [
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  const right = node(nodes, "kernelrouter:router-wan|side:right");
  assertEquals(right.data["ipaddrs"], ["192.168.132.93/24"]);
  assertEquals(right.data["ifaces"], [
    {
      host: "chassis-1",
      iface: {
        kind: "vlan",
        vlanParent: { kind: "physical", name: "eth0" },
        vlanId: 2280,
      },
    },
  ]);

  const left = node(nodes, "kernelrouter:router-wan|side:left");
  // The transit leg's own veth pair, constructed implicitly in
  // buildKernelRouterEndpoint from the ovnRouter() name — the LONG leg
  // names exceed IFNAMSIZ, so the IR resolves the IFNAMSIZ-safe
  // `shortName` (readable when it fits, else a deterministic 6-hex
  // fnv1a32) and the deployer builds the real pair from it. `mac` is the
  // netns-side leg's own MAC, folded from this side's transit address
  // (10.12.80.14, the same fold an LRP uses) so the OVN router's
  // MAC_Binding survives the leg being recreated.
  const shortId = fnv1a32("router-wan").toString(16).padStart(8, "0").slice(
    0,
    6,
  );
  assertEquals(left.data["ipaddrs"], [
    "10.12.80.14/28",
    "fd00::10:12:80:f/124",
  ]);
  assertEquals(left.data["ifaces"], [
    {
      host: "chassis-1",
      iface: {
        kind: "veth",
        ifaceName: "veth-krn-router-wan",
        peerName: "veth-ovn-router-wan",
        shortName: shortId,
        mac: "00:00:0a:0c:50:0e",
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
        // A kernel/tunnel router transit is internal mesh plumbing, not
        // segment traffic — flow export defaults OFF (types.ts flow).
        flow: false,
      },
    },
  ]);
});

Deno.test("iface resolution: a VLAN on an xdp parent carries the resolved AF_XDP settings", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const home = net.collisionDomain("home");
    const xdp = {
      kind: "xdp",
      name: "ovnxdp",
      mode: "generic",
      nRxq: 1,
    } as const;
    const r1 = net.defineOvnRouter("router-xdp", (router) => ({
      routingDomains: [],
      endpoints: [
        router.ovnRouterEndpoint({
          l2Segment: home,
          ipaddrs: [IPv4.parse("192.168.128.1/24")],
          ifaces: [
            { host, iface: { kind: "vlan", vlanParent: xdp, vlanId: 1128 } },
          ],
        }),
        router.ovnRouterEndpoint({
          l2Segment: net.collisionDomain("backbone"),
          ipaddrs: [IPv4.parse("172.22.0.1/16")],
        }),
      ],
    }));
    return { routers: [r1] };
  });

  const ls = node(toIR(network), "ls:home");
  assertEquals(ls.data.interfaces, [
    {
      host: "chassis-1",
      iface: {
        kind: "vlan",
        vlanParent: {
          kind: "xdp",
          name: "ovnxdp",
          mode: "generic",
          nRxq: 1,
        },
        vlanId: 1128,
        shortName: "br-home",
      },
    },
  ]);
});

Deno.test("iface resolution: a bare xdp iface defaults to best-effort/1; an explicit physical parent stays physical", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-xdp-defaults", (router) => ({
      routingDomains: [],
      endpoints: [
        router.ovnRouterEndpoint({
          l2Segment: net.collisionDomain("home"),
          ipaddrs: [IPv4.parse("192.168.128.1/24")],
          ifaces: [{ host, iface: { kind: "xdp", name: "ovnxdp" } }],
        }),
        router.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.1/16")],
        }),
      ],
    }));
    const r2 = net.defineOvnRouter("router-physical-parent", (router) => ({
      routingDomains: [],
      endpoints: [
        router.ovnRouterEndpoint({
          l2Segment: net.collisionDomain("dmz"),
          ipaddrs: [IPv4.parse("192.168.129.1/24")],
          ifaces: [
            {
              host,
              iface: {
                kind: "vlan",
                vlanParent: { kind: "physical", name: "eth2" },
                vlanId: 129,
              },
            },
          ],
        }),
        router.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.2/16")],
        }),
      ],
    }));
    return { routers: [r1, r2] };
  });

  const nodes = toIR(network);
  assertEquals(node(nodes, "ls:home").data.interfaces[0]!.iface, {
    kind: "xdp",
    name: "ovnxdp",
    mode: "best-effort",
    nRxq: 1,
    shortName: "br-home",
  });
  assertEquals(node(nodes, "ls:dmz").data.interfaces[0]!.iface, {
    kind: "vlan",
    vlanParent: { kind: "physical", name: "eth2" },
    vlanId: 129,
    shortName: "br-dmz",
  });
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
      return {
        routingDomains: [domain],
        endpoints: [
          router.kernelRouterEndpoint({
            host,
            transit: transitNetwork(
              IPv4.parse("10.12.80.1/28"),
              IPv6.parse("fd00::10:12:80:1/124"),
            ),
            ipaddrs: [IPv4.parse("192.168.132.93/24")],
            routes: [
              {
                dst: IPv4.parse("0.0.0.0/0"),
                via: IPv4.parse("192.168.132.1"),
              },
              { dst: IPv6.parse("::/0") },
            ],
            services: [
              { kind: "kernel.ipv4.masq" },
              { kind: "kernel.ipv6.masq" },
            ],
            ifaces: [
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);

  // OVN side of the transit link: transit addresses only — the WAN
  // 192.168.132.93/24 must not be here, it lives on the kernel router's
  // own `right` (asserted by the sibling test above). And no ipv6_ra
  // configs: the kernel.* services were split off, never reaching the
  // OVN endpoint's RA handling.
  const leftLrp = node(
    nodes,
    `ovnrouter:router-wan|lrp:${
      endpointOn(network.allRouters[0]!, "transit-router-wan").name
    }`,
  );
  assertEquals(leftLrp.data["addresses"], [
    "10.12.80.1/28",
    "fd00::10:12:80:1/124",
  ]);
  assertEquals(leftLrp.data["ipv6RaConfigs"], undefined);

  // Both default routes point at the kernel router's transit side, even
  // though the v4 one was declared with a literal ISP `via`.
  assertEquals(
    node(nodes, "ovnrouter:router-wan|route:test-domain|0.0.0.0/0")
      .data["nexthop"],
    "10.12.80.14",
  );
  assertEquals(
    node(nodes, "ovnrouter:router-wan|route:test-domain|::/0").data["nexthop"],
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint({
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
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
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
        return {
          routingDomains: [],
          endpoints: [
            router.kernelRouterEndpoint({
              host,
              transit: transitNetwork(
                IPv4.parse("10.12.80.1/28"),
                IPv6.parse("fd00::10:12:80:1/124"),
              ),
              ipaddrs: [IPv4.parse("192.168.132.93/24")],
              securityGroup: foreign,
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
                },
              ],
            }),
            router.ovnRouterEndpoint({
              l2Segment: backbone,
              ipaddrs: [IPv4.parse("172.22.12.80/16")],
            }),
          ],
        };
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint({
            host,
            transit: transitNetwork(
              IPv4.parse("10.12.80.1/28"),
              IPv6.parse("fd00::10:12:80:1/124"),
            ),
            ipaddrs: [],
            services: [{ kind: "kernel.app.dhcp-client", style: "dhcpcd" }],
            ifaces: [
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
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
    node(
      nodes,
      `ovnrouter:router-wan|lrp:${
        endpointOn(network.allRouters[0]!, "transit-router-wan").name
      }`,
    ).data["ipv6RaConfigs"],
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint({
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
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint({
            host,
            transit: transitNetwork(
              IPv4.parse("10.12.80.1/28"),
              IPv6.parse("fd00::10:12:80:1/124"),
            ),
            ipaddrs: [],
            services: [{ kind: "kernel.app.docker", image: "ubuntu" }],
            ifaces: [
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint((endpoint) => {
            endpoint.host = host;
            endpoint.transit = transitNetwork(
              IPv4.parse("10.12.80.1/28"),
              IPv6.parse("fd00::10:12:80:1/124"),
            );
            endpoint.ipaddrs = [IPv4.parse("192.168.140.93/24")];
            endpoint.ifaces = [
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ];
            endpoint.buildAppDocker("dhcpcd", (app) => {
              app.cmd("/sbin/dhcpcd");
              app.build({ from: "alpine:latest", packages: ["dhcpcd"] });
            });
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
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
      return {
        routingDomains: [],
        endpoints: [
          router.tunnelRouterEndpoint({
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
          }),
          router.ovnRouterEndpoint({
            routingDomains: [vodaRoute],
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.0.140/16")],
          }),
        ],
      };
    });
    // A neighbor participant on the backbone, in the neighbor domain.
    const r2 = net.defineOvnRouter("router-neighbor", (router) => {
      return {
        routingDomains: [neighborRoute],
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: net.collisionDomain("neighbor"),
            ipaddrs: [IPv4.parse("192.168.130.1/24")],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.0.130/16")],
          }),
        ],
      };
    });
    // A voda participant on the backbone (so the tunnel's right side
    // learns the voda default; the tunnel's left default must NOT leak).
    const r3 = net.defineOvnRouter("router-voda", (router) => {
      return {
        routingDomains: [vodaRoute],
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: net.collisionDomain("voda"),
            ipaddrs: [IPv4.parse("192.168.132.1/24")],
            routes: [
              {
                dst: IPv4.parse("0.0.0.0/0"),
                via: IPv4.parse("192.168.132.1"),
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
    });
    return { routers: [r1, r2, r3] };
  });

  const nodes = toIR(network);
  // neighbor defaults out the tunnel (rewritten to its backbone addr).
  assertEquals(
    node(
      nodes,
      "ovnrouter:router-neighbor|route:Neighbor-defaultRoute|0.0.0.0/0",
    ).data,
    {
      nexthop: "172.22.0.140",
      masq: false,
      domain: "Neighbor-defaultRoute",
      metric: 400,
    },
  );
  // No leakage: the tunnel default stays inside Neighbor-defaultRoute,
  // so voda (only in Voda-defaultRoute) keeps its OWN literal default.
  // The tunnel router, although it participates in Voda via its right
  // side, keeps its OWN tunnel egress (0.0.0.0/0 -> its netns) instead
  // of overwriting it with voda's learned default (2026-08-30).
  assertEquals(
    node(nodes, "ovnrouter:router-voda|route:Voda-defaultRoute|0.0.0.0/0").data,
    {
      nexthop: "192.168.132.1",
      masq: false,
      domain: "Voda-defaultRoute",
      metric: 401,
    },
  );
  assertEquals(
    node(
      nodes,
      "ovnrouter:router-mullvad-de|route:Neighbor-defaultRoute|0.0.0.0/0",
    ).data,
    {
      nexthop: "10.12.81.14",
      masq: false,
      domain: "Neighbor-defaultRoute",
      metric: 400,
    },
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
Deno.test("tunnelRouterEndpoint: zerotier is a docker app with its state volume", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const zt = net.routingDomain("Zerotier-route");
    const r1 = net.defineOvnRouter("router-zerotier", (router) => {
      return {
        routingDomains: [],
        endpoints: [
          router.tunnelRouterEndpoint({
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
                identity: { public: "pub", secret: "sec" },
              },
            ],
            upstreamBackbone: {
              l2Segment: backbone,
              ipaddrs: [IPv4.parse("172.22.0.152/16")],
            },
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.0.142/16")],
          }),
        ],
      };
    });
    return { routers: [r1] };
  });
  const nodes = toIR(network);
  // The netns wire script gets the via-less supernet to egress the tunnel.
  const right = node(nodes, "kernelrouter:router-zerotier|side:right").data as {
    apps?: Array<{
      kind: string;
      cmd?: string[];
      build?: { dockerfile?: string };
      volumes?: Array<{ host: string; container: string }>;
    }>;
  };
  const app = right.apps?.find((a) => a.kind === "docker");
  assertEquals(
    app?.build?.dockerfile?.includes("ZT_NETWORK_ID=02cfbec15c2319ff"),
    true,
  );
  assertEquals(app?.cmd?.join("\n").includes("192.168.0.0/16"), true);
  assertEquals(app?.volumes, [
    {
      host: "/var/lib/ovn-fabric/router-zerotier/zerotier",
      container: "/var/lib/zerotier-one",
    },
  ]);
  assertEquals(
    (app as { identity?: { public: string; secret: string } }).identity,
    { public: "pub", secret: "sec" },
  );
  // R2 (the OVN logical router) ALSO gets a route to forward the supernet
  // INTO its netns via the kernel-side transit address (10.12.85.14) —
  // otherwise it has no way to send ztnet traffic to the tunnel
  // (2026-08-30).
  assertEquals(
    node(nodes, "ovnrouter:router-zerotier|route:Zerotier-route|192.168.0.0/16")
      .data,
    {
      nexthop: "10.12.85.14",
      masq: false,
      domain: "Zerotier-route",
      metric: 400,
    },
  );
});

// The ZeroTier docker app keeps its identity under the router's state
// directory.
Deno.test("tunnelRouterEndpoint: zerotier state directory is derived from the router name", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-zt", (router) => {
      return {
        routingDomains: [],
        endpoints: [
          router.tunnelRouterEndpoint({
            host,
            transit: transitNetwork(IPv4.parse("10.12.85.1/28")),
            upstream: transitNetwork(IPv4.parse("10.12.86.1/28")),
            services: [{ kind: "zerotier", networkId: "02cfbec15c2319ff" }],
            upstreamBackbone: {
              l2Segment: backbone,
              ipaddrs: [IPv4.parse("172.22.0.152/16")],
            },
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.0.142/16")],
          }),
        ],
      };
    });
    return { routers: [r1] };
  });
  const nodes = toIR(network);
  const right = node(nodes, "kernelrouter:router-zt|side:right").data as {
    apps?: Array<{
      kind: string;
      volumes?: Array<{ host: string; container: string }>;
    }>;
  };
  const app = right.apps?.find((a) => a.kind === "docker");
  assertEquals(
    app?.volumes?.[0]?.host,
    "/var/lib/ovn-fabric/router-zt/zerotier",
  );
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
      return {
        routingDomains: [],
        endpoints: [
          router.kernelRouterEndpoint({
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
              {
                host,
                iface: { kind: "vlan", vlanParent: "eth0", vlanId: 2280 },
              },
            ],
          }),
          router.ovnRouterEndpoint({
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.12.80/16")],
          }),
        ],
      };
    });
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  const hostNode = node(nodes, "host:chassis-1");
  assertEquals(hostNode.data["dependencies"], [
    "arp",
    "ping",
    "traceroute",
    "curl",
    "python",
    "ovn",
    "ovs",
    "ip",
    "iptables",
    "dhclient",
  ]);
  // os not set -> assume Ubuntu (2026-08-23).
  assertEquals(hostNode.data["os"], { name: "ubuntu", version: "26.04" });
});

// Flow export (2026-10-10): Host.monitoring.ipfix becomes the host's
// `ipfix` IR data (WHERE to export), and a leg's `flow: false` rides
// that leg's ovn.ls interface blob as `iface.flow` (WHETHER it is
// sampled) — only reported when OFF; absent means inherit.
Deno.test("hostToIR: monitoring.ipfix becomes host data.ipfix", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost(
      "chassis-1",
      undefined,
      { role: { kind: "chassis" } },
      { ipfix: { target: "192.168.129.88:4739", sampling: 512 } },
    );
    const domain = net.collisionDomain("home");
    const backbone = net.collisionDomain("backbone");
    const r1 = net.defineOvnRouter("router-1", (router) => ({
      routingDomains: [],
      endpoints: [
        router.ovnRouterEndpoint({
          l2Segment: domain,
          ipaddrs: [IPv4.parse("192.168.1.1/24")],
          ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
        }),
        router.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.1/16")],
        }),
      ],
    }));
    return { routers: [r1] };
  });

  const hostData = node(toIR(network), "host:chassis-1").data as Record<
    string,
    unknown
  >;
  const ipfix = hostData["ipfix"] as {
    targets: string;
    sampling: number;
    cacheActiveTimeout?: number;
  };
  assertEquals(ipfix.targets, "192.168.129.88:4739");
  assertEquals(ipfix.sampling, 512);
  assertEquals(ipfix.cacheActiveTimeout, undefined);
});

Deno.test("collisionDomainToIR: flow:false rides the leg's bridge iface", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const sampled = net.collisionDomain("sampled");
    const quiet = net.collisionDomain("quiet");
    const r1 = net.defineOvnRouter("router-1", (router) => ({
      routingDomains: [],
      endpoints: [
        router.ovnRouterEndpoint({
          l2Segment: sampled,
          ipaddrs: [IPv4.parse("192.168.1.1/24")],
          ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
        }),
        router.ovnRouterEndpoint({
          l2Segment: quiet,
          ipaddrs: [IPv4.parse("192.168.2.1/24")],
          ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
          flow: false,
        }),
      ],
    }));
    return { routers: [r1] };
  });

  const nodes = toIR(network);
  const quietIfaces = node(nodes, "ls:quiet").data.interfaces;
  assertEquals(quietIfaces[0]!.iface.flow, false);
  // No flag -> no `flow` key at all (inherit), not `flow: true`.
  const sampledIfaces = node(nodes, "ls:sampled").data.interfaces;
  assertEquals("flow" in sampledIfaces[0]!.iface, false);
});

// defineOvnRouter object form (OvnRouterSpec): routingDomains is declared
// up-front and `endpoints` is an array of kind-tagged endpoint specs
// resolved by the builder — a fully declarative alternative to the
// builder-function form (2026-09-08, array 2026-09-16).
Deno.test("defineOvnRouter object form: declarative OvnRouterSpec resolves endpoints", () => {
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
          endpoints: [
            {
              kind: "ovn",
              l2Segment: a,
              ipaddrs: [IPv4.parse("192.168.1.1/24")],
              ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
            },
            {
              kind: "ovn",
              l2Segment: b,
              ipaddrs: [IPv4.parse("192.168.2.1/24")],
            },
          ],
        }),
      ],
    };
  });
  assertEquals(network.allRouters.length, 1);
  const r = network.allRouters[0];
  if (!r) throw new Error("expected one router");
  assertEquals(r.name, "router-x");
  assertEquals(r.routingDomains?.[0]?.name, "Route-D");
  assertEquals(
    r.endpoints.map((e) => e.l2Segment.name).sort(),
    ["seg-a", "seg-b"],
  );
  const firstAddr = endpointOn(r, "seg-a").ipaddrs[0];
  if (!firstAddr) throw new Error("expected one seg-a ipaddr");
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
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint((ep) => ({
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
                services: [
                  ep.attachTo(dns, {
                    ipaddrs: [IPv4.parse("192.168.1.53/24")],
                  }),
                ],
              })),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
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
  // The host runs a container, so it needs docker — even without a
  // kernel router.
  assertEquals(
    node(nodes, "host:chassis-1").data["dependencies"]?.includes("docker"),
    true,
  );
  // …and the ENDPOINT references it (its NIC on that segment).
  assertEquals(
    node(
      nodes,
      `ovnrouter:router-x|lrp:${
        endpointOn(network.allRouters[0]!, "seg-a").name
      }`,
    ).data["serviceRefs"],
    [
      {
        service: "kernel.app.container:dns",
        name: "seg-a",
        // The NIC's MAC is folded from its own address (192.168.1.53).
        mac: "00:00:c0:a8:01:35",
        ipaddrs: ["192.168.1.53/24"],
      },
    ],
  );
});

// Metric precedence for a workload's kernel routes: a route's own metric,
// else its attachment's (per-NIC) metric, else the routing domain's
// (2026-09-19). Lower wins in the deployer — this is how one asks for
// management-v2 over a control-plane fallback.
Deno.test("route metrics: domain, attachment and per-route precedence", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    const domain = net.routingDomain("Dom", {
      metric: 500,
      routes: [{ dst: IPv4.parse("10.9.0.0/24") }],
    });
    const dns = net.service("dns", (svc) => {
      svc.image = "x";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          return {
            routingDomains: [domain],
            endpoints: [
              router.ovnRouterEndpoint((ep) => ({
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
                services: [
                  ep.attachTo(dns, {
                    ipaddrs: [IPv4.parse("192.168.1.53/24")],
                    metric: 700,
                    routes: [
                      {
                        dst: IPv4.parse("10.10.0.0/24"),
                        via: IPv4.parse("192.168.1.1"),
                        metric: 900,
                      },
                      {
                        dst: IPv4.parse("10.11.0.0/24"),
                        via: IPv4.parse("192.168.1.1"),
                      },
                    ],
                  }),
                ],
              })),
              router.ovnRouterEndpoint((ep) => ({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
                services: [
                  // No explicit routes → inherits the domain's 10.9.0.0/24;
                  // metric comes from the ATTACHMENT (300), more specific
                  // than the domain's 500.
                  ep.attachTo(dns, {
                    ipaddrs: [IPv4.parse("192.168.2.53/24")],
                    metric: 300,
                  }),
                ],
              })),
            ],
          };
        }),
      ],
    };
  });
  const nodes = toIR(network);
  const refsOn = (seg: string) =>
    (node(
      nodes,
      `ovnrouter:router-x|lrp:${endpointOn(network.allRouters[0]!, seg).name}`,
    ).data.serviceRefs ?? [])[0]!;
  const segA = Object.fromEntries(
    (refsOn("seg-a").routes ?? []).map((r) => [r.dst, r]),
  );
  const segB = Object.fromEntries(
    (refsOn("seg-b").routes ?? []).map((r) => [r.dst, r]),
  );
  // A route's OWN metric wins.
  assertEquals(segA["10.10.0.0/24"]!.metric, 900);
  // A route with none takes the ATTACHMENT's metric.
  assertEquals(segA["10.11.0.0/24"]!.metric, 700);
  // An inherited domain route ALSO takes the attachment's metric (300),
  // which outranks the domain's 500.
  assertEquals(segB["10.9.0.0/24"]!.metric, 300);
  assertEquals(segB["10.9.0.0/24"]!.via, "192.168.2.1");
});

// Neither the route, the attachment nor the domain declares a metric → the
// generator assigns one by execution order: the workload's FIRST NIC is
// lowest (most preferred), each subsequent +100 (2026-09-19).
Deno.test("route metrics fall back to execution order (first NIC lowest)", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    const domain = net.routingDomain("Dom", {
      routes: [{ dst: IPv4.parse("10.9.0.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "x";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => ({
          routingDomains: [domain],
          endpoints: [
            router.ovnRouterEndpoint((ep) => ({
              l2Segment: a,
              ipaddrs: [IPv4.parse("192.168.1.1/24")],
              ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("192.168.1.5/24")] }),
              ],
            })),
            router.ovnRouterEndpoint((ep) => ({
              l2Segment: b,
              ipaddrs: [IPv4.parse("192.168.2.1/24")],
              ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("192.168.2.5/24")] }),
              ],
            })),
          ],
        })),
      ],
    };
  });
  const nodes = toIR(network);
  const metricOn = (seg: string): number | undefined => {
    const lrp = node(
      nodes,
      `ovnrouter:router-x|lrp:${endpointOn(network.allRouters[0]!, seg).name}`,
    );
    const refs = lrp.data.serviceRefs ?? [];
    return (refs[0]!.routes ?? [])[0]!.metric;
  };
  // The routing domain now always carries an auto metric (400+), so
  // both NICs inherit it; the execution-order fallback is no longer used.
  assertEquals(metricOn("seg-a"), 400);
  assertEquals(metricOn("seg-b"), 400);
});

// A control-plane attachment with no resolver of its own is fed the
// platform's GLOBAL resolver — controlPlane.resolver, else the CoreDNS
// address. CoreDNS gets it too (harmless: its Corefile sets an explicit
// forwarder, so it never resolves via resolv.conf), and an explicit
// attachment.resolver wins (2026-09-19).
Deno.test("resolver: control-plane attachments inherit the platform resolver", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const controlPlane = net.collisionDomain("control-plane");
    const bb = net.collisionDomain("bb");
    const app = net.service("app", (s) => {
      s.image = "x";
    });
    const platform = net.createPlatform("plat", {
      controlPlane: {
        collisionDomain: controlPlane,
        hosts: [host],
        ipaddrs: [IPv4.parse("10.43.0.1/24")],
        ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
      },
      zones: [],
    });
    platform.addEgress({
      l2Segment: bb,
      ipaddrs: [IPv4.parse("172.16.0.1/16")],
    });
    platform.addService({ service: app });
    return { hosts: [host], routers: [] };
  });
  const refsOf = (seg: string) => {
    const router = network.allRouters.find((r) =>
      r.endpoints.some((e) => e.l2Segment.name === seg)
    )!;
    const lrp = node(
      toIR(network),
      `ovnrouter:${router.name}|lrp:${endpointOn(router, seg).name}`,
    );
    return lrp.data.serviceRefs ?? [];
  };
  const refs = refsOf("control-plane");
  const coredns = refs.find((r) =>
    r.service === "kernel.app.container:platform.dns"
  )!;
  const appRef = refs.find((r) => r.service === "kernel.app.container:app")!;
  const corednsAddr = coredns.ipaddrs![0]!.split("/")[0]!;
  // The app NIC is fed the CoreDNS address...
  assertEquals(appRef.resolver, { nameservers: [corednsAddr] });
  // ...and so is CoreDNS itself — harmless, its Corefile forwarder is what
  // it resolves with, not resolv.conf.
  assertEquals(coredns.resolver, { nameservers: [corednsAddr] });
  // The registry on the network carries the same global resolver.
  assertEquals(
    network.platformControlPlane?.resolver?.nameservers.map((n) => n.to_s()),
    [corednsAddr],
  );
});

// An explicit controlPlane.resolver overrides the CoreDNS default.
Deno.test("resolver: controlPlane.resolver overrides the CoreDNS default", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const controlPlane = net.collisionDomain("control-plane");
    const bb = net.collisionDomain("bb");
    const app = net.service("app", (s) => {
      s.image = "x";
    });
    const platform = net.createPlatform("plat", {
      controlPlane: {
        collisionDomain: controlPlane,
        hosts: [host],
        ipaddrs: [IPv4.parse("10.43.0.1/24")],
        ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
        resolver: { nameservers: [IPv4.parse("9.9.9.9")] },
      },
      zones: [],
    });
    platform.addEgress({
      l2Segment: bb,
      ipaddrs: [IPv4.parse("172.16.0.1/16")],
    });
    platform.addService({ service: app });
    return { hosts: [host], routers: [] };
  });
  const router = network.allRouters.find((r) =>
    r.endpoints.some((e) => e.l2Segment.name === "control-plane")
  )!;
  const lrp = node(
    toIR(network),
    `ovnrouter:${router.name}|lrp:${endpointOn(router, "control-plane").name}`,
  );
  const refs = lrp.data.serviceRefs ?? [];
  assertEquals(
    refs.find((r) => r.service === "kernel.app.container:app")!.resolver,
    { nameservers: ["9.9.9.9"] },
  );
});

// An attachment's own resolver wins over the control-plane default.
Deno.test("resolver: attachment resolver is emitted for a plain segment NIC", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    const svc = net.service("svc", (s) => {
      s.image = "x";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => ({
          routingDomains: [],
          endpoints: [
            router.ovnRouterEndpoint((ep) => ({
              l2Segment: a,
              ipaddrs: [IPv4.parse("192.168.1.1/24")],
              ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
              services: [
                ep.attachTo(svc, {
                  ipaddrs: [IPv4.parse("192.168.1.5/24")],
                  resolver: { nameservers: [IPv4.parse("8.8.8.8")] },
                }),
              ],
            })),
            router.ovnRouterEndpoint({
              l2Segment: b,
              ipaddrs: [IPv4.parse("192.168.2.1/24")],
            }),
          ],
        })),
      ],
    };
  });
  const network2 = network;
  const router = network2.allRouters[0]!;
  const lrp = node(
    toIR(network2),
    `ovnrouter:router-x|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = lrp.data.serviceRefs ?? [];
  assertEquals(refs[0]!.resolver, { nameservers: ["8.8.8.8"] });
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
            return {
              routingDomains: [],
              endpoints: [
                router.ovnRouterEndpoint((ep) => ({
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
                })),
                router.ovnRouterEndpoint({
                  l2Segment: b,
                  ipaddrs: [IPv4.parse("192.168.2.1/24")],
                }),
              ],
            };
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
        return {
          routingDomains: [],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: l2,
              ipaddrs: [IPv4.parse(v4)],
              ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
              services: [ep.attachTo(svc, { ipaddrs: [IPv4.parse(v4)] })],
            })),
            r.ovnRouterEndpoint({
              l2Segment: backbone,
              ipaddrs: [IPv4.parse("172.22.0.9/16")],
            }),
          ],
        };
      });
    return {
      hosts: [host],
      collisionDomains: [backbone],
      routers: [
        seg("router-home-v2", seg128, dns128, "192.168.128.5/24"),
        seg("router-management-v2", seg129, dns129, "192.168.129.5/24"),
        net.defineOvnRouter("router-control-plane-v2", (r) => {
          return {
            routingDomains: [],
            endpoints: [
              r.ovnRouterEndpoint((ep) => ({
                l2Segment: cp,
                ipaddrs: [IPv4.parse("10.43.0.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
                services: [
                  ep.attachTo(dns128, {
                    ipaddrs: [IPv4.parse("10.43.0.128/24")],
                  }),
                  ep.attachTo(dns129, {
                    ipaddrs: [IPv4.parse("10.43.0.129/24")],
                  }),
                ],
              })),
              r.ovnRouterEndpoint({
                l2Segment: backbone,
                ipaddrs: [IPv4.parse("172.22.0.2/16")],
              }),
            ],
          };
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
    node(
      nodes,
      `ovnrouter:router-home-v2|lrp:${
        endpointOn(
          network.allRouters.find((r) => r.name === "router-home-v2")!,
          "home-v2",
        ).name
      }`,
    ).data[
      "serviceRefs"
    ],
    [
      {
        service: "kernel.app.container:dns-128",
        name: "home-v2",
        mac: "00:00:c0:a8:80:05",
        ipaddrs: ["192.168.128.5/24"],
      },
    ],
  );
  const cpRefs = (node(
    nodes,
    `ovnrouter:router-control-plane-v2|lrp:${
      endpointOn(
        network.allRouters.find((r) => r.name === "router-control-plane-v2")!,
        "control-plane",
      ).name
    }`,
  )
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
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint((_ep) => ({
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
                services: [{ kind: "ipv6.slaac" }],
              })),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
        }),
      ],
    };
  });
  const firstRouter = network.allRouters[0];
  if (!firstRouter) throw new Error("expected one router");
  const left = endpointOn(firstRouter, "seg-a");
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
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint((ep) => ({
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth0" } }],
                services: [
                  ep.attachTo(dns, { ipaddrs: [IPv4.parse("192.168.1.5/24")] }),
                ],
              })),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
        }),
        net.defineOvnRouter("router-cp", (router) => {
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint((ep) => ({
                l2Segment: cp,
                ipaddrs: [IPv4.parse("10.43.0.1/24")],
                ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
                services: [
                  ep.attachTo(dns, {
                    ipaddrs: [IPv4.parse("10.43.0.5/24")],
                  }),
                ],
              })),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("172.22.0.2/16")],
              }),
            ],
          };
        }),
      ],
    };
  });
  assertEquals(
    dns.endpointRefs.map((r) => r.endpoint.l2Segment.name).sort(),
    ["control-plane", "seg-a"],
  );
  // Every attachment carries its endpoint (no per-attachment flags).
  assertEquals(
    dns.endpointRefs.every((r) => r.endpoint.l2Segment.name.length > 0),
    true,
  );
});

// ── endpoint identity (EndpointBase.name) ────────────────────────────
Deno.test("fnv1a64: canonical FNV-1a 64 test vectors, fixed 16-hex width", () => {
  assertEquals(fnv1a64(""), "cbf29ce484222325");
  assertEquals(fnv1a64("a"), "af63dc4c8601ec8c");
  assertEquals(fnv1a64("foobar"), "85944171f73967e8");
});

Deno.test("deriveEndpointName: stable, role- and domain-sensitive, explicit name wins", () => {
  const base = deriveEndpointName({}, "router-x", "ovn", "seg-a");
  // Deterministic: same inputs, same name.
  assertEquals(base, deriveEndpointName({}, "router-x", "ovn", "seg-a"));
  assertEquals(base.startsWith("lrp-"), true);
  // A different router, role or attachment domain is a different identity.
  assertNotEquals(base, deriveEndpointName({}, "router-y", "ovn", "seg-a"));
  assertNotEquals(base, deriveEndpointName({}, "router-x", "kernel", "seg-a"));
  assertNotEquals(base, deriveEndpointName({}, "router-x", "ovn", "seg-b"));
  // An explicit name overrides the derivation outright.
  assertEquals(
    deriveEndpointName({ name: "lrp-home" }, "router-x", "ovn", "seg-a"),
    "lrp-home",
  );
});

Deno.test("defineOvnRouter: each endpoint gets its own derived name", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint({
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
              }),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
        }),
      ],
    };
  });
  const r = network.allRouters[0];
  if (!r) throw new Error("expected one router");
  assertEquals(
    endpointOn(r, "seg-a").name,
    deriveEndpointName({}, "router-x", "ovn", "seg-a"),
  );
  assertEquals(
    endpointOn(r, "seg-b").name,
    deriveEndpointName({}, "router-x", "ovn", "seg-b"),
  );
  assertNotEquals(endpointOn(r, "seg-a").name, endpointOn(r, "seg-b").name);
});

Deno.test("defineOvnRouter: an explicit name overrides the derived name", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint({
                name: "lrp-home",
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
              }),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
        }),
      ],
    };
  });
  const explicit = network.allRouters[0];
  if (!explicit) throw new Error("expected one router");
  assertEquals(endpointOn(explicit, "seg-a").name, "lrp-home");
});

Deno.test("defineOvnRouter: two endpoints with the same explicit name are rejected", () => {
  assertThrows(
    () =>
      defineNetwork("test-net", (net) => {
        const host = net.localHost("chassis-1");
        const a = net.collisionDomain("seg-a");
        const b = net.collisionDomain("seg-b");
        return {
          hosts: [host],
          routers: [
            net.defineOvnRouter("router-x", (router) => {
              return {
                routingDomains: [],
                endpoints: [
                  router.ovnRouterEndpoint({
                    name: "dup",
                    l2Segment: a,
                    ipaddrs: [IPv4.parse("192.168.1.1/24")],
                  }),
                  router.ovnRouterEndpoint({
                    name: "dup",
                    l2Segment: b,
                    ipaddrs: [IPv4.parse("192.168.2.1/24")],
                  }),
                ],
              };
            }),
          ],
        };
      }),
    Error,
    "same name",
  );
});

Deno.test("defineOvnRouter: one explicit endpoint name across two routers is rejected", () => {
  assertThrows(
    () =>
      defineNetwork("test-net", (net) => {
        const host = net.localHost("chassis-1");
        const a = net.collisionDomain("seg-a");
        const b = net.collisionDomain("seg-b");
        const c = net.collisionDomain("seg-c");
        const d = net.collisionDomain("seg-d");
        return {
          hosts: [host],
          routers: [
            net.defineOvnRouter("router-a", (router) => ({
              routingDomains: [],
              endpoints: [
                router.ovnRouterEndpoint({
                  name: "lrp-shared",
                  l2Segment: a,
                  ipaddrs: [IPv4.parse("192.168.1.1/24")],
                }),
                router.ovnRouterEndpoint({
                  l2Segment: b,
                  ipaddrs: [IPv4.parse("192.168.2.1/24")],
                }),
              ],
            })),
            net.defineOvnRouter("router-b", (router) => ({
              routingDomains: [],
              endpoints: [
                router.ovnRouterEndpoint({
                  name: "lrp-shared",
                  l2Segment: c,
                  ipaddrs: [IPv4.parse("192.168.3.1/24")],
                }),
                router.ovnRouterEndpoint({
                  l2Segment: d,
                  ipaddrs: [IPv4.parse("192.168.4.1/24")],
                }),
              ],
            })),
          ],
        };
      }),
    Error,
    "unique across the whole network",
  );
});

// ── RoutingDomain.routes: calculated vs explicit override ────────────
// A domain with no explicit routes distributes what its participants
// actually RESOLVED (anchor + interconnect); a workload NIC on one of its
// segments inherits that, next-hopped at the segment's gateway.
Deno.test("routingDomain: calculated routes reach a workload via its segment gateway", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg = net.collisionDomain("seg-a");
    const bseg = net.collisionDomain("seg-b");
    const bb = net.collisionDomain("bb");
    const d = net.routingDomain("D");
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: seg,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
        // The anchor: router-b's segment endpoint declares the default, so
        // domain D resolves a default route for its participants.
        net.defineOvnRouter("router-b", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint({
              l2Segment: bseg,
              ipaddrs: [IPv4.parse("10.1.0.1/24")],
              routes: [
                {
                  dst: IPv4.parse("0.0.0.0/0"),
                  via: IPv4.parse("10.1.0.254"),
                },
              ],
            }),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.2/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters.find((r) => r.name === "router-a");
  if (!router) throw new Error("expected router-a");
  const lrp = node(
    toIR(network),
    `ovnrouter:router-a|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = (lrp.data as { serviceRefs?: { routes?: unknown }[] })
    .serviceRefs ?? [];
  const routes = refs[0]?.routes as { dst: string; via: string }[];
  // D's calculated set = the anchor's default + the interconnect peer
  // subnets; the on-link 10.0.0.0/24 is skipped. Everything is next-hopped
  // at the segment gateway.
  assertEquals(routes.map((r) => r.dst).sort(), ["0.0.0.0/0", "10.1.0.0/24"]);
  assertEquals(routes.every((r) => r.via === "10.0.0.1"), true);
});

Deno.test("routingDomain: explicit routes replace the calculated set", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg = net.collisionDomain("seg-a");
    const bb = net.collisionDomain("bb");
    const d = net.routingDomain("D", {
      routes: [{ dst: IPv4.parse("192.168.129.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: seg,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const lrp = node(
    toIR(network),
    `ovnrouter:router-a|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = (lrp.data as { serviceRefs?: { routes?: unknown }[] })
    .serviceRefs ?? [];
  // Exactly the declared prefix — no default alongside it.
  assertEquals(refs[0]?.routes, [
    { dst: "192.168.129.0/24", via: "10.0.0.1", metric: 400 },
  ]);
});

Deno.test("routingDomain: a dst the attachment is on-link for is skipped", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg = net.collisionDomain("seg-a");
    const bb = net.collisionDomain("bb");
    const d = net.routingDomain("D", {
      routes: [{ dst: IPv4.parse("10.0.0.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: seg,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const lrp = node(
    toIR(network),
    `ovnrouter:router-a|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = (lrp.data as { serviceRefs?: { routes?: unknown }[] })
    .serviceRefs ?? [];
  // On-link, so the connected route already covers it — nothing emitted.
  assertEquals(refs[0]?.routes, undefined);
});

Deno.test("workload routes: a prefix reached from two NICs stays on both (per-NIC tables)", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const segA = net.collisionDomain("seg-a");
    const segB = net.collisionDomain("seg-b");
    // Both domains distribute the same destination — e.g. ControlPlane-Route
    // and Zerotier-route both reaching the management subnet. The
    // deployer puts each NIC's routes in their OWN table (selected by a
    // source rule), so both are kept rather than one being dropped.
    const d1 = net.routingDomain("D1", {
      routes: [{ dst: IPv4.parse("10.5.0.0/24") }],
    });
    const d2 = net.routingDomain("D2", {
      routes: [{ dst: IPv4.parse("10.5.0.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d1, d2],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: segA,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: segB,
              ipaddrs: [IPv4.parse("10.1.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 11 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.1.0.9/24")] }),
              ],
            })),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const nodes = toIR(network);
  const withRoute = router.endpoints.filter((ep) => {
    const lrp = node(nodes, `ovnrouter:router-a|lrp:${ep.name}`);
    const refs = (lrp.data as {
      serviceRefs?: { routes?: { dst: string }[] }[];
    }).serviceRefs ?? [];
    return (refs[0]?.routes ?? []).some((r) => r.dst === "10.5.0.0/24");
  });
  assertEquals(withRoute.length, 2);
});

// ── platform builders (dnsProvider real; certIssuer/createPlatform stubs) ──
Deno.test("platform: dnsProvider is a plain object, unique by name", () => {
  let provider: DnsProvider | undefined;
  defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    provider = net.dnsProvider("cloudflare", {
      kind: "cloudflare",
      token: "cf-token",
    });
    assertThrows(
      () =>
        net.dnsProvider("cloudflare", { kind: "cloudflare", token: "other" }),
      Error,
      "declared more than once",
    );
    return { hosts: [host], routers: [] };
  });
  assertEquals(provider?.name, "cloudflare");
  assertEquals(provider?.spec, { kind: "cloudflare", token: "cf-token" });
});

Deno.test("platform: createPlatform is a dummy carrying ingressService + publish", () => {
  let platform: Platform | undefined;
  defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const controlPlane = net.collisionDomain("control-plane");
    platform = net.createPlatform("mam-hh-platform", {
      controlPlane: { collisionDomain: controlPlane, hosts: [host] },
      zones: [],
    });
    return { hosts: [host], routers: [] };
  });
  assertEquals(platform?.name, "mam-hh-platform");
  assertEquals(platform?.ingressService.name, "mam-hh-platform-ingress");
  const published = platform?.publish({
    host: "ui.adviser.com",
    port: "webui",
  });
  assertEquals(published?.host, "ui.adviser.com");
  assertEquals(published?.platform, platform);
});

Deno.test("platform: attachTo(ingressService) injects endpoint + service into publish", () => {
  let platform: Platform | undefined;
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const management = net.collisionDomain("management-v2");
    const bb = net.collisionDomain("bb");
    const controlPlane = net.collisionDomain("control-plane");
    platform = net.createPlatform("plat", {
      controlPlane: { collisionDomain: controlPlane, hosts: [host] },
      zones: [],
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-management-v2", (r) => ({
          routingDomains: [],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: management,
              ipaddrs: [IPv4.parse("192.168.129.1/24")],
              services: [
                ep.attachTo(platform!.ingressService, {
                  ipaddrs: [IPv4.parse("192.168.129.33/24")],
                  publish: [
                    platform!.publish({
                      host: "ui.adviser.com",
                      port: "webui",
                    }),
                  ],
                }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const lrp = endpointOn(router, "management-v2");
  const attach = (lrp.services ?? []).find((s) => s.kind === "service.attach");
  const publish = (attach as { publish?: readonly Publish[] } | undefined)
    ?.publish;
  assertEquals(publish?.[0]?.host, "ui.adviser.com");
  // The injected endpoint is the pre-`services` base (same object
  // EndpointService.endpoint carries), so compare by identity-of-name.
  assertEquals(publish?.[0]?.endpoint?.name, lrp.name);
  assertEquals(publish?.[0]?.service?.name, "plat-ingress");
  // The reusable service records the attachment back-reference.
  assertEquals(platform?.ingressService.endpointRefs.length, 1);
});

// ── endpoint identity (EndpointBase.name) ────────────────────────────
Deno.test("fnv1a64: canonical FNV-1a 64 test vectors, fixed 16-hex width", () => {
  assertEquals(fnv1a64(""), "cbf29ce484222325");
  assertEquals(fnv1a64("a"), "af63dc4c8601ec8c");
  assertEquals(fnv1a64("foobar"), "85944171f73967e8");
});

Deno.test("deriveEndpointName: stable, role- and domain-sensitive, explicit name wins", () => {
  const base = deriveEndpointName({}, "router-x", "ovn", "seg-a");
  // Deterministic: same inputs, same name.
  assertEquals(base, deriveEndpointName({}, "router-x", "ovn", "seg-a"));
  assertEquals(base.startsWith("lrp-"), true);
  // A different router, role or attachment domain is a different identity.
  assertNotEquals(base, deriveEndpointName({}, "router-y", "ovn", "seg-a"));
  assertNotEquals(base, deriveEndpointName({}, "router-x", "kernel", "seg-a"));
  assertNotEquals(base, deriveEndpointName({}, "router-x", "ovn", "seg-b"));
  // An explicit name overrides the derivation outright.
  assertEquals(
    deriveEndpointName({ name: "lrp-home" }, "router-x", "ovn", "seg-a"),
    "lrp-home",
  );
});

Deno.test("defineOvnRouter: each endpoint gets its own derived name", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint({
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
              }),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
        }),
      ],
    };
  });
  const r = network.allRouters[0];
  if (!r) throw new Error("expected one router");
  assertEquals(
    endpointOn(r, "seg-a").name,
    deriveEndpointName({}, "router-x", "ovn", "seg-a"),
  );
  assertEquals(
    endpointOn(r, "seg-b").name,
    deriveEndpointName({}, "router-x", "ovn", "seg-b"),
  );
  assertNotEquals(endpointOn(r, "seg-a").name, endpointOn(r, "seg-b").name);
});

Deno.test("defineOvnRouter: an explicit name overrides the derived name", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-x", (router) => {
          return {
            routingDomains: [],
            endpoints: [
              router.ovnRouterEndpoint({
                name: "lrp-home",
                l2Segment: a,
                ipaddrs: [IPv4.parse("192.168.1.1/24")],
              }),
              router.ovnRouterEndpoint({
                l2Segment: b,
                ipaddrs: [IPv4.parse("192.168.2.1/24")],
              }),
            ],
          };
        }),
      ],
    };
  });
  const explicit = network.allRouters[0];
  if (!explicit) throw new Error("expected one router");
  assertEquals(endpointOn(explicit, "seg-a").name, "lrp-home");
});

Deno.test("defineOvnRouter: two endpoints with the same explicit name are rejected", () => {
  assertThrows(
    () =>
      defineNetwork("test-net", (net) => {
        const host = net.localHost("chassis-1");
        const a = net.collisionDomain("seg-a");
        const b = net.collisionDomain("seg-b");
        return {
          hosts: [host],
          routers: [
            net.defineOvnRouter("router-x", (router) => {
              return {
                routingDomains: [],
                endpoints: [
                  router.ovnRouterEndpoint({
                    name: "dup",
                    l2Segment: a,
                    ipaddrs: [IPv4.parse("192.168.1.1/24")],
                  }),
                  router.ovnRouterEndpoint({
                    name: "dup",
                    l2Segment: b,
                    ipaddrs: [IPv4.parse("192.168.2.1/24")],
                  }),
                ],
              };
            }),
          ],
        };
      }),
    Error,
    "same name",
  );
});

Deno.test("defineOvnRouter: one explicit endpoint name across two routers is rejected", () => {
  assertThrows(
    () =>
      defineNetwork("test-net", (net) => {
        const host = net.localHost("chassis-1");
        const a = net.collisionDomain("seg-a");
        const b = net.collisionDomain("seg-b");
        const c = net.collisionDomain("seg-c");
        const d = net.collisionDomain("seg-d");
        return {
          hosts: [host],
          routers: [
            net.defineOvnRouter("router-a", (router) => ({
              routingDomains: [],
              endpoints: [
                router.ovnRouterEndpoint({
                  name: "lrp-shared",
                  l2Segment: a,
                  ipaddrs: [IPv4.parse("192.168.1.1/24")],
                }),
                router.ovnRouterEndpoint({
                  l2Segment: b,
                  ipaddrs: [IPv4.parse("192.168.2.1/24")],
                }),
              ],
            })),
            net.defineOvnRouter("router-b", (router) => ({
              routingDomains: [],
              endpoints: [
                router.ovnRouterEndpoint({
                  name: "lrp-shared",
                  l2Segment: c,
                  ipaddrs: [IPv4.parse("192.168.3.1/24")],
                }),
                router.ovnRouterEndpoint({
                  l2Segment: d,
                  ipaddrs: [IPv4.parse("192.168.4.1/24")],
                }),
              ],
            })),
          ],
        };
      }),
    Error,
    "unique across the whole network",
  );
});

// ── RoutingDomain.routes: calculated vs explicit override ────────────
// A domain with no explicit routes distributes what its participants
// actually RESOLVED (anchor + interconnect); a workload NIC on one of its
// segments inherits that, next-hopped at the segment's gateway.
Deno.test("routingDomain: calculated routes reach a workload via its segment gateway", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg = net.collisionDomain("seg-a");
    const bseg = net.collisionDomain("seg-b");
    const bb = net.collisionDomain("bb");
    const d = net.routingDomain("D");
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: seg,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
        // The anchor: router-b's segment endpoint declares the default, so
        // domain D resolves a default route for its participants.
        net.defineOvnRouter("router-b", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint({
              l2Segment: bseg,
              ipaddrs: [IPv4.parse("10.1.0.1/24")],
              routes: [
                {
                  dst: IPv4.parse("0.0.0.0/0"),
                  via: IPv4.parse("10.1.0.254"),
                },
              ],
            }),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.2/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters.find((r) => r.name === "router-a");
  if (!router) throw new Error("expected router-a");
  const lrp = node(
    toIR(network),
    `ovnrouter:router-a|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = (lrp.data as { serviceRefs?: { routes?: unknown }[] })
    .serviceRefs ?? [];
  const routes = refs[0]?.routes as { dst: string; via: string }[];
  // D's calculated set = the anchor's default + the interconnect peer
  // subnets; the on-link 10.0.0.0/24 is skipped. Everything is next-hopped
  // at the segment gateway.
  assertEquals(routes.map((r) => r.dst).sort(), ["0.0.0.0/0", "10.1.0.0/24"]);
  assertEquals(routes.every((r) => r.via === "10.0.0.1"), true);
});

Deno.test("routingDomain: explicit routes replace the calculated set", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg = net.collisionDomain("seg-a");
    const bb = net.collisionDomain("bb");
    const d = net.routingDomain("D", {
      routes: [{ dst: IPv4.parse("192.168.129.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: seg,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const lrp = node(
    toIR(network),
    `ovnrouter:router-a|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = (lrp.data as { serviceRefs?: { routes?: unknown }[] })
    .serviceRefs ?? [];
  // Exactly the declared prefix — no default alongside it.
  assertEquals(refs[0]?.routes, [
    { dst: "192.168.129.0/24", via: "10.0.0.1", metric: 400 },
  ]);
});

Deno.test("routingDomain: a dst the attachment is on-link for is skipped", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const seg = net.collisionDomain("seg-a");
    const bb = net.collisionDomain("bb");
    const d = net.routingDomain("D", {
      routes: [{ dst: IPv4.parse("10.0.0.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: seg,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const lrp = node(
    toIR(network),
    `ovnrouter:router-a|lrp:${endpointOn(router, "seg-a").name}`,
  );
  const refs = (lrp.data as { serviceRefs?: { routes?: unknown }[] })
    .serviceRefs ?? [];
  // On-link, so the connected route already covers it — nothing emitted.
  assertEquals(refs[0]?.routes, undefined);
});

Deno.test("workload routes: a prefix reached from two NICs stays on both (per-NIC tables)", () => {
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const segA = net.collisionDomain("seg-a");
    const segB = net.collisionDomain("seg-b");
    // Both domains distribute the same destination — e.g. ControlPlane-Route
    // and Zerotier-route both reaching the management subnet. The
    // deployer puts each NIC's routes in their OWN table (selected by a
    // source rule), so both are kept rather than one being dropped.
    const d1 = net.routingDomain("D1", {
      routes: [{ dst: IPv4.parse("10.5.0.0/24") }],
    });
    const d2 = net.routingDomain("D2", {
      routes: [{ dst: IPv4.parse("10.5.0.0/24") }],
    });
    const svc = net.service("svc", (s) => {
      s.image = "img";
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-a", (r) => ({
          routingDomains: [d1, d2],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: segA,
              ipaddrs: [IPv4.parse("10.0.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 10 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.0.0.9/24")] }),
              ],
            })),
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: segB,
              ipaddrs: [IPv4.parse("10.1.0.1/24")],
              ifaces: [
                {
                  host,
                  iface: { kind: "vlan", vlanParent: "eth0", vlanId: 11 },
                },
              ],
              services: [
                ep.attachTo(svc, { ipaddrs: [IPv4.parse("10.1.0.9/24")] }),
              ],
            })),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const nodes = toIR(network);
  const withRoute = router.endpoints.filter((ep) => {
    const lrp = node(nodes, `ovnrouter:router-a|lrp:${ep.name}`);
    const refs = (lrp.data as {
      serviceRefs?: { routes?: { dst: string }[] }[];
    }).serviceRefs ?? [];
    return (refs[0]?.routes ?? []).some((r) => r.dst === "10.5.0.0/24");
  });
  assertEquals(withRoute.length, 2);
});

// ── platform builders (dnsProvider real; certIssuer/createPlatform stubs) ──
Deno.test("platform: dnsProvider is a plain object, unique by name", () => {
  let provider: DnsProvider | undefined;
  defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    provider = net.dnsProvider("cloudflare", {
      kind: "cloudflare",
      token: "cf-token",
    });
    assertThrows(
      () =>
        net.dnsProvider("cloudflare", { kind: "cloudflare", token: "other" }),
      Error,
      "declared more than once",
    );
    return { hosts: [host], routers: [] };
  });
  assertEquals(provider?.name, "cloudflare");
  assertEquals(provider?.spec, { kind: "cloudflare", token: "cf-token" });
});

Deno.test("platform: createPlatform is a dummy carrying ingressService + publish", () => {
  let platform: Platform | undefined;
  defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const controlPlane = net.collisionDomain("control-plane");
    platform = net.createPlatform("mam-hh-platform", {
      controlPlane: { collisionDomain: controlPlane, hosts: [host] },
      zones: [],
    });
    return { hosts: [host], routers: [] };
  });
  assertEquals(platform?.name, "mam-hh-platform");
  assertEquals(platform?.ingressService.name, "mam-hh-platform-ingress");
  const published = platform?.publish({
    host: "ui.adviser.com",
    port: "webui",
  });
  assertEquals(published?.host, "ui.adviser.com");
  assertEquals(published?.platform, platform);
});

Deno.test("platform: attachTo(ingressService) injects endpoint + service into publish", () => {
  let platform: Platform | undefined;
  const network = defineNetwork("test-net", (net) => {
    const host = net.localHost("chassis-1");
    const management = net.collisionDomain("management-v2");
    const bb = net.collisionDomain("bb");
    const controlPlane = net.collisionDomain("control-plane");
    platform = net.createPlatform("plat", {
      controlPlane: { collisionDomain: controlPlane, hosts: [host] },
      zones: [],
    });
    return {
      hosts: [host],
      routers: [
        net.defineOvnRouter("router-management-v2", (r) => ({
          routingDomains: [],
          endpoints: [
            r.ovnRouterEndpoint((ep) => ({
              l2Segment: management,
              ipaddrs: [IPv4.parse("192.168.129.1/24")],
              services: [
                ep.attachTo(platform!.ingressService, {
                  ipaddrs: [IPv4.parse("192.168.129.33/24")],
                  publish: [
                    platform!.publish({
                      host: "ui.adviser.com",
                      port: "webui",
                    }),
                  ],
                }),
              ],
            })),
            r.ovnRouterEndpoint({
              l2Segment: bb,
              ipaddrs: [IPv4.parse("172.16.0.1/16")],
            }),
          ],
        })),
      ],
    };
  });
  const router = network.allRouters[0];
  if (!router) throw new Error("expected one router");
  const lrp = endpointOn(router, "management-v2");
  const attach = (lrp.services ?? []).find((s) => s.kind === "service.attach");
  const publish = (attach as { publish?: readonly Publish[] } | undefined)
    ?.publish;
  assertEquals(publish?.[0]?.host, "ui.adviser.com");
  // The injected endpoint is the pre-`services` base (same object
  // EndpointService.endpoint carries), so compare by identity-of-name.
  assertEquals(publish?.[0]?.endpoint?.name, lrp.name);
  assertEquals(publish?.[0]?.service?.name, "plat-ingress");
  // The reusable service records the attachment back-reference.
  assertEquals(platform?.ingressService.endpointRefs.length, 1);
});

// A kernel device's MAC must be the SAME on every recreation, or an OVN
// router keeps a MAC_Binding for the device that used to hold the address
// (2026-10-11). The first IPv4 folds exactly like an LRP's MAC; a device
// with no IPv4 gets a locally-administered MAC hashed from its name.
Deno.test("macFromAddrsOrName: first IPv4 wins, else a stable hash of the name", () => {
  assertEquals(
    macFromAddrsOrName(
      [IPv6.parse("fd00::10:12:80:f/124"), IPv4.parse("10.12.80.14/28")],
      "veth-krn-router-wan",
    ),
    "00:00:0a:0c:50:0e",
  );
  const v6Only = [IPv6.parse("fd00::10:12:80:f/124")];
  const hashed = macFromAddrsOrName(v6Only, "veth-krn-router-wan");
  assertEquals(/^02(:[0-9a-f]{2}){5}$/.test(hashed), true);
  assertEquals(macFromAddrsOrName(v6Only, "veth-krn-router-wan"), hashed);
  assertEquals(macFromAddrsOrName([], "veth-krn-router-wan"), hashed);
  assertNotEquals(macFromAddrsOrName(v6Only, "veth-krn-router-lan"), hashed);
});

// net.defineOvnRouter()'s `options` (OvnRouterOptions, types.ts): whatever
// is unset — the whole object or a single field — is defaulted at
// declaration time, and the resolved value reaches the IR as the router's
// own `ovn.lr` node, carrying the FINAL Logical_Router.options smap.
function routerWithOptions(options?: { macBindingAgeThreshold?: number }) {
  return defineNetwork("test-net", (net) => {
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      routers: [
        net.defineOvnRouter("router-x", (router) => ({
          routingDomains: [],
          endpoints: [
            router.ovnRouterEndpoint({
              l2Segment: a,
              ipaddrs: [IPv4.parse("192.168.1.1/24")],
            }),
            router.ovnRouterEndpoint({
              l2Segment: b,
              ipaddrs: [IPv4.parse("192.168.2.1/24")],
            }),
          ],
          ...(options ? { options } : {}),
        })),
      ],
    };
  });
}

Deno.test("defineOvnRouter options: omitted -> macBindingAgeThreshold defaults to 300", () => {
  const network = routerWithOptions();
  assertEquals(network.allRouters[0]!.options, { macBindingAgeThreshold: 300 });
  assertEquals(node(toIR(network), "ovnrouter:router-x"), {
    id: "ovnrouter:router-x",
    kind: "ovn.lr",
    key: { name: "router-x" },
    data: { options: { mac_binding_age_threshold: 300 } },
  });
});

Deno.test("defineOvnRouter options: empty object still defaults, explicit value wins (0 too)", () => {
  assertEquals(routerWithOptions({}).allRouters[0]!.options, {
    macBindingAgeThreshold: 300,
  });
  assertEquals(
    node(
      toIR(routerWithOptions({ macBindingAgeThreshold: 60 })),
      "ovnrouter:router-x",
    )
      .data["options"],
    { mac_binding_age_threshold: 60 },
  );
  // 0 is OVN's own "never age" — an explicit choice, not "unset".
  assertEquals(
    node(
      toIR(routerWithOptions({ macBindingAgeThreshold: 0 })),
      "ovnrouter:router-x",
    )
      .data["options"],
    { mac_binding_age_threshold: 0 },
  );
});

Deno.test("defineOvnRouter options: the declarative object form takes them too", () => {
  const network = defineNetwork("test-net", (net) => {
    const a = net.collisionDomain("seg-a");
    const b = net.collisionDomain("seg-b");
    return {
      routers: [
        net.defineOvnRouter("router-x", {
          routingDomains: [],
          endpoints: [
            {
              kind: "ovn",
              l2Segment: a,
              ipaddrs: [IPv4.parse("192.168.1.1/24")],
            },
            {
              kind: "ovn",
              l2Segment: b,
              ipaddrs: [IPv4.parse("192.168.2.1/24")],
            },
          ],
          options: { macBindingAgeThreshold: 120 },
        }),
      ],
    };
  });
  assertEquals(network.allRouters[0]!.options, { macBindingAgeThreshold: 120 });
});

Deno.test("defineOvnRouter options: a value OVN would reject throws at declaration", () => {
  assertThrows(
    () => routerWithOptions({ macBindingAgeThreshold: -1 }),
    Error,
    "macBindingAgeThreshold",
  );
  assertThrows(
    () => routerWithOptions({ macBindingAgeThreshold: 1.5 }),
    Error,
    "macBindingAgeThreshold",
  );
});
