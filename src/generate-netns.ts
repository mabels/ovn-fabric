// src/generate-netns.ts — emitUplinkNetns(): the netns-side setup
// commands for one uplink (creates its netns, moves its real interface
// in, wires up the transfer-link veth, brings up SLAAC/dhclient, and
// configures NAT for whichever segments currently resolve to it).
//
// Pulled into generate-ovn.ts's single per-host script (see that
// file's scriptForHost) rather than being its own standalone generator
// — "one file, copy it, run it, everything is done" applies to the
// whole topology, not per-tier. This file exists separately only to
// keep generate-ovn.ts from growing unwieldy; it has no CLI surface
// of its own.
//
// Scope: the netns side of each uplink's transfer link. This is the
// direction SLAAC/DHCP actually need real kernel mechanism, not OVN
// config:
//   - IPv4: a DHCP client started directly on the real interface,
//     inside the netns, backgrounded so this synchronous setup script
//     can finish while lease acquisition continues independently.
//     WHICH client (dhclient, dhcpcd, ...) is pluggable per uplink —
//     see Uplink.discovery.client (types.ts) and emitIpv4Discovery
//     below, which dispatches to one small emit function per client,
//     all with the same idempotent shape. (ovn_uplink_sync.py
//     supervises dhclient via a real systemd unit with Restart=always
//     — this generator does not replace that level of supervision,
//     only the one-time wiring.)
//   - IPv6: kernel SLAAC autoconf via accept_ra. Forwarding (needed for
//     routing between the veth and the real interface) SUPPRESSES RA
//     acceptance unless accept_ra is explicitly set to 2 — confirmed
//     live, this session — so this always sets accept_ra=2 on the real
//     interface for any uplink with discovery.ipv6=slaac.
//
// NAT subnets are NOT stored on Uplink itself — they're derived by the
// caller (generate-ovn.ts), at generation time, from which segments in
// the same NetworkDefinition currently resolve (via
// UplinkSelector.resolve()) to this uplink. Same "reflects resolution
// AT GENERATION TIME" rule as the backbone join — re-run after
// switching a segment's uplink to regenerate.
//
// Idempotent throughout: safe to re-run. Network namespace and
// interface existence are checked before creating; iptables rules are
// checked via -C before adding (iptables has no --may-exist).

import type {
  Backdoor,
  DhcpClient,
  InterfaceKind,
  Segment,
  Uplink,
} from "./types.ts";

function netnsName(u: Uplink): string {
  return `ns-uplink-${u.name}`;
}

/** Real kernel interface an uplink's netns ultimately reaches the real
 * world (or, for "dummy" with no backdoor, a placeholder) through —
 * undefined for any InterfaceKind this generator can't wire up yet.
 * Shared by emitUplinkNetns (this uplink's own real interface) and the
 * backdoor NAT rule (the `via` uplink's real interface) so the
 * backdoor/vlan/dummy/physical dispatch only lives in one place.
 *
 * A real InterfaceKind (vlan/physical/wireguard) ALWAYS wins over a
 * backdoor, checked first below: an uplink can have both — a real
 * WireGuard tunnel needs its own bootstrap path to reach its peer at
 * all, which is exactly what a backdoor provides, but the TUNNEL is
 * still what's actually realIface for NAT/segment purposes, not the
 * backdoor's own veth (see emitBackdoorNat's fork on this same
 * check). Only when the uplink has NO real interface of its own (the
 * "dummy" placeholder kind) does the backdoor's netns-side veth (see
 * emitBackdoorVethAndRoute below) stand in as realIface instead.
 * Confirmed live, this session: an earlier version kept a separate,
 * address-less "dummy" device alongside the backdoor veth — two
 * interfaces for one job, with the actually-useful one (the backdoor
 * veth) never treated as `realIface` at all, so accept_ra/discovery/
 * NAT below never applied to it. One interface, not two — for the
 * dummy case. Added 2026-07-06 for real wireguard uplinks: same
 * principle, different fork of the SAME check. */
