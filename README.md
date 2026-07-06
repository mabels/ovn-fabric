# ovn-fabric

A declarative OVN/OVS network topology generator. You describe a network —
hosts, segments, uplinks, VPN tunnels — as plain TypeScript, and ovn-fabric
emits **one self-installing shell script per host**. Copy that script to the
host and run it: it creates the OVS bridges/interfaces, builds the full OVN
logical topology (routers, switches, NAT), and installs itself as a
boot-time systemd unit — idempotently, so re-running it (or rebooting) is
always safe.

No netplan, no hand-written `ovn-nbctl`/`ovs-vsctl` invocations, no
configuration drift between "what I meant to set up" and "what's actually
running." The config is the single source of truth; the generated script is
a disposable, regeneratable artifact.

## Install

**Via npx** (no local Deno install required — the official
[`deno`](https://www.npmjs.com/package/deno) npm package is pulled in
automatically as a dependency the first time you run this). Use the explicit
`-p`/package form — npx's implicit "guess the command from the package name"
doesn't reliably resolve a scoped package name (`@adviser/ovn-fabric`)
against its differently-named bin (`ovn-fabric`):

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate-ovn path/to/topology.ts > install.sh
```

**Via Deno**, if you already have it (the CLI is the `/cli` export — the bare
package name is the library, for use from your own `topology.ts`, see below):

```sh
deno run -A jsr:@adviser/ovn-fabric/cli generate-ovn path/to/topology.ts > install.sh
```

**As a permanent global command**:

```sh
deno install -g -A -n ovn-fabric jsr:@adviser/ovn-fabric/cli
```

## Quickstart

Copy [`examples/minimal-topology.ts`](examples/minimal-topology.ts) as a
starting point. Everything you need to declare a topology comes from the one
package import — `defineNetwork`, every uplink/segment factory, and the
public types (`Host`, `Uplink`, `Segment`, `ManualUplink`, ...) all live at
the same `@adviser/ovn-fabric` / `jsr:@adviser/ovn-fabric` path:

```ts
import {
  defineNetwork,
  segmentPhysical,
  uplinkPhysical,
  ManualUplink,
} from "jsr:@adviser/ovn-fabric";

export const network = defineNetwork("minimal", (net) => {
  const host = net.localHost("this-host");

  const wan = net.uplink("wan", uplinkPhysical({
    id: "1",
    name: "eth0",
    nat: { ipv4: [{ kind: "masq" }], ipv6: [{ kind: "masq" }] },
    host,
  }));

  net.segment("lan", segmentPhysical({
    id: "10",
    name: "eth1",
    uplink: new ManualUplink(wan),
    gatewaySuffix: 2, // or gatewayIp/gatewayIpv6 for a literal override — see segmentPhysical's doc comment
    slaac: false,
    host,
  }));
});
```

Then:

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate topology.ts       # sanity-check what it declares
npx -p @adviser/ovn-fabric ovn-fabric generate-ovn topology.ts   # emit the install script(s)
```

`generate-ovn` prints one script per distinct `Host` your config declares.
For a multi-host config, scripts are separated by a `# ===== host: X =====`
marker line — never printed before the first script, since each script is
meant to be saved and run (or `systemctl`-exec'd) as-is, and a leading
non-shebang line would break that.

## Model

- **Host** — where a segment's or uplink's config actually gets applied
  (`net.sshHost(name, address, user)` or `net.localHost(name)`). Every
  segment/uplink declares its own host explicitly, so a multi-chassis
  topology is a config change, not a redesign.
- **Uplink** — a real (or eventually-real) path to the outside world:
  `uplinkVlan`, `uplinkPhysical`, `uplinkDummy` (placeholder, no backing
  interface yet), `uplinkWireguard` (a real `wg-quick`-managed tunnel).
- **Segment** — a client-facing network (`segmentPhysical`, `segmentVlan`),
  joined to the shared backbone and routed out through whichever uplink it
  points at.
- **Backdoor** — a second, dedicated transfer-link-shaped connection an
  uplink can borrow from an *already-real* uplink's own egress. Used for
  two distinct purposes: as a stand-in real interface while an uplink is
  still a placeholder (`uplinkDummy`), or as pure bootstrap egress
  alongside a real interface (e.g. a WireGuard tunnel's own handshake/
  keepalive traffic needs a mundane path to the internet before the tunnel
  itself is up).
- **NAT** — per-uplink and per-segment; currently `{ kind: "masq" }` for
  IPv4/IPv6 independently.

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
- WireGuard uplinks use `wg-quick`, not hand-rolled `wg setconf` — this
  relies on `wg-quick`'s own fwmark + policy-routing so the tunnel's own
  traffic keeps using whatever path already exists (see Backdoor above)
  while everything else gets diverted into the tunnel.

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
