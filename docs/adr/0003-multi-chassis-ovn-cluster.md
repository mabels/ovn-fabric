# ADR 0003: `defineNetwork` as one OVN cluster, not one host

Status: research — real findings recorded, no code changed yet

## Context

`hgw` (`topology.hgw.ts`, the Greifswald/`anger-core` site) and `mam-hh-ovn`
(`topology.ts`, Hamburg) are declared as two separate `defineNetwork()`
calls today — two independent OVN deployments (each with its own
`ovn-central`), coordinating only at the IP-routing level over a shared
zerotier mesh. No shared OVN control plane, no OVN-native mechanism
connecting them.

While working out how to get `hgw` actually running, a deeper question
surfaced: what does `defineNetwork` *mean* when more than one host is
involved? The type model already anticipates multi-host — every
`Uplink`/`Segment` carries its own `host: Host`, and `generate-ovn.ts`
already groups by `host.name` and emits one script per host
(`scriptForHost`). But `generate-ovn.ts`'s own `requiredPackages` doc
comment (added 2026-07-19) already flags the real gap plainly:

> `ovn-central` (northd + the NB/SB databases) only needs to run on ONE
> host in a real multi-chassis topology, while every OTHER chassis would
> need just `openvswitch-switch` + `ovn-host`... Splitting that out is a
> real, un-implemented TODO for whenever multi-chassis actually happens
> — not needed today, since every Host this generator currently targets
> runs its own full stack.

So today, two hosts under one `defineNetwork` would produce two
*independent, uncoordinated* OVN clusters, not one shared one — the type
signature says "one network," the generator currently builds "N networks
that happen to be declared together."

This splits into two genuinely separate axes, easy to conflate:

1. **Shared address pool** — coordinating non-overlapping IP allocation
   across independently-clustered routers. This is § Multi-router
   coordination in ADR 0002 (reconciler → allocator → deployer,
   compare-and-swap). Unrelated to whether there's one OVN cluster or
   many.
2. **Shared control plane** — one `ovn-central` (NB/SB DB) that multiple
   chassis' `ovn-controller`s point at, instead of each running their
   own. This ADR is about axis 2.

## What was actually tested, not just discussed

Three real Ubuntu 26.04 LXC containers, built specifically to verify
axis 2 empirically before designing anything around it:

- `net-test-hh-a` (`192.168.128.30`, ctid 300, Proxmox host
  `192.168.129.14`) — **central**: runs `ovn-central` (northd + NB/SB
  DBs), SB DB opened to remote connections (`ovn-sbctl set-connection
  ptcp:6642:0.0.0.0`), NB likewise on 6641. Also a chassis itself
  (`ovn-controller` pointed at its own local SB DB via unix socket).
- `net-test-hh-b` (`192.168.128.31`, ctid 301, same Proxmox host) —
  chassis only (`openvswitch-switch` + `ovn-host`), same LAN as central,
  `ovn-remote=tcp:192.168.128.30:6642`.
- `net-test-hgw-a` (`192.168.19.30`, ctid 302, Proxmox host
  `192.168.19.14` — the Greifswald/`anger-core` site) — chassis only,
  **different physical site**, same `ovn-remote` pointed across the
  existing zerotier-routed path between sites.

All three: `openvswitch-switch`/`ovn-central`/`ovn-host` 26.03.0-2 /
OVS 3.7.1-2 (same versions already running on the real `mam-hh-ovn`
router). LXC config replicated exactly from the real, already-working
`mam-hh-core`/`anger-core` containers — `features: nesting=1,keyctl=1`,
privileged (no `unprivileged` line), `lxc.apparmor.profile: unconfined`,
`lxc.cgroup2.devices.allow: a`, `lxc.cap.drop:` (empty), `lxc.mount.auto:
proc:rw sys:rw`, plus a `/dev/net/tun` bind mount — these are load-
bearing for OVS/OVN to work inside an LXC container at all, not
incidental.

### Findings, each independently confirmed live

1. **One shared control plane across two physical sites works, with no
   new infrastructure.** `ovn-sbctl list chassis` on the central host
   showed all three chassis registered — including `net-test-hgw-a`,
   reachable only via the existing zerotier-routed path between sites
   (confirmed reachable beforehand: `ping` from the `hh` site to
   `192.168.19.30`, TTL 58, ~27ms — several hops, real VPN-routed path,
   not local).