function realIfaceFor(u: Uplink): string | undefined {
  if (u.if.kind === "wireguard") return u.if.ifaceName;
  if (u.backdoor !== undefined) return backdoorVethNetns(u.backdoor);
  if (u.if.kind === "vlan") {
    return u.if.ifaceName ?? `${u.if.vlanParent}.${u.if.vlanId}`;
  }
  if (u.if.kind === "dummy") return dummyIface(u);
  if (u.if.kind === "physical") return u.if.name;
  return undefined;
}

/** WireGuard: writes the conf file (built from `ifc.config` — see
 * WireguardInterfaceConfig, types.ts) to /etc/wireguard/<ifaceName>.conf
 * on the host, then shells out to `wg-quick up`, idempotently, inside
 * this uplink's own netns.
 *
 * The conf is written to a temp file first and compared (`cmp -s`)
 * against the real path before replacing it — same idempotency shape
 * as this script's own self-install step (generate-ovn.ts) — so an
 * unchanged config never bounces an already-up tunnel. Only when the
 * content actually changed does it bring the old tunnel down first
 * (wg-quick has no in-place reconfigure; the interface must be
 * recreated to pick up new keys/peers).
 *
 * `wg show <ifaceName>` is the idempotency check for actually bringing
 * it up: wg-quick has no --may-exist equivalent, but a running
 * interface with that name means it already did its job.
 *
 * NOTE: this embeds ifc.config.privateKey directly in the generated
 * script — see InterfaceKind's "wireguard" variant (types.ts) for why
 * that's a deliberate, explicit exception to this project's usual
 * credential-handling policy, not an oversight. */
function emitWireguardInterface(
  u: Uplink,
  ns: string,
  ifc: Extract<InterfaceKind, { kind: "wireguard" }>,
): string[] {
  const cfg = ifc.config;
  const confPath = `/etc/wireguard/${ifc.ifaceName}.conf`;
  const tmpPath = `/tmp/${ifc.ifaceName}.conf.new`;

  const confBody = [
    "[Interface]",
    `PrivateKey = ${cfg.privateKey}`,
    `Address = ${cfg.address}`,
    ...(cfg.listenPort !== undefined ? [`ListenPort = ${cfg.listenPort}`] : []),
    ...(cfg.dns !== undefined ? [`DNS = ${cfg.dns}`] : []),
    "[Peer]",
    `PublicKey = ${cfg.peer.publicKey}`,
    `AllowedIPs = ${cfg.peer.allowedIps}`,
    `Endpoint = ${cfg.peer.endpoint}`,
    ...(cfg.peer.persistentKeepalive !== undefined
      ? [`PersistentKeepalive = ${cfg.peer.persistentKeepalive}`]
      : []),
  ];

  return [
    `# --- wireguard: ${u.name} (${ifc.ifaceName}) ---`,
    "mkdir -p /etc/wireguard",
    "chmod 700 /etc/wireguard",
    `cat > ${tmpPath} << 'WGCONF'`,
    ...confBody,
    "WGCONF",
    `chmod 600 ${tmpPath}`,
    `cmp -s ${tmpPath} ${confPath} 2>/dev/null && rm -f ${tmpPath} || {`,
    `  ip netns exec ${ns} wg-quick down ${confPath} >/dev/null 2>&1 || true`,
    `  mv ${tmpPath} ${confPath}`,
    "}",
    `ip netns exec ${ns} wg show ${ifc.ifaceName} >/dev/null 2>&1 || ` +
      `ip netns exec ${ns} wg-quick up ${confPath}`,
  ];
}

// Real kernel interface names (veth ends, the OVS bridge below) are
// capped at IFNAMSIZ = 16 bytes including the NUL terminator, i.e. 15
// usable characters — confirmed live, this session: "br-uplink-1280-
// transfer" (23 chars) and "br-uplink-isp-primary-transfer" (30 chars)
// both fail with ofproto "Invalid argument", and "veth-ovn-isp-modem"
// (18 chars) would fail the same way. u.name is human-chosen and can
// be arbitrarily long/descriptive (that's the point of it), so it must
// NEVER be used to build a real interface name. u.slot is a small
// sequential int (0-4095, unique per uplink, assigned by
// NetworkBuilder — see types.ts) and is used here instead. OVN-side
// logical object names (emitUplinkTransfer in generate-ovn.ts) are NOT
// real interfaces — those keep u.name for readability.

