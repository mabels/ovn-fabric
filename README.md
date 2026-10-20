# ovn-fabric

A declarative OVN/OVS network topology generator. You describe a network —
hosts, routers, collision domains, VPN tunnels — as plain TypeScript, and
ovn-fabric emits a desired-state **IR** (JSON): one `{id, kind, key, data}`
node per host / collision domain / router port / workload. Feed that IR to
the Python deployer (`deployer/`), which turns it into **one self-contained
Python script** carrying every host's pass. Copy that script to a host and
run it: it creates the OVS bridges/interfaces, builds the full OVN logical
topology (routers, switches, NAT), and installs the kernel routers and
workloads as boot-time systemd units — idempotently, so re-running it is
always safe.

No netplan, no hand-written `ovn-nbctl`/`ovs-vsctl` invocations, no
configuration drift between "what I meant to set up" and "what's actually
running." The config is the single source of truth; the IR and the generated
scripts are disposable, regeneratable artifacts.

## Install

**Via npx** (no local Deno install required — the official
[`deno`](https://www.npmjs.com/package/deno) npm package is pulled in
automatically as a dependency the first time you run this). Use the explicit
`-p`/package form — npx's implicit "guess the command from the package name"
doesn't reliably resolve a scoped package name (`@adviser/ovn-fabric`)
against its differently-named bin (`ovn-fabric`):

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate-ir path/to/topology.ts > ir.json
```

**Via Deno**, if you already have it (the CLI is the `/cli` export — the bare
package name is the library, for use from your own `topology.ts`, see below):

```sh
deno run -A jsr:@adviser/ovn-fabric/cli generate-ir path/to/topology.ts > ir.json
```

**As a permanent global command**:

```sh
deno install -g -A -n ovn-fabric jsr:@adviser/ovn-fabric/cli
```

## Quickstart

Copy [`examples/minimal-topology.ts`](examples/minimal-topology.ts) as a
starting point. Everything you need to declare a topology comes from the one
package import — `defineNetwork`, `IPv4`/`IPv6`, `transitNetwork`, and the
public types (`Host`, `CollisionDomain`, `Router`, the router-endpoint
shapes, the service/workload shapes) all live at the same
`@adviser/ovn-fabric` / `jsr:@adviser/ovn-fabric` path:

```ts
import { defineNetwork, IPv4 } from "jsr:@adviser/ovn-fabric";

export const network = defineNetwork("minimal", (net) => {
  const central = net.sshHost(
    "central",
    { ipv4: IPv4.parse("10.99.0.1/32") },
    "root",
    undefined,
    { role: { kind: "central" } },
  );
  const chassis = net.sshHost(
    "chassis",
    { ipv4: IPv4.parse("10.99.0.2/32") },
    "root",
    undefined,
    { role: { kind: "chassis" } },
  );

  const lan = net.collisionDomain("lan");
  const upstream = net.collisionDomain("upstream");

  const router = net.defineOvnRouter("router-lan", (router) => ({
    routingDomains: [],
    endpoints: [
      router.ovnRouterEndpoint({
        l2Segment: lan,
        ipaddrs: [IPv4.parse("192.168.10.1/24")],
        ifaces: [
          { host: chassis, iface: { kind: "vlan", vlanParent: "eth1", vlanId: 10 } },
        ],
        services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
      }),
      router.ovnRouterEndpoint({
        l2Segment: upstream,
        ipaddrs: [IPv4.parse("10.0.0.2/30")],
      }),
    ],
  }));

  return { hosts: [central, chassis], routers: [router] };
});
```

An OVN cluster needs a `central` chassis (so every chassis has an
`ovn-remote` to point at) plus at least one `chassis` host; a
single-host config with neither can be emitted as IR but not deployed.

Then:

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate topology.ts       # sanity-check what it declares
npx -p @adviser/ovn-fabric ovn-fabric generate-ir topology.ts    # emit the desired-state IR
```

A topology can take parameters from the run that loads it. Both commands
accept a repeatable `--runtime key=val`, and the topology reads them — and
the process environment — through `runtime()`:

```ts
import { runtime } from "@adviser/ovn-fabric";

const rt = runtime();
const debug = rt.args["debug"] === "1"; // --runtime debug=1
const site = rt.env["SITE"] ?? "hh"; // SITE=... in the environment
```

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate-ir --runtime debug=1 topology.ts
```

Then hand the IR to the Python deployer, which emits the deployer script:

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate-ir topology.ts \
  | python3 -m deployer.cli - > install.py
```

`install.py` needs only `python3` on the target. It holds the cluster-wide
OVN pass and every host's own pass, for both directions; pick what runs:

```sh
python3 install.py                      # create: every host, then the cluster pass
python3 install.py --action delete      # the reverse
python3 install.py --host NAME          # only that host's pass (run it on that host)
python3 install.py --cluster            # only the cluster-wide ovn-nbctl pass
python3 install.py --ovn-purge          # wipe the OVS database and, on the central, OVN's NB/SB
```

`--verbose` echoes every command.

Deploying a router can cut the host's own internet access while the deploy
runs, so nothing is installed inline. Each host's create **queues** what the
host needs instead — one JSON job file per package, docker image and systemd
unit in `/var/spool/ovn-fabric/install`, each named after the moment it was
queued:

```text
20261011T195501.000101Z-package-ovs.json
20261011T195501.000417Z-image-coredns_coredns_latest.json
20261011T195533.120008Z-unit-ovn-kernel-router-x.service.json
```

and installs the queue's consumer, `ovn-fabric-install.service`. It looks at
the directory every 10 seconds and works on the files in name order — the
order they were queued in: a job for something the host already has is
dropped without a connection, a missing package or image is installed
whenever the host is online (a public DNS resolver — `1.1.1.1`, `8.8.8.8`
or `9.9.9.9`, asked by address — resolves a query), and a router or workload unit
that did not come up is restarted once the packages and images queued
before it are there. A job's file is removed when the job is done, so what
is left in the directory is what the host still lacks
(`journalctl -u ovn-fabric-install` says why). A host that is online when
the deploy starts gets everything right away: the pass runs the consumer
once in the foreground before it goes on. `--skip-deps` leaves the queue
and its consumer out altogether.

`--ovn-purge` runs INSTEAD of an action and resets OVS/OVN to
a freshly-installed state — the logical topology of the whole cluster goes
with the NB/SB databases, so deploy again afterwards.

## Model

- **Host** — where a router's config actually gets applied
  (`net.sshHost(name, address, user)` or `net.localHost(name)`). Every
  router endpoint declares the host its interfaces live on, so a
  multi-chassis topology is a config change, not a redesign.
- **CollisionDomain** — a bare OVN logical switch (L2 only):
  `net.collisionDomain(name)`, or `net.backbone(name)` for the one
  cluster-wide backbone switch. Every router endpoint names the domain its
  port binds into.
- **Router / RouterEndpoint** — an OVN `Logical_Router`
  (`net.defineOvnRouter(name, (router) => ({ routingDomains, endpoints }))`)
  with an **array** of endpoints (at least two; no fixed left/right — a
  port is identified by its own derived `name`, not its position). An
  `ovnRouterEndpoint` carries explicit `ipaddrs`, the `l2Segment` it joins,
  optional `ifaces` (the real NIC/VLAN it bridges), routing-domain
  membership, and `services` (`ipv6.slaac`/`ipv6.ra`, or an `attachTo(...)`
  workload NIC).
- **KernelRouter** — a real Linux netns forwarding between two real
  interfaces (a router endpoint whose other side is a **KernelRouter**):
  `kernelRouterEndpoint()` for a WAN/uplink, or `tunnelRouterEndpoint()`
  for a generic tunnel (WireGuard/ZeroTier) with a mesh transit on one side
  and an upstream leg on the other. Both are consumed into a plain OVN
  endpoint by their builder; the netns itself is emitted as `kernel.router`
  IR nodes.
- **RoutingDomain** — a membership + route-distribution tag,
  `net.routingDomain(name, { routes? })`. With no (or an empty) `routes`,
  the domain's destinations are **calculated** from what its participants
  actually resolve (anchor routes + interconnect), minus infrastructure
  transit/backdoor/backbone prefixes; an explicit non-empty `routes` array
  overrides that. The domain's routes reach both its **router** participants
  (next-hopped at a peer's address on a shared domain) and any **workload**
  attached to a participating segment (next-hopped at that segment's
  gateway).
- **Service / workload** — a reusable, network-free workload
  (`net.service(name, (svc) => {...})`) attached to one or more endpoints
  with `ep.attachTo(svc, { ipaddrs, routes? })`: one NIC per attachment (the
  k8s pod model). A NIC's routes are its explicit `routes`, else the routes
  of the RoutingDomains its endpoint's router participates in, via that
  segment's gateway. Each workload becomes a `kernel.app.container` node (a
  docker container the host runs).

See the doc comments in `src/types.ts` for the full model — every field has
an explanation of what it's for and why it's shaped the way it is.

## Design notes worth knowing before you rely on this

- Boot safety: the router and workload units install no packages — a
  missing tool never blocks router startup. Packages only ever come through
  the install queue (above), whose consumer waits for the internet instead
  of failing and puts a time limit on every command it runs.
- Idempotency throughout: `--may-exist`/`--if-exists` on every
  `ovs-vsctl`/`ovn-nbctl` call, `ip link show ... || ...` guards, `cmp -s`
  before overwriting any file (including WireGuard confs — the tunnel only
  bounces if the content actually changed).
- WireGuard tunnels use `wg-quick`, not hand-rolled `wg setconf` — this
  relies on `wg-quick`'s own fwmark + policy-routing so the tunnel's own
  traffic keeps using the kernel netns's upstream leg while everything else
  gets diverted into the tunnel.

## Write-ups

- [Bridging OVN into a physical home network (or: how "what is OVS?" led here)](https://mabels.github.io/ovn-fabric/blog/ovn-fabric-writeup.html) —
  the motivation, the OVN/OVS architecture, and how ovn-fabric bridges OVN's
  virtual world to real VLANs and WireGuard. Source: [docs/blog/ovn-fabric-writeup.md](docs/blog/ovn-fabric-writeup.md).

## Publishing (maintainers)

`package.json`/`deno.json` carry a `0.0.0` placeholder version in git — there's
nothing to bump by hand. Tag a commit `vX.Y.Z` and push it; CI derives the
version from that tag, patches it into both files, then publishes to npm and
JSR via OIDC trusted publishing (see `.github/workflows/ci.yaml` and
`scripts/patch-version.mjs`; no token secrets involved, but it does require a
one-time registry-side setup — see the comment at the top of that workflow
file).

## License

Apache-2.0 — see [LICENSE](LICENSE).
