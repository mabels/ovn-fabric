// src/generate-ovn.ts — emit ONE self-installing shell script per host
// that does everything: sets up OVS bridges/interfaces for each
// segment (no netplan — see below), configures the OVN logical
// topology (segments, uplink transfer links, backbone joins), and
// installs itself as a boot-time systemd unit so it re-applies on
// every reboot. Pure function: NetworkDefinition in, one script per
// host out. Never executes anything itself — see ADR 0001 §2. Copy the
// output to the target host and run it; everything is done.
//
// Order matters and is enforced here: OVS bridges/ports MUST exist
// before any ovn-nbctl localnet port can bind to them, so this script
// always emits the interface-setup section first, then the OVN config.
//
// Why no netplan for segment interfaces: confirmed live, this session
// — systemd-networkd actively fights OVS for ownership of an interface
// (it tries to set itself as netlink master alongside OVS and gets
// stuck reasserting control), and a netns-owned VLAN interface
// destroyed by netns teardown is NOT reliably recreated by netplan
// afterward even with a declared vlans: entry. This generator creates
// every VLAN subinterface and OVS bridge/port itself, imperatively.
//
// Scope: SEGMENTS and the backbone join to whichever uplink each
// segment currently resolves to, plus each uplink's OWN transfer-link
// OVN objects (needed for the backbone join to have something to join
// to). Uplinks' netns/dhclient/NAT supervision is explicitly out of
// scope — that's ovn_uplink_sync.py's territory, with its own
// independent lifecycle and config; this script enables that tool's
// systemd unit but does not duplicate its logic.
//
// Grouped by Host (not by abstract tier) because that's what actually
// matters operationally: everything one host needs, in the one file
// that gets copied there and run.
//
// The backbone join reflects each segment's UplinkSelector resolution
// AT GENERATION TIME — re-run after switching an uplink (e.g.
// ManualUplink.switchTo(...)) to regenerate the join against the new
// target.
//
// Idiom matched to how this project's OVN config was hand-built and
// verified live, all session long: every mutating command uses
// --may-exist / --if-exists, one logical operation per line, object
// names derived from the segment's/uplink's own name (sw-home, not
// sw-128), one comment header per block naming what produced it.

import { macFromV4, segmentBackboneNet, uplinkBackboneNet } from "./addressing.ts";
import {
  backdoorBridge,
  emitBackdoorNat,
  emitUplinkNetns,
  uplinkTransferBridge,
} from "./generate-netns.ts";
import type { NetworkDefinition } from "./define.ts";
import type { DhcpClient, Host, Segment, Uplink } from "./types.ts";

/** One (network_name, OVS bridge) pair that must appear in
 * ovn-bridge-mappings — otherwise ovn-controller has no physical
 * bridge to patch a localnet port into and it silently never carries
 * traffic. Collected once per host across segments AND uplink
 * transfer links, then emitted as a single external-ids write (the
 * mapping is one comma-joined string, not additive across calls). */
interface BridgeMapping {
  readonly networkName: string;
  readonly bridge: string;
}

function v4Prefix(addrIndex: { ipv4: { to_string(): string } }): string {
  return addrIndex.ipv4.to_string();
}
function v6Prefix(addrIndex: { ipv6: { to_string(): string } }): string {
  return addrIndex.ipv6.to_string();
}

// `ovn-nbctl --may-exist lrp-add` is NOT actually idempotent across a
// config change: it only no-ops when the EXISTING port's mac AND
// networks both already match exactly — if either changed (e.g. a
// segment's gatewaySuffix moves from .2 to .1, which folds into a
// different mac too, see macFromV4), it errors out instead of
// reconfiguring the port. Confirmed live, 2026-07-06, after regenerating
// "neighbor" with a new gatewaySuffix:
//   ovn-nbctl: lrp-neighbor: port already exists with mac 00:00:c0:a8:82:02
// Rather than re-implementing ovn-nbctl's own mac/networks comparison
// ourselves (which would mean parsing `--bare --columns=...` output in
// the generated shell, fragile and unverified against a live box),
// unconditionally drop and re-add the port — simple and always
// correct. Nothing else is lost by the brief drop: gateway-chassis
// assignment (emitGatewayChassis), the ipv6_ra_configs toggle, and the
// switch-side lsp's router-port option are all re-applied
// unconditionally right after, every time this script runs, regardless
// of whether the port was just recreated or already existed unchanged.
function emitIdempotentLrpAdd(
  router: string,
  lrp: string,
  mac: string,
  v4: string,
  v6: string,
): string[] {
  return [
    `ovn-nbctl --if-exists lrp-del ${lrp}`,
    `ovn-nbctl lrp-add ${router} ${lrp} ${mac} ${v4} ${v6}`,
  ];
}

// Every logical router port that faces a localnet network (segment
// client-facing side, segment/uplink backbone-facing side, uplink
// transfer-link side) needs a gateway chassis assignment, or
// ovn-controller has no scheduling decision to act on for that port at
// all and never programs ARP-responder/patch flows for it — confirmed
// live, this session: gateway_chassis was "[]" on every lrp after a
// full topology apply, and `ovs-ofctl dump-flows br-int` had zero
// flows for the affected network, i.e. the very first hop (segment
// gateway IP) was unreachable. This mirrors what the original
// hand-built setup-ovn-topology.sh did unconditionally for every
// router port and the generator had dropped. $CHASSIS is resolved
// once, in the emitted shell script itself (see scriptForHost), not
// here — this generator never queries live state (ADR 0001 §2).
function emitGatewayChassis(lrp: string): string[] {
  return [
    `ovn-nbctl lrp-set-gateway-chassis ${lrp} "$CHASSIS" 100`,
    `ovn-nbctl set logical_router_port ${lrp} options:reside-on-redirect-chassis=true`,
  ];
}