function vethOvn(u: Uplink): string {
  return `veth-ovn-${u.slot}`;
}

function vethNetns(u: Uplink): string {
  return `veth-krn-${u.slot}`;
}

/** OVS bridge that carries this uplink's transfer-link veth — must
 * match the bridge emitUplinkTransferInterface() (generate-ovn.ts)
 * creates and registers in ovn-bridge-mappings, so the OVN-side veth
 * created here has somewhere to attach. Slot-based name, not
 * name-based — see comment above. */
export function uplinkTransferBridge(u: Uplink): string {
  return `br-up-${u.slot}`;
}

/** Real kernel name of a "dummy" uplink's placeholder interface (see
 * InterfaceKind, types.ts) — slot-based, same IFNAMSIZ-safe convention
 * as vethOvn/vethNetns/uplinkTransferBridge above. Unlike those, this
 * one the generator actually CREATES (not just names) — see the "real
 * interface" block in emitUplinkNetns. */
function dummyIface(u: Uplink): string {
  return `dummy-up-${u.slot}`;
}

// ── ipv4 discovery: pluggable client ────────────────────────────────
// discovery.ipv4 === "dhcp" says WHAT is needed (a DHCPv4 lease);
// discovery.client says WHICH PROGRAM does it. Each client gets its
// own tiny emit function, all with the same idempotent shape
// ("already running for this interface? no-op : start it"), so the
// generated script always has ONE clearly-labelled block per uplink
// for this (see the "# --- ipv4 discovery: ..." comment below) instead
// of the mechanism being buried inline. Adding a third client (or,
// later, folding a WireGuard uplink into this same dispatch shape —
// see WireGuard design discussion, 2026-07-06) means adding one
// function and one switch arm here, not touching the two below.

function emitDhclient(u: Uplink, ns: string, realIface: string): string[] {
  return [
    `ip netns exec ${ns} pgrep -f "dhclient.*${realIface}" >/dev/null || ` +
      `ip netns exec ${ns} dhclient -nw ` +
      `-pf /run/dhclient.${u.name}.pid ` +
      `-lf /var/lib/dhcp/dhclient.${u.name}.leases ` +
      `${realIface}`,
  ];
}

function emitDhcpcd(u: Uplink, ns: string, realIface: string): string[] {
  // NOTE: flags not yet verified against a live host — dhcpcd isn't
  // installed on the reference deployment (only dhclient has been
  // confirmed live so far). -b backgrounds immediately, -q quiets normal
  // output. Idempotency uses the same pgrep guard as the dhclient
  // branch above rather than a hardcoded pidfile path, since dhcpcd's
  // default pidfile naming varies by distro/version.
  return [
    `ip netns exec ${ns} pgrep -f "dhcpcd.*${realIface}" >/dev/null || ` +
      `ip netns exec ${ns} dhcpcd -b -q ${realIface}`,
  ];
}

/** client === "static": not a program at all, just configure the fixed
 * address + default gateway directly (see StaticIpv4, types.ts) —
 * what dhclient/dhcpcd would otherwise learn dynamically. Useful for
 * an uplink that turns out to want a stable reserved LAN address
 * rather than whatever the router's DHCP server hands out. Same
 * idempotent shape as the two above, just no client process
 * to check for — only the address and the route. */
function emitStaticIpv4(u: Uplink, ns: string, realIface: string): string[] {
  const cfg = u.discovery?.static4;
  if (cfg === undefined) {
    return [
      `# WARNING: client "static" requested for ${u.name} but no ` +
        `discovery.static4 given — nothing configured, see types.ts`,
    ];
  }
  const addrHost = cfg.address.split("/")[0];
  return [
    `ip netns exec ${ns} ip addr show ${realIface} | grep -q ${addrHost} || ` +
      `ip netns exec ${ns} ip addr add ${cfg.address} dev ${realIface}`,
    `ip netns exec ${ns} ip route show | grep -q '^default' || ` +
      `ip netns exec ${ns} ip route add default via ${cfg.gateway} dev ${realIface}`,
  ];
}

