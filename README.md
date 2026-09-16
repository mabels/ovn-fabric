# ovn-fabric

A declarative OVN/OVS network topology generator. You describe a network —
hosts, routers, collision domains, VPN tunnels — as plain TypeScript, and
ovn-fabric emits a desired-state **IR** (JSON): one `{id, kind, key, data}`
node per host / collision domain / router port / workload. Feed that IR to
the Python deployer (`deployer/`), which turns it into **one self-installing
shell script per host**. Copy that script to the host and run it: it creates
the OVS bridges/interfaces, builds the full OVN logical topology (routers,
switches, NAT), and installs itself as a boot-time systemd unit —
idempotently, so re-running it (or rebooting) is always safe.

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

  const router = net.defineOvnRouter("router-lan", (router) => {
    router.left = router.ovnRouterEndpoint({
      l2Segment: lan,
      ipaddrs: [IPv4.parse("192.168.10.1/24")],
      ifaces: [
        { host: chassis, iface: { kind: "vlan", vlanParent: "eth1", vlanId: 10 } },
      ],
      services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
    });
    router.right = router.ovnRouterEndpoint({
      l2Segment: upstream,
      ipaddrs: [IPv4.parse("10.0.0.2/30")],
    });
    return { routingDomains: [] };
  });

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

Then hand the IR to the Python deployer, which emits the per-host script(s):

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate-ir topology.ts \
  | python3 -m deployer.cli - --emit python > install.py
```

The deployer prints one script per distinct `Host` your config declares. For
a multi-host config, scripts are separated by a `# ===== host: X =====`
marker line — never printed before the first script, since each script is
meant to be saved and run (or `systemctl`-exec'd) as-is, and a leading
non-shebang line would break that.

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
  (`net.defineOvnRouter(name, (router) => {...})`) with a `left`/`right`
  endpoint. An `ovnRouterEndpoint` carries explicit `ipaddrs`, the
  `l2Segment` it joins, optional `ifaces` (the real NIC/VLAN it bridges),
  routing-domain membership, and `services` (`ipv6.slaac`/`ipv6.ra`, or an
  `attachTo(...)` workload NIC).
- **KernelRouter** — a real Linux netns forwarding between two real
  interfaces (a router endpoint whose other side is a **KernelRouter**):
  `kernelRouterEndpoint()` for a WAN/uplink, or `tunnelRouterEndpoint()`
  for a generic tunnel (WireGuard/ZeroTier) with a mesh transit on one side
  and an upstream leg on the other. Both are consumed into a plain OVN
  endpoint by their builder; the netns itself is emitted as `kernel.router`
  IR nodes.
- **Service / workload** — a reusable, network-free workload
  (`net.service(name, (svc) => {...})`) attached to one or more endpoints
  with `ep.attachTo(svc, { ipaddrs, routes?, primary? })`: one NIC per
  attachment (the k8s pod model). Each becomes a `kernel.app.container`
  node (a docker container the host runs).

See the doc comments in `src/types.ts` for the full model — every field has
an explanation of what it's for and why it's shaped the way it is.

## Design notes worth knowing before you rely on this

- Boot safety: package checks (`dpkg -s`) on the systemd/boot path are
  `timeout`-capped and warn-only — a missing tool never blocks router
  startup. Real `apt-get install` only ever runs in the manual/first-run
  branch, never from `systemctl`.
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