// ── interface setup: segment's real kernel interface -> OVS bridge ──
// No netplan — see header comment. Uplinks are NOT handled here; their
// real interface is owned by ovn_uplink_sync.py's netns, not an OVS
// bridge at all.

function emitSegmentInterface(s: Segment): string[] {
  const br = `br-${s.name}`;
  const lines: string[] = [`# --- segment interface: ${s.name} ---`];

  if (s.if.kind === "physical") {
    lines.push(
      `ovs-vsctl --may-exist add-br ${br}`,
      `ovs-vsctl set bridge ${br} fail-mode=standalone`,
      `ovs-vsctl --may-exist add-port ${br} ${s.if.name}`,
    );
  } else if (s.if.kind === "vlan") {
    // ifaceName override lets a segment reuse a VLAN tag that already
    // has a differently-purposed subinterface elsewhere (e.g. VLAN 129
    // already carries this host's own management IP on ens18.129,
    // outside OVS) without colliding with it — see InterfaceKind,
    // types.ts.
    const vlanIface = s.if.ifaceName ?? `${s.if.vlanParent}.${s.if.vlanId}`;
    lines.push(
      `ip link show ${vlanIface} >/dev/null 2>&1 || ` +
        `ip link add link ${s.if.vlanParent} name ${vlanIface} ` +
        `type vlan id ${s.if.vlanId}`,
      `ip link set ${vlanIface} up`,
      `ovs-vsctl --may-exist add-br ${br}`,
      `ovs-vsctl set bridge ${br} fail-mode=standalone`,
      `ovs-vsctl --may-exist add-port ${br} ${vlanIface}`,
    );
  } else {
    lines.push(
      `# unsupported segment interface kind "${s.if.kind}" for ${s.name} ` +
        `— skipped, see generate-ovn.ts`,
    );
  }

  lines.push("");
  return lines;
}

// ── interface setup: uplink transfer-link veth -> OVS bridge ────────
// The OVN-side veth itself (veth-uplink-<name>) is created later, in
// the netns tier (generate-netns.ts emitUplinkNetns) — but the bridge
// it attaches to, and that bridge's entry in ovn-bridge-mappings, must
// exist before the OVN topology section runs (same ordering rule as
// segment interfaces above). The veth is added as a port to this
// bridge by emitUplinkNetns itself once it creates the veth.

function emitUplinkTransferInterface(u: Uplink): string[] {
  const br = uplinkTransferBridge(u);
  return [
    `# --- uplink transfer-link interface: ${u.name} ---`,
    `ovs-vsctl --may-exist add-br ${br}`,
    `ovs-vsctl set bridge ${br} fail-mode=standalone`,
    "",
  ];
}

// ── interface setup: backdoor veth -> its OWN OVS bridge ────────────
// See Backdoor (types.ts). Same "bridge must exist before OVN config
// binds a localnet port to it" ordering rule as every other interface
// setup in this file — the veth itself is created later, in
// generate-netns.ts's emitBackdoorVethAndRoute (called from within
// emitUplinkNetns, since it IS the uplink's real interface — see
// realIfaceFor, generate-netns.ts).

function emitBackdoorInterface(u: Uplink): string[] {
  if (u.backdoor === undefined) return [];
  const br = backdoorBridge(u.backdoor);
  return [
    `# --- backdoor interface: ${u.name} -> ${u.backdoor.via.name} ---`,
    `ovs-vsctl --may-exist add-br ${br}`,
    `ovs-vsctl set bridge ${br} fail-mode=standalone`,
    "",
  ];
}

// ── tier 1: segment's own router/switch (OVN logical config) ────────