function emitIpv4Discovery(
  u: Uplink,
  ns: string,
  realIface: string,
): string[] {
  // Explicit client always wins. With none given, fall back to
  // "dhclient" — but ONLY when discovery.ipv4 is "dhcp" (the
  // historical default for a real WAN uplink). A "static"
  // discovery.ipv4 with NO explicit client — e.g. a backdoor's merged
  // real interface (see realIfaceFor above), whose address/route are
  // already fully handled by emitBackdoorVethAndRoute — deliberately
  // does nothing here at all.
  const client: DhcpClient | undefined = u.discovery?.client ??
    (u.discovery?.ipv4 === "dhcp" ? "dhclient" : undefined);
  if (client === undefined) return [];

  const lines = [`# --- ipv4 discovery: ${client} on ${realIface} ---`];
  switch (client) {
    case "dhclient":
      lines.push(...emitDhclient(u, ns, realIface));
      break;
    case "dhcpcd":
      lines.push(...emitDhcpcd(u, ns, realIface));
      break;
    case "static":
      lines.push(...emitStaticIpv4(u, ns, realIface));
      break;
  }
  return lines;
}

export function emitUplinkNetns(
  u: Uplink,
  segmentsOnThisUplink: readonly Segment[],
): string[] {
  const ns = netnsName(u);
  const vOvn = vethOvn(u);
  const vNetns = vethNetns(u);

  const realIface = realIfaceFor(u);
  if (realIface === undefined) {
    return [
      `# unsupported uplink interface kind "${u.if.kind}" for ${u.name} ` +
        `— skipped, see generate-netns.ts`,
      "",
    ];
  }

  const ovnSide = u.addresses[0]; // host=1
  const netnsSide = u.addresses[1]; // host=2

  const lines: string[] = [`# --- uplink: ${u.name} (netns side) ---`];

  // netns + veth pair (idempotent)
  lines.push(
    // `ip netns list` prints "<name> (id: N)", not just "<name>" — a
    // plain `grep -qx ${ns}` (exact whole-line match) never matches an
    // existing namespace, so a re-run always tries `ip netns add`
    // again and fails with "File exists" under `set -e`, aborting
    // everything after it (confirmed live, this session — the same
    // "safe to re-run" idempotency this generator is built around).
    // Isolate just the name field first, then match exactly.
    `ip netns list | awk '{print $1}' | grep -qx ${ns} || ip netns add ${ns}`,
    `ip netns exec ${ns} ip link set lo up`,
    `ip link show ${vOvn} >/dev/null 2>&1 || ` +
      `ip link add ${vOvn} type veth peer name ${vNetns}`,
    // Move the netns-side veth end over. Checking "is ${vNetns}
    // visible in the root namespace" (not "is it absent") is the
    // correct idempotency test: right after creation both veth ends
    // exist in the root ns, so a check for absence never fires and
    // the move never happens on first run — confirmed live, this
    // session ("Cannot find device" on the subsequent `ip netns exec
    // ... set up`, because the interface was still sitting in root).
    // If ${vNetns} is NOT visible in root, it's already been moved by
    // a prior run — nothing to do, `|| true` makes that a no-op.
    `ip link show ${vNetns} >/dev/null 2>&1 && ` +
      `ip link set ${vNetns} netns ${ns} || true`,
    // Attach the OVN-side veth to its transfer-link OVS bridge —
    // without this, ovn-bridge-mappings has nothing plugged into it
    // and the localnet port never sees traffic (confirmed live: a
    // bridge existing with no port attached errors on ofproto add).
    `ovs-vsctl --may-exist add-port ${uplinkTransferBridge(u)} ${vOvn}`,
    `ip link set ${vOvn} up`,
    `ip netns exec ${ns} ip link set ${vNetns} up`,
    `ip addr show ${vOvn} | grep -q ${ovnSide.ipv4.to_s()} || ` +
      `ip addr add ${ovnSide.ipv4.to_string()} dev ${vOvn}`,
    `ip addr show ${vOvn} | grep -q ${ovnSide.ipv6.to_s()} || ` +
      `ip addr add ${ovnSide.ipv6.to_string()} dev ${vOvn}`,
    `ip netns exec ${ns} sh -c "ip addr show ${vNetns} | grep -q ` +
      `${netnsSide.ipv4.to_s()} || ip addr add ${netnsSide.ipv4.to_string()} ` +
      `dev ${vNetns}"`,
    `ip netns exec ${ns} sh -c "ip addr show ${vNetns} | grep -q ` +
      `${netnsSide.ipv6.to_s()} || ip addr add ${netnsSide.ipv6.to_string()} ` +
      `dev ${vNetns}"`,
  );

  // real interface: create if missing, move into netns.
  //
  // The backdoor's own veth+route (if any) is emitted UNCONDITIONALLY
  // here, regardless of whether this uplink ALSO has a real interface
  // of its own — it's a separate, always-useful bootstrap path (see
  // Backdoor, types.ts), not a substitute for one except in the
  // "dummy" placeholder case (see realIfaceFor above).
  if (u.backdoor !== undefined) {
    lines.push(...emitBackdoorVethAndRoute(u, u.backdoor));
  }
  if (u.if.kind === "wireguard") {
    // A real interface of its own — created here alongside (not
    // instead of) the backdoor block above.
    lines.push(...emitWireguardInterface(u, ns, u.if));
  } else if (u.backdoor === undefined) {
    // No backdoor AND no wireguard tunnel: fall through to the plain
    // vlan/dummy/physical creation. (When a backdoor IS present and
    // `u.if.kind === "dummy"`, realIface already resolved to the
    // backdoor's own veth above — nothing further to create here; see
    // realIfaceFor's doc comment.)
    lines.push(
      `ip netns exec ${ns} ip link show ${realIface} >/dev/null 2>&1 || {`,
      `  ip link show ${realIface} >/dev/null 2>&1 || ` +
        (u.if.kind === "vlan"
          ? `ip link add link ${u.if.vlanParent} name ${realIface} ` +
            `type vlan id ${u.if.vlanId}`
          : u.if.kind === "dummy"
          ? `ip link add ${realIface} type dummy`
          : `true # physical interface ${realIface} must already exist`),
      `  ip link set ${realIface} netns ${ns}`,
      `}`,
      `ip netns exec ${ns} ip link set ${realIface} up`,
    );
  }

  // forwarding + back-routes (kernel-side, not arithmetic — plain shell)
  lines.push(
    `ip netns exec ${ns} sysctl -qw net.ipv4.ip_forward=1`,
    `ip netns exec ${ns} sysctl -qw net.ipv6.conf.all.forwarding=1`,
    `ip netns exec ${ns} ip route show | grep -q '^10.0.0.0/8' || ` +
      `ip netns exec ${ns} ip route add 10.0.0.0/8 via ${ovnSide.ipv4.to_s()} dev ${vNetns}`,
    `ip netns exec ${ns} ip route show | grep -q '^192.168.0.0/16' || ` +
      `ip netns exec ${ns} ip route add 192.168.0.0/16 via ${ovnSide.ipv4.to_s()} dev ${vNetns}`,
    `ip netns exec ${ns} ip -6 route show | grep -q '^fd00::/8' || ` +
      `ip netns exec ${ns} ip -6 route add fd00::/8 via ${ovnSide.ipv6.to_s()} dev ${vNetns}`,
  );

  // discovery: SLAAC needs accept_ra=2 (forwarding suppresses RA
  // otherwise — confirmed live, this session). DHCPv4 needs dhclient
  // started on the real interface, inside the netns.
  if (u.discovery?.ipv6 === "slaac") {
    lines.push(
      `ip netns exec ${ns} sh -c "echo 2 > ` +
        `/proc/sys/net/ipv6/conf/${realIface}/accept_ra"`,
    );
  }
  lines.push(...emitIpv4Discovery(u, ns, realIface));

  // NAT: derived by the caller from which segments currently resolve
  // to this uplink — NOT stored on Uplink itself, see header comment.
  // segAddr.to_string() below is the segment's GATEWAY host address
  // with the segment's prefix length (e.g. 192.168.128.2/24, not
  // 192.168.128.0/24) — this is intentional and correct, not a bug:
  // iptables/ip6tables (and IPAddress.parse non-strict) both mask a
  // host address by its prefix length before matching, so
  // "-s 192.168.128.2/24" matches the entire 192.168.128.0/24 network
  // exactly the same as writing the bare network address would.
  for (const seg of segmentsOnThisUplink) {
    const segAddr = seg.addresses[0];
    if (u.nat?.ipv4?.some((r) => r.kind === "masq")) {
      lines.push(
        `ip netns exec ${ns} iptables -t nat -C POSTROUTING -s ` +
          `${segAddr.ipv4.to_string()} -o ${realIface} -j MASQUERADE 2>/dev/null || ` +
          `ip netns exec ${ns} iptables -t nat -A POSTROUTING -s ` +
          `${segAddr.ipv4.to_string()} -o ${realIface} -j MASQUERADE`,
      );
    }
    if (u.nat?.ipv6?.some((r) => r.kind === "masq")) {
      lines.push(
        `ip netns exec ${ns} ip6tables -t nat -C POSTROUTING -s ` +
          `${segAddr.ipv6.to_string()} -o ${realIface} -j MASQUERADE 2>/dev/null || ` +
          `ip netns exec ${ns} ip6tables -t nat -A POSTROUTING -s ` +
          `${segAddr.ipv6.to_string()} -o ${realIface} -j MASQUERADE`,
      );
    }
  }

  lines.push("");
  return lines;
}

