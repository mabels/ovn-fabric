# ADR 0011: Logical addresses in the IR — the runtime allocates

Status: proposed (2026-11-12)

## Context

ovn-fabric is a TypeScript topology DSL that generates a desired-state IR
(JSON) and a Python host service (`ir-apply create | run | delete`) that
applies it: OVN logical routers and switches, kernel netns routers,
WireGuard/ZeroTier/Tailscale gateways, docker workloads. The IR is the
boundary between the two halves (ADR 0002). `run` compares the IR's
predicted RawIR with the live RawIR collected from the host (ADR 0006,
0008) and applies only what is missing; `create` and `delete` are
unconditional and never look at live state.

Today **every address in the IR is concrete**. The user writes some of them
in the topology (a LAN gateway `192.168.128.1/24`, a DNS container's
`192.168.128.5`, the transit networks `svc: transitNetwork(10.12.87.1/28, …)`);
the DSL computes the rest. ADR 0009 introduced shared cores — `internet-core`
(`172.21.0.0/24`, the "switch panel" every tenant's NAT gateway, VPN
gateway and wan router has a leg on) and one core per tenant
(`172.22.x.0/24`). Their legs' addresses are handed out by
`internet.allocate()` / `tenant.allocate()` **in declaration order**
(`src/tenant.ts`). That was a deliberate shortcut: it hides the numbers from
the user.

### What went wrong (VM 302, 2026-11-11)

Adding one VPN gateway (`home-vpn-tailscale`) to the home tenant put its NAT
leg in the middle of the declaration order. It took `172.21.0.5`, and every
leg after it moved up by one — `home-nat` .5→.6, `neighbor-nat` .6→.7,
`usa-nat-mullvad` .7→.8, the platform .8→.9 — although nothing about those
routers changed.

- The NATs moved (they got a natural key, ADR 0010).
- The router ports did not: same id, differing `networks`/`mac` → `run`
  logs "present, but differs" and skips.
- Result: two ports on `internet-core` with `172.21.0.5` (and, with the
  MAC folded from the IPv4, the same MAC). ARP answered by both; the new
  gateway's replies went to the old port. Only a `delete` + `run` cleaned it
  up.

In production that would be every tenant's egress breaking for one added
gateway.

### Why the obvious local fixes are not enough

- **Hash the name into the core prefix.** A `/24` is too small: with 10
  legs the birthday collision chance is ~16 %, with 20 ~53 %. A larger
  prefix (`/16`) makes collisions rare but takes address space that can
  collide with the user's other networks — the core prefixes must stay small
  and out of the way.
- **Pin a slot per leg in the topology** (`port: 5`). Local and explicit,
  but it is exactly the complexity the DSL exists to hide; the user manages
  numbers again.
- **An allocation state file next to the topology.** Works, but it is a
  second source of truth beside the topology that must stay in sync, and
  the live system already *is* that state.
- **Make `run` replace a node that differs** (same id, other fields →
  remove, then create). Needed anyway, but it only makes the renumbering
  *apply*; it still renumbers, with an outage on every moved port.

- **OVN's own IPAM** (`addresses=dynamic` with `other_config:subnet` /
  `exclude_ips`). It allocates switch ports only, not router-port
  `networks`, so it cannot place the core legs.

The order allocation is not the root cause; the root cause is that the
generator — which has no live state — is forced to pick concrete numbers
for addresses nobody outside the fabric cares about.

## Decision

**Every address in the IR becomes a logical address: a host number on a
collision domain, owned by a name. Concrete numbers are chosen by the
runtime, which can see the live system; a written address pins its number.**

### 1. One kind of address: LogicalIP

```ts
LogicalIP("internet-core", "home-nat")                              // the runtime picks the number
LogicalIP("cp-cable-control-plane", "platform.dns", { host: 10 })  // pinned: .10 / ::a
LogicalIP("home-cable-lan", "home-rt-main", { host: 1 })            // written by the user: 192.168.128.1/24
LogicalIP("home-svc-tailscale-core", "home-vpn-tailscale", { host: "last" })  // .14 / ::f
```