function emitSegment(s: Segment): string[] {
  const sw = `sw-${s.name}`;
  const router = `router-${s.name}`;
  const lspLocalnet = `lsp-${s.name}-localnet`;
  const lspRouter = `lsp-${s.name}-router`;
  const lrp = `lrp-${s.name}`;
  const mac = macFromV4(s.addresses[0].ipv4);
  const networkName = `seg-${s.name}`;

  const lines = [
    `# --- segment: ${s.name} ---`,
    `ovn-nbctl --may-exist ls-add ${sw}`,
    `ovn-nbctl --may-exist lsp-add ${sw} ${lspLocalnet}`,
    `ovn-nbctl lsp-set-type ${lspLocalnet} localnet`,
    `ovn-nbctl lsp-set-addresses ${lspLocalnet} unknown`,
    `ovn-nbctl lsp-set-options ${lspLocalnet} network_name=${networkName}`,
    `ovn-nbctl --may-exist lr-add ${router}`,
    ...emitIdempotentLrpAdd(
      router,
      lrp,
      mac,
      v4Prefix(s.addresses[0]),
      v6Prefix(s.addresses[0]),
    ),
    ...emitGatewayChassis(lrp),
  ];

  // SLAAC: clients on this segment self-configure their global IPv6
  // address from Router Advertisements OVN sends on lrp's behalf. The
  // advertised prefix is taken directly from lrp's own IPv6 network
  // above — no separate DHCP_Options object needed (this is the
  // opposite direction from an uplink's netns, which RECEIVES RA from
  // the ISP — see ADR 0001 / transfer-links.md). Per-segment
  // configurable (Segment.slaac) since not every segment should
  // necessarily advertise — e.g. disabled for "home".
  //
  // ipv6_ra_configs:send_periodic — ENABLED as of 2026-07-11.
  //
  // This used to be a confirmed no-op, deliberately left unset: every
  // logical router compiles an unconditional, very-high-priority flow —
  // `lr_in_ip_routing, priority=10550, match=(nd_rs || nd_ra),
  // action=drop` — meant to stop RS/RA from ever being routed across
  // router ports. Solicited RA (the lr_in_nd_ra_options/
  // lr_in_nd_ra_response responder, driven by address_mode alone)
  // answers via direct loopback output and never reaches that stage, so
  // it worked regardless. But `send_periodic`'s self-timer-driven RA is
  // pinctrl-injected via `resubmit(CONTROLLER,8)`, re-enters the
  // pipeline near the top, and got killed by that same drop rule before
  // ever reaching a physical port — confirmed live 2026-07-07 (pinctrl:dbg
  // showed the RA being generated on schedule with the correct MAC/LLA;
  // ovn-sbctl lflow-list showed the drop rule verbatim; it never
  // appeared on the wire).
  //
  // Root cause (upstream, Ilya Maximets, ovn-org/ovn#313, 2026-07-09):
  // controller/pinctrl.c's prepare_ipv6_ras() only marks a periodic RA
  // packet "preserved" (exempt from that drop rule) for
  // l2gateway/l3gateway/chassisredirect port types — missing the DGP
  // (distributed gateway port, type "patch") case every segment router
  // here actually is, so the local-only bit stayed set and the packet
  // was dropped like any other. Fixed upstream and installed as a local
  // patched build on mam-hh-ovn (26.03.0-2+ravlocal11) — confirmed live
  // 2026-07-11: enabling send_periodic (with a short interval, one LRP
  // at a time) produced genuinely unsolicited RA (no preceding RS) on
  // the physical wire, captured via tcpdump on the segment's own bridge
  // and independently confirmed on a MikroTik packet trace on the same
  // segment. Safe to turn on for real now. A host-side radvd is still
  // not a viable alternative either way: the router's IP/MAC here is a
  // pure OVN logical construct with no backing Linux kernel netdev to
  // bind radvd to.
  //
  // min_interval/max_interval are left unset (OVN's own RFC 4861
  // defaults apply) — tune these explicitly later if periodic
  // notification needs to be faster, e.g. for the gateway-cutover
  // re-solicit problem this also helps with (an already-connected client
  // whose default router changed underneath it has no OTHER way to find
  // out short of that entry's advertised lifetime expiring).
  if (s.slaac) {
    lines.push(
      `ovn-nbctl set logical_router_port ${lrp} ipv6_ra_configs:address_mode=slaac`,
      `ovn-nbctl set logical_router_port ${lrp} ipv6_ra_configs:send_periodic=true`,
    );
  } else {
    lines.push(
      `ovn-nbctl remove logical_router_port ${lrp} ipv6_ra_configs address_mode`,
      `ovn-nbctl remove logical_router_port ${lrp} ipv6_ra_configs send_periodic`,
    );
  }

  lines.push(
    `ovn-nbctl --may-exist lsp-add ${sw} ${lspRouter}`,
    `ovn-nbctl lsp-set-type ${lspRouter} router`,
    `ovn-nbctl lsp-set-addresses ${lspRouter} router`,
    `ovn-nbctl lsp-set-options ${lspRouter} router-port=${lrp}`,
    "",
  );

  return lines;
}

// ── tier 3: uplink's own router/switch (transfer link, OVN side) ────
// NOT covered here: the netns side (dhclient/SLAAC, NAT, back-routes —
// kernel-side, not OVN config at all — see ovn_uplink_sync.py).

function emitUplinkTransfer(u: Uplink): string[] {
  const sw = `sw-uplink-${u.name}-transfer`;
  const lrp = `lrp-uplink-${u.name}-transfer`;
  const lspLocalnet = `lsp-uplink-${u.name}-transfer-localnet`;
  const lspRouter = `lsp-uplink-${u.name}-transfer-router`;
  const router = `router-uplink-${u.name}`;
  const ovnSide = u.addresses[0]; // host=1, see factories.ts
  const netnsSide = u.addresses[1]; // host=2 — the netns peer, real next-hop
  const mac = macFromV4(ovnSide.ipv4);
  const networkName = `seg-uplink-${u.name}-transfer`;

  return [
    `# --- uplink: ${u.name} (transfer link, OVN side) ---`,
    `ovn-nbctl --may-exist ls-add ${sw}`,
    `ovn-nbctl --may-exist lsp-add ${sw} ${lspLocalnet}`,
    `ovn-nbctl lsp-set-type ${lspLocalnet} localnet`,
    `ovn-nbctl lsp-set-addresses ${lspLocalnet} unknown`,
    `ovn-nbctl lsp-set-options ${lspLocalnet} network_name=${networkName}`,
    `ovn-nbctl --may-exist lr-add ${router}`,
    ...emitIdempotentLrpAdd(
      router,
      lrp,
      mac,
      v4Prefix(ovnSide),
      v6Prefix(ovnSide),
    ),
    ...emitGatewayChassis(lrp),
    `ovn-nbctl --may-exist lsp-add ${sw} ${lspRouter}`,
    `ovn-nbctl lsp-set-type ${lspRouter} router`,
    `ovn-nbctl lsp-set-addresses ${lspRouter} router`,
    `ovn-nbctl lsp-set-options ${lspRouter} router-port=${lrp}`,
    // Outbound default route: everything beyond the directly-connected
    // transfer /28 and the backbone /16 must go to the netns peer —
    // without this the uplink router has literally zero idea how to
    // reach the internet at all. Confirmed live: a missing default
    // route showed up as `ovn-nbctl lr-route-list router-uplink-<name>`
    // coming back completely empty after a full topology apply — outbound traffic
    // reaching this router had nowhere to go.
    `ovn-nbctl --may-exist lr-route-add ${router} 0.0.0.0/0 ${netnsSide.ipv4.to_s()}`,
    `ovn-nbctl --may-exist lr-route-add ${router} ::/0 ${netnsSide.ipv6.to_s()}`,
    "",
  ];
}

