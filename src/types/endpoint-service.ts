import type { IPv4, IPv6 } from "../ip.ts";
import type { AddrLike } from "../ipnet.ts";
import type { InterfaceKind, WireguardInterfaceConfig } from "./interface.ts";
import type { Publish, Resolver } from "./platform.ts";
import type { RouterEndpointRoute } from "./routing.ts";
import type { DenyInputAllow } from "./security-group.ts";
import type { Service } from "./service.ts";

// ── RouterEndpoint services: IPv6 RA/SLAAC + kernel-side services ──
// IPv6 RA is split into its two REAL, independently meaningful OVN
// behaviors instead of one flag toggling both together, because they
// genuinely differ (confirmed live, the ipv6_ra_configs history/
// upstream-bug comment):
//   - "ipv6.slaac" sets ipv6_ra_configs:address_mode=slaac — this alone
//     already makes OVN answer solicited Router Solicitations (the
//     lr_in_nd_ra_options/lr_in_nd_ra_response responder), even with no
//     other option set.
//   - "ipv6.ra" sets ipv6_ra_configs:send_periodic=true (+ optional
//     min/max interval overrides) — genuinely UNSOLICITED, self-timer-
//     driven RA, pinctrl-injected, which needed a real upstream OVN fix
//     (ovn-org/ovn#313) before it worked at all on a DGP/patch port.
// Both live in ipv6_ra_configs (a single OVSDB smap column), but they're
// independently useful (e.g. "ipv6.slaac" alone for solicited-only, no
// periodic chatter), so RouterEndpoint models them as two composable
// services instead of one boolean that can't express that.
//
// The `kernel.*` kinds are the OPPOSITE world — services that apply
// INSIDE a KernelRouter's netns, never to an OVN LRP. Two families:
//   - `kernel.ipv4.masq`/`kernel.ipv6.masq`: a SHORTCUT that expands
//     (through net.securityGroup(), define.ts) to a security group named
//     `masq-<router>` carrying the matching POSTROUTING MASQUERADE rules
//     for the netns's real-world-facing interface.
//   - `kernel.app.*`: an APPLICATION running inside the netns on that
//     interface — a DHCP client (`kernel.app.dhcp-client`) today, docker
//     containers/wireguard later. Resolved into a KernelApp descriptor
//     (same file) in buildKernelRouterEndpoint, carried on the
//     KernelRouterSide that runs it, and turned into the actual
//     `ip netns exec` service script by the deployer — including the
//     release/kill on delete (a DHCP client is a daemon that holds the
//     netns alive, so it MUST be stopped before `ip netns delete`).
// Split off from the OVN `ipv6.*` kinds in buildKernelRouterEndpoint so
// the OVN endpoint never sees them — resolveIpv6RaConfigs (src/ir.ts)
// throws on a kind it doesn't know.
export type RouterEndpointService =
  | { readonly kind: "ipv6.slaac" }
  | {
    readonly kind: "ipv6.ra";
    /** Seconds. Omit for OVN's own RFC 4861 default. */
    readonly minInterval?: number;
    /** Seconds. Omit for OVN's own RFC 4861 default. */
    readonly maxInterval?: number;
  }
  | { readonly kind: "kernel.ipv4.masq" }
  | { readonly kind: "kernel.ipv6.masq" }
  // OVN SNAT on the router itself: masquerade the router's OTHER endpoint
  // subnets to this (egress) endpoint's own address of that family. This
  // is router state (`lr-nat-add`), consumed by src/ir.ts's computeOvnNats
  // and never seen by resolveIpv6RaConfigs.
  // `sources`: only these networks, not all the router's other subnets.
  | { readonly kind: "ovn.ipv4.masq"; readonly sources?: readonly IPv4[] }
  | { readonly kind: "ovn.ipv6.masq"; readonly sources?: readonly IPv6[] }
  | {
    readonly kind: "ovn.ipv4.block-ingress";
    readonly allow?: readonly DenyInputAllow[];
  }
  | {
    readonly kind: "ovn.ipv6.block-ingress";
    readonly allow?: readonly DenyInputAllow[];
  }
  | {
    readonly kind: "kernel.app.dhcp-client";
    readonly style: "dhclient" | "dhcpcd";
  }
  | {
    readonly kind: "kernel.app.wireguard";
    /** The real kernel interface name AND the .conf's basename on disk
     * (/etc/wireguard/<ifaceName>.conf) — see InterfaceKind's
     * "wireguard" variant, whose config this mirrors. */
    readonly ifaceName: string;
    /** The full wg-quick conf content (privateKey/address/peer) —
     * written verbatim, PrivateKey included, per the documented
     * deliberate exception to this project's credential policy. */
    readonly config: WireguardInterfaceConfig;
  }
  | {
    readonly kind: "kernel.app.zerotier";
    /** The ZeroTier network ID to join (16 hex chars) — see
     * InterfaceKind's "zerotier" variant. */
    readonly networkId: string;
    /** This tunnel's OWN dedicated ZeroTier home directory (identity/
     * per-network state) — see InterfaceKind's "zerotier" variant. */
    readonly instanceDir: string;
  }
  | {
    readonly kind: "kernel.app.docker";
    /** The image to run, e.g. "ubuntu". OPTIONAL — defaults to the
     * router name (`ovn-fabric-<router>`) at resolve time (2026-08-31). */
    readonly image?: string;
    /** Optional: build the image on first use if missing — `from` is the
     * base image (determines apk vs apt), `packages` the ones installed
     * at image-build time, `dockerfile` the full escape hatch. */
    readonly build?: {
      readonly from: string;
      readonly packages?: readonly string[];
      readonly dockerfile?: string;
    };
    /** The container name — PREFIXED with the router name at resolve
     * time (`<router>-<name>`), so it's globally unique and delete can
     * `docker rm -f` exactly what create started. Defaults to
     * `<router>-docker` when omitted. */
    readonly name?: string;
    /** The command the container runs, e.g. "sleep 86400" or
     * ["sleep", "86400"] — the trailing args of `docker run`
     * (normalized to tokens at resolve time). */
    readonly cmd?: string | readonly string[];
    /** The container's addresses on the attached network (a segment, or a
     * kernel router's services segment) — parsed IPv4/IPv6 with prefixes,
     * same as an endpoint's `ipaddrs`. For the veth-injection mode the
     * FIRST entry is the container's interface address and the peer end is
     * that subnet's first host (`.1`); for a container bound to a segment
     * they are its addresses on that segment. */
    readonly ipaddrs?: readonly AddrLike[];
    /** The container's OWN routes (e.g. its default via the segment
     * gateway). Omitted/empty → a default route (per family on the
     * endpoint) is added via the endpoint's address (the segment/router
     * gateway) at resolve time (2026-09-08). */
    readonly routes?: readonly RouterEndpointRoute[];
  }
  | {
    /** An ATTACHMENT: bind a reusable workload (`srvRef`, a net.service) to
     * this endpoint's segment — one NIC on this L2 (2026-09-08). */
    readonly kind: "service.attach";
    readonly srvRef: Service;
    readonly ipaddrs: readonly AddrLike[];
    /** Explicit routes for THIS NIC (over the segment gateway). Omitted/
     * empty → the NIC inherits the routes of the RoutingDomains its
     * endpoint's router participates in, next-hopped at this endpoint's
     * address (workloadDomainRoutes, src/ir.ts). */
    readonly routes?: readonly RouterEndpointRoute[];
    /** Default metric for THIS NIC's routes (lower wins) — the per-NIC
     * ranking of a workload's default routes. Overrides an inherited
     * domain metric for this attachment; a route's own `metric` still wins
     * over it (2026-09-19). */
    readonly metric?: number;
    /** DNS resolver for THIS NIC — wins over the control-plane default
     * (see Resolver, 2026-09-19). */
    readonly resolver?: Resolver;
    /** Name + cert bindings for this attachment, authored as
     * platform.publish(...). The builder injects `endpoint` + `service`
     * into each entry at attach time (like EndpointService.endpoint). */
    readonly publish?: readonly Publish[];
  }
  // CONFIG-SIDE WORKLOAD SHORTCUTS — kept in the topology; define.ts maps
  // them onto the generic kernel.app.container / kernel.app.service (the IR
  // knows nothing about wireguard/zerotier, 2026-09-08).
  | {
    readonly kind: "wireguard";
    readonly ifaceName: string;
    readonly config: Extract<
      InterfaceKind,
      { kind: "wireguard" }
    >["config"];
    readonly masq?: readonly ("ipv4" | "ipv6")[];
  }
  | {
    readonly kind: "zerotier";
    readonly networkId: string;
    readonly masq?: readonly ("ipv4" | "ipv6")[];
    readonly routes?: readonly RouterEndpointRoute[];
    /** Reuse an existing ZeroTier node identity: the exact
     * identity.public / identity.secret files. */
    readonly identity?: {
      readonly public: string;
      readonly secret: string;
    };
  }
  | {
    readonly kind: "tailscale";
    /** The control server (e.g. a headscale), `tailscale up --login-server`. */
    readonly loginServer: string;
    /** A pre-auth key: written to a file of mode 600 in its state
     * directory, read by `tailscale up --auth-key=file:…` — never in env.
     * Optional: a node restored from `state` is logged in already. */
    readonly authKey?: string;
    /** The node itself: its `tailscaled.state` (machine key, node key,
     * logged-in profile), as ZeroTier's `identity`. Written (mode 600)
     * only when the file is missing — tailscaled keeps it up to date —
     * so a lost state directory comes back as the same node. */
    readonly state?: string;
    /** More `tailscale up` options, e.g. ["--advertise-routes=…"]. */
    readonly upArgs?: readonly string[];
    readonly masq?: readonly ("ipv4" | "ipv6")[];
    readonly routes?: readonly RouterEndpointRoute[];
  };