- **The scope is the collision domain** (the `ls:` id of the IR), not a
  prefix. An address is unique only within its collision domain, like a VLAN
  ID only within its parent; tenant prefixes **may overlap** — two tenants may
  use the same prefix on two isolated switches, and nothing checks prefixes
  for overlap across collision domains. The prefixes (IPv4, IPv6) are
  properties of the collision domain. A resource reached from several
  collision domains is shared the way a VLAN is (`src/ir.ts`: two kernel
  routers on one VLAN get one shared collision domain with its bridge, each
  router a veth on it) — never by the same address in two scopes.
- **The owner is a stable logical name** (ADR 0009 naming): the component
  the address belongs to. A foreign address — a next hop on an upstream
  router, such as the AVM's `192.168.132.1` — is a LogicalIP too, pinned, with
  a name the DSL derives from where it is written.
- **One LogicalIP per (collision domain, name), resolving into every family**
  of the collision domain. Allocated addresses use **one host number** for
  all families (§5), so a name's IPv4 and IPv6 cannot disagree.
- **`{ host }` pins the number.** Everything the user writes in the topology
  becomes a LogicalIP with `host` taken from the written address — so a
  written address never moves, without a second type. Its shape:

  ```ts
  type HostRef = number | "first" | "last";
  type Host =
    | HostRef                                  // every family of the collision domain
    | { ipv4: HostRef }                        // IPv4 only
    | { ipv6: HostRef }                        // IPv6 only
    | { ipv4: HostRef; ipv6: HostRef };        // both, each its own
  ```

  - a `number` is the host number (in IPv6 the same number, in hex — §5);
  - `"first"` / `"last"` are the collision domain's first and last address
    **per family**, as `transitNetwork` uses them today (`ip.ts`
    `first()`/`last()`): in a `/28` `.1` and `.14` (`.15` is broadcast), in a
    `/124` `::1` and `::f` (host 0, the subnet-router anycast, is skipped). So a transit's two ends are `"first"` and
    `"last"`, whatever their numbers are per family;
  - a plain value applies to **every family of the collision domain**; an
    object lists **exactly the families the name has** — `{ ipv4: 5 }` is an
    IPv4-only address, with no IPv6 allocated for it;
  - an object with both families pins each on its own, for a written pair
    whose numbers differ.

  The DSL also pins what someone outside depends on — the platform DNS at
  `.10`, whose address ends up in `docker run --dns` and resolver configs —
  and exposes an optional `host` on the builders that make core legs.

**A collision domain's addressing is declared once, with its allocation
strategy:**

```ts
const core = net.ipNet({
  ipv4: "172.21.0.0/24",
  ipv6: "2600:70ff:b3c0::21:0/112",
  allocator: "low-high",            // default
  // or per family: allocator: { ipv4: "low-high", ipv6: "high-low" }
});
core.host(10);                      // pinned; a leg without one is allocated
```

`allocator` is `"low-high"` (lowest free number first), `"high-low"`
(highest free first — a removed name's number is reused only when the low
end is full), `"random"`, or an object with one strategy per family. It is
**serialized in the IR** as one `ip.net` node per collision domain, with its
prefixes; every address refers to it:

```
ipnet:ls:internet-core  { ipv4: "172.21.0.0/24", ipv6: "…/112",
                          allocator: { ipv4: "low-high", ipv6: "low-high" } }
address                 { net: "ipnet:ls:internet-core", owner: "<IR id>", host?: … }
```

