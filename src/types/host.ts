import type { IPv4, IPv6 } from "../ip.ts";

// ── host / chassis ─────────────────────────────────────────────────
// Where a given router's OVN/OVS configuration is actually applied. A
// given deployment may run everything on one chassis, but the model
// does not assume that — each router endpoint declares the Host its
// interfaces live on, so a future topology with multiple chassis is a
// config change, not a redesign.

export type AccessMethod =
  | { method: "ssh"; user: string }
  | { method: "local" }; // the generator's own host — no SSH needed

// ── host address: fqdn and/or real IPv4/IPv6 ────────────────────────
// A host can be reachable at more than one of these simultaneously — an
// FQDN for humans/SSH, real IPv4/IPv6 for OVN's own Geneve tunnel
// endpoint (see OvnHostConfig.encapIp below). These are genuinely
// independent concerns that can differ (SSH via a management name,
// tunnel traffic via a dedicated network) — not three equivalent ways
// to spell the same thing. At least one field must be given; validated
// at construction (sshHost/localHost), not statically enforceable
// without a much less readable union type.
export interface HostAddress {
  readonly fqdn?: string;
  readonly ipv4?: IPv4;
  readonly ipv6?: IPv6;
}

function primaryHostAddress(address: HostAddress): string {
  if (address.fqdn) return address.fqdn;
  if (address.ipv4) return address.ipv4.to_s();
  if (address.ipv6) return address.ipv6.to_s();
  throw new Error(
    "HostAddress requires at least one of fqdn/ipv4/ipv6",
  );
}

// ── OVN cluster membership ──────────────────────────────────────────
// `defineNetwork` is one OVN cluster (one shared control plane, ADR
// 0003), not one host — every Host participating in it is a chassis,
// and exactly one role in the cluster is "central" (runs ovn-central:
// northd + the NB/SB databases). Every other chassis runs only
// ovn-host/ovn-controller, pointed at the central chassis's SB DB
// remotely instead of a local one — this is the real, previously
// undone TODO from the legacy generator's requiredPackages doc comment
// (2026-07-19): today every Host gets its own full independent stack,
// so N hosts under one defineNetwork silently become N uncoordinated
// clusters, not one shared one.
export type OvnRole =
  | { readonly kind: "central" }
  | { readonly kind: "chassis" };

export interface OvnHostConfig {
  readonly role: OvnRole;
  /** Real IP used as this chassis's Geneve tunnel endpoint
   * (external-ids:ovn-encap-ip). Defaults to this Host's own
   * address.ipv4 ?? address.ipv6 when omitted — only needed as an
   * explicit override when tunnel traffic and SSH/generator
   * reachability use different networks. Never an FQDN: the SB DB's
   * own Chassis.encaps.ip column is a real address, not a hostname —
   * confirmed against a live 3-chassis cluster, 2026-08-09. */
  readonly encapIp?: IPv4 | IPv6;
  /** Defaults to "geneve" — OVN's modern default, and what every real
   * chassis tested against so far actually uses. */
  readonly encapType?: "geneve" | "stt" | "vxlan";
}

// ── OVN cluster-wide (NB_Global) options ────────────────────────────
// Set once per cluster, not per chassis — these live on the NB
// database itself. See ADR 0003 for where each of these came from
// (OVN's real NB_Global schema, not this project's invention).
export interface OvnClusterOptions {
  /** Encrypt every inter-chassis Geneve tunnel via IPsec (ESP, IKE via
   * libreswan/ovn-ipsec). Off by default — no real deployment uses
   * this yet; identified against a live cluster but not yet verified
   * end to end (ADR 0003, "Open questions"). */
  readonly ipsec?: boolean;
  /** OUI-ish prefix OVN uses when auto-generating MAC addresses for
   * ports declared with dynamic addressing. Unused by this project
   * today — every router port declares an explicit MAC. */
  readonly macPrefix?: string;
  /** Batches similar logical switches into shared flow tables — a real
   * scale optimization, irrelevant at today's segment counts. */
  readonly useLogicalDpGroups?: boolean;
  readonly northd?: {
    readonly probeInterval?: number;
    readonly threads?: number;
  };
}

// ── monitoring: IPFIX flow export ──────────────────────────────────
// Host-level, not per-Segment/Uplink, and deliberately scoped to
// br-int only: that's OVN's own integration bridge, which already
// carries every logical packet for every segment/uplink resolved onto
// this chassis (ovn-controller creates and owns br-int itself — this
// generator never creates it, only points IPFIX at it). Sampling
// there once captures the whole chassis' traffic; sampling per-segment
// bridge instead would mean N separate IPFIX declarations reporting
// overlapping/duplicate data for anything that also crosses br-int.
export interface IpfixExport {
  /** "<collector-ip>:<port>" — passed straight to `ovs-vsctl create
   * IPFIX targets=...`. E.g. an Akvorado inlet's NodePort listener. */
  readonly target: string;
  /** 1 = every packet, no sampling. N = export 1 in every N packets.
   * Omit to leave OVS's own IPFIX table default in place (unsampled). */
  readonly sampling?: number;
  /** Seconds of inactivity before a cached flow is force-expired and
   * exported. Omit for OVS's own default. */
  readonly cacheActiveTimeout?: number;
  readonly cacheMaxFlows?: number;
}

