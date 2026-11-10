import { assertEquals } from "jsr:@std/assert@1";
import { defineNetwork } from "./define.ts";
import { IPv4, IPv6 } from "./ip.ts";
import { toIR } from "./ir.ts";
import { transitNetwork } from "./mod.ts";

function network() {
  return defineNetwork("test-net", (net) => {
    const host = net.localHost({ name: "chassis-1" });
    const vlan = (vlanId: number) =>
      ({
        kind: "vlan",
        vlanParent: { kind: "physical", name: "eth0" },
        vlanId,
      }) as const;
    net.internet({ core: { ipv4: IPv4.parse("172.21.0.0/24") } });
    const avm = net.wan("avm", () => ({
      host,
      iface: vlan(2280),
      ipaddrs: [IPv4.parse("192.168.132.93/24")],
      routes: [{
        dst: IPv4.parse("0.0.0.0/0"),
        via: IPv4.parse("192.168.132.1"),
      }],
      svc: transitNetwork(IPv4.parse("10.12.80.1/28")),
    }));
    const lan = (
      tenant: { collisionDomain(n: string): unknown },
      addr: string,
      id: number,
    ) => ({
      // deno-lint-ignore no-explicit-any
      l2Segment: tenant.collisionDomain("lan") as any,
      ipaddrs: [IPv4.parse(addr)],
      ifaces: [{ host, iface: vlan(id) }],
    });
    const home = net.tenant("home", (tenant) => ({
      wans: [avm],
      core: { ipv4: IPv4.parse("172.22.0.0/24") },
      routers: [
        tenant.defineOvnRouter("main", (router) => ({
          endpoints: [
            router.ovnRouterEndpoint(lan(tenant, "192.168.128.1/24", 1128)),
            router.ovnRouterEndpoint({
              l2Segment: tenant.collisionDomain("dmz"),
              ipaddrs: [IPv4.parse("192.168.129.1/24")],
              ifaces: [{ host, iface: vlan(1129) }],
            }),
          ],
        })),
      ],
    }));
    const usa = net.tenant("usa", (tenant) => ({
      core: { ipv4: IPv4.parse("172.22.2.0/24") },
      routers: [
        tenant.defineOvnRouter("lan", (router) => ({
          endpoints: [
            router.ovnRouterEndpoint(lan(tenant, "192.168.131.1/24", 1131)),
          ],
        })),
        tenant.vpnGateway("mullvad", {
          kind: "wireguard",
          host,
          default: true,
          config: {
            privateKey: "k",
            address: "10.64.0.2/32",
            peer: {
              publicKey: "p",
              endpoint: "203.0.113.1:51820",
              allowedIps: "0.0.0.0/0",
            },
          },
          svc: {
            core: transitNetwork(
              IPv4.parse("10.12.83.1/28"),
              IPv6.parse("fd00::10:12:83:1/124"),
            ),
            nat: transitNetwork(
              IPv4.parse("10.12.84.1/28"),
              IPv6.parse("fd00::10:12:84:1/124"),
            ),
          },
          wans: [avm],
        }),
      ],
    }));
    return { hosts: [host], tenants: [home, usa], wans: [avm] };
  });
}

function routes(
  nodes: Record<string, unknown>,
  router: string,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [id, node] of Object.entries(nodes)) {
    if (!id.startsWith(`ovnrouter:${router}|route:`)) continue;
    const n = node as { key: { prefix: string }; data: { nexthop: string } };
    out[n.key.prefix] = n.data.nexthop;
  }
  return out;
}

Deno.test("tenant: the parts are named <scope>-<code>[-<name>]", () => {
  const net = network();
  assertEquals(net.allRouters.map((r) => r.name).sort(), [
    "home-nat",
    "home-rt-main",
    "internet-wan-avm",
    "usa-nat-mullvad",
    "usa-rt-lan",
    "usa-vpn-mullvad",
  ]);
  const domains = net.allCollisionDomains.map((d) => d.name);
  for (
    const name of [
      "internet-core",
      "internet-svc-avm-core",
      "home-core",
      "home-cable-lan",
      "home-cable-dmz",
      "usa-core",
      "usa-svc-mullvad-core",
      "usa-svc-mullvad-nat",
    ]
  ) {
    assertEquals(domains.includes(name), true, name);
  }
});