// ── backdoor: borrowed egress for a VPN-like uplink ─────────────────
// See Backdoor (types.ts) for the why. The kernel-side shape here is
// deliberately the SAME veth-pair/address/route dance as a normal
// transfer link (emitUplinkNetns above) — a backdoor IS a transfer
// link, just one whose OVN-side lands on an ALREADY-real uplink's
// router instead of spinning up a dedicated one of its own. The one
// thing that must NOT be reused is the address block: bd.addresses
// comes from its own slot (see factories.ts, uplinkDummy), so it can
// never share a /28 with `u`'s own front-door transfer link — sharing
// one was tried and is broken (see Backdoor's doc comment for why).

function backdoorVethOvn(bd: Backdoor): string {
  return `veth-bdo-${bd.slot}`;
}

function backdoorVethNetns(bd: Backdoor): string {
  return `veth-bdk-${bd.slot}`;
}

/** OVS bridge carrying a backdoor's own veth — must match the bridge
 * emitBackdoorInterface() (generate-ovn.ts) creates and registers in
 * ovn-bridge-mappings. Slot-based, same convention as
 * uplinkTransferBridge() above. */
export function backdoorBridge(bd: Backdoor): string {
  return `br-bd-${bd.slot}`;
}

/** Creates the backdoor's own veth pair, addresses both ends, and adds
 * the default route out through it — this IS u's real interface (see
 * realIfaceFor above), not a separate placeholder alongside one, so
 * it's called from emitUplinkNetns's "real interface" section, in the
 * exact spot a plain vlan/dummy/physical device would otherwise be
 * created. NAT is deliberately NOT here — see emitBackdoorNat below,
 * called separately since it targets `via`'s netns, not this one. */