// ── backdoor: borrowed egress, OVN side ─────────────────────────────
// See Backdoor (types.ts). Structurally identical to emitUplinkTransfer
// above — a localnet-backed logical switch plus a router port with a
// gateway chassis — except the router port lands on the `via` uplink's
// ALREADY-EXISTING router, not a new one, and there's no default route
// to add here: `via`'s router already has its own (0.0.0.0/0 -> its own
// netns, from emitUplinkTransfer when `via` itself was emitted), and
// that's exactly the route this borrowed traffic should also use.

function emitBackdoor(u: Uplink): string[] {
  if (u.backdoor === undefined) return [];
  const bd = u.backdoor;

  const sw = `sw-backdoor-${u.name}`;
  const lrp = `lrp-backdoor-${u.name}`;
  const lspLocalnet = `lsp-backdoor-${u.name}-localnet`;
  const lspRouter = `lsp-backdoor-${u.name}-router`;
  const viaRouter = `router-uplink-${bd.via.name}`;
  const ovnSide = bd.addresses[0]; // host=1, lives on `via`'s router
  const mac = macFromV4(ovnSide.ipv4);
  const networkName = `seg-backdoor-${u.name}`;

  return [
    `# --- backdoor: ${u.name} -> ${bd.via.name} (borrowed egress, OVN side) ---`,
    `ovn-nbctl --may-exist ls-add ${sw}`,
    `ovn-nbctl --may-exist lsp-add ${sw} ${lspLocalnet}`,
    `ovn-nbctl lsp-set-type ${lspLocalnet} localnet`,
    `ovn-nbctl lsp-set-addresses ${lspLocalnet} unknown`,
    `ovn-nbctl lsp-set-options ${lspLocalnet} network_name=${networkName}`,
    ...emitIdempotentLrpAdd(
      viaRouter,
      lrp,
      mac,
      v4Prefix(ovnSide),
      v6Prefix(ovnSide),
    ),
    ...emitGatewayChassis(lrp),
    `ovn-nbctl --may-exist lsp-add ${sw} ${lspRouter}`,
    `ovn-nbctl lsp-set-type ${lspRouter} router`,
    `ovn-nbctl lsp-set-addresses ${lspRouter} router`,
    `ovn-nbctl lsp-set-options ${lspRouter} router-port=${lrp}`,
    "",
  ];
}

// ── tier 2: backbone join (segment <-> currently-resolved uplink) ───
//
// One segment can now have MULTIPLE simultaneous backbone joins: its
// primary `uplink` (default route + NAT), plus zero or more
// `extraRoutes` entries (a more-specific prefix into a SECONDARY
// uplink — e.g. a private supernet into a ZeroTier-mesh uplink — see
// ExtraRoute, types.ts). Each join needs its OWN object names (so a
// segment joining two different uplinks doesn't reuse one LRP/LSP name
// for both) and its OWN segment-side backbone address (so two joins
// for the same segment don't claim the identical address on the
// shared sw-backbone bus) — `nameSuffix`/`segBackboneHost` below exist
// for exactly that: the primary join keeps today's exact names/host=1
// (nameSuffix "", so nothing about an already-working segment's
// primary join changes), and each extra route gets a distinct suffix
// and host offset (2, 3, ...).

interface BackboneRoute {
  readonly v4Prefix: string;
  /** Omit for a v4-only route (e.g. a private-supernet extra route
   * with no IPv6 equivalent declared). */
  readonly v6Prefix?: string;
}

function emitBackboneJoin(
  s: Segment,
  resolved: Uplink,
  segBackboneHost: number,
  nameSuffix: string,
  routes: readonly BackboneRoute[],
): string[] {
  const segRouter = `router-${s.name}`;
  const segLrpBb = `lrp-${s.name}-bb${nameSuffix}`;
  const segLspBb = `lsp-backbone-${s.name}${nameSuffix}`;

  const upRouter = `router-uplink-${resolved.name}`;
  const upLrpBb = `lrp-uplink-${resolved.name}-bb`;
  const upLspBb = `lsp-backbone-uplink-${resolved.name}`;

  const segId = s.addresses[0].id();
  const segBackbone = segmentBackboneNet(segId, segBackboneHost);
  const upBackbone = uplinkBackboneNet(
    resolved.addresses[0].id(),
    resolved.slot,
    1,
  );

  const segMac = macFromV4(segBackbone.ipv4);
  const upMac = macFromV4(upBackbone.ipv4);

  const routeLines = routes.flatMap((r) => {
    const lines = [
      `ovn-nbctl --may-exist lr-route-add ${segRouter} ${r.v4Prefix} ${upBackbone.ipv4.to_s()}`,
    ];
    if (r.v6Prefix !== undefined) {
      lines.push(
        `ovn-nbctl --may-exist lr-route-add ${segRouter} ${r.v6Prefix} ${upBackbone.ipv6.to_s()}`,
      );
    }
    return lines;
  });

  return [
    `# --- backbone join: segment ${s.name} -> uplink ${resolved.name}${
      nameSuffix ? ` (${nameSuffix.replace(/^-/, "")})` : ""
    } ---`,
    ...emitIdempotentLrpAdd(
      segRouter,
      segLrpBb,
      segMac,
      v4Prefix(segBackbone),
      v6Prefix(segBackbone),
    ),
    ...emitGatewayChassis(segLrpBb),
    `ovn-nbctl --may-exist lsp-add sw-backbone ${segLspBb}`,
    `ovn-nbctl lsp-set-type ${segLspBb} router`,
    `ovn-nbctl lsp-set-addresses ${segLspBb} router`,
    `ovn-nbctl lsp-set-options ${segLspBb} router-port=${segLrpBb}`,
    ...emitIdempotentLrpAdd(
      upRouter,
      upLrpBb,
      upMac,
      v4Prefix(upBackbone),
      v6Prefix(upBackbone),
    ),
    ...emitGatewayChassis(upLrpBb),
    `ovn-nbctl --may-exist lsp-add sw-backbone ${upLspBb}`,
    `ovn-nbctl lsp-set-type ${upLspBb} router`,
    `ovn-nbctl lsp-set-addresses ${upLspBb} router`,
    `ovn-nbctl lsp-set-options ${upLspBb} router-port=${upLrpBb}`,
    ...routeLines,
    // Backroute: the uplink router has no other way to learn how to
    // reach this segment's client subnet — it's two hops away
    // (uplink router -> sw-backbone -> segment router -> segment
    // switch), not a directly-connected network from the uplink
    // router's point of view, so nothing auto-populates this.
    // Confirmed live, this session, from BOTH directions: `ovn-nbctl
    // lr-route-list` on the uplink router came back completely empty,
    // and independently, a real client on the segment saw `traceroute`
    // reach the uplink router's backbone address (10.80.0.9) but then
    // get no further response — the uplink router had received the
    // packet and generated a TTL-exceeded reply, but had nowhere to
    // route that reply back to, so it silently dropped it. Emitted
    // unconditionally for EVERY join (primary or extra route) — the
    // secondary uplink needs this just as much as the primary one
    // does, regardless of which prefix(es) it's carrying.
    // `ovn-nbctl lr-route-add` normalizes a host+prefix (e.g.
    // 192.168.128.2/24) down to the true network address itself —
    // confirmed live — so v4Prefix(s.addresses[0]) is safe to pass
    // directly here without a separate network-address helper.
    `ovn-nbctl --may-exist lr-route-add ${upRouter} ${v4Prefix(s.addresses[0])} ${segBackbone.ipv4.to_s()}`,
    `ovn-nbctl --may-exist lr-route-add ${upRouter} ${v6Prefix(s.addresses[0])} ${segBackbone.ipv6.to_s()}`,
    "",
  ];
}

