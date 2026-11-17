// src/secret-types.ts — the form of the secrets of the living example,
// examples/topology.cluster-tenants.ts: everything that is the site (keys,
// names, addresses, which interface carries which network), so the topology
// itself holds none of it.
//
// A template, not a general contract: the field names are that example's.
// A site with another shape copies this and names its own. The secrets file
// (examples/topology.cluster-tenants.secrets.ts, and the real one next to a
// real topology) returns a `Secrets` from getSecrets(ctx).

import { IPAddress } from "jsr:@adviser/ipaddress@0.2.12";
import type { IPv4 } from "./ip.ts";
import type { IpNetSpec } from "./ipnet.ts";
import type { InterfaceKind, WireguardInterfaceConfig } from "./types.ts";

/** The chassis: its name and the address ovn-fabric reaches it on. */
export interface Chassis {
  readonly name: string;
  readonly ipv4: IPv4;
}

/** Which interface of the chassis carries which network — the same keys as
 * `networks`, so an endpoint reads `ifaces.homeLan` next to `networks.homeLan`. */
export interface Ifaces {
  readonly homeLan: InterfaceKind;
  readonly homeDmz: InterfaceKind;
  readonly neighborLan: InterfaceKind;
  readonly usaLan: InterfaceKind;
  readonly controlPlane: InterfaceKind;
  readonly wanCableBox: InterfaceKind;
  readonly wanCableModem: InterfaceKind;
  readonly wanWireless: InterfaceKind;
}

/** What changes per context (`--runtime ctx=…`: prod, test, …). */
export interface Context {
  readonly chassis: Chassis;
  readonly ifaces: Ifaces;
}

/** The pool the transit cables are cut from (see transitBlocks). */
export interface TransitPool {
  readonly ipv4: string;
  readonly ipv6: string;
}

export interface Secrets extends Context {
  /** The network and its platform. */
  readonly names: { readonly network: string; readonly platform: string };
  /** The site's networks, by the part of the topology that uses them. */
  readonly networks: {
    readonly internetCore: IpNetSpec;
    readonly wanCableBox: IpNetSpec;
    readonly homeCore: IpNetSpec;
    readonly homeLan: IpNetSpec;
    readonly homeDmz: IpNetSpec;
    readonly neighborCore: IpNetSpec;
    readonly neighborLan: IpNetSpec;
    readonly usaCore: IpNetSpec;
    readonly usaLan: IpNetSpec;
    readonly controlPlane: IpNetSpec;
  };
  /** Pinned host numbers on a network that is not ours. */
  readonly hosts: {
    readonly wanCableBox: {
      /** ovn-fabric's address on the cable box's net: the gateway towards
       * ovn-fabric — the next hop the uplink needs for back routes when it
       * does not NAT. */
      readonly toOvnFabricGateway: number;
    };
  };
  readonly transitPool: TransitPool;
  /** What the VPN gateways route into the tenant's core. */
  readonly routes: {
    readonly zerotier: readonly string[];
    readonly tailscale: readonly string[];
  };
  /** Where the platform's CoreDNS forwards to. */
  readonly dnsForwarders: readonly string[];
  /** The ZeroTier network home-vpn-zerotier joins, and its node identity. */
  readonly zerotier: {
    readonly networkId: string;
    readonly identity: { readonly public: string; readonly secret: string };
  };
  /** The headscale server home-vpn-tailscale logs in to; optionally its
   * pre-auth key and node state (tailscaled.state). */
  readonly tailscale: {
    readonly loginServer: string;
    readonly authKey?: string;
    readonly state?: string;
  };
  /** usa-vpn-geo-vpn's WireGuard config, as the gateway takes it. */
  readonly geoVpn: WireguardInterfaceConfig;
  /** The platform's DNS zone and the ACME account's email. */
  readonly domain: { readonly zone: string; readonly acmeEmail: string };
}

/** What a secrets file exports. */
export interface SecretsModule {
  getSecrets(ctx: string | undefined): Secrets;
}

/** A transit cable's net: a /28 and a /124. */
const TRANSIT_V4 = 28;
const TRANSIT_V6 = 124;

/** The transit cables (the svc of a wan or VPN gateway), cut from `pool`:
 * each call returns the next /28 + /124, in the order the topology asks —
 * a new cable goes at the end, or the ones after it are numbered anew.
 * Throws when the smaller of the two pools is used up. */
export function transitBlocks(pool: TransitPool): () => IpNetSpec {
  const v4 = subnets(pool.ipv4, TRANSIT_V4);
  const v6 = subnets(pool.ipv6, TRANSIT_V6);
  let next = 0;
  return () => {
    const ipv4 = v4[next];
    const ipv6 = v6[next];
    if (ipv4 === undefined || ipv6 === undefined) {
      throw new Error(
        `transit pool used up after ${next} blocks (${pool.ipv4}, ${pool.ipv6})`,
      );
    }
    next++;
    return { ipv4, ipv6 };
  };
}

function subnets(prefix: string, size: number): string[] {
  const net = IPAddress.parse(prefix);
  const blocks = net?.subnet(size);
  if (!blocks) {
    throw new Error(`transit pool ${prefix}: not a net with /${size} subnets`);
  }
  return blocks.map((block) => block.to_string());
}
