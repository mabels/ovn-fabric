// topology.cluster-tenants.ts — a complete, living example: the topology of a
// real site, run in production and by the smoke test in CI. One chassis
// carries:
//
//   - three wans: a cable box, a cable modem and a wireless link
//   - tenant home: lan and dmz, reachable through ZeroTier and Tailscale
//   - tenant neighbor: one lan, out through the wans
//   - tenant usa: one lan, out only through a WireGuard VPN
//   - a platform: the control plane with CoreDNS, reachable from home's dmz
//
// Names on the host follow ADR 0009, <scope>-<code>[-<name>] with the codes
// core cable rt nat vpn svc wan; a `// → name` comment shows what a part is
// called there. Addresses follow ADR 0011: a router's own address is pinned
// (`.host(1)`), a leg on a core is allocated by the runtime.

import { defineNetwork, runtime, transitNetwork } from "../src/mod.ts";
import { type SecretsModule, transitBlocks } from "../src/secret-types.ts";
import * as exampleSecrets from "./topology.cluster-tenants.secrets.ts";

// Everything that is the site — keys, names, addresses, which interface
// carries which network — comes from the secrets, for the context the
// CLI names: `--runtime ctx=test` (prod when unset). This file holds none.
//
// Which secrets: `--runtime secrets=<file>` (relative to the working
// directory), else the made-up ones next to this file.
const secretsFile = runtime().args["secrets"];
const { getSecrets }: SecretsModule = secretsFile === undefined
  ? exampleSecrets
  : await import(new URL(secretsFile, `file://${Deno.cwd()}/`).href);
//
// This is one way to handle secrets, not a requirement: a topology can just
// as well write its addresses, keys and names inline. We split them out so
// this example can be the live topology of a real site and still be public
// without leaking anything about it — the real values sit in a secrets file
// next to it that is never committed; examples/ ships a made-up one.
const secrets = getSecrets(runtime().args["ctx"]);

// The transit cables (the svc of a wan or VPN gateway) take the next
// /28 + /124 of the site's pool, in the order they appear below.
const nextTransitBlock = transitBlocks(secrets.transitPool);

