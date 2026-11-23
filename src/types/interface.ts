/** The [Peer] stanza of a wg-quick conf — see InterfaceKind's
 * "wireguard" variant below. */
export interface WireguardPeer {
  readonly publicKey: string;
  /** "host:port" */
  readonly endpoint: string;
  /** wg-quick's own comma-joined syntax, e.g. "0.0.0.0/0,::0/0" —
   * stored as one string (not parsed here) since this generator never
   * needs to reason about individual prefixes, only reproduce the
   * conf file verbatim. */
  readonly allowedIps: string;
  readonly persistentKeepalive?: number;
}

/** The [Interface] stanza plus its one [Peer] — everything the deployer
 * needs to reconstruct a wg-quick conf byte-for-byte. See InterfaceKind's
 * "wireguard" variant
 * for why the PrivateKey lives here, in a git-tracked file, rather
 * than behind an env var/secret manager as this project's credentials
 * normally would. */
export interface WireguardInterfaceConfig {
  readonly privateKey: string;
  /** wg-quick's own comma-joined syntax, e.g.
   * "10.64.56.207/32,fc00:bbbb:bbbb:bb01::1:38ce/128" */
  readonly address: string;
  readonly listenPort?: number;
  readonly dns?: string;
  readonly peer: WireguardPeer;
}

/** How OVS's userspace datapath drives an AF_XDP port. `src/ir.ts`
 * resolves the default ("best-effort": try zero-copy, then native copy,
 * then generic) before anything reaches the IR. */
export type XdpMode =
  | "best-effort"
  | "native-with-zerocopy"
  | "native"
  | "generic";

/** A real, untagged NIC/port — also usable as a VLAN's parent. */
export interface PhysicalIface {
  readonly kind: "physical";
  readonly name: string;
}

/** A NIC owned by OVS's userspace datapath via AF_XDP. `mode`/`nRxq` are
 * optional at the DSL level; `src/ir.ts` resolves them to
 * "best-effort"/1 so the IR always carries the final fact. A NIC used
 * this way cannot also parent kernel VLANs: XDP runs before the kernel's
 * VLAN stack (see ADR 0005). */
export interface XdpIface {
  readonly kind: "xdp";
  readonly name: string;
  readonly mode?: XdpMode;
  readonly nRxq?: number;
}

/** A VLAN's parent. A bare string is accepted as shorthand for
 * `{ kind: "physical", name }` (every pre-XDP topology); an explicit
 * `physical` keeps the kernel `.${vlanId}` sub-interface, an `xdp` parent
 * moves the VLAN split into OVS. */
export type InterfaceParent = string | PhysicalIface | XdpIface;