export interface HostMonitoring {
  readonly ipfix?: IpfixExport;
}

/** The distros the runtime can translate a host's abstract dependencies
 * for (fabric/services/abstract_deps.py). */
export type Distro = "ubuntu" | "debian" | "fedora" | "rocky";

/** The hardware/target flavour of a distro, when its packages differ —
 * e.g. Raspberry Pi kernels and firmware. Omitted means `generic`. */
export type HostPlatform = "generic" | "rpi";

/** The OS a Host runs (2026-08-23) — what the runtime needs to translate
 * the topology's dependencies into the right packages: OVS/OVN for a
 * chassis, iproute2/iptables for a kernel router's netns, dhcpcd or
 * isc-dhcp-client for a dhcp-client app, wireguard-tools, docker. `distro`
 * names the distribution, `version` its release ("24.04" for Ubuntu, "44"
 * for Fedora) when the translation needs it, `platform` a hardware flavour
 * such as `rpi`. Missing fields — or the whole `os` — are discovered on the
 * host. No deinstall ever happens. */
export interface HostOs {
  readonly distro: Distro;
  readonly version?: string;
  readonly platform?: HostPlatform;
}

/** The package managers the runtime has an implementation for
 * (fabric/services/packages.py). The distro translation picks one; a Host
 * no longer declares it. */
export type PackageManager = "apt" | "dnf";

/** One concrete dependency a host's abstract name translates to, either a
 * package its manager installs (optionally at a version) or a package file
 * already on the host. */
export type DependencyEntry =
  | {
    readonly kind: "dep.pkg";
    readonly name: string;
    readonly version?: string;
  }
  | {
    readonly kind: "dep.pkgFile";
    readonly path: string;
    readonly sha256?: string;
  }
  | {
    readonly kind: "dep.repo";
    readonly id: string;
    readonly baseurl: string;
    readonly gpgkey?: string;
  };

export interface Host {
  readonly name: string;
  readonly address: HostAddress;
  /** What the generator actually connects to — derived once from
   * `address` (fqdn, else ipv4, else ipv6) rather than re-picked
   * ad hoc everywhere a connection string is needed. */
  readonly connectAddress: string;
  readonly access: AccessMethod;
  /** The distro/version the deployer installs dependencies for — see
   * HostOs. Undefined: the runtime discovers the host's OS. */
  readonly os?: HostOs;
  /** Concrete replacements for the distro translation, by abstract
   * dependency name (`ovn`, `traceroute`, `docker`, ...). The runtime
   * installs these instead of what the distro table would choose. */
  readonly dependencyOverrides?: Readonly<
    Record<string, readonly DependencyEntry[]>
  >;
  readonly monitoring?: HostMonitoring;
  /** What the runtime's reconcile leaves alone on this host — see
   * HostReconcile. */
  readonly reconcile?: HostReconcile;
  /** Undefined means this Host is not part of the OVN cluster at all
   * (e.g. a pure bastion/management host with no chassis role) — no
   * ovn-central/ovn-host gets installed there. See OvnHostConfig. */
  readonly ovn?: OvnHostConfig;
}

/** What the runtime's reconcile leaves alone on a host: the host's own
 * interfaces — e.g. `["host", "lo"]` for the management NIC and the
 * loopback (in every netns) — with what is on them (addresses, routes).
 * Interfaces of docker, OVS and OVN, and the physical parents the
 * topology names (a VLAN's parent), are known without being listed. */
export interface HostReconcile {
  readonly hostInterfaces?: readonly string[];
}

/** A host of the network: where its parts run. */
export interface HostSpec {
  readonly name: string;
  readonly os?: HostOs;
  readonly dependencyOverrides?: Readonly<
    Record<string, readonly DependencyEntry[]>
  >;
  readonly ovn?: OvnHostConfig;
  readonly monitoring?: HostMonitoring;
  readonly reconcile?: HostReconcile;
}

/** A host reached over SSH, as `user`. */
export interface SshHostSpec extends HostSpec {
  readonly address: HostAddress;
  readonly user: string;
}

function hostParts(spec: HostSpec) {
  const { os, dependencyOverrides, ovn, monitoring, reconcile } = spec;
  return {
    ...(os ? { os } : {}),
    ...(dependencyOverrides ? { dependencyOverrides } : {}),
    ...(ovn ? { ovn } : {}),
    ...(monitoring ? { monitoring } : {}),
    ...(reconcile ? { reconcile } : {}),
  };
}

export function sshHost(spec: SshHostSpec): Host {
  return {
    name: spec.name,
    address: spec.address,
    connectAddress: primaryHostAddress(spec.address),
    access: { method: "ssh", user: spec.user },
    ...hostParts(spec),
  };
}

export function localHost(spec: HostSpec): Host {
  return {
    name: spec.name,
    address: { fqdn: "localhost" },
    connectAddress: "127.0.0.1",
    access: { method: "local" },
    ...hostParts(spec),
  };
}
