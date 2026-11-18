// An EXAMPLE of topology.cluster-tenants.secrets.ts — every value here is
// made up: documentation address ranges (192.0.2.0/24, 198.51.100.0/24,
// 2001:db8::/32), example.com, and <<placeholders>> for keys. Copy it next
// to your topology, fill in your own site, and keep that copy out of git
// (mode 600).
//
// The topology asks getSecrets(ctx) for everything that is the site: keys,
// names, addresses, and per context the chassis and its interfaces. The
// context comes from the CLI: `--runtime ctx=test` (prod when unset).
//
// Imports go through specifiers deno.json maps (jsr:@adviser/ovn-fabric…),
// never relative paths: the same source must also load from the
// TOPOLOGY_SECRETS_TS env var as a data: module, which has no base URL.

import { IPAddress } from "jsr:@adviser/ipaddress@0.2.13";
// deno-lint-ignore no-unversioned-import -- mapped to ../src by deno.json
import { IPv4 } from "jsr:@adviser/ovn-fabric";
// deno-lint-ignore no-unversioned-import -- mapped to ../src by deno.json
import type { Context, Secrets } from "jsr:@adviser/ovn-fabric/secret-types";

function parse(text: string): IPAddress {
  const addr = IPAddress.parse(text);
  if (addr === null) throw new Error(`not an address: ${text}`);
  return addr;
}

/** The global IPv6 network your ISP delegates — a real, routed prefix, not a
 * ULA: https://mabels.github.io/mseg-tester/blog/fd00-is-not-your-192-168-short.html */
const IPV6_PREFIX = parse("2001:db8:1::/48");

/** `offset` placed in IPV6_PREFIX, as text (net.ipNet takes text). */
function v6(offset: string): string {
  const net = IPV6_PREFIX.add_host(parse(offset));
  if (net === null) throw new Error(`v6: ${offset} is no subnet of the prefix`);
  return net.to_string();
}

// ── per context: the chassis and which interface carries which network ───

function prod(): Context {
  const eth0 = { kind: "physical", name: "eth0" } as const;
  const vlan = (vlanId: number) =>
    ({ kind: "vlan", vlanParent: eth0, vlanId }) as const;
  return {
    chassis: { name: "ovn-prod", ipv4: IPv4.parse("192.0.2.10") },
    ifaces: {
      homeLan: eth0,
      homeDmz: vlan(11),
      neighborLan: vlan(20),
      usaLan: vlan(30),
      controlPlane: vlan(40),
      wanCableBox: vlan(100),
      wanCableModem: vlan(101),
      wanWireless: vlan(102),
    },
  };
}

/** A test chassis: the same networks as VLANs on a trunk NIC. */
function test(): Context {
  const trunk = { kind: "physical", name: "ovnkern" } as const;
  const vlan = (vlanId: number) =>
    ({ kind: "vlan", vlanParent: trunk, vlanId }) as const;
  return {
    chassis: { name: "ovn-test", ipv4: IPv4.parse("192.0.2.11") },
    ifaces: {
      homeLan: vlan(1010),
      homeDmz: vlan(1011),
      neighborLan: vlan(1020),
      usaLan: vlan(1030),
      controlPlane: vlan(1040),
      wanCableBox: vlan(1100),
      wanCableModem: vlan(1101),
      wanWireless: vlan(1102),
    },
  };
}

const CONTEXTS: Record<string, () => Context> = { prod, test };

// ── the one entry point ───────────────────────────────────────────────────

/** Everything that is the site, for the context `ctx` (prod when unset). */
export function getSecrets(ctx: string | undefined): Secrets {
  const name = ctx ?? "prod";
  const context = CONTEXTS[name];
  if (context === undefined) {
    throw new Error(
      `ctx "${name}": not one of ${Object.keys(CONTEXTS).join(", ")}`,
    );
  }
  return {
    ...context(),
    names: { network: "example-site", platform: "example-platform" },
    // IPv6: a core is a /112 in the prefix's first /64 (`::<n>:0`), a LAN is
    // a /64 numbered in the 4th hextet (`0:0:0:<n>::`).
    networks: {
      internetCore: { ipv4: "10.1.0.0/24", ipv6: v6("::1:0/112") },
      wanCableBox: { ipv4: "198.51.100.0/24" },
      homeCore: { ipv4: "10.2.0.0/24", ipv6: v6("::2:0/112") },
      homeLan: { ipv4: "10.10.0.0/24", ipv6: v6("0:0:0:10::/64") },
      homeDmz: { ipv4: "10.11.0.0/24", ipv6: v6("0:0:0:11::/64") },
      neighborCore: { ipv4: "10.3.0.0/24", ipv6: v6("::3:0/112") },
      neighborLan: { ipv4: "10.20.0.0/24", ipv6: v6("0:0:0:20::/64") },
      usaCore: { ipv4: "10.4.0.0/24", ipv6: v6("::4:0/112") },
      usaLan: { ipv4: "10.30.0.0/24", ipv6: v6("0:0:0:30::/64") },
      controlPlane: { ipv4: "10.40.0.0/24", ipv6: v6("::40:0/112") },
    },
    hosts: { wanCableBox: { toOvnFabricGateway: 2 } },
    transitPool: {
      ipv4: "10.99.0.0/24",
      ipv6: v6("0:0:0:5452:414e:5349:5400:0/112"), // spells TRANSIT in hex
    },
    routes: {
      zerotier: ["10.0.0.0/8"],
      // Tailscale's own ranges (CGNAT and its ULA): the defaults, not a site.
      tailscale: ["100.64.0.0/10", "fd7a:115c:a1e0::/48"],
    },
    dnsForwarders: ["9.9.9.9"],
    zerotier: {
      networkId: "<<zerotier-network-id>>",
      identity: {
        public: "<<zerotier-identity.public>>",
        secret: "<<zerotier-identity.secret>>",
      },
    },
    tailscale: { loginServer: "https://headscale.example.com" },
    geoVpn: {
      privateKey: "<<wireguard-private-key>>",
      address: "10.64.0.2/32,2001:db8:6464::2/128",
      peer: {
        publicKey: "<<wireguard-peer-public-key>>",
        endpoint: "198.51.100.1:51820",
        allowedIps: "0.0.0.0/0,::0/0",
      },
    },
    domain: { zone: "example.com", acmeEmail: "admin@example.com" },
  };
}