export function emitBackdoorVethAndRoute(u: Uplink, bd: Backdoor): string[] {
  const ns = netnsName(u);
  const vOvn = backdoorVethOvn(bd);
  const vNetns = backdoorVethNetns(bd);
  const ovnSide = bd.addresses[0]; // host=3, lives on the `via` router
  const netnsSide = bd.addresses[1]; // host=4, lives in this uplink's netns

  const lines: string[] = [
    `# --- backdoor: ${u.name} -> ${bd.via.name} (borrowed egress, netns side) ---`,
  ];

  // veth pair: OVN side stays in root, netns side moves into THIS
  // uplink's own netns (not `via`'s) — same idempotency reasoning as
  // emitUplinkNetns's own veth pair above.
  lines.push(
    `ip link show ${vOvn} >/dev/null 2>&1 || ` +
      `ip link add ${vOvn} type veth peer name ${vNetns}`,
    `ip link show ${vNetns} >/dev/null 2>&1 && ` +
      `ip link set ${vNetns} netns ${ns} || true`,
    `ovs-vsctl --may-exist add-port ${backdoorBridge(bd)} ${vOvn}`,
    `ip link set ${vOvn} up`,
    `ip netns exec ${ns} ip link set ${vNetns} up`,
    `ip addr show ${vOvn} | grep -q ${ovnSide.ipv4.to_s()} || ` +
      `ip addr add ${ovnSide.ipv4.to_string()} dev ${vOvn}`,
    `ip addr show ${vOvn} | grep -q ${ovnSide.ipv6.to_s()} || ` +
      `ip addr add ${ovnSide.ipv6.to_string()} dev ${vOvn}`,
    `ip netns exec ${ns} sh -c "ip addr show ${vNetns} | grep -q ` +
      `${netnsSide.ipv4.to_s()} || ip addr add ${netnsSide.ipv4.to_string()} ` +
      `dev ${vNetns}"`,
    `ip netns exec ${ns} sh -c "ip addr show ${vNetns} | grep -q ` +
      `${netnsSide.ipv6.to_s()} || ip addr add ${netnsSide.ipv6.to_string()} ` +
      `dev ${vNetns}"`,
  );

  // Default route via the backdoor — deliberately just the DEFAULT
  // route, not a broad backroute like emitUplinkNetns's 10.0.0.0/8 /
  // 192.168.0.0/16: this uplink's netns ALSO has its own front-door
  // transfer link (more specific routes there, if any exist), and
  // those must keep winning for anything internal. Only genuinely
  // internet-bound traffic (not matched by anything more specific)
  // should fall through to the backdoor.
  lines.push(
    `ip netns exec ${ns} ip route show | grep -q '^default' || ` +
      `ip netns exec ${ns} ip route add default via ${ovnSide.ipv4.to_s()} dev ${vNetns}`,
    `ip netns exec ${ns} ip -6 route show | grep -q '^default' || ` +
      `ip netns exec ${ns} ip -6 route add default via ${ovnSide.ipv6.to_s()} dev ${vNetns}`,
  );

  lines.push("");
  return lines;
}