function emitSegmentBackboneJoin(s: Segment): string[] {
  const lines: string[] = [];

  // No uplink assigned yet — deliberately no backbone join, no route,
  // no NAT for this segment (see Segment.uplink, types.ts). Isolated
  // is the safe default until a real uplink (e.g. a WireGuard VPN
  // tunnel) exists for it, rather than falling through to whichever
  // uplink happens to be declared elsewhere in the config.
  if (s.uplink === undefined) {
    lines.push(`# --- segment ${s.name}: no uplink assigned, isolated ---`, "");
  } else {
    lines.push(
      ...emitBackboneJoin(s, s.uplink.resolve(), 1, "", [
        { v4Prefix: "0.0.0.0/0", v6Prefix: "::/0" },
      ]),
    );
  }

  // Extra routes: each gets its own backbone join, its own segment-side
  // backbone host offset (2, 3, ... — 1 is taken by the primary join
  // above, or would be if this segment ever gets one later), and a
  // name suffix that includes BOTH the entry's index and the target
  // uplink's name. The index is required, not cosmetic: two
  // extraRoutes entries for the same segment CAN target the same
  // uplink (e.g. one route for the mesh's client subnets, a second
  // narrower one just for that uplink's own transfer-link block, for
  // debugging) — suffixing by uplink name alone would collide in that
  // case. segBackboneNet's host range is 1-7 (see addressing.ts) —
  // plenty for the primary join plus a handful of extra routes per
  // segment.
  (s.extraRoutes ?? []).forEach((extra, i) => {
    const resolved = extra.uplink.resolve();
    lines.push(
      ...emitBackboneJoin(s, resolved, i + 2, `-extra-${i}-${resolved.name}`, [
        { v4Prefix: extra.prefix, v6Prefix: extra.prefix6 },
      ]),
    );
  });

  return lines;
}

// ── required OS packages ────────────────────────────────────────────
// Two different consumers, deliberately kept apart (2026-07-06, per
// explicit feedback):
//
//   - emitAptInstall: actually installs anything missing (apt-get).
//     Slow, needs network, and can block on a held apt/dpkg lock
//     (unattended-upgrades, etc.) — none of that is acceptable at boot
//     time, before the backbone router comes up. So this is emitted
//     ONLY in scriptForHost's manual/first-run branch (the `$1 !=
//     --setup-only` block, BEFORE systemctl takes over) — never on
//     the systemd/boot path.
//
//   - emitPreflightChecks: the fast, boot-safe counterpart used on the
//     systemd/`--setup-only` path instead. Purely informational — no
//     apt-get, just `dpkg -s`, every check capped at `timeout 5` and
//     never allowed to fail the script — a missing package or a stuck
//     dpkg lock produces a WARNING on stderr and setup continues
//     regardless, so this can never block (or be blocked by) the
//     backbone router/OVN setup that follows it.
//
// Both share the same package list — iproute2 (`ip`) and iptables
// (`iptables`/`ip6tables`) are hard, unconditional dependencies of
// nearly every line this generator emits, so they're always included;
// openvswitch-switch/ovn-central/ovn-host are ALSO unconditional (added
// 2026-07-19) — every line emitOvsBridge*/emitSegment/emitUplink* etc.
// produces is an ovs-vsctl/ovn-nbctl/ovn-sbctl command, and the
// self-installed systemd unit's own `After=/Wants=
// openvswitch-switch.service ovn-central.service` already assumed these
// packages exist — this generator just never actually installed them
// itself, leaving that as an undocumented manual bootstrap step (the
// gap that motivated this change: confirmed live on mam-hh-core, a
// brand-new host, where none of ovs-vsctl/ovn-nbctl/ovn-controller nor
// the openvswitch-switch/ovn-central/ovn-host packages existed at all
// until installed by hand). Bundling all three together matches this
// project's current single-chassis assumption (see header comment) —
// ovn-central (northd + the NB/SB databases) only needs to run on ONE
// host in a real multi-chassis topology, while every OTHER chassis
// would need just openvswitch-switch + ovn-host (ovn-controller
// pointing at the central host's ovn-remote, not a local ovn-central of
// its own). Splitting that out is a real, un-implemented TODO for
// whenever multi-chassis actually happens — not needed today, since
// every Host this generator currently targets runs its own full stack.
// dhclient/dhcpcd/wireguard-tools stay conditional, added only when
// this topology actually uses them (derived from every uplink's
// discovery.client, falling back to dhclient's package when
// discovery.ipv4 is "dhcp" with no explicit client — same default
// emitIpv4Discovery uses, see generate-netns.ts — and from whether any
// uplink is InterfaceKind "wireguard"), not a fixed list.