export type InterfaceKind =
  | {
    kind: "vlan";
    vlanParent: InterfaceParent;
    vlanId: number;
    /** Override the kernel subinterface name — defaults to
     * `<parent name>.${vlanId}` when omitted. Needed whenever that
     * default name is already in use for something else on the same
     * VLAN tag (e.g. VLAN 129 already carries this host's own
     * management IP on `ens18.129` via netplan/networkd — a second,
     * differently-named vlan subinterface for the same tag can safely
     * coexist and feed an OVS bridge without touching the existing
     * one; Linux delivers matching-tagged frames to every registered
     * vlan netdevice for a given (parent, vlanId) pair, not just one). */
    ifaceName?: string;
    /** Deterministic MAC for this subinterface. */
    mac?: string;
  }
  | PhysicalIface // a real, untagged NIC/port
  | XdpIface // a real NIC driven by OVS's userspace datapath (AF_XDP)
  | { kind: "bridge-port"; bridge: string; port: string }
  /** A real WireGuard tunnel, managed via `wg-quick` rather than
   * hand-rolled `wg setconf`/`ip link` calls — wg-quick's own fwmark +
   * policy-routing dance is exactly what's needed here (the tunnel's
   * own handshake/keepalive UDP packets must keep leaving via whatever
   * route already existed — its kernel netns's upstream leg, typically —
   * while every OTHER packet gets diverted into the tunnel), and re-deriving
   * that by hand would just duplicate a battle-tested implementation.
   * Confirmed live, 2026-07-06: this is genuinely how it behaves when
   * a default route already exists in the netns before `wg-quick up`
   * runs.
   *
   * `config` (see WireguardInterfaceConfig below) is declared directly
   * in the topology and written verbatim to
   * /etc/wireguard/<ifaceName>.conf on the target host by the deployer —
   * INCLUDING the PrivateKey. This is a DELIBERATE, EXPLICIT exception to this
   * project's usual "never hard-code credentials, use env vars/secret
   * managers" policy, made 2026-07-06 after being asked to confirm:
   * the tradeoff (a real credential living in a git-tracked source
   * file) was accepted on purpose, not overlooked. Treat topology.ts
   * — and any generated script built from it — with the same care as
   * any other credential-bearing file (this repo's git-safety rules
   * around scanning for secrets before any commit/push still apply in
   * full).
   *
   * `ifaceName` is the real kernel interface name AND the .conf's
   * basename on disk, overridable when the chosen name doesn't fit:
   * it exceeds IFNAMSIZ (15 usable characters), or would rename an
   * already-running real interface unnecessarily. */
  | {
    kind: "wireguard";
    ifaceName: string;
    config: WireguardInterfaceConfig;
  }
  /** A real ZeroTier client confined to this uplink's own network
   * namespace — same overall shape as "wireguard" above (a real tunnel
   * this generator stands up and supervises inside the netns), but
   * ZeroTier is a persistent userspace DAEMON (no in-kernel device the
   * way WireGuard has), and it names its own resulting interface
   * itself rather than accepting one from the caller. Because of that,
   * the real interface name is NOT known at generation time — the
   * deployer's zerotier service script captures it into a shell
   * variable at RUNTIME, the same way `$CHASSIS` is already resolved
   * live in the generated script rather than computed here. NOT yet
   * verified against a live host (unlike "wireguard", which went
   * through several rounds of live correction) — treat the exact
   * zerotier-one/zerotier-cli invocations this produces as a first
   * draft to test and iterate on, same as wireguard's own history.
   *
   * `authorization` is deliberately NOT modeled here: joining a
   * network only gets this node as far as an unauthorized member —
   * approving it is a controller-side admin action on a DIFFERENT
   * system, outside anything this generator runs or has credentials
   * for. */
  | {
    kind: "zerotier";
    /** The ZeroTier network ID to join (16 hex chars), e.g.
     * "02cfbec15c2319ff"). */
    networkId: string;
    /** This uplink's OWN dedicated ZeroTier home directory —
     * identity.secret/identity.public and all per-network state live
     * there, deliberately NOT shared with any host-level zerotier-one
     * instance (or any other zerotier-kind uplink). Persistent across
     * reboots/netns recreation on purpose: losing this directory means
     * losing this node's ZT identity, which means losing its
     * controller authorization too (see the "moving a ZeroTier
     * installation" discussion, this session — identity.secret is the
     * one truly load-bearing file). OPTIONAL — when omitted,
     * buildTunnelRouterEndpoint (define.ts) derives
     * `/var/lib/zerotier-one-<router name>` from the enclosing router's
     * name. */
    instanceDir?: string;
  }
  /** A placeholder Linux dummy interface — no real backing device, no
   * real-world connectivity. Stands in for a real mechanism (e.g. a
   * WireGuard tunnel) that isn't built yet, so the rest of the chain
   * can be wired up and tested end-to-end first. No `name` field, on
   * purpose: unlike "vlan"/"physical", a dummy interface has no
   * pre-existing real-world name to preserve — the deployer derives
   * and creates it itself, IFNAMSIZ-safe. */
  | { kind: "dummy" }
  /** A veth pair owned by a KernelRouter's netns — the real wiring of an
   * OVN<->kernel transit link (2026-08-18): `ifaceName` is the netns-
   * side leg (created in root, then moved into the kernel router's
   * netns, where that side's addresses/routes bind to it), `peerName`
   * is the root-side leg (attached to the transit domain's OVS bridge).
   * Constructed implicitly by NetworkBuilder.kernelRouterEndpoint()
   * (define.ts) from the enclosing defineOvnRouter()'s LONG name — the
   * IFNAMSIZ-safe shortening is the lower layers' job (src/ir.ts, same
   * as bridges' shortName) — never declared by config authors. */
  | {
    kind: "veth";
    ifaceName: string;
    peerName: string;
    /** Netns-side leg's MAC, whenever the generator pins one. */
    mac?: string;
  };