/** NAT for traffic borrowing this backdoor's egress — lives in `via`'s
 * netns (the uplink with the real interface), NOT this uplink's own
 * netns, since masquerading has to happen where the real interface
 * actually is. Called separately from emitBackdoorVethAndRoute (from
 * generate-ovn.ts, alongside every other uplink's own NAT) rather than
 * folded into it, since it targets a different netns entirely.
 *
 * Scoped to `via`'s OWN declared nat config (bd.via.nat), exactly like
 * segment NAT above is scoped to the resolving uplink's nat config —
 * NOT unconditional (an earlier version added this rule regardless of
 * whether `via` even wants NAT, per feedback 2026-07-06: "we do nat on
 * the avm uplink").
 *
 * WHAT gets matched forks on whether the backdoor is standing in as
 * `u`'s own realIface (see realIfaceFor above) or merely bootstrapping
 * a SEPARATE real interface (e.g. a wireguard tunnel):
 *
 *   - No real interface of its own (the "dummy" placeholder case): the
 *     backdoor's netns-side veth IS realIface, so actual segment
 *     traffic really does flow through it — matches on each SEGMENT's
 *     own subnet (segAddr below), e.g. 192.168.130.0/24 for
 *     "neighbor", exactly like emitUplinkNetns's own segment-NAT loop.
 *     NOT bd.addresses: routed segment traffic keeps its original
 *     source address all the way through the OVN topology (every hop
 *     here is routing, not NAT), so that's the address that has to
 *     appear in the rule. Confirmed live, 2026-07-06 — an earlier
 *     version matched on bd.addresses here unconditionally and
 *     silently NAT'd nothing useful (nmap against that /28 only ever
 *     found the backdoor's own interface answering).
 *
 *   - A real interface of its own (e.g. wireguard): the backdoor
 *     carries ONLY that interface's own bootstrap/handshake/keepalive
 *     traffic (once it's up, real segment traffic goes through the
 *     tunnel instead, matched by u's OWN nat/realIface in
 *     emitUplinkNetns's segment-NAT loop, same as every non-backdoor
 *     uplink). That bootstrap traffic is sourced from the backdoor's
 *     OWN transit address (bd.addresses[1]), never from a segment
 *     subnet — so THAT'S what gets matched here instead.
 *
 * Requires `via`'s netns to already exist by the time this runs — true
 * as long as `via` is declared (and therefore processed) before this
 * uplink, which NetworkBuilder enforces (see define.ts). */