Deno.test("tenant: out through its NAT gateway, which masquerades its cables", () => {
  const nodes = toIR(network());
  assertEquals(routes(nodes, "home-rt-main")["0.0.0.0/0"], "172.22.0.2"); // home-nat
  assertEquals(routes(nodes, "home-nat")["0.0.0.0/0"], "172.21.0.1"); // the wan
  const snat = Object.values(nodes).flatMap((n) => {
    const node = n as {
      kind: string;
      data: { rules?: { kind: string; logicalIps?: string[] }[] };
    };
    return node.kind === "security.group" ? node.data.rules ?? [] : [];
  }).filter((r) =>
    r.kind === "snat" && r.logicalIps?.some((ip) => ip.startsWith("192.168.12"))
  );
  assertEquals([...(snat[0]?.logicalIps ?? [])].sort(), [
    "192.168.128.0/24",
    "192.168.129.0/24",
  ]);
});

Deno.test("tenant: leaves through its first active wan", () => {
  const net = defineNetwork("test-net", (net) => {
    const host = net.localHost({ name: "chassis-1" });
    const vlan = (vlanId: number) =>
      ({
        kind: "vlan",
        vlanParent: { kind: "physical", name: "eth0" },
        vlanId,
      }) as const;
    net.internet({ core: { ipv4: IPv4.parse("172.21.0.0/24") } });
    const starlink = net.wan("starlink", () => ({
      host,
      iface: vlan(2282),
      addressing: "dhcp",
      active: false,
    }));
    const avm = net.wan("avm", () => ({
      host,
      iface: vlan(2280),
      ipaddrs: [IPv4.parse("192.168.132.93/24")],
      routes: [{
        dst: IPv4.parse("0.0.0.0/0"),
        via: IPv4.parse("192.168.132.1"),
      }],
      svc: transitNetwork(IPv4.parse("10.12.80.1/28")),
    }));
    const neighbor = net.tenant("neighbor", (tenant) => ({
      wans: [starlink, avm],
      core: { ipv4: IPv4.parse("172.22.1.0/24") },
      routers: [
        tenant.defineOvnRouter("lan", (router) => ({
          endpoints: [router.ovnRouterEndpoint({
            l2Segment: tenant.collisionDomain("lan"),
            ipaddrs: [IPv4.parse("192.168.130.1/24")],
            ifaces: [{ host, iface: vlan(1130) }],
          })],
        })),
      ],
    }));
    return { hosts: [host], tenants: [neighbor], wans: [starlink, avm] };
  });
  // starlink is 172.21.0.1, avm 172.21.0.2: the inactive one is skipped.
  assertEquals(routes(toIR(net), "neighbor-nat")["0.0.0.0/0"], "172.21.0.2");
});

Deno.test("tenant: no route leads into another tenant or onto internet-core", () => {
  const nodes = toIR(network());
  for (
    const router of [
      "home-rt-main",
      "home-nat",
      "usa-rt-lan",
      "usa-vpn-mullvad",
    ]
  ) {
    const own = router.startsWith("home")
      ? ["192.168.128", "192.168.129"]
      : ["192.168.131"];
    for (const dst of Object.keys(routes(nodes, router))) {
      const foreign = dst.startsWith("192.168.") &&
        !own.some((p) => dst.startsWith(p));
      assertEquals(foreign, false, `${router}: ${dst}`);
      assertEquals(dst.startsWith("172.21."), false, `${router}: ${dst}`);
    }
  }
  // usa has no NAT gateway: out only into the tunnel.
  assertEquals(routes(nodes, "usa-rt-lan")["0.0.0.0/0"], "172.22.2.2");
});