so the runtime allocates by the declared strategy, never by a default of its
own. **A net is only configuration.** The runtime creates **one allocator per
collision domain** from the net that domain is addressed with; the same net
used on two collision domains gives two independent allocators (two pools —
overlap is allowed). Nets are compared **by value** (prefixes and
allocator), not by object: writing the same `net.ipNet({…})` inline twice on
one collision domain is one net. A collision domain addressed with two nets
that **differ** is an error: it would have no single configuration. The DSL
binds nothing — which net addresses which collision domain follows from
where its addresses are used, and the runtime checks it. `"random"` stays deterministic — a pseudo-random order seeded by the
collision domain and the name (`create` is reproducible from the IR alone);
it only spreads numbers, it never draws from a random source. The DSL's own
cores (`net.internet({ core })`, a tenant's `core`, the control plane) are
`net.ipNet`s like any other.

**Using a net's address in the topology:**

```ts
const lan = net.ipNet({ ipv4: "192.168.128.0/24", ipv6: "2600:70ff:b3c0:128::/64" });
lan.host(1)              // the LogicalIP: one name, every family of the net
lan.host(1).ipaddrs()    // its addresses as a list — an endpoint's `ipaddrs:`
lan.host(1).asIpv4()     // its IPv4 alone — a next hop (`via:`)
lan.host(1).asIpv6()     // its IPv6 alone
```

A next hop is one address of one family (the family of its route's `dst`),
so `via:` takes `asIpv4()` / `asIpv6()`, never the whole LogicalIP. Asking a
LogicalIP for a family its net does not have is an error at generation.

What the runtime **allocates** (no `host`):
- the legs on the shared cores — a leg on a core is an address on the core's
  net without `host`; TypeScript counts nothing (`internet.allocate()`,
  `tenant.allocate()` in `src/tenant.ts` go away);
- the control-plane addresses (platform services, ingress legs; the counter
  `allocateAddrs` in `src/define.ts` goes away), except the pinned DNS.

Everything else is pinned. Route destinations stay prefixes (they are no
addresses of anyone).

**Not our world.** An address that belongs to no collision domain of the
fabric — a resolver's upstream (`forward . 1.1.1.1` in a CoreDNS config), a
WireGuard peer's endpoint — is not a typed address at all: it stays part of
its config text, as written. The fabric neither allocates nor checks it.

**Out of scope: allocating transit networks.** `svc: transitNetwork(…)` stays
as written — pinned LogicalIPs, each transit a `/28` on purpose (the
addresses between its two endpoints are free for a debugging machine to
join). Allocating transits belongs to a larger design that is not part of
this ADR.

### 2. What stays in TypeScript

The route computation stays where it is: which prefix goes to which router,
via whom (`ir.ts`, routing domains, `anchorAddressSharedWith`). A next hop
is a LogicalIP like any other — `nexthop: LogicalIP("internet-core",
"internet-wan-avm")` instead of `172.21.0.1`. The checks TypeScript does
today still work, because a LogicalIP knows its collision domain:

- dst and next hop in the same family;
- a next hop lies on a collision domain the router is attached to
  (`viaIsOnSegment`);
- "is this next hop the router's own address" — by identity (collision
  domain, name), not by value;
- two names with the same pinned number in one collision domain, or a pinned
  number outside its prefix → error at generation. (The same LogicalIP
  *referenced* in many places — a resolver, next hops — is no clash.)

**Address and prefix.** The same address appears with its prefix length (an
LRP's `networks`, a NIC's addresses) and bare (a next hop, an SNAT's external
IP, a nameserver). The reference does not say which: it always resolves to
the address **with** its prefix (from its net), and the consumer takes the
form it needs through the IP address library (`to_string()` with the prefix,
`to_s()` without) — as the code does today.

What TypeScript can no longer check is a clash between two *allocated*
values; that moves to the runtime (§3).

### 3. What the runtime does

Before planning — and before release-before-claim, whose natural keys
(a veth's MAC) depend on resolved addresses — the runtime **resolves** every
collision domain once: every LogicalIP gets its address, every derived MAC
is computed (§5). The result is the **address metadata** (§3b), handed to
every service; a service turns the references of its IR node into concrete
values through it. The IR itself is not rewritten. Everything below the
services (primitives, RawIR prediction, the `run` comparison, ADR 0010)
gets concrete values exactly as today and never sees a LogicalIP.

**Finding a name's live number — an owner tag on the carrier.** The object
that carries an allocated address is tagged with its owner when it is
created: `<collision domain>|<name>`. The resolver reads the tags from the
live RawIR, not port ids or names — so the number sticks to the name, and a
later change of how ids are derived (`deriveEndpointName`'s hash) renumbers
nothing. The tag lives on the very object that holds the address; it is no
separate state store.

| Carrier | Where the address is | Where the owner tag is |
|---|---|---|
| OVN router port | LRP `networks` | LRP `external_ids:ovn-fabric-owner` |
| OVN switch port (a container NIC on a switch) | LSP `addresses` | LSP `external_ids:ovn-fabric-owner` |
| kernel device (a netns leg, a container NIC) | `addr` on the link | the link's `alias` (ifalias) |

An SNAT's external IP is its router port's own address (`src/ir.ts`): it has
no tag of its own, it follows the port. A rename changes the name, so the
renamed component gets a new number: a rename is a removal plus an addition.

**Fail closed.** If the live view of a collision domain is incomplete — a
collector timeout, a partial read, an authority that cannot be reached —
resolution of that collision domain **fails** and nothing is allocated in it;
otherwise held addresses would look free and be handed out again.

**Precedence**, per collision domain, highest first:
1. pinned numbers (`{ host }`) of the IR;
2. live numbers of names still in the IR — such a name keeps its number;
3. new names, sorted, by the collision domain's allocator: `low-high`
   (lowest free first), `high-low` (highest free first) or `random`
   (deterministic order seeded by domain and name), per family as declared.

**The pool** for new names is the collision domain's prefix minus the
reserved addresses (IPv4 network and broadcast, `/31` and `/32` as the kernel
treats them; IPv6 host 0, the subnet-router anycast) minus 1–2 **minus every
address live in the collision domain**: ours and foreign, ports of names no
longer in the IR, and addresses still referenced by live NAT or route rows of
ours whose port is gone. An address becomes free only once nothing live holds
it — so a name removed in this run never hands its address to a name added in
the same run (its port is removed only at the end, by the cleanup), and no
reservation or timeout is needed.

**Per command:**
- **`run`** resolves as above.
- **`create`** is not changed by this ADR: it consults nothing live (it
  stays unconditional) and is for an empty system. OVN does not reject a
  duplicate address (`lrp-add` with an address another port holds succeeds),
  so on a non-empty collision domain `create` can place a name on a held
  address without an error; emptying the system first is the operator's job
  (see `prune`, open questions). `create` is **the same allocator with an
  empty live view**: `allocate(domain, names, live = ∅)`, over every
  allocated name **of the collision domain in the whole IR** — not only this
  host's. So every host computes the same assignment, and a fresh system is
  reproducible from the IR alone. It can differ from what `run` keeps on a
  system with a history; that is intended.
- **`delete`** removes what is live, as today; it needs no allocation.

**Errors** (each names the collision domain and the names involved; nothing
is changed):
- a live number outside the collision domain's prefix in the IR (the prefix
  was changed) — a **hard error**. Renumbering a collision domain is
  deliberate: a reinstall (`delete` + `create`) does it, as on VM 302 on
  2026-11-11. This is documented, not automated;
- the pool is exhausted: the names that could not be placed;
- the live view of the collision domain is incomplete (fail closed, above);
- a live name whose number a pinned `{ host }` of the IR now takes — always a
  user error (an address written into the topology that the fabric already
  uses); `run` never moves the live name to make room.

**The resolved view.** The assignment is reported in the run's journal and by
a view command (not `resolve`: that name is the system-dependency command,
`fabric/__main__.py`), in both modes — from live for `run`, deterministic
for `create`. The view is output only: it is never accepted as IR input,
since every allocated LogicalIP in it would turn into a pinned one.

Every place that carries an address resolves through the same metadata
(§3b): LRP `networks`, NAT external IPs (the port's own address), OVN and
kernel route next hops, veth and container NIC addresses, security-group
sources and destinations, ingress/egress legs, resolver nameservers, and
addresses inside config text.

### 3a. An address's natural key: (collision domain, address)

OVN does not refuse two router ports with the same address on one switch
(VM 302: `lrp-add` with `172.21.0.5` succeeded while another port held it).
So an address is a single resource **within its scope**, and gets a natural
key (ADR 0010):

```
naturalKey = addr:<collision domain>|<address>
```

Never the address alone: overlapping tenant prefixes make `172.22.0.3` two
different resources in two collision domains. A live port of ours holding an
address that a planned port claims in the same collision domain is released
before the plan applies (release before claim) — whether the clash comes from
an allocation or from a pinned address. An LRP's collision domain is not on
the LRP in the RawIR (it sits on its peer LSP, `options:router-port`); the
natural key follows that link.

Built 2026-11-12 for OVN router ports (`raw_reconciler/natural_key.py`
`address_keys`, one key per address; `fabric/release.py`). A planned router
port's switch comes from its planned peer port step — a router-type LSP's
footprint is not learned. Kernel addresses (netns devices, container NICs)
have no address key yet.

### 3b. References and the address metadata

**The IR never holds a number for a fabric address — only a reference.** A
service that needs an address names *what* it needs; the runtime knows *which
number* it is. One vocabulary serves every place:

```ts
type AddrRef =
  | { net: "ipnet:ls:home-cable-lan" }                 // "net": the collision domain's prefix(es)
  | { net: "ipnet:ls:internet-core", owner: "<id>" }   // "host(xyz)": one name's address
  | { self: true }                                     // the address of the port/NIC the service sits on
  | string;                                            // not our world: "1.1.1.1/32", as written
```

A reference names no family; the place that uses it does (a rule's
`family`, a route's `dst`), and takes that family of the reference — asking
for a family the collision domain does not have is an error. Prefixes the
user writes as destinations (a route's `dst`) stay prefixes.

**In the DSL**, the topology writes logical things, never numbers of the
fabric — a security group's sources, for example:

```ts
sources: [lan, dmz.host(5), workload, "1.1.1.1/32"]
//        net   host(xyz)    host(xyz) not our world
```

**At runtime — on the Python side, never in TypeScript** — resolution (§3)
builds one **address metadata** object per `create` / `run`, from pinned
numbers, live numbers and allocation, and passes it to every service. The
IR carries only the references and the `ip.net` nodes; the metadata is never
serialized into it:

```python
addrs.net("ipnet:ls:home-cable-lan", "ipv4")   # 192.168.128.0/24
addrs.of(ref, "ipv4")                          # 172.21.0.7/24 — with its prefix (§2)
addrs.self("ipv4")                             # the address of the service's own port
```

What it serves:

- **Masquerade.** An OVN `snat` is `{ kind: "snat", family, externalIp:
  { net, owner: "<the outgoing LRP>", family }, logicalIps: ["192.168.128.0/24",
  …] }`: a reference to the outgoing port's own address — `{ self }` would
  be ambiguous, a security group sits on the router, not on one port — and
  the prefixes of the router's other legs, as strings (a net's prefix is
  configuration, TypeScript knows it). Kernel masquerade needs no address
  (`-o <iface>`). The DSL has one spelling, `masq: ["ipv4", "ipv6"]`; kernel
  or OVN follows from the router.
- **Security groups and firewall rules** stay abstract: sources and
  destinations are references, rendered into concrete rules on the host only.
- **Next hops** are `{ net, owner }` references (§2).
- **Config text.** A generated file names an address by reference, as a
  template the service renders with the metadata (`{{ addr "<owner>" }}` in a
  Corefile, a `tailscale up` argument, DNS records later).
- **Dependencies.** A service records which references it resolved. When one
  of them resolves to another number, `run` knows exactly which rules and
  configs to apply again.

A service never reads a number from the IR, and TypeScript never writes one
for an address it does not pin — there is no `resolve()` in the generator.

### 4. One authority per collision domain

All hosts applying the same IR must resolve a collision domain identically.
That holds for OVN collision domains only if the allocator sees the whole
IR's names (§3) and the whole NB view of the switch.

- **OVN collision domains** (logical switches; `internet-core`, tenant cores,
  the control plane): the authority is the OVN northbound DB. A chassis
  cannot read NB (`ovn-nbctl` without `--db` works on the central only), but
  it is connected to the southbound DB, and SB's `Port_Binding.mac` carries
  every router port's addresses (`00:00:ac:15:00:05 172.21.0.5/24
  2600:70ff:b3c0::21:5/112`, seen on VM 302). A chassis that needs the number
  of an allocated address — a kernel route, a container's `--dns`, a
  security-group rule — reads it there. The central allocates; a chassis only
  reads what the central placed.
- **Host-local collision domains** (a veth pair between an OVN port and a
  netns that exists on one host only): the authority is that host.

The IR marks each collision domain with its authority, so no host guesses.

### 4a. The allocator is an API, served locally for now

A central allocator may come later (a cluster-wide one, e.g. on etcd — known
today only as a possibility). So the allocator is built **as an API from the
start**, even though today it is served in-process, locally:

- **Request / response, no shared state.** `allocate(domain, names, live) →
  assignment`: the collision domain (prefixes, allocator strategy, pinned
  numbers, authority), the names it must place, and the live numbers and
  addresses. It returns name → host numbers and touches nothing itself.
  Resolution (§3) is its only caller, and talks to it only through this
  interface — no reaching into its internals.
- **Serialisable.** Request and response are plain data, so the same call can
  later go over the wire unchanged.
- **Deterministic.** The same request gives the same response, on any host.
- **Replaceable server.** Today the local implementation answers. A central
  allocator later answers the same request — how it serialises concurrent
  allocations on one collision domain (etcd, OVSDB transactions, anything
  else) is its own business; callers do not change.

Until a central allocator exists, `run`s that change the names in one OVN
collision domain — adding or removing — are not run concurrently: one at a
time per collision domain. The service's lock is per host only
(`fabric/services/apply.py`); nothing serialises across hosts yet.

### 5. One host number per name — IPv6 and MAC follow it

When every family of a collision domain uses the same strategy (the
default), the allocator places a name as **one host number** for all
families, the convention the cores have today:

```
internet-core 172.21.0.0/24 + 2600:70ff:b3c0::21:0/112, "home-nat" → host 9
  IPv4 172.21.0.9   IPv6 2600:70ff:b3c0::21:9   (192.168.199.9 → xxx::9)
```

The host number goes into the IPv6 host part **as the same number**, in
hex: host 10 is `.10` and `::a`, host 300 is `.1.44` (in a `/16`) and
`::12c`. Numeric, not the decimal digits read as hex — that would waste
most of the IPv6 space and break past host 9999. A collision domain's IPv6
prefix must have **at least as many host bits** as its IPv4 prefix (a `/16`
needs a `/112` or larger); the generator checks it. Different numbers per
family come only from a pinned address with one number per family, or from a
collision domain that declares a different strategy per family — then each
family is allocated on its own.

**The MAC stays the fold of the resolved IPv4** (`macFromV4`,
`00:00:<ipv4 hex>`), computed by the resolver (§3) for every port and NIC —
it moves from TypeScript (`resolveMac`, `macFromAddrsOrName`) to the runtime,
one place only. No hash of the name: two names in a collision domain never
hold the same host number at once, so the MAC is unique per collision domain.
A **reused** address (rare: a removal, then an addition) gives the new owner
the **same MAC**; a peer's stale ARP entry or MAC_Binding then still points
at the right MAC, and the OVN MAC_Binding age threshold (300 s by default,
`DEFAULT_MAC_BINDING_AGE_THRESHOLD`) clears what is left. A collision domain
without IPv4 keeps the name-hashed fallback (`02:…`). Only a MAC the
topology sets **explicitly** is carried in the IR; a derived one is not.

**Out of scope: `serve-graph`.** The graph page was built for debugging and
may move to the runtime side. Until then it shows addresses as the IR has
them — LogicalIPs as `(collision domain, name)` with their `{ host }` if
any — and no resolved numbers. It gets no allocator of its own.

## Consequences

- Adding, removing or reordering a component never moves another one's
  address. The 302 incident cannot recur.
- One address type in the IR. A written address is a pinned LogicalIP, so it
  never moves; the core legs' and the control plane's numbers leave the
  topology and the IR, and the runtime chooses them.
- The IR's hash changes only when intent changes, not when an allocation
  shifts.
- The IR protocol changes shape for every node kind that carries an
  address — a breaking protocol change for both halves.
- The MAC derivation moves to the runtime.
- Debugging needs the resolved view; the IR alone no longer shows numbers
  for allocated addresses.
- Address-clash checks for allocated values move from generation to the
  runtime; a duplicate address in a collision domain is caught by its natural
  key (§3a).
- A rename (of a router, a tenant, a role) changes the owner name, so it
  changes that component's address — the one edit that still moves an
  address.
- A system converged by `run` and a fresh `create` of the same IR can have
  different numbers. Intended: the live system is the state for addresses
  only the fabric needs.

## Migration

1. **The generator emits references** (TypeScript only). Every address is a
   reference (§3b): written ones pinned, core legs and control-plane addresses
   without `host`, transit ends `"first"`/`"last"`, an `ip.net` node per
   collision domain with its allocator; only explicit MACs; `masq: […]` as
   the one DSL spelling — the tenant NAT gateways and the control plane's
   egress and ingress legs alike (`externalIp` a reference to the port). TypeScript
   counts nothing any more. The deployer cannot read this IR until step 3 —
   accepted (2026-11-12). Done before it: the DSL (`net.ipNet`,
   `src/ipnet.ts`) and a byte-identical round trip of the old IR through the
   logical form, as a check of the shape (a temporary converter, removed
   once the generator wrote references itself: `src/ir-addresses.ts`; the
   types live in `src/protocol.ts`).
2. **The natural key `(collision domain, address)`** (§3a).
3. **The runtime resolves and allocates** (Python): the address metadata
   (§3b) handed to every service, allocation (§3, §4), owner tags on the
   carriers.
   Seed: on the first `run` no carrier has an owner tag yet — a carrier
   without a tag is matched once by its IR id (as `run` matches it today) and
   tagged, so every name keeps its live number and introducing it moves
   nothing. A fresh
   `create` gives sorted-order numbers from then on; tests that compare
   `create` output change.
4. Independently: `run` applies a node that differs (same id, other fields)
   by **delete + create** — the old node is removed before its unit runs, and
   the unit's dependent steps run again — so any change that does move an
   address is applied rather than skipped. No in-place update: one way to
   apply a change, the same for every kind. What differs is only what a
   step sets itself: a back reference its children fill (`portRefs`,
   `routeRefs`, a link's `masterRef`) or a field another step of the plan
   changes (the bridge mapping on a bridge) is no difference of the node.
   Built 2026-11-12: `run` removes what differs before the units run, then
   looks again; a step with a value only known at run time is not replaced.

## Open questions

None left.

Deferred, with reasons:

- **The template syntax for config text** (§3b): how a generated file
  names a reference, and which services render templates. Today no config
  text holds a fabric address (checked on the cluster topology, 2026-11-12);
  it becomes needed with the platform services.


- **Reinstalling one host while others stay live** is **not supported**. A
  `create` on that host allocates against an empty view (§3) while the other
  hosts' ports still hold numbers in the same OVN collision domains, so it can
  place a name on a held number. Reinstall the whole system, or use `run`.
- **`prune`.** `create` stays as it is and needs an empty system. A later,
  radical variant of `delete`, named `prune`: it removes from the
  **reconciled live RawIR** instead of from the IR, respecting only the IR's
  exclude list (`hostInterfaces`) — so any system can be emptied before a
  `create`, whatever IR (or history) built it.

Related: ADR 0002 (IR), 0006 (plan-driven reconciliation), 0008 (learned
RawIR), 0009 (tenants and wans), 0010 (natural keys).