export const network = defineNetwork(secrets.names.network, (net) => {
  /** Routes into a tunnel, one per destination. */
  const routesTo = (dsts: readonly string[]) =>
    dsts.map((dst) => ({ dst: net.ipNet(dst) }));
  /** A transit cable: the next block of the pool. */
  const transit = () => transitNetwork(net.ipNet(nextTransitBlock()));

  const chassisSsh = net.sshHost({
    name: secrets.chassis.name,
    address: { ipv4: secrets.chassis.ipv4 },
    user: "root",
    ovn: { role: { kind: "central" } },
    reconcile: { hostInterfaces: ["host"] },
  });

  // ── wans: scope `internet` ─────────────────────────────────────────────
  // Each wan → internet-wan-<name>; they all meet on internet-core (made by
  // the first wan). A wan masquerades what reaches it and knows no tenant
  // network: no return routes into the tenants, no routing domain.

  const internet = net.internet({
    // → internet-core
    core: net.ipNet(secrets.networks.internetCore),
  });

  const wanCableBoxNet = net.ipNet(secrets.networks.wanCableBox);
  const wanCableBoxHosts = secrets.hosts.wanCableBox;

  const wanCableBox = net.wan("cable-box", () => ({
    // → internet-wan-cable-box
    host: chassisSsh,
    iface: secrets.ifaces.wanCableBox,
    // our address on the cable box's net: the gateway towards ovn-fabric
    ipaddrs: wanCableBoxNet.host(wanCableBoxHosts.toOvnFabricGateway).ipaddrs(),
    routes: [
      {
        dst: net.ipNet("0.0.0.0/0"),
        via: wanCableBoxNet.host(1).asIpv4(), // the cable box
      },
      { dst: net.ipNet("::/0") }, // SLAAC/RA on the WAN
    ],
    svc: transit(), // → internet-svc-cable-box-core
  }));

  const wanCableModem = net.wan("cable-modem", () => ({
    // → internet-wan-cable-modem
    host: chassisSsh,
    iface: secrets.ifaces.wanCableModem,
    addressing: "dhcp",
    active: false, // not built yet
  }));

  const wanWireless = net.wan("wireless", () => ({
    // → internet-wan-wireless
    host: chassisSsh,
    iface: secrets.ifaces.wanWireless,
    addressing: "dhcp",
    active: false, // not built yet
  }));

  // ── tenant home: lan, dmz, ZeroTier, Tailscale ─────────────────────────
  // → home-core, home-nat (the tenant is one routing domain; nothing to
  // declare).
  const home = net.tenant("home", (tenant) => ({
    wans: [wanCableBox, wanWireless], // in order: the first active one is used
    core: net.ipNet(secrets.networks.homeCore),
    routers: [
      tenant.defineOvnRouter("main", (router) => ({
        // → home-rt-main
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: tenant.collisionDomain("lan"), // → home-cable-lan
            ipaddrs: net.ipNet(secrets.networks.homeLan).host(1).ipaddrs(),
            ifaces: [{ host: chassisSsh, iface: secrets.ifaces.homeLan }],
            services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
          }),
          router.ovnRouterEndpoint({
            l2Segment: tenant.collisionDomain("dmz"), // → home-cable-dmz
            ipaddrs: net.ipNet(secrets.networks.homeDmz).host(1).ipaddrs(),
            ifaces: [{ host: chassisSsh, iface: secrets.ifaces.homeDmz }],
            services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
          }),
        ],
      })),
      tenant.vpnGateway("zerotier", {
        // → home-vpn-zerotier
        kind: "zerotier",
        host: chassisSsh,
        ...secrets.zerotier, // networkId + identity
        routes: routesTo(secrets.routes.zerotier), // into home-core
        masq: ["ipv4", "ipv6"], // out of zt0: the mesh has no route back to home
        svc: {
          core: transit(), // → home-svc-zerotier-core
          nat: transit(), // → home-svc-zerotier-nat
        },
        wans: [wanCableBox, wanWireless], // its encrypted traffic, via → home-nat-zerotier
      }),
      tenant.vpnGateway("tailscale", {
        // → home-vpn-tailscale
        kind: "tailscale",
        host: chassisSsh,
        // loginServer, and optionally the node (tailscaled.state, seeded
        // when missing) and its pre-auth key (a file of mode 600 in its
        // state dir).
        ...secrets.tailscale,
        routes: routesTo(secrets.routes.tailscale), // into home-core
        masq: ["ipv4", "ipv6"], // out of tailscale0, like zt0
        svc: {
          core: transit(), // → home-svc-tailscale-core
          nat: transit(), // → home-svc-tailscale-nat
        },
        wans: [wanCableBox, wanWireless], // its encrypted traffic, via → home-nat-tailscale
      }),
    ],
  }));

  // ── tenant neighbor ───────────────────────────────────────────────────
  const neighbor = net.tenant("neighbor", (tenant) => ({
    wans: [wanWireless, wanCableBox],
    core: net.ipNet(secrets.networks.neighborCore),
    routers: [
      tenant.defineOvnRouter("lan", (router) => ({
        // → neighbor-rt-lan
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: tenant.collisionDomain("lan"), // → neighbor-cable-lan
            ipaddrs: net.ipNet(secrets.networks.neighborLan).host(1).ipaddrs(),
            ifaces: [{ host: chassisSsh, iface: secrets.ifaces.neighborLan }],
            services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
          }),
        ],
      })),
    ],
  }));

  // ── tenant usa: out only through a VPN ────────────────────────────────
  // No `wans`: no usa-nat — when the tunnel is down, usa is offline.
  const usa = net.tenant("usa", (tenant) => ({
    core: net.ipNet(secrets.networks.usaCore),
    routers: [
      tenant.defineOvnRouter("lan", (router) => ({
        // → usa-rt-lan
        endpoints: [
          router.ovnRouterEndpoint({
            l2Segment: tenant.collisionDomain("lan"), // → usa-cable-lan
            ipaddrs: net.ipNet(secrets.networks.usaLan).host(1).ipaddrs(),
            ifaces: [{ host: chassisSsh, iface: secrets.ifaces.usaLan }],
            services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
          }),
        ],
      })),
      tenant.vpnGateway("geo-vpn", {
        // → usa-vpn-geo-vpn; the tunnel device on the host is wg0
        kind: "wireguard",
        host: chassisSsh,
        default: true, // the tenant's only way out
        config: secrets.geoVpn, // its key, addresses and peer
        masq: ["ipv4", "ipv6"], // out of the tunnel (the provider gives one address)
        svc: {
          core: transit(), // → usa-svc-geo-vpn-core
          nat: transit(), // → usa-svc-geo-vpn-nat
        },
        wans: [wanCableBox, wanWireless], // its encrypted traffic, via → usa-nat-geo-vpn
      }),
    ],
  }));

  // ── the platform: control plane, DNS, certificates ─────────────────────
  const cpRoute = net.routingDomain("cp");

  const cloudflare = net.dnsProvider("cloudflare", {
    kind: "cloudflare",
    token: "<<cloudflare-api-token>>",
  });
  const letsencryptProd = net.certIssuer("letsencrypt-prod", {
    kind: "acme",
    directory: "https://acme-v02.api.letsencrypt.org/directory",
    email: secrets.domain.acmeEmail,
    challenge: { kind: "dns-01" },
  });

  const homeDmz = home.collisionDomains["dmz"];
  if (homeDmz === undefined) throw new Error('tenant "home" has no dmz cable');

  net.createPlatform(secrets.names.platform, {
    controlPlane: {
      routingDomains: [cpRoute],
      collisionDomain: net.collisionDomain("control-plane"),
      hosts: [chassisSsh],
      ipaddrs: net.ipNet(secrets.networks.controlPlane).host(1).ipaddrs(),
      ifaces: [{ host: chassisSsh, iface: secrets.ifaces.controlPlane }],
      // The only crossing between tenants: the router of home's dmz
      // (home-rt-main) gets a leg on the control plane and masquerades the
      // dmz into it.
      ingress: [homeDmz],
    },
    zones: [
      {
        zones: [secrets.domain.zone],
        dnsProvider: cloudflare,
        certIssuer: letsencryptProd,
      },
    ],
    services: [
      {
        service: net.service("platform.dns", {
          image: "ovn-fabric-coredns:netdebug",
          cmd: ["-conf", "/etc/coredns/Corefile"],
          files: [
            {
              path: "/etc/coredns/Corefile",
              content: [
                ".:53 {",
                "    errors",
                "    log",
                "    health :8080",
                "    ready :8181",
                "    cache 30",
                `    forward . ${secrets.dnsForwarders.join(" ")}`,
                "    reload",
                "}",
              ],
              // The image runs CoreDNS as root (netshoot, no USER); the
              // official coredns/coredns image would be uid/gid 65532.
              user: 0,
              group: 0,
              mode: "644",
            },
          ],
          build: {
            from: "nicolaka/netshoot:latest",
            dockerfile: `FROM coredns/coredns:latest AS coredns
FROM nicolaka/netshoot:latest
COPY --from=coredns /coredns /coredns
ENTRYPOINT ["/coredns"]
CMD ["-conf", "/etc/coredns/Corefile"]
`,
          },
        }),
        role: "dns",
      },
    ],
    egress: [
      {
        // its leg on internet-core
        l2Segment: internet.core,
        ipaddrs: internet.net.allocated().ipaddrs(), // the runtime allocates
        gatewayChassis: chassisSsh,
        routes: wanCableBox.defaultRoutes(),
        routingDomains: [cpRoute],
        masq: ["ipv4", "ipv6"],
      },
    ],
  });

  return {
    hosts: [chassisSsh],
    wans: [wanCableBox, wanCableModem, wanWireless],
    tenants: [home, neighbor, usa],
  };
});
