# ovn-fabric

A declarative OVN/OVS network topology generator. You describe a network —
hosts, routers, collision domains, VPN tunnels — as plain TypeScript, and
ovn-fabric emits a desired-state **IR** (JSON): one `{id, kind, key, data}`
node per host / collision domain / router port / workload. Each host runs
**one Python service**, the fabric runtime (`fabric/` on top of the generic
event runtime in `runtime/`), shipped as a single PEX. Hand it the IR and it
makes the host match: OVS bridges and VLANs, the OVN logical topology
(routers, switches, NAT), kernel-router namespaces, WireGuard, and docker
workloads. It also keeps them that way, by replaying the deployed IR after a
reboot and bringing back containers that died.

No netplan, no hand-written `ovn-nbctl`/`ovs-vsctl` invocations, no
configuration drift between "what I meant to set up" and "what's actually
running." The config is the single source of truth; the IR is a disposable,
regeneratable artifact.

**Documentation:** [mabels.github.io/ovn-fabric](https://mabels.github.io/ovn-fabric/)
(getting started, the topology DSL, the API reference, deploying, host
requirements, troubleshooting, architecture).

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
  const central = net.sshHost({
    name: "central",
    address: { ipv4: IPv4.parse("10.99.0.1/32") },
    user: "root",
    ovn: { role: { kind: "central" } },
  });
  const chassis = net.sshHost({
    name: "chassis",
    address: { ipv4: IPv4.parse("10.99.0.2/32") },
    user: "root",
    ovn: { role: { kind: "chassis" } },
  });

  const lan = net.collisionDomain("lan");
  const upstream = net.collisionDomain("upstream");

  const router = net.defineOvnRouter("router-lan", (router) => ({
    routingDomains: [],
    endpoints: [
      router.ovnRouterEndpoint({
        l2Segment: lan,
        ipaddrs: [IPv4.parse("192.168.10.1/24")],
        ifaces: [
          {
            host: chassis,
            iface: { kind: "vlan", vlanParent: "eth1", vlanId: 10 },
          },
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

## Deploying to a host

From a checkout, `scripts/fabric-deploy.sh` does both steps over ssh: it
deploys the runtime to the host, then runs a `fabric` command there with the
IR on stdin:

```sh
deno run -A src/cli.ts generate-ir topology.ts \
  | scripts/fabric-deploy.sh root@HOST ir-apply run          # converge to this IR
scripts/fabric-deploy.sh root@HOST ir-apply delete < ir.json  # rewind it
scripts/fabric-deploy.sh root@HOST ir-apply run --dry-run < ir.json
```

**Deploying the runtime** (`scripts/fabric-install.sh`, a wrapper around
`python3 -m fabric deploy`) builds the PEX (`dist/fabric.pex`) and copies it
into the host's store by hash. It links the service to it, restarts it, and
checks that it answers with that hash; if it doesn't, it goes back to the
previous one. `--ref previous` switches back by hand. On the host:

```text
/var/lib/ovn-fabric/runtime/             stored runtimes (the last 10 are kept)
/usr/local/lib/ovn-fabric/fabric.pex     -> the deployed one
/etc/systemd/system/ovn-fabric.service   the one service
/run/ovn-fabric.sock                     where it listens
```

The host's hostname says which of the IR's hosts it is.

**On the host, the deployed PEX is the CLI** (`fabric …`). It talks to the
runtime over the socket, and every request runs as a job that outlives the
ssh session:

```sh
fabric status                    # deployed IR, last apply, background jobs, kept-alive containers
fabric jobs                      # running and recent jobs
fabric follow JOB                # a job's log so far, then live
fabric ir-list | ir-get | ir-put | ir-delete
fabric ir-apply run deployed     # run the deployed IR again
```

`ir-apply` has three actions. Only `run` looks at the live state:

- `create` makes everything the IR describes,
- `delete` rewinds it,
- `run` converges: it compares the IR with what is live, creates what is
  missing and, unless `--no-cleanup` is given, removes leftovers.

Docker images are built in the background, so a run is done as soon as the
network is up. Workloads follow when their images are ready.

The runtime installs the host's packages itself, for each distro and
platform (apt or dnf; Debian, Ubuntu, Fedora or Rocky; generic or
Raspberry Pi). After a reboot it replays the deployed IR. While an IR is
applied, it keeps that IR's containers alive, waiting longer between
retries for one that keeps dying.

## Model

- **Host** — where a router's config actually gets applied
  (`net.sshHost({ name, address, user, … })` or `net.localHost({ name, … })`). Every
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

- One writer: the runtime is the only thing that changes the host's
  fabric state. The CLI only sends requests to it, and every apply is a job
  you can `follow`.
- Plan, then act: an apply plans the IR into units of primitives
  (`fabric/primitives`). What each primitive does to the live state was
  learned on a real system (`primate`, ADR 0008). The live state is read
  through `ladops` (`ip -j`, `ovs-vsctl`, `ovn-nbctl`, `nft -j`, docker).
- Natural keys and stable addresses: a resource is identified by what it
  is, not by its generated name (ADR 0010). Allocated addresses are
  remembered across reboots (`/var/lib/ovn-fabric/allocator.json`,
  ADR 0011/0012).
- The physical uplink is the host's job: fabric creates VLANs on the NIC the
  topology names (e.g. `ovnkern`). The NIC itself must exist under that
  name and be brought up by the host's own network configuration.
- WireGuard tunnels use `wg-quick`, not hand-rolled `wg setconf` — this
  relies on `wg-quick`'s own fwmark + policy-routing so the tunnel's own
  traffic keeps using the kernel netns's upstream leg while everything else
  gets diverted into the tunnel.

## Repository layout

- `src/` is the TypeScript DSL and the `ovn-fabric` CLI (`generate`,
  `generate-ir`). `src/protocol.ts` defines the IR schema.
- `protocol/` holds the Python dataclasses generated from that schema (the
  desired-state IR and the raw live-state snapshot).
- `runtime/` is the generic event runtime (ADR 0007): messages, peers,
  transports, jobs, journal. It knows nothing about networks.
- `fabric/` is ovn-fabric on top of it: the services (`ir`, `apply`,
  `sysdeps`, `docker`, …), planning and reconciliation, host-OS discovery,
  the PEX bundle and deploy, and the CLI (`python3 -m fabric`).
- `ladops/` reads and drives the live system: Linux networking, netns, OVS,
  OVN, OVSDB, iptables/nftables, docker.
- `raw_reconciler/` collects the live host state as a raw graph snapshot
  (`python3 -m raw_reconciler`).
- `primate/` learns what each primitive does to that snapshot, on a real
  system (`python3 -m primate list | learn`).
- `scripts/` contains `fabric-deploy.sh`, `fabric-install.sh`,
  `fabric-remote.sh` and the QEMU, cloud-init and Proxmox test helpers.
- `docs/adr/` records the architecture decisions behind all of the above.

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