const ALWAYS_REQUIRED_PACKAGES = [
  "iproute2",
  "iptables",
  "openvswitch-switch",
  "ovn-central",
  "ovn-host",
];

const CLIENT_PACKAGE: Record<Exclude<DhcpClient, "static">, string> = {
  dhclient: "isc-dhcp-client",
  dhcpcd: "dhcpcd5",
};

function requiredPackages(uplinks: readonly Uplink[]): string[] {
  const packages = new Set(ALWAYS_REQUIRED_PACKAGES);
  for (const u of uplinks) {
    const client = u.discovery?.client;
    if (client === "dhclient" || client === "dhcpcd") {
      packages.add(CLIENT_PACKAGE[client]);
    } else if (client === undefined && u.discovery?.ipv4 === "dhcp") {
      packages.add(CLIENT_PACKAGE.dhclient);
    }
    if (u.if.kind === "wireguard") packages.add("wireguard-tools");
    if (u.if.kind === "zerotier") {
      // "zerotier-one" is deliberately included here (for the boot-safe
      // preflight check, emitPreflightChecks below — a plain `dpkg -s`
      // works regardless of how it got installed) but excluded from
      // emitAptInstall's plain `apt-get install` line below: it isn't
      // installable that way with no ZeroTier apt repo configured yet
      // — see emitZerotierInstall. "jq" IS a normal apt package (used
      // to parse `zerotier-cli -j listnetworks`, see
      // emitZerotierInterface, generate-netns.ts), so it stays in the
      // regular apt-get line.
      packages.add("zerotier-one");
      packages.add("jq");
    }
  }
  return [...packages].sort();
}

/** Manual/first-run only — see header comment above. Indented two
 * spaces throughout: spliced directly inside scriptForHost's `$1 !=
 * --setup-only` block. */
function emitAptInstall(uplinks: readonly Uplink[]): string[] {
  const packages = requiredPackages(uplinks).filter((p) => p !== "zerotier-one");
  const lines = [
    "  # install required packages (this branch only runs on a manual,",
    "  # direct invocation — never on the systemd/boot path, see header",
    "  # comment on requiredPackages above)",
    `  apt-get install -y ${packages.join(" ")}`,
  ];
  lines.push(...emitZerotierInstall(uplinks));
  return lines;
}

/** Manual/first-run only, same as emitAptInstall above — only emitted
 * (non-empty) when this topology actually declares a "zerotier"-kind
 * uplink. ZeroTier isn't installable via a plain `apt-get install` with
 * no repo configured yet; the official installer adds the repo AND
 * installs the package in one step. Timeout-capped (curl, or the
 * installer itself, can hang indefinitely on a stalled connection) —
 * but this whole branch already only ever runs on a manual, direct
 * invocation, never the systemd/boot path (see header comment above),
 * so a slow or failed install here can't block startup either way. No
 * `sudo` in the pipeline: this whole script already runs as root (same
 * as the plain `apt-get install` line above, which also has none). */
function emitZerotierInstall(uplinks: readonly Uplink[]): string[] {
  if (!uplinks.some((u) => u.if.kind === "zerotier")) return [];
  return [
    "  # zerotier-one: no apt repo configured yet, so a plain apt-get",
    "  # can't install it -- the official installer adds the repo AND",
    "  # installs the package. Skipped if already present (idempotent,",
    "  # and avoids re-curling on every manual run).",
    "  dpkg -s zerotier-one >/dev/null 2>&1 || " +
      'timeout 120 bash -c "curl -s https://install.zerotier.com | bash"',
    "  # The installer enables+starts the package's own main",
    "  # zerotier-one.service, running as a HOST-level daemon against",
    "  # the default /var/lib/zerotier-one -- completely separate from,",
    "  # and not needed alongside, the per-uplink instance(s) this",
    "  # script manages itself inside each uplink's own netns (see",
    "  # emitZerotierInterface, generate-netns.ts). Left running, it's",
    "  # a second, pointless ZT identity nobody authorizes or uses.",
    "  # `disable --now` is idempotent (safe even if already disabled)",
    "  # and covers both 'never start at boot' and 'stop it now'.",
    "  systemctl disable --now zerotier-one.service 2>/dev/null || true",
  ];
}

/** Systemd/boot path only — see header comment above. */
function emitPreflightChecks(uplinks: readonly Uplink[]): string[] {
  const lines = [
    "# ── preflight: required tooling present? (best-effort, never ───",
    "# blocks startup — 5s cap per package, see header comment) ─────",
    "check_pkg() {",
    '  timeout 5 dpkg -s "$1" >/dev/null 2>&1 || ' +
      'echo "WARNING: package \\"$1\\" not detected (or check timed out) -- continuing anyway" >&2',
    "}",
  ];
  for (const pkg of requiredPackages(uplinks)) lines.push(`check_pkg ${pkg}`);
  lines.push("");
  return lines;
}

/**
 * IPFIX export off br-int — see HostMonitoring/IpfixExport (types.ts)
 * for why this is host-level and br-int-scoped rather than per-segment.
 * Idempotent by construction: `--if-exists clear ... ipfix` drops any
 * previously-created IPFIX row for br-int before creating a fresh one,
 * so re-running this never accumulates orphaned rows in the OVSDB —
 * same "converge, don't accumulate" shape as every other emit* here.
 */
