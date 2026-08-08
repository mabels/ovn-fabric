# ADR 0002: Flat intermediate representation for reconciliation

Status: proposed

## Context

The generator today (`generate-ovn.ts`/`generate-netns.ts`) is a pure
function of `topology.ts`: it produces one self-installing shell script,
never touches a live host itself, and every emitted line is additive/
idempotent (`--may-exist`, `ip addr show | grep -q ... || ip addr add
...`). This is deliberate — referenced throughout the codebase as "ADR
0001" (no file for it exists in this repo; it's known only from
comments, e.g. `generate-ovn.ts`'s "this generator never queries live
state" and `factories.ts`'s "the netns owns that, not this generator").
This ADR doesn't relitigate 0001; it extends the model where 0001's
scope stops.

Two real gaps surfaced while designing block-based address allocation
(carving segment subnets out of a shared supernet instead of each
segment claiming its own fixed `/24` by id):

1. **Allocation stability.** Keying an allocator by declaration order
   (the same trick `NetworkBuilder` already uses for transfer-link
   `slot`) is fine for OVN-internal plumbing nothing external ever
   references, but wrong for segment gateways: inserting a segment
   above another, or growing an earlier one's size, would silently
   renumber every later segment — exactly the kind of change a DHCP
   reservation or firewall rule elsewhere would never notice happened.

2. **Stale config on change.** Nothing today removes a real, previously
   -applied fact that the config no longer describes. `ovn-nbctl
   --if-exists lrp-del` + `lrp-add` (`emitIdempotentLrpAdd`,
   `generate-ovn.ts`) already unconditionally recreates every OVN
   logical router port on each run, so that side self-heals for free.
   Nothing analogous exists for real kernel-level artifacts a config
   change can orphan — a static uplink address, a route, a NAT rule, a
   WireGuard/ZeroTier identity moved elsewhere. `emitStaticIpv4`, for
   instance, only ever adds.

Both gaps reduce to the same missing capability: knowing what was true
*last time*, so this run can diff against it. That requires state the
generator doesn't have today.

### Alternatives considered and rejected

- **A continuously-running, always-listening reconciler daemon on each
  router**, reachable independently of a deploy being in flight. Real
  advantages (continuous drift correction, no invocation needed). Its
  bootstrapping circularity (pushing new config needs reaching the
  daemon, which is the thing that manages reachability), new persistent
  attack surface, and loss of a clean audit trail are real costs this
  design isn't ready to take on for a ~50-deploys/year cadence — deferred
  as an explicit future step (§ Multi-router coordination), not rejected
  outright. What *is* adopted now is the narrower middle ground: a
  reconciler/deployer that lives on the router but only runs when
  invoked, the same way the current self-installing script is already
  reached (SSH, or the existing systemd-on-boot path) — it gets native
  OVSDB access and resolves "which state do we trust" (always the
  freshly-reconciled live state, never a persisted record) without the
  always-listening daemon's bootstrapping problem, since nothing new
  has to be reachable to trigger it.
- **XML** (or any nested-tree-as-diff-substrate notation). Rejected on
  taste, but also on a real technical point: diffing a nested tree needs
  reorder-aware list diffing at every nesting level (interface's
  addresses, service's instances, router's routes, ...). A flat,
  normalized map sidesteps that entirely — see Decision.
- **A hand-rolled diff engine.** The flat-map diff itself doesn't
  warrant one — see Decision, point 5. General-purpose JSON-diff
  libraries were also considered and rejected: they diff nested trees by
  array index, the exact positional-matching problem the flat model
  exists to avoid.

## Decision

### 1. Flat, normalized graph

`Record<string, GraphNode>` — every node keyed by a stable string, no
node ever embeds another node's full data. Cross-references are
key-strings, resolved by lookup within the same map (same pattern as
Redux's `createEntityAdapter` / Apollo's normalized cache: proven prior
art for exactly this "diffable, reference-holding entity graph"
problem).

### 2. Node identity granularity

A node exists at the finest grain where the *real system* enforces
uniqueness — not at "whatever object the config happened to nest it
under." An IP address is its own node, keyed by its value, with its
current interface as a plain field on it — not a list entry buried
inside that interface's node. This is what makes "the address moved
from eth0 to eth1" a single-key `update` (the `iface` field changed)
instead of an uncorrelated remove-from-one-node/add-to-another-node
pair requiring bespoke move-detection. The same treatment applies
uniformly to anything whose *value* carries independent real-world
meaning: an address, a WireGuard public key, a route prefix. Things with
no independent identity (a NAT masquerade rule, for instance) stay
inline attributes of their owning node — there's nothing to reparent.

