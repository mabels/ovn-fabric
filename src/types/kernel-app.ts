import type { Addr } from "../ipnet.ts";
import type { WireguardInterfaceConfig } from "./interface.ts";
import type { SecurityGroupRef } from "./security-group.ts";
import type { AppSecurityEnable } from "./service.ts";

/** A fully-resolved kernel-netns APPLICATION — the `kernel.app.*`
 * service kinds (RouterEndpointService, types.ts) resolved once, at
 * declaration time (buildKernelRouterEndpoint, define.ts), into the
 * descriptor the deployer turns into the actual service script. The
 * `kernel.app.` prefix is dropped here (`kernel.app.dhcp-client` ->
 * `kind: "dhcp-client"`). Every app runs as a systemd unit
 * (Restart=always) supervising the process inside the netns; a running
 * app daemon holds its netns alive past `ip netns delete`, so the
 * deployer's delete pass MUST stop or release it first. */
export type KernelApp =
  | { readonly kind: "dhcp-client"; readonly style: "dhclient" | "dhcpcd" }
  | {
    readonly kind: "wireguard";
    /** The real kernel interface name AND the .conf's basename
     * (mirrors the `kernel.app.wireguard` service's `ifaceName`). */
    readonly ifaceName: string;
    /** The full wg-quick conf content, written verbatim. */
    readonly config: WireguardInterfaceConfig;
    /** Security groups whose `masq` rules apply out the TUNNEL interface.
     * Masq belongs to the TUNNEL iface here, NOT to the upstream leg (the
     * kernel-side shortcut targets the side's real iface, which would be
     * wrong for a tunnel router). */
    readonly securityGroups: readonly SecurityGroupRef<"kernel">[];
  }
  | {
    readonly kind: "zerotier";
    /** The ZeroTier network ID to join (mirrors the
     * `kernel.app.zerotier` service's `networkId`). */
    readonly networkId: string;
    /** This tunnel's OWN dedicated ZeroTier home directory. */
    readonly instanceDir: string;
    /** Security groups whose `masq` rules apply out the tunnel interface.
     * The real iface name is only known at RUNTIME (ZeroTier names it),
     * so the wire script applies them to the captured interface. */
    readonly securityGroups: readonly SecurityGroupRef<"kernel">[];
    /** Via-less routes the tunnel carries out the mesh — the tunnel
     * endpoint's declared `routes` (e.g. the ztnet supernet
     * 192.168.0.0/16). The interface name is only known at RUNTIME, so
     * the wire script adds each as `ip route add <dst> dev "$var"` once
     * ZeroTier names it (2026-08-30). */
    readonly routes?: readonly {
      readonly dst: string;
      readonly via?: string;
    }[];
  }
  | {
    readonly kind: "docker";
    /** What it may do beyond Docker's defaults (a Service's); set by the
     * endpoint builders — a VPN client's container gets "TUN" — never by
     * the topology. */
    readonly securityEnable?: readonly AppSecurityEnable[];
    /** The image to run — resolved by buildKernelRouterEndpoint from the
     * service's optional `image`, defaulting to `ovn-fabric-<router>` when
     * omitted (2026-08-31). */
    readonly image: string;
    /** Optional: build the image on first use if missing (packages
     * installed at image-build time; `from` picks apk vs apt). */
    readonly build?: {
      readonly from: string;
      readonly packages?: readonly string[];
      readonly dockerfile?: string;
    };
    /** Whether the container OWNS the router's interfaces (the transit veth
     * leg + this side's real interface moved into its netns, running `cmd`
     * against the real one — the "container is the router" mode,
     * dhcpcd-in-docker) is decided on the TS side when the IR interfaces are
     * derived, and the IR carries the OUTCOME structurally, with NO flag: an
     * interface-owning docker has NO veth addressing (`ip`/`routerIp`
     * absent — it owns the real interface, not a veth), a veth-injection
     * client always has it. The deployer branches on that concrete IR fact,
     * never on a TS concept (2026-08-31). */
    /** The container name — always resolved by
     * buildKernelRouterEndpoint (the service's `name` prefixed with the
     * router name, `<router>-<name>`, default `<router>-docker`), so
     * delete can `docker rm -f` exactly what create started. */
    readonly name: string;
    /** The container's command — trailing args of `docker run`, split
     * into tokens by buildKernelRouterEndpoint from the service's
     * `cmd` string. */
    readonly cmd?: readonly string[];
    /** The container's interface address, e.g. "10.200.0.2/24" — the
     * container's end of the veth injected into the router netns. */
    readonly ip?: Addr;
    /** The router end of that veth — the subnet's first host, e.g.
     * "10.200.0.1/24", resolved by buildKernelRouterEndpoint. */
    readonly routerIp?: Addr;
    /** The SHORT veth prefix for this app's veth pair, computed from
     * the container name (`ve-<hash>`, src/ir.ts's kernelRouterSideToIR)
     * — the router-netns end is `veth-<vethName>`-free, the pair is
     * `ve-<hash>` / `ve-<hash>-c`, both ≤ IFNAMSIZ. */
    readonly vethName?: string;
    /** Host bind mounts, e.g. the ZeroTier identity directory. Each is
     * created on the host before `docker run` and passed as
     * `-v <host>:<container>`. */
    readonly volumes?: readonly {
      readonly host: string;
      readonly container: string;
    }[];
    /** Extra `docker run` flags inserted before the image, e.g.
     * ["--dns", "10.43.0.10"]. */
    readonly run?: readonly string[];
    /** ZeroTier identity files seeded into its state directory before
     * the container starts. */
    readonly identity?: {
      readonly public: string;
      readonly secret: string;
    };
    /** Files on the host the app reads when it starts (in a volume). With
     * `content`: written. Without: made if missing and kept — the app
     * writes into it itself. */
    readonly stateFiles?: readonly {
      readonly path: string;
      readonly content?: string;
      /** Its mode (e.g. "600" for a key); default the system's. */
      readonly mode?: string;
      /** With `content`: written only when missing — a seed the app keeps
       * up to date itself (a tailscaled.state), never written over. */
      readonly keep?: boolean;
      /** Its owner and group as uid/gid (the container sees the host's
       * file), as a service file's. Default: root. */
      readonly user?: number;
      readonly group?: number;
    }[];
    /** Security groups whose `masq` rules apply out `tunnel.iface`. */
    readonly securityGroups?: readonly SecurityGroupRef<"kernel">[];
    /** A device the app makes itself, under a name it is told (not in the
     * image: the image need not have `ip` or `iptables`). The deployer waits
     * for it and wires it from the host, through the container's netns. */
    readonly tunnel?: {
      readonly iface: string;
      readonly routes: readonly string[];
    };
  };