2. **The data plane does not go through the central host.** A logical
   switch (`sw-test`) with one port bound on `hh-b` and one on `hgw-a`
   (via `ovs-vsctl add-port br-int <if> -- set interface <if>
   external_ids:iface-id=<logical-port-name>`, matching a real
   `ovn-nbctl lsp-add` port) produced a **direct** Geneve tunnel between
   the two chassis (`ovs-vsctl show` on `hh-b`: a tunnel port with
   `local_ip="192.168.128.31", remote_ip="192.168.19.30"` — straight to
   `hgw-a`, no hop through `hh-a`'s own `encap-ip`). A real ping across
   it: 0% loss, TTL 64 (one logical hop). `ovn-central` going down would
   not break this already-programmed flow — only new topology changes
   (new port bindings, chassis re-registering) need the SB DB reachable.
3. **`br-int`'s own local port is not part of any logical switch by
   default.** Addressing it directly (`ip addr add ... dev br-int`) on
   any of the three chassis produced no cross-chassis connectivity at
   all — that port has no `external_ids:iface-id`, so OVN's OpenFlow
   pipeline has no rule for it. The only thing that binds a real
   interface to a logical port is `external_ids:iface-id` matching a
   real `ovn-nbctl lsp-add` port name — the interface itself can be
   anything (a VM's tap, a container's veth, or — as used for this
   test — a throwaway veth pair with one end moved into a scratch netns
   purely to avoid touching the chassis's own root-namespace routing
   table, not something OVN requires).
4. **OVN's own native IPsec support was identified but not yet tested**
   (ESP-encrypted Geneve, IKE via `libreswan`/`ovn-ipsec`, enabled
   globally via `NB_Global.ipsec`, PSK or certificate chassis auth) —
   real, documented OVN capability, exact command syntax not yet
   verified against this OVN 26.03.0 install. Next thing to test on this
   rig, not yet done.

## Conceptual direction (not yet a decision)

`defineNetwork` should mean **one OVN cluster** — one shared control
plane, `Host`s as chassis participating in it — not "N independent
per-host OVN deployments that happen to share a config file," which is
what it structurally produces today. Restated in the terms this session
converged on:

- `defineNetwork` = the cluster (shared `ovn-central`).
- A **collision domain** (a segment/logical switch, `net.segment` today)
  is declared *within* that cluster, not owned by any single host.
- **Logical routers** connect collision domains to each other and to
  uplinks — this is already the existing `net.uplink`/backbone-join
  model, just not yet cluster-aware.

Concretely, this means `Host` needs a role distinguishing **central**
(runs `ovn-central`, one per cluster — or an HA group of 3, not tested
here) from **chassis-only** (`ovn-host` + `ovn-controller` pointed
remotely) — today `requiredPackages` installs `ovn-central`
unconditionally on every host, exactly the gap the 2026-07-19 comment in
`generate-ovn.ts` already named. Gateway-chassis pinning (the field
already visible on real `Logical_Router_Port` data from `mam-hh-ovn` —
`gateway chassis: [effd37ab-...]` — OVN's native mechanism for tying
NAT/external-egress router ports to one specific chassis) is the
remaining piece needed before a segment's uplink/NAT can be assigned to
a *specific* chassis in a real multi-chassis `defineNetwork`.

## Open questions / not yet done

- Exact `Host` role field shape (`central` / `chassis`), and how
  `requiredPackages`/`scriptForHost` should branch on it.
- Gateway-chassis selection: config-declared, or derived automatically
  (e.g. "whichever chassis has the real uplink interface")?
- HA central (3-node clustered NB/SB) — not tested, single central host
  only so far.
- IPsec — identified, not yet verified live on this rig.
- How axis 1 (shared address pool, ADR 0002's allocator) and axis 2
  (this ADR) compose once both exist — likely orthogonal, not yet
  confirmed.
- The live test rig (`net-test-hh-a`/`-b`, `net-test-hgw-a`, logical
  switch `sw-test`) is left running for continued use, not torn down.
