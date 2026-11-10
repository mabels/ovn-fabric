import { assertEquals } from "jsr:@std/assert@1";
import { IPv4 } from "./ip.ts";
import { defineNetwork } from "./define.ts";
import { buildGraph } from "./graph.ts";
import { transitNetwork } from "./mod.ts";

function network() {
  return defineNetwork("test-net", (net) => {
    const host = net.localHost({ name: "chassis-1" });
    const backbone = net.collisionDomain("backbone");
    const domain = net.routingDomain("mesh");
    const home = net.defineOvnRouter("router-home", (router) => ({
      routingDomains: [domain],
      endpoints: [
        router.ovnRouterEndpoint((ep) => ({
          l2Segment: net.collisionDomain("home"),
          ipaddrs: [IPv4.parse("192.168.128.1/24")],
          ifaces: [{ host, iface: { kind: "physical", name: "eth1" } }],
          services: [
            ep.attachTo(
              net.service("dns", { image: "x", env: { TOKEN: "env-secret" } }),
              { ipaddrs: [IPv4.parse("192.168.128.5/24")] },
            ),
          ],
        })),
        router.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.128/16")],
        }),
      ],
    }));
    const tunnel = net.defineOvnRouter("router-wg", (router) => ({
      routingDomains: [],
      endpoints: [
        router.tunnelRouterEndpoint({
          routingDomains: [domain],
          host,
          transit: transitNetwork(IPv4.parse("10.12.83.1/28")),
          upstream: transitNetwork(IPv4.parse("10.12.84.1/28")),
          services: [{
            kind: "wireguard",
            ifaceName: "wg0",
            config: {
              privateKey: "wg-secret-key",
              address: "10.64.0.2/32",
              peer: {
                publicKey: "wg-peer-key",
                endpoint: "203.0.113.1:51820",
                allowedIps: "0.0.0.0/0",
              },
            },
          }],
          routes: [{ dst: IPv4.parse("0.0.0.0/0") }],
          upstreamBackbone: {
            l2Segment: backbone,
            ipaddrs: [IPv4.parse("172.22.0.151/16")],
          },
        }),
        router.ovnRouterEndpoint({
          l2Segment: backbone,
          ipaddrs: [IPv4.parse("172.22.0.141/16")],
        }),
      ],
    }));
    return { hosts: [host], routers: [home, tunnel] };
  });
}

Deno.test("graph: no key, identity or env reaches it", () => {
  const text = JSON.stringify(buildGraph(network()));
  for (
    const secret of [
      "wg-secret-key",
      "wg-peer-key",
      "env-secret",
      "203.0.113.1",
    ]
  ) {
    assertEquals(text.includes(secret), false, secret);
  }
});

Deno.test("graph: routers carry their legs and routes, segments link them", () => {
  const graph = buildGraph(network());
  const home = graph.nodes.find((n) => n.id === "router:router-home")!;
  assertEquals(home.legs.map((l) => [l.on, l.addrs]).sort(), [
    ["backbone", ["172.22.0.128/16"]],
    ["home", ["192.168.128.1/24"]],
  ]);
  assertEquals(
    home.routes.some((r) => r.dst === "0.0.0.0/0" && r.via === "172.22.0.141"),
    true,
  );
  const edges = graph.edges.map((e) => `${e.source} ${e.target}`);
  assertEquals(edges.includes("router:router-home segment:home"), true);
  assertEquals(edges.includes("workload:dns segment:home"), true);
  // The tunnel's netns: its left on the transit, its right on the backdoor.
  const kernel = graph.nodes.find((n) => n.kind === "netns")!;
  assertEquals(kernel.legs.length, 2);
  assertEquals(
    kernel.routes.some((r) => r.dst === "0.0.0.0/0" && r.via === "10.12.84.1"),
    true,
  );
});

Deno.test("graph: hosts, apps, rules and boxes", () => {
  const graph = buildGraph(network());
  const ids = graph.nodes.map((n) => n.id);
  assertEquals(ids.includes("host:chassis-1"), true);
  const app = graph.nodes.find((n) => n.kind === "app")!;
  assertEquals(app.name, "wg-quick wg0");
  // The tunnel: its router, netns, transit segments and -upstream, one box.
  const netns = graph.nodes.find((n) => n.kind === "netns")!;
  const tunnelBox = graph.nodes.filter((n) => n.group === netns.group)
    .map((n) => n.id).sort();
  assertEquals(tunnelBox.includes("router:router-wg"), true);
  assertEquals(tunnelBox.includes("router:router-wg-upstream"), true);
  assertEquals(tunnelBox.includes(app.id), true);
  // A segment only its router is on is in its box; the backbone in none.
  const home = graph.nodes.find((n) => n.id === "router:router-home")!;
  assertEquals(
    graph.nodes.find((n) => n.id === "segment:home")!.group,
    home.group,
  );
  assertEquals(
    graph.nodes.find((n) => n.id === "workload:dns")!.group,
    home.group,
  );
  assertEquals(
    graph.nodes.find((n) => n.id === "segment:backbone")!.group,
    undefined,
  );
  assertEquals(
    graph.nodes.find((n) => n.id === "host:chassis-1")!.group,
    undefined,
  );
});