export function emitBackdoorNat(
  u: Uplink,
  segmentsOnThisUplink: readonly Segment[],
): string[] {
  const bd = u.backdoor;
  if (bd === undefined) return [];

  const viaNs = netnsName(bd.via);
  const viaRealIface = realIfaceFor(bd.via);
  if (viaRealIface === undefined) return [];

  const lines: string[] = [];

  if (realIfaceFor(u) === backdoorVethNetns(bd)) {
    // Backdoor IS u's realIface — NAT scoped per resolving segment.
    for (const seg of segmentsOnThisUplink) {
      const segAddr = seg.addresses[0];
      if (bd.via.nat?.ipv4?.some((r) => r.kind === "masq")) {
        lines.push(
          `ip netns exec ${viaNs} iptables -t nat -C POSTROUTING -s ` +
            `${segAddr.ipv4.to_string()} -o ${viaRealIface} -j MASQUERADE 2>/dev/null || ` +
            `ip netns exec ${viaNs} iptables -t nat -A POSTROUTING -s ` +
            `${segAddr.ipv4.to_string()} -o ${viaRealIface} -j MASQUERADE`,
        );
      }
      if (bd.via.nat?.ipv6?.some((r) => r.kind === "masq")) {
        lines.push(
          `ip netns exec ${viaNs} ip6tables -t nat -C POSTROUTING -s ` +
            `${segAddr.ipv6.to_string()} -o ${viaRealIface} -j MASQUERADE 2>/dev/null || ` +
            `ip netns exec ${viaNs} ip6tables -t nat -A POSTROUTING -s ` +
            `${segAddr.ipv6.to_string()} -o ${viaRealIface} -j MASQUERADE`,
        );
      }
    }
  } else {
    // u has its own real interface (e.g. wireguard) — backdoor only
    // carries ITS bootstrap traffic, sourced from the backdoor's own
    // transit address, not any segment.
    const netnsSide = bd.addresses[1];
    if (bd.via.nat?.ipv4?.some((r) => r.kind === "masq")) {
      lines.push(
        `ip netns exec ${viaNs} iptables -t nat -C POSTROUTING -s ` +
          `${netnsSide.ipv4.to_string()} -o ${viaRealIface} -j MASQUERADE 2>/dev/null || ` +
          `ip netns exec ${viaNs} iptables -t nat -A POSTROUTING -s ` +
          `${netnsSide.ipv4.to_string()} -o ${viaRealIface} -j MASQUERADE`,
      );
    }
    if (bd.via.nat?.ipv6?.some((r) => r.kind === "masq")) {
      lines.push(
        `ip netns exec ${viaNs} ip6tables -t nat -C POSTROUTING -s ` +
          `${netnsSide.ipv6.to_string()} -o ${viaRealIface} -j MASQUERADE 2>/dev/null || ` +
          `ip netns exec ${viaNs} ip6tables -t nat -A POSTROUTING -s ` +
          `${netnsSide.ipv6.to_string()} -o ${viaRealIface} -j MASQUERADE`,
      );
    }
  }

  return lines;
}
