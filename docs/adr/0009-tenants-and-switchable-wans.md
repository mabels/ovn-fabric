# ADR 0009: Naming the moving parts — tenants, gateways, wans

Status: proposed (2026-11-10)

## Context

Today one shared `backbone` joins every router, and isolation between
networks exists only because a route is missing. Neighbor (130) got its own
uplink to be isolated, which pins it and defeats switching uplinks. The new
shape: isolated **tenants**, each leaving through its own NAT gateway onto a
shared core where the **wans** are, so a tenant switches wan by changing one
route. Before building it, this ADR fixes the names.

## The picture

```
 tenant home                                                   scope internet
 ┌──────────────────────────────────────────────┐
 │ home-cable-lan (128) ── home-rt-lan ─┐       │
 │ home-cable-dmz (129) ── home-rt-dmz ─┼─ home-core ── home-nat ───────────┐
 │                                      └─ home-vpn-zerotier ⋯ (VPN, below) │
 └──────────────────────────────────────────────┘                           │
 tenant neighbor                                                            ├─ internet-core ─┬─ internet-wan-avm
 │ neighbor-cable-lan (130) ── neighbor-rt-lan ── neighbor-core ── neighbor-nat ─┤               └─ internet-wan-starlink
 tenant usa                                                                 │
 │ usa-cable-lan (131) ── usa-rt-lan ── usa-core ── usa-vpn-mullvad ⋯ ── usa-nat-mullvad ─┤
 tenant cp                                                                  │
 │ cp-core (control plane) ── cp-nat ─────────────────────────────────────────┘
 │        └── ingress leg of home-rt-dmz (only crossing between tenants)
```

A VPN gateway in detail:

```
home-core ── home-vpn-zerotier ══ home-svc-zerotier-core ══ [netns: zt0] ══ home-svc-zerotier-nat ══ home-nat-zerotier ── internet-core
```

## The parts

Generated names follow `<scope>-<code>[-<name>]`; the scope is a tenant or
`internet`; every kind has one fixed code.

| Part | Code | Example | What it does | Today in the topology | With the new names |
|---|---|---|---|---|---|
| tenant | (scope) | `home` | networks that may reach each other, and nothing else | — (only `routingDomain` membership) | `net.tenant("home", { wans: [avm, starlink] })` |
| cable | `cable` | `home-cable-lan` | one L2 network | `net.collisionDomain("home-v2")` → `ls:home-v2` | `home.collisionDomain("lan", …)` |
| router | `rt` | `home-rt-lan` | a router of the tenant; its leg on the core is added by the tenant | `net.defineOvnRouter("router-home-v2", …)` with `l2Segment: backbone` | `home.defineOvnRouter("lan", …)` |
| core | `core` | `home-core` | joins a tenant's routers and gateways | `net.collisionDomain("backbone")`, shared by all | `home.backbone`, made by the tenant |
| NAT gateway | `nat` | `home-nat` | the tenant's way out: SNAT, default route only | an endpoint with `{ kind: "ovn.ipv4.masq" }`; the platform's `egress` | `home.natGateway`, made by the tenant |
| VPN gateway | `vpn` | `home-vpn-zerotier` | a tunnel in a netns, its inside on the tenant's core | `router.tunnelRouterEndpoint({ … })` → `router-zerotier-v2` | `home.vpnGateway("zerotier", { kind: "zerotier", … })` |
| svc cable | `svc` | `home-svc-zerotier-core`, `…-nat` | OVN↔kernel cable, named after its other end | `transit:` / `upstream:` → `ls:transit-…`, `ls:backdoor-…`, `veth-krn/ovn/up-*` | made by the VPN gateway |
| VPN's NAT gateway | `nat` | `home-nat-zerotier` | the tunnel's encrypted traffic out | `router-zerotier-v2-upstream` | made by the VPN gateway |
| internet core | `core` | `internet-core` | where NAT gateways meet the wans | — (all on `backbone`) | made by the wans |
| wan | `wan` | `internet-wan-avm` | a physical uplink; knows no tenant network | `defineOvnRouter("home-voda-avm-uplink", …)` + `kernelRouterEndpoint`; `router-starlink-v2` | `net.wan("avm", { … })` |
| ingress leg | — | port on `cp-core` | a cable router's leg into cp, with SNAT | `controlPlane: { ingress: [managementV2] }` (built on 302) | the same |

## Rules

- The topology uses readable words on the tenant object (`home.backbone`,
  `home.natGateway`); the codes only appear in generated names.
- A tenant is a scope routers are defined in: `home.defineOvnRouter(...)`,
  as many as needed. Every router in it gets a leg on the tenant's core;
  it may only use the tenant's own cables and core (the build fails
  otherwise). `routingDomain` exchanges routes between a tenant's routers;
  no route leaves the tenant.
- ACLs between cables go on router ports (port-scoped), or into a router
  policy when one router serves the cables.
- `wan` is only ever a kind, never a side: a cable is named after the part
  at its other end (`-core`, `-nat`).
- OVN names stay full; a Linux name over 15 characters is hashed. Devices
  inside a namespace keep their runtime names (`zt0`, `wg0`).
- The names follow the clouds: tenant (OpenStack project, AWS/GCP VPC),
  NAT gateway and VPN gateway (AWS, Azure, GCP).

## Open questions

1. The name of a tenant's ordered choice of wans.
2. `home.collisionDomain(...)` or `home.cable(...)` in the DSL.
3. `internet` as the scope name of the shared side.
4. Core addresses: one prefix per tenant with addresses allocated by the
   tenant (as the platform allocates services), or declared per router as
   today.