### 2a. Key notation: `|` between scope and local part, not `/`

Scoped keys were first drafted as `{scope}/{kind}:{value}`, e.g.
`router:home/addr:192.168.128.2/24` — broken, because the value itself
already contains `/` (every CIDR prefix does) and `:` (every IPv6
literal does), so a key built that way can't be split back into
`{scope, value}` unambiguously; a naive `split("/")` cuts the CIDR
prefix length too. Fix: every scoped key uses `|` between the scope
prefix and the local `kind:identity` part instead —
`router:home|addr:192.168.128.2/24` — since `|` never appears in a
scope tag, a kind prefix, or any value this system stores (address,
CIDR, hostname, interface name). Applied uniformly to every scoped kind,
not just the one that happened to already collide, so there's one rule
to remember rather than "some kinds are safe with `/`, some aren't."
Root-level kinds with no scope (`infra.host`) are unaffected — nothing
to separate a scope from.

### 3. Namespaced `kind`

`kind` is a dotted string — `net.uplink`, `net.segment`, `ovn.lrp`,
`ipv4.addr`, `ipv6.addr`, `ipv4.route`, `ipv6.route`, `infra.host` —
rather than a bare name, so the taxonomy stays extensible (`wg.*`,
`zt.*`, ... later) without collisions, and nodes can be grouped/filtered
by namespace prefix the same way `scope` prefixes already group by
netns/router/host.

### 4. Schema via ArkType

One ArkType schema per kind, unioned into the overall `GraphNode` type.
Validated against a real slice of `topology.mam-hh-core.ts` (46 nodes,
all 4 segments, all 6 uplinks) before committing to the shape.

### 5. Diff: a plain function, not an engine

```ts
function diffGraphs(before: Graph, after: Graph): DiffOp[]
```

One pass over the union of both key sets. `add` for a key only in
`after`, `remove` for a key only in `before`, `update` for a key in both
whose `data` differs (deep-equal via `@std/assert`'s `equal` — already a
project dependency, reused rather than hand-rolled). Every op carries
`{ pre, post }` (whichever side applies). No move-detection logic
anywhere in this function — a reparenting field changing is just an
ordinary `update`; a per-kind reconciler decides what operation that
implies (e.g. an address's `owner` changing means del-on-old +
add-on-new, since neither the kernel nor `ovn-nbctl` has an atomic move
primitive regardless of how the fact is diffed).

### 6. Referential integrity is a separate pass

ArkType validates a node's own shape. It does not, and structurally
cannot easily, confirm that a reference field (`owner`, `hostRef`,
`nexthopRef`, ...) actually resolves to a key present in the same
snapshot. That's an explicit second pass: walk every reference field,
confirm the target key exists, fail loudly if not.

### 7. Safe apply ordering is a separate pass

`diffGraphs` alone doesn't guarantee a safe execution order — it can
hand back "remove uplink X" and "keep a route whose nexthop is X" in the
same batch with no indication that's wrong. A topological sort over the
same reference edges (adds ordered referenced-before-referencer, removes
ordered referencer-before-referenced) is a second, separate pass over
the op list, using an existing library (`npm:toposort` or
`npm:graphlib`) rather than a hand-rolled Kahn's-algorithm
implementation.

### 8. State comes from live reconciliation, not a persisted history file

Superseded from an earlier draft of this point, which had a git-tracked
history file serving as the allocator's stability record, the diff's
`before` input, *and* the rollback target all at once — replaced by the
reconciler/allocator/deployer split (§ Multi-router coordination): a
router's own live state, reconciled fresh at both plan time (pulled by
the allocator) and apply time (re-reconciled locally by the deployer),
is *always* the source of truth for both allocation stability and the
diff's `before`. Nothing external has to be kept in sync with reality,
because nothing external is authoritative anymore — collapsing what was
two records that could quietly disagree (a file, and the actual host)
into one. The only thing still persisted is the deployer's own local
rollback checkpoint — the last successfully-applied IR, kept purely as a
revert target, never read back as an input to allocation or diffing (the
next apply re-reconciles live state again rather than trusting its own
prior note-to-self).

### 9. Recovery: apply-then-confirm, not a running daemon

Matches `netplan try`: apply the new state, start a timer, require an
explicit confirmation; auto-revert to the deployer's local rollback
checkpoint (§ 8) if confirmation never arrives (the same failure this
protects against — the change broke reachability — is what would prevent
confirming). Revert reuses `diffGraphs` with `before`/`after` swapped; no
separate rollback logic. Chosen over an always-listening daemon for the
reasons under Alternatives above — bounded-risk, nothing new has to be
reachable to trigger it, fits the ~50 deploys/year cadence.