function emitMonitoring(host: Host): string[] {
  const ipfix = host.monitoring?.ipfix;
  if (ipfix === undefined) return [];

  const createArgs = [`targets="${ipfix.target}"`];
  if (ipfix.sampling !== undefined) createArgs.push(`sampling=${ipfix.sampling}`);
  if (ipfix.cacheActiveTimeout !== undefined) {
    createArgs.push(`cache_active_timeout=${ipfix.cacheActiveTimeout}`);
  }
  if (ipfix.cacheMaxFlows !== undefined) createArgs.push(`cache_max_flows=${ipfix.cacheMaxFlows}`);

  return [
    "# ── monitoring: IPFIX export off br-int ─────────────────────────",
    "ovs-vsctl -- --if-exists clear Bridge br-int ipfix \\",
    `  -- --id=@ipfix create IPFIX ${createArgs.join(" ")} \\`,
    "  -- set Bridge br-int ipfix=@ipfix",
    "",
  ];
}

/**
 * Chassis registration — the OTHER undocumented manual bootstrap step
 * this generator never emitted (added 2026-07-19, same day as the
 * OVS/OVN package-install fix above, and for the same reason: confirmed
 * live on mam-hh-core that `ovn-controller` can be fully installed AND
 * running and STILL never register a Chassis row in the SB database,
 * because none of `ovn-remote`/`ovn-encap-type`/`ovn-encap-ip`/
 * `ovn-cms-options` were set. Without a registered chassis, br-int gets
 * zero patch ports and NOTHING routes, anywhere — not a segment-specific
 * symptom, the whole host is dark. `ovn-sbctl show`/`list Chassis` being
 * empty despite ovn-northd showing thousands of idl-cells of real
 * southbound data is the tell: the data's there, nothing has claimed it.
 *
 * `system-id` is NOT set here — ovn-host's own postinst already
 * generates and persists that automatically on first package install
 * (confirmed present on both mam-hh-ovn and mam-hh-core without this
 * generator ever touching it), so re-deriving or overwriting it here
 * would only risk breaking an already-stable chassis identity.
 *
 * `ovn-encap-type=geneve` is hardcoded, not configurable — every real
 * deployment so far uses it and there's no declared need for an
 * alternative (stt/vxlan) yet; add a Host field if that ever changes.
 *
 * `ovn-remote` defaults to the local SB socket — matches this project's
 * current single-chassis assumption (see header comment and
 * requiredPackages' doc comment above): every Host this generator
 * targets runs its own full ovn-central alongside ovn-controller. A
 * real multi-chassis topology would need this to point at the
 * DESIGNATED central host's SB db (e.g. tcp:<central-ip>:6642) for
 * every OTHER chassis — an un-implemented TODO, same one flagged
 * against ovn-central in ALWAYS_REQUIRED_PACKAGES above.
 *
 * `ovn-cms-options=enable-chassis-as-gw` is unconditional: every router
 * port this generator emits gets `lrp-set-gateway-chassis` (see
 * emitGatewayChassis above) — a chassis cannot be scheduled for ANY of
 * them without this flag, and every Host here is (today) the only
 * chassis in its topology, so it must always be gateway-eligible.
 */
function emitChassisRegistration(host: Host): string[] {
  return [
    "# ── chassis registration (ovn-controller needs ALL of this to ───",
    "# actually register in the SB database — see emitChassisRegistration",
    "# doc comment, generate-ovn.ts, for why this silently leaves the",
    "# whole host dark otherwise) ────────────────────────────────────",
    'ovs-vsctl set open_vswitch . external-ids:ovn-remote="unix:/var/run/ovn/ovnsb_db.sock"',
    "ovs-vsctl set open_vswitch . external-ids:ovn-encap-type=geneve",
    `ovs-vsctl set open_vswitch . external-ids:ovn-encap-ip="${host.address}"`,
    "ovs-vsctl set open_vswitch . external-ids:ovn-cms-options=enable-chassis-as-gw",
    "",
  ];
}

// ── per-host assembly: one self-installing script ────────────────────