### 10. Reconciler / writer split, per kind

Reconciler: `(pre, post) -> intent` — decides *what* needs to happen,
pure and testable in TypeScript. Writer: executes an intent against a
real target — today, emits shell command text (same as the current
`emit*` functions in `generate-ovn.ts`/`generate-netns.ts`); potentially,
later, a direct API call — without the reconciler changing.

### Node kind table

| kind | key | reparenting field | reconciler | writer |
|---|---|---|---|---|
| `infra.host` | `host:<name>` | — | attribute diff | SSH connect / local exec; bootstrap mechanics depend on `hostType` |
| `net.uplink` | `host:<h>\|uplink:<name>` | `hostRef` | container-level attrs | split: `ovn-nbctl` (logical side), `ip`/`wg-quick`/`zerotier-cli` (real side) |
| `net.segment` | `host:<h>\|segment:<name>` | `hostRef` | same shape as `net.uplink` | same split |
| `ovn.lrp` | `router:<scope>\|lrp:<name>` | `ownerRef` | unconditional delete+recreate (already true today, `emitIdempotentLrpAdd`) | `ovn-nbctl` |
| `ipv4.addr` / `ipv6.addr` | `<scope>\|addr:<value>` | `owner` | value-keyed — owner change = del-old + add-new | `ip addr add/del` (real) or `ovn-nbctl lrp-add` + `--if-exists lrp-del` (OVN, already unconditional) |
| `ipv4.route` / `ipv6.route` | `router:<scope>\|route:<prefix>` | `nexthopRef` | prefix-keyed — nexthop change = del-old + add-new | `ovn-nbctl lr-route-add` + `--if-exists lr-route-del`, or `ip route add/del` netns-side |
| `net.iface` | `<scope>\|link:<ifname>` | `scope` (a device reparents by which namespace it's in, not a `hostRef`/`ownerRef` field) | presence-keyed — pure add/remove | `ip link set <dev> netns <ns>` |
| `ovs.iface` | `host:<h>\|ovsiface:<name>` | — | attribute diff | `ovs-vsctl` |

`ovn.lrp`'s key gained the port's own `<name>` — the table originally
just said `router:<scope>|lrp`, which collides every LRP owned by the
same router onto one key. Found building `reconciler/ovn/` against real
data: `router-home` alone legitimately owns 4 distinct LRPs at once.

`net.iface`/`ovs.iface` weren't in this table at all before the
reconciler package (`reconciler/linux_net/`, `reconciler/ovs/`) was
built against real data — added so "which devices exist in a namespace"
(the record of what got moved into it from the global namespace) and
OVS's own port/interface inventory are both represented, not just
addresses/routes.

`NatRule`'s `{ kind: "masq" }` is not a one-shot instruction consumed
when building the IR — the rule it produces is bound to a specific real
interface (`-o <realIface>`, see `emitBackdoorNat`/the segment-NAT
emission in `generate-netns.ts`) and has exactly the stale-config
problem this ADR exists to solve: if the segment's resolved uplink
changes, the *old* rule needs removing, not just a new one adding
(today's emission is additive-only, same gap as `emitStaticIpv4`). This
needed more design than fit in one table row — see the dedicated
section below.

Still deferred, not designed in this pass (see Consequences):
`wg.identity`, `zt.network`, a `backdoor` node kind.

## Multi-router coordination: reconciler / allocator+generator / deployer

A single router can allocate against its own live state (§ Decision,
point 8) just fine on its own. It cannot do this correctly once there's
more than one router sharing a pool: each router only ever sees its own
state, so two routers' local allocators could independently hand out the
*same* "free" block from a shared supernet with no way to know about each
other. Fixing this means splitting the work across three phases, on two
different runtimes — not because more phases are inherently better, but
because "decide what's free across the whole fleet" and "apply to one
specific router" are only safe to do in different places.

### The three phases

1. **Reconciler → IR, on each router's own hardware.** Gathers that
   router's live OVN/kernel state locally (native OVSDB access, not
   remote `ovn-nbctl show` text-parsing — one of the real advantages the
   rejected always-listening-daemon option would have had, still
   available here without the daemon). Computes
   `sha256(canonicalSerialize(IR))` as a version token for exactly this
   snapshot. This (IR + hash) is pulled by, or pushed to, generator
   hardware.
2. **Allocator + generator, on generator hardware** — a workstation, CI,
   or a control-plane box, but deliberately *not* any individual router,
   since the whole point is a view spanning all of them. Holds the
   reconciled IR from every router in the fleet at once — the one place
   that can hand out non-overlapping blocks fleet-wide, which no
   per-router-local process can do correctly. Combines that fleet-wide
   view with `topology.ts`'s desired shape to compute each router's new
   desired IR, and pairs it with the exact hash from step 1 the
   decision was computed against. The actual allocation math — host→
   network, overlap detection, collapsing/excluding ranges to find what's
   free in a shared pool — runs on `mabels-ipaddress` (see § Reconciler/
   deployer runtime below), the same library the deployer itself uses;
   this was originally left unspecified as "hand out non-overlapping
   blocks," which glossed over the fact that a real address-arithmetic
   capability is required wherever this computation happens, not just a
   lookup.
3. **Deployer, back on that router's hardware.** Before applying,
   re-runs the reconciler *right now*, computes a fresh hash of current
   live state, and compares it to the hash shipped alongside the new IR
   in step 2.

### Compare-and-swap, not blind apply

Same pattern as HTTP's `ETag`/`If-Match`, or git's fast-forward-only
push check: don't apply a decision computed against a snapshot that
might have moved since.

- **Hashes match** — nothing on this router has changed since the
  allocator's snapshot; the decision is still valid. Diff
  `(fresh-live-IR, new-desired-IR)` and apply.
- **Hashes differ** — something changed between step 1's snapshot and
  now: manual drift, or a second deploy that raced ahead and already
  landed. Refuse to apply a decision based on state that's no longer
  accurate. This is not a blind-retry loop — the whole pipeline re-runs
  from step 1 for that router (fresh reconcile → fresh allocation
  decision against the now-current fleet view → a new deploy attempt
  with a new expected hash), so a stale decision can never silently
  apply.

This also settles a question left open in the address-allocation
discussion: a removed entity's block becomes reclaimable exactly when a
fresh reconcile no longer shows it in use — no separate grace-period
rule needed, since the allocator (step 2) never plans against anything
but a just-pulled, current snapshot.

### Scope extension over ADR 0001, named plainly

Generator hardware now needs read access to pull each router's
reconciled IR at plan time (step 2). This is a genuine, deliberate
exception to "the generator never touches live state" (ADR 0001) — not
a reversal of it. ADR 0001's principle was about the shell-generation
logic never needing to *inspect* live state to decide what commands to
*write*; that still holds — `topology.ts` → desired shape is still a
pure function of the config file. What's new is a distinct, narrow
read-only channel that exists specifically to solve fleet-wide
allocation coordination, which is a problem that didn't exist before
multiple routers shared one pool.

## Reconciler/deployer runtime: Python, not Rust or scriptc

### What was actually tested, not just discussed

Two alternatives were built and run for real before this was decided —
not evaluated on paper:

- **scriptc** (`vercel-labs/scriptc`, TypeScript-to-native, experimental,
  pre-1.0): a hand-written reconciler (no schema library) compiled
  100% statically, real ELF Linux binary, 448KB, ~2ms per invocation
  including real `ip -j` subprocess calls, verified against a real
  kernel's live state inside a container. That part works. But three
  real, reproducible gaps surfaced in under two hours of testing: (1)
  cross-compiling to *any* Linux target from macOS fails outright —
  `arc4random_buf` (BSD/macOS-only) is called unconditionally in
  scriptc's own bundled runtime with no Linux implementation, reproduced
  identically across glibc/musl × x86_64/arm64; native builds on Linux
  itself work, but only with clang ≥15 and glibc ≥2.36, a real
  toolchain-version floor; (2) the `coverage` command's own prediction of
  "builds successfully" doesn't reliably match what a real `--dynamic`
  build does — it missed a whole class of blocker (object property
  assignment) entirely; (3) every schema-validation library tried
  (ArkType, Zod, an Ajv-generated standalone validator) either can't
  compile statically at all (ArkType — architectural, its type inference
  isn't backed by a traceable runtime JS shape) or hits that
  property-assignment gap in its generated code (Ajv). None of this is
  "scriptc is bad" — it's "pre-1.0 tooling has pre-1.0 rough edges,"
  which is a real cost for code that applies changes to live network
  infrastructure specifically.
- **Rust**: not built, but weighed honestly against what the workload
  actually is — subprocess spawn, JSON parsing, walking data into IR
  nodes, sorting, hashing. None of that exercises what Rust is actually
  for (ownership-based memory safety under concurrency, zero-cost
  performance-critical abstractions). Its value here would be 100% "small
  binary, mature toolchain," 0% the workload's own needs — real
  advantages (`cargo build --target` cross-compilation that isn't a live
  experiment, `typify` for JSON-Schema-to-struct codegen), but a real,
  ongoing cost of a second language and toolchain for work that doesn't
  need Rust's power to get done.

### Python (stdlib-first) chosen instead

- **No build step at all.** Copy a `.py` file (or a bundled archive, see
  below) to the router, run it against whatever Python 3 is already
  there. No cross-compile toolchain, no target-triple/sysroot debugging,
  no "does this construct compile yet" uncertainty.
- **Effectively zero runtime footprint**, not "small" — Python 3 is
  already present on the Debian/Ubuntu-family distros this project
  targets, so there's no runtime to ship at all, unlike Deno (~80–100MB)
  or even scriptc's own dynamic-engine tier (~620KB).
- **stdlib alone covers everything identified so far**: `subprocess`
  (the `ip -j`/`iptables`/`ovn-nbctl` calls — CLI-output parsing
  throughout, not netlink or OVSDB's native protocol, matching the
  iptables-has-no-clean-API constraint that was always going to force
  this anyway), `json`, `hashlib` (the version-hash from § Multi-router
  coordination), `dataclasses`/`enum`/`typing`, and — confirmed via
  PyPI's own metadata, not assumed — `ipaddress` (stdlib since Python
  3.3, PEP 3144).
- **`mabels-ipaddress`** (PyPI, MIT, [github.com/mabels/ipaddress](https://github.com/mabels/ipaddress))
  is the one real external dependency: the author's own address-handling
  library, with a Python port carrying the same API/semantics as the
  `npm:ipaddress@0.2.6` package already backing `src/ip.ts` on the
  TypeScript side. This is what actually provides the allocation
  arithmetic (host→network, `.overlaps()`, collapsing/excluding ranges)
  that § Multi-router coordination's allocator step needs — chosen over
  Python's stdlib `ipaddress` (which, while real and capable, isn't the
  same library the generator side already standardizes on) specifically
  for that same-author, same-semantics-across-languages property.
- **`pex`** (Pantsbuild, Apache 2.0) bundles `mabels-ipaddress` — the one
  real dependency — plus the reconciler/deployer's own source into one
  self-contained executable at build time, so the router needs nothing
  beyond its already-present `python3`. Chosen over `shiv` (LinkedIn,
  BSD-2-Clause, also viable) on usage evidence: ~6.2M PyPI downloads/month
  vs. ~470K, roughly 13x — the more established, more widely-used tool of
  the two. Both are pure-Python bundlers; a dependency with C extensions
  would need a binary matching the router's exact architecture/libc,
  the same class of problem as scriptc's cross-compile failure — noted
  as a real limit of this approach, not glossed over, though nothing
  identified so far needs one.

### Type stability across the TypeScript/Python boundary

`ArkType`'s `.toJsonSchema()` (already proven real, draft 2020-12 output
— § Decision, point 4) feeds `datamodel-code-generator` (build-time-only,
runs on generator hardware, never ships to a router) targeting plain
stdlib `dataclasses`. Verified end to end: the generated module imports
only `dataclasses`/`enum`/`typing`, runs under a bare system `python3`
with nothing pip-installed, and — for free, no hand-written validator
needed — raises a `TypeError` naming the exact missing field if the
shape doesn't match. That last property is deliberately leaned on rather
than fought: this data is internal, produced by this project's own
generator, not adversarial external input, so what actually needs
catching is *version skew* (a stale reconciler bundle receiving
newer-shaped data), and a required-argument `TypeError` catches exactly
that failure mode without needing defensive-grade schema validation on
the consuming side.

The generated dataclasses module is its own shared package, imported by
every per-kind reconciler (`linux_net`, `ovn`, `ovs`, ...) — not owned by
or bundled inside any single one of them, since more than one reconciler
needs the same IR node types. See § Reconciler package layout below.

### Reconciler package layout

```
reconciler/
  cli.py            # argparse entry point, sweeps every namespace, orchestrates per-kind reconcilers
  netns.py          # shared: list_netns(), netns_scope(), run() — every net-aware reconciler takes netns as an argument, doesn't enumerate it itself
  ovsdb.py          # shared: OVSDB CLI JSON decoding (`-f json list <table>`), used by both ovn/ and ovs/
  ir_types/         # generated dataclasses (datamodel-code-generator output) — shared, not owned by any one reconciler
  linux_net/        # `ip -j link/addr/route`, global + every real netns — implemented
  iptables/         # `nft -j list ruleset`, per-netns — implemented
  ovn/              # `ovn-nbctl -f json list <table>` — implemented (ovn.lrp only; logical switches/ports still deferred)
  ovs/              # `ovs-vsctl -f json list <table>` — implemented (ovs.iface only)
```

CLI shape, deliberately extensible from the start rather than
hard-coded to today's kinds:

```
reconciler [--output FILE] [--kind {host,linux-net,iptables,ovn,ovs} ...] [--scope SCOPE] [--nice]
```

`--kind` repeatable, defaults to every known kind when omitted — adding
a fourth reconciler later means adding a subpackage and one registry
entry, not touching the CLI's own parsing logic.

## Firewall / security-group subschema

### Ordering: fractional/lexicographic sort keys, not sequential integers

Rule evaluation order within a chain is semantically meaningful
(first-match-wins for terminating targets), so some explicit order has
to be stored. A plain sequential integer means inserting a rule between
positions 3 and 4 forces renumbering every rule from 4 onward — the same
declaration-order fragility this whole ADR exists to avoid, just
recurring one level down.

Fix: `order` is a string drawn from a densely, infinitely subdividable
ordered alphabet (base-62 — digits, then uppercase, then lowercase,
compared lexicographically as plain strings), not an integer. Given two
adjacent rules with keys `A < B`, a newly inserted rule gets a generated
key `M` such that `A < M < B` — computed by treating the strings as
digits of a base-62 fraction and finding the midpoint, extending
precision by one more character only when there's no room left at the
current length (the same reasoning as finding a real number between 1.1
and 1.11 needing a third decimal digit: 1.105). Computing `M` touches
only the new rule's own key — `A`, `B`, and every other rule in the
chain are untouched. Inserting at the very front or back is
`generateKeyBetween(null, first)` / `generateKeyBetween(last, null)`.

This is the algorithm behind Figma's realtime-reorderable layers and
Jira's LexoRank — not something to hand-roll: `npm:fractional-indexing`
(Figma's own published implementation) is the concrete dependency,
consistent with this ADR's existing stance on toposort/deep-equal (§
Alternatives, "A hand-rolled diff engine").

One tradeoff to state plainly, not hide: repeatedly inserting at the
exact midpoint between the same two adjacent keys grows key length over
many edits (each insertion needs one more digit of precision). Bounded
for realistic editing patterns; the standard mitigation used by the same
real systems is an occasional full rebalance — a one-time renumber
across an entire chain, done rarely, not on every insert.

`order` lives in each `ipv4.fwrule`/`ipv6.fwrule`'s `data`, scoped to
comparability within the same table+chain (not globally), and stays
*out* of the rule's identity — reordering a rule shouldn't change what
it is, same reasoning as `action` below.

### Security groups decouple rule definition from attachment

A rule's real identity — "masquerade traffic from this segment" —
shouldn't be entangled with *where* it's currently applied. AWS/Azure
solve this by splitting rule DEFINITION (a named, reusable security
group) from ATTACHMENT (a separate fact: this group is bound to that
interface). Adopted here, in two layers — config-facing vs. diff-facing
— so authoring stays simple while the graph stays flat:

- `net.securitygroup` — a new node kind. Unlike a rule, a group's
  identity IS an explicit name, like `net.uplink`/`net.segment` already
  are: a reusable, referenced container a human/tool names once, not
  something derived from its members (which would make renaming or
  membership churn destabilize the identity).
- `net.uplink`/`net.segment` gain `data.applySecurityGroup: string[]` —
  the config-facing field, a plain list of group names. This is
  authoring convenience, not what gets diffed directly.
- At IR-build time, `toIR()` expands each entry in that list into its
  own `net.sgattachment` node — keeping attach/detach diff-clean (an
  ordinary add/remove of one flat fact) instead of becoming
  list-membership diffing, which the whole flat-graph design exists to
  avoid elsewhere (§ Decision, point 2).
- `net.sgattachment`'s key is `canonicalKey({kind, group, target})` (§
  below). Both ends changing is a genuinely different fact, not a
  reparenting — "group A on interface X" and "group A on interface Y"
  can coexist; nothing here is "the same attachment, moved."
- `ipv4.fwrule`/`ipv6.fwrule`'s `owner` points at a `net.securitygroup`,
  not directly at an uplink/segment.

### Canonical, self-typed keys for multi-field identities

Supersedes the earlier `fwrule:<name>` idea entirely — identity isn't a
human- (or AI-) assigned name, it's the match tuple itself, the same way
an AWS Security Group rule's identity IS its match tuple (the original
`authorize`/`revoke` API matches by exact protocol+port+CIDR; even the
newer `SecurityGroupRuleId` API assigns identity at creation specifically
so a rule's content can be edited without losing that identity — identity
and content are already a separate concern there too).

General mechanism, reusable by any future multi-field-identity kind, not
just `fwrule`: take the identity fields as a plain object — including a
`kind` field naming the node kind, so the serialized key is
self-describing on its own, without cross-referencing the containing
node — sort all keys alphabetically (this is what buys determinism;
construction order stops mattering), `JSON.stringify` single-line, use
that as the local part of the key.

`home`'s MASQ rule — a dimension the rule doesn't match on is omitted
entirely, not filled with a `"*"` sentinel (added once the reconciler
was actually built against real router data: `"*"` would be one more
magic string a real value could in principle collide with, and every
unmatched dimension would otherwise still cost a key/data slot for a
constraint that isn't there):

```
router:home|{"chain":"POSTROUTING","kind":"ipv4.fwrule","src":"192.168.128.0/24","table":"nat"}
```

`action` (MASQUERADE/DNAT/SNAT/ACCEPT/DROP/REJECT/...) and `order` are
deliberately excluded from this object and live in `data` instead — same
reasoning throughout this section: the rule needs to stay *the same
rule* whether its action, position, or attaching interface changes.

### ArkType schemas per key shape, not just per node envelope

The node-envelope schemas (§ Decision, point 4) validate
`{key, kind, scope, data}` — they don't validate the *structure* of a
key that's itself a canonicalized object before serialization. Each
multi-field-identity kind gets its own small schema for that
pre-serialization shape:

```ts
const FwRuleKey = type({
  kind: "'ipv4.fwrule'|'ipv6.fwrule'",
  table: "'nat'|'filter'|'mangle'",
  chain: "'PREROUTING'|'POSTROUTING'|'FORWARD'|'INPUT'|'OUTPUT'",
  "proto?": "string",
  "src?": "string",
  "dst?": "string",
  "sport?": "string",
  "dport?": "string",
  "iif?": "string",
  "oif?": "string",
});

const SgAttachmentKey = type({
  kind: "'net.sgattachment'",
  group: "string",
  target: "string",
});

function canonicalKey<T extends Record<string, unknown>>(shape: T): string {
  return JSON.stringify(shape, Object.keys(shape).sort());
}
```

`iif`/`oif` extend the shape drafted above — added once the reconciler
was actually built against real router data (`reconciler/iptables/`,
via `nft -j list ruleset`): every real MASQUERADE rule captured matches
on `-o <realIface>` as an essential part of what makes it a distinct
rule (the same source CIDR gets a separate rule per uplink it's
NAT'd through). Omitting it would collide genuinely different rules
onto the same key — the same class of gap `dev` filled for
`ipv4.route`/`ipv6.route` (§ Decision, node kind table).

`canonicalKey` validates against the kind's own `*Key` schema first,
then serializes — a malformed match tuple is caught before it's ever
written into a key, not discovered later as an unparseable string.

### Node kind table for this subschema

| kind | key | reparenting field | reconciler | writer |
|---|---|---|---|---|
| `net.securitygroup` | `host:<h>\|securitygroup:<name>` | `hostRef` | container-level attrs | n/a — pure grouping, nothing to write on its own |
| `net.sgattachment` | `<scope>\|` + `canonicalKey({kind, group, target})` | — (both ends changing = a different fact, not reparenting) | presence-keyed — pure add/remove | OVN or `iptables` chain-jump wiring, depending on `target`'s kind |
| `ipv4.fwrule` / `ipv6.fwrule` | `<scope>\|` + `canonicalKey({kind, table, chain, proto?, src?, dst?, sport?, dport?, iif?, oif?})` | `owner` (→ `net.securitygroup`) | signature-keyed — owner/action/order change = del-old-rule + add-new-rule (iptables has no atomic "update a rule" primitive either — same shape as addr/route) | whole table+chain, atomically replaced (`nft -f`), not per-row `-A`/`-D` — see below |

Writer strategy corrected once the reconciler (`reconciler/iptables/`)
was actually built against real router data: a per-row `-D <handle>`
writer doesn't work at all — `nft`'s own rule `handle` (confirmed in
every real captured rule) isn't stable identity, it's reassigned
whenever the ruleset reloads, so nothing the reconciler's canonical key
computes can be turned back into the handle a delete needs. The
reconciler still reports one node per real rule (unchanged — this is
still the right *read* granularity, and still what gets diffed); it's
specifically the *write* side that isn't row-addressable. The deployer
computes the desired table+chain's full rule list and applies it as one
atomic transaction (`nft -f <generated-script>`, replacing the whole
table or chain in one go) — the same "atomic-swap, not incremental
patch" reasoning as § Multi-router coordination's compare-and-swap,
just applied one level down, to a single kind instead of a whole IR.

## Consequences

**Gained:** allocation stability that survives insertion/reordering
without a positional trick, and works correctly across multiple routers
sharing one pool; principled stale-config cleanup instead of an
ever-growing set of add-only commands, sourced from live state that can
never quietly disagree with reality; reconciliation logic that lives in
typed, unit-testable code instead of embedded shell heuristics — as
TypeScript on the generator side, as Python (stdlib-first, one real
dependency) on the router side, deliberately *not* the same language
throughout, because the two sides have genuinely different constraints
(rich authoring vs. zero-footprint deployment) and forcing one language
across both would mean paying one side's cost on the side that doesn't
need it.

**Cost:** two new generator-side dependencies (ArkType, a topo-sort
library, plus `fractional-indexing` once § Firewall subschema is
implemented); one new router-side dependency (`mabels-ipaddress`,
bundled via `pex` so it never needs installing on the router itself);
`datamodel-code-generator` and `pex` as build-time-only tooling; a new
module to maintain in each language; "config in, script out" becomes a
pipeline (reconcile live state → allocate against the fleet-wide view →
diff → order → emit) instead of one direct pass, and now spans two
runtimes (router hardware, generator hardware) and two languages instead
of one — more moving parts, even though the *deployed* artifact on each
router is still one thing applied locally, same spirit as today.

**Explicitly deferred, not rejected:**
- The *always-listening* variant of the router-runtime reconciler (§
  Alternatives) — the invoked-on-demand version (reconciler + allocator
  + deployer, § Multi-router coordination) is adopted now; revisit
  always-listening if deploy frequency/scale or a real need for
  continuous (not just apply-time) drift correction changes the
  tradeoff.
- LRP modeling only covers each uplink's/segment's *primary* port in
  this pass — backbone-join LRPs (one per `extraRoutes` entry, per
  `emitBackboneJoin`) aren't in the node-kind table yet.
- `Backdoor` and WireGuard/ZeroTier identity (`wg.*`/`zt.*`) are named in
  the design discussion but not yet schema'd.
- Firewall/security-group (§ Firewall / security-group subschema) is
  fully designed but not yet implemented — `fractional-indexing` isn't a
  project dependency yet, and `toIR()`'s `applySecurityGroup` expansion
  step doesn't exist.

## Open questions

1. Deep-equal on full `data` for update-detection, or a cheaper
   content-hash per node?
2. Does a node carry its own edge list explicitly, or are edges "whatever
   reference-shaped fields happen to be in `data`" (harder to topo-sort
   generically, easier to keep in sync with the types)?
3. Per-netns vs. per-host scoping for `addr`/`route` keys — per-netns is
   more precise but means the same literal address value can legitimately
   be two different keys across two netns on one host; needs to be a
   stated rule, not an accident.
4. Should `UplinkSelector` (Fixed/Priority/Manual) ever appear in the
   graph, or only its currently-resolved choice? Current lean: only the
   resolved reference is a graph fact — the selection *strategy* is a
   config-time input, same reasoning as `SegmentGateway` never appearing
   in the graph itself, only what it resolved to.
5. Exact canonical form for `src`/`dst`/`sport`/`dport` in `FwRuleKey` —
   e.g. does a port range serialize as `"80-443"`, does a bare port
   normalize to `"80-80"` or stay `"80"`, does a CIDR get normalized to
   its network address before keying (so `192.168.128.5/24` and
   `192.168.128.0/24` don't accidentally produce different rules for
   what's meant to be the same match)? Under-specified in this pass.
6. When does a fractional-indexing rebalance actually trigger — a hard
   key-length threshold, a scheduled maintenance pass, or never
   (accept unbounded growth as a non-issue at this project's scale)?
7. Can `net.sgattachment`'s `target` ever be another `net.securitygroup`
   (AWS allows a security group to be referenced as another group's
   source/destination, not just as something attached to an interface)?
   Not addressed — this pass only covers group-to-interface attachment.