function scriptForHost(
  net: NetworkDefinition,
  host: Host,
  segments: readonly Segment[],
  uplinks: readonly Uplink[],
): string {
  const unitName = `topology-${net.name}`;
  const selfPath = `/usr/local/sbin/topologie-gen/${unitName}.sh`;

  const interfaceLines: string[] = [];
  const bridgeMappings: BridgeMapping[] = [];

  for (const s of segments) {
    interfaceLines.push(...emitSegmentInterface(s));
    bridgeMappings.push({ networkName: `seg-${s.name}`, bridge: `br-${s.name}` });
  }
  for (const u of uplinks) {
    interfaceLines.push(...emitUplinkTransferInterface(u));
    bridgeMappings.push({
      networkName: `seg-uplink-${u.name}-transfer`,
      bridge: uplinkTransferBridge(u),
    });
    if (u.backdoor !== undefined) {
      interfaceLines.push(...emitBackdoorInterface(u));
      bridgeMappings.push({
        networkName: `seg-backdoor-${u.name}`,
        bridge: backdoorBridge(u.backdoor),
      });
    }
  }

  // Single external-ids write — ovn-bridge-mappings is one
  // comma-joined string, not additive, so every (network_name, bridge)
  // pair for this host must be collected before emitting it. Without
  // this, every localnet port above binds to nothing.
  if (bridgeMappings.length > 0) {
    const mappingValue = bridgeMappings
      .map((m) => `${m.networkName}:${m.bridge}`)
      .join(",");
    interfaceLines.push(
      "# bridge-mappings: tells ovn-controller which physical OVS " +
        "bridge each network_name (used by localnet ports above) binds to",
      `ovs-vsctl set open_vswitch . external-ids:ovn-bridge-mappings=${mappingValue}`,
      "",
    );
  }

  const ovnLines: string[] = [];
  if (segments.length > 0) ovnLines.push("ovn-nbctl --may-exist ls-add sw-backbone", "");
  for (const segment of segments) ovnLines.push(...emitSegment(segment));
  for (const uplink of uplinks) {
    ovnLines.push(...emitUplinkTransfer(uplink));
    // Must come after emitUplinkTransfer above, in this same pass: a
    // backdoor attaches to its `via` uplink's router, which only
    // exists once `via`'s own emitUplinkTransfer has run — guaranteed
    // by NetworkBuilder requiring `via` to be declared (and therefore
    // iterated over) earlier (see define.ts).
    ovnLines.push(...emitBackdoor(uplink));
  }
  for (const segment of segments) {
    ovnLines.push(...emitSegmentBackboneJoin(segment));
  }

  // netns tier: per uplink, derive which segments currently resolve to
  // it (AT GENERATION TIME, same rule as the backbone join above) and
  // set up its netns/veth/SLAAC/dhclient/NAT accordingly.
  const netnsLines: string[] = [];
  for (const uplink of uplinks) {
    const segmentsOnThisUplink = segments.filter(
      (s) => s.uplink?.resolve().name === uplink.name,
    );
    netnsLines.push(...emitUplinkNetns(uplink, segmentsOnThisUplink));
    // Must come after `via`'s OWN netns section has already run (its
    // NAT rule targets `via`'s netns directly) — guaranteed by the same
    // declaration-order requirement as emitBackdoor above. Only the NAT
    // rule is emitted here — the backdoor's veth/address/route setup
    // now happens INSIDE emitUplinkNetns above, since it IS this
    // uplink's real interface (see realIfaceFor, generate-netns.ts).
    netnsLines.push(...emitBackdoorNat(uplink, segmentsOnThisUplink));
  }

  return [
    "#!/bin/sh",
    `# Generated by topology-gen from network "${net.name}", host "${host.name}".`,
    "# ONE file: copy this to the host and run it. It sets up OVS",
    "# bridges/interfaces for each segment (no netplan), configures the",
    "# full OVN topology (segments, uplink transfer links, backbone",
    "# joins), sets up each uplink's netns/veth/SLAAC/dhclient/NAT, and",
    "# installs itself as a boot-time systemd unit so it re-applies on",
    "# every reboot. Idempotent: safe to re-run. Backbone joins and NAT",
    "# subnets reflect each segment's UplinkSelector resolution AT",
    "# GENERATION TIME — re-run after switching an uplink to regenerate",
    "# against the new target.",
    "set -e",
    "",
    "# ── self-install as a boot-time unit (idempotent) ──────────────",
    `mkdir -p "$(dirname ${selfPath})"`,
    // cp errors if $0 and selfPath are literally the same file — true
    // when this runs as the already-installed systemd unit (ExecStart
    // invokes selfPath directly, so $0 IS selfPath). cmp -s treats
    // "same file" as "identical", skipping the copy safely either way.
    `cmp -s "$0" "${selfPath}" 2>/dev/null || cp "$0" "${selfPath}"`,
    `chmod +x "${selfPath}"`,
    "",
    `cat > /etc/systemd/system/${unitName}.service << 'UNIT'`,
    "[Unit]",
    `Description=Topology setup for network "${net.name}" (generated, no netplan)`,
    "After=network-pre.target openvswitch-switch.service ovn-central.service",
    "Wants=openvswitch-switch.service ovn-central.service",
    "",
    "[Service]",
    "Type=oneshot",
    `ExecStart=${selfPath} --setup-only`,
    "RemainAfterExit=yes",
    "",
    "[Install]",
    "WantedBy=multi-user.target",
    "UNIT",
    "",
    "systemctl daemon-reload",
    `systemctl enable "${unitName}.service"`,
    "",
    'if [ "$1" != "--setup-only" ]; then',
    ...emitAptInstall(uplinks),
    `  systemctl start "${unitName}.service"`,
    "  exit 0",
    "fi",
    "",
    ...emitPreflightChecks(uplinks),
    ...emitChassisRegistration(host),
    "# ── interface setup (must run before OVN config below) ─────────",
    ...interfaceLines,
    "# ── OVN logical topology ────────────────────────────────────────",
    // Resolved once, here, in the emitted script — NOT queried by the
    // generator itself (ADR 0001 §2, generator never touches live
    // state). Every router port below needs this for gateway-chassis
    // scheduling; on a single-chassis host this is the only chassis.
    'CHASSIS=$(ovs-vsctl get open . external-ids:system-id | tr -d \'"\')',
    "",
    ...ovnLines,
    "# ── uplink netns setup (SLAAC/dhclient/NAT) ─────────────────────",
    ...netnsLines,
    ...emitMonitoring(host),
  ].join("\n");
}

/**
 * One self-installing script per distinct Host referenced by the
 * network's segments or uplinks. Keyed by host name. A single-chassis
 * topology will always return exactly one entry; nothing here assumes
 * that, so a multi-host topology "just works" by declaring more hosts.
 */
export function generateOvnScripts(
  net: NetworkDefinition,
): ReadonlyMap<string, string> {
  const hostsByName = new Map<string, Host>();
  for (const s of net.allSegments) hostsByName.set(s.host.name, s.host);
  for (const u of net.allUplinks) hostsByName.set(u.host.name, u.host);

  const scripts = new Map<string, string>();
  for (const host of hostsByName.values()) {
    const segments = net.allSegments.filter((s) => s.host.name === host.name);
    const uplinks = net.allUplinks.filter((u) => u.host.name === host.name);
    scripts.set(host.name, scriptForHost(net, host, segments, uplinks));
  }
  return scripts;
}
