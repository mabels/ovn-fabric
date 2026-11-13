# ADR 0012: Deploying the runtime, and a run that does not wait for docker

Status: proposed (2026-11-12). Decided with Meno: deploy is its own step;
`run` only ensures the deployed runtime runs; a run is done when the network
is; a new run keeps unchanged background jobs and releases the rest; the
CLI is the deployed PEX on the host, a PEX elsewhere only deploys.

## Context

A host runs one service, the runtime (`ovn-fabric.service`, ADR 0007),
listening on a named socket (`/run/ovn-fabric.sock`). The CLI is already a
client of it: `python3 fabric.pex --socket /run/ovn-fabric.sock ir-apply run`
hands the request over and prints the log the runtime streams back.
`scripts/fabric-remote.sh` runs that CLI on the host over ssh. At boot,
`ovn-fabric-reconcile.service` (a oneshot) calls the same CLI:
`ir-apply run deployed --no-cleanup`.

Three things make that slow and fragile:

1. **Docker blocks the run.** The runtime applies one IR at a time
   (`fabric/services/apply.py`, one lock), its units one after another, and
   a container's image is a step *inside* its unit (`_emit_image`,
   `deployer/ir_common.py`): a kernel router's unit is netns → addresses →
   routes → **image build** → container. One slow or failing build stalls
   that router and every unit after it. Seen on VM 302 (2026-11-12): the
   clock jumped ~26 h during a run, apt inside the ZeroTier and Tailscale
   image builds refused the package lists ("not valid yet"), and the run
   stopped there — the network of every later unit with it. At boot the same
   happens before a single router is up.
2. **The runtime is installed by a separate script** (`scripts/fabric-install.sh`:
   build, scp, write the unit, restart). There is one installed version, no
   record of which, and no way back but to rebuild the old one.
3. **The CLI owns the request.** Ctrl-C or a dropped ssh ends it
   (`fabric/__main__.py`), so a long run cannot be left and looked at later.

ADR 0007 already planned the fix for (1) as its step 4 — "sysdeps and
images as services: … `image/*` states; workloads `need` them" — and
step 5: "workloads as their own services (`needs` image and network, kept
alive)". This ADR makes that concrete, together with deploying the runtime.

## Decision

### 0. Two roles of one executable

The PEX is the runtime and its CLI at once; *where* it runs decides what it
may do:

- **On the host, the deployed PEX is the CLI.** It is the very executable
  the runtime runs, reaching it over the local socket. `run`, `status`,
  `follow` and the IR commands exist only there — so CLI and runtime always
  have the same hash. Another PEX started on a host is refused (it is not
  the deployed runtime: deploy it, or use the deployed one).
- **A PEX started anywhere else** — from a source checkout, or a built
  `.pex` on the operator's machine — can only **deploy**:
  `fabric deploy <user@host>` puts *itself* onto the host (ssh, scp) and
  makes it the deployed runtime there. Everything else is the host's
  deployed PEX, reached over ssh (as `scripts/fabric-remote.sh` does).

### 1. Deploying the runtime is its own step, into a store by hash

`fabric deploy <user@host>`, run from wherever a PEX is built, installs a
runtime; nothing else ever does. It deploys the executable it runs as.

The runtime is stored the way IRs are (`fabric/services/ir.py`): once per
content, under its sha256, with names pointing at hashes:

```
/var/lib/ovn-fabric/runtime/<hex>.pex        the executable, by content
/var/lib/ovn-fabric/runtime/<hex>.meta.json  {version, bytes, storedAt}
/var/lib/ovn-fabric/runtime/refs.json        {"deployed": "sha256:…", "previous": "sha256:…"}
```

`deploy`:

1. copies the PEX to the host unless that hash is already stored;
2. moves `deployed` to it (the old one becomes `previous`);
3. points the unit at the deployed executable (the unit's `ExecStart`
   names the stored path, or a link the deploy replaces);
4. restarts the service and waits until it answers `version` with that
   hash — else it moves `deployed` back and restarts the previous one.

`fabric deploy <host> --ref previous` (or a hash) switches back without
copying anything. Stored runtimes nothing refers to are removed, keeping
the last 10. `scripts/fabric-install.sh`
becomes a thin wrapper around `deploy`, or goes.

### 2. `run` makes sure the runtime runs — it never deploys

Before it hands a request over, the CLI asks the socket for `version`:

- nothing deployed → an error: "`fabric deploy` first";
- deployed but not running → `systemctl start ovn-fabric`, then wait for
  `version`;
- running → hand the request over.

The CLI on the host is the deployed executable (§0), so its hash and the
runtime's are the same; a PEX that is not the deployed one is refused, never
quietly used.

### 3. A run is done when the network is

The plan is split by what a unit needs:

- **network units** — OVN switches, routers, ports, routes, NAT, ACLs,
  netns, links, addresses, kernel routes, rules. They run as today, in plan
  order, and decide the run's outcome;
- **workloads** — every container: a kernel router's app (Tailscale,
  ZeroTier, dhcpcd, WireGuard in docker) and every `kernel.app.container`
  (the platform DNS). They are handed to the runtime and do not hold the
  run up.

The run's answer ends with the network units and then one block naming
the job that goes on:

```
background: 7 units started — fabric follow 1c9be5e2…
  image: ovn-fabric-home-vpn-tailscale-tailscale
  kernel router: home-vpn-tailscale
  image: ovn-fabric-mam-hh-platform-coredns:netdebug
  workload: cp-mam-hh-platform-platform.dns
  NIC: control-plane on br-1d9dc1c9
```

`fabric follow <req>` shows it, `fabric jobs` lists it with its state.
`fabric status` shows what the runtime holds: the deployed IR and whether
it is applied, the last apply and who started it (a client, the boot, a
repair), the background job with each unit's state, and every container it
keeps alive with its state and how often it was brought back; its exit
code is 1 when a container is down or an apply failed.

### 4. The background is one job: the same apply, with `background`

*Revised while building it, Meno's suggestion (2026-11-12): not a service
per image and per workload — one job, inside which everything is ordinary
and in order.*

Once the network units ran, a create or a run sends the runtime the same
`IrApplyReq` again with `background: true` and the values the network
captured as `inputs`. That request is a job (§7) and runs only the scopes
with an image — a workload with its NICs, a kernel router whose app is a
container (Tailscale, ZeroTier):

- **the images first, in parallel** — all at once by default;
  `ir-apply run --image-jobs N` bounds it (0 = all). An image already
  present is decided at once (its unit decides itself);
- **then each scope in plan order, once its image is there** — the same
  units as before, unchanged: docker run, netns link, veths, NIC addresses
  and routes, the app's masquerade and routes. A scope whose image failed
  is skipped (`its image failed`).

For a run the job takes a look of its own at the live state and decides
unit by unit as a run does; it does no release, no replacing and no
cleanup — the network part did those for the whole plan. The job does not
hold the one-apply-at-a-time lock. If the network part failed, no
background job is started.

**The runtime keeps the containers alive** (`fabric/supervise.py`). A
container of the applied IR that is not running — it died, was stopped or
removed by hand, docker restarted without it — is brought back by running
the deployed IR again (no cleanup): what is there is left alone, the
container is made anew with its netns link, veths and NIC addresses on
their numbers. It looks on every docker event (die, stop, destroy) and
every 30 s, and decides only while no apply and no background job runs —
those stop containers on purpose. One that keeps dying waits longer each
time (5 s, doubling, at most 5 min; after 10 min running it starts over).
Only while the IR applied here is the deployed one: after a delete —
even one keeping the name `deployed` — nothing is kept alive.

### 5. A new apply replaces what the previous one left running

The background job belongs to its work: action, IR hash, host. When a new
create or run starts while it is still going:

- **the same work** (the same IR again) → it is **kept**: no new job, the
  answer names the running one (`goes on (the same work, from before)`);
- **other work** (another IR, a delete) → it is **released** at the new
  apply's start: it stops before its next unit; the new apply waits for
  the units it is in (a pull, a container start) to end, then goes on.
  What it left half done is the new run's to replace or clean up, as any
  leftover is.

The log names both: `… the same work — it goes on`, `… released — waiting
for its current units`. A new apply while one is still in its network part
waits for it (the lock stays).

### 6. Boot: the runtime replays, a restart only takes over

A reboot and a restart of the runtime are different things:

- **after a reboot** the kernel's state is gone — netns, veths, kernel
  routes, rules, the containers; OVN's databases keep theirs. The deployed
  IR has to be applied again: the network, then the workloads.
- **after a restart** of the runtime (a `deploy`, a crash) the network and
  the containers are still there. Applying again would be wrong; the
  runtime only takes over supervising the workloads it finds running.

The runtime tells them apart by the kernel's **boot id**
(`/proc/sys/kernel/random/boot_id`), stored with every run that finished.
When it starts:

- a different boot id → it **replays** the `deployed` IR itself, as a run
  (network first; images and workloads in the background, §3–§4), without
  cleanup (nobody watches a boot);
- the same boot id → nothing is applied: the network and the containers
  are there; keeping them alive (§4) takes over — one that is down is
  brought back;
- no boot id recorded yet (a runtime from before this) → it records the
  boot id and replays nothing.

The separate oneshot `ovn-fabric-reconcile.service` (`fabric/plan.py`
`_emit_boot`) goes: the runtime is the one that knows. Images are usually
present already (docker keeps them), so the workloads start right after the
network.

**Addresses across a reboot: the allocator state file.** `run` keeps every
name on its live number (ADR 0011 §3) — but after a reboot the live view
lacks what only the kernel held: a workload NIC's allocated address would be
placed anew and could move. So every run writes the assignment it resolved
to an **allocator state file** (`/var/lib/ovn-fabric/allocator.json`:
collision domain → owner → numbers per family, with the run's IR hash and
boot id). It is the allocator's memory, written by the runtime only — not a
second topology. Allocation reads it as its own precedence level (ADR 0011
§3: pinned > live > state file > new), so a replay after a reboot puts every
name back on its number; OVN router ports are live anyway (the NB keeps
them). Decided with Meno (2026-11-12).

### 7. Following a request

A request belongs to the runtime, not to the connection that made it. The
CLI follows its log by default; Ctrl-C or a dropped connection stops the
*following*, not the request. `fabric follow <request>` attaches again,
`--detach` returns once the request was accepted.

## Consequences

- A slow or failing image no longer holds the network up — neither in a
  run nor at boot. A run's outcome says whether the network converged;
  workload failures are reported by `fabric status`, not by the run's exit
  code.
- Which runtime a host runs is a recorded hash; going back is one command.
- `run` cannot change the runtime by accident; `deploy` is explicit.
- Containers are owned by the runtime between runs (started, kept alive,
  replaced), not only during an apply.
- The plan gains a second kind of unit; the executor's sequential loop
  stays for the network part.

## Migration

1. **Runtime store and `fabric deploy`** (with `--ref`, the unit pointing at
   the stored executable); `fabric-install.sh` calls it. *Built 2026-11-12:*
   `fabric/store.py` (`ContentStore`, shared with the IR store),
   `fabric/runtime_store.py` (`deploy`, `runtime-install`, `runtime-list`),
   `version` reports the running PEX's hash; tried on VM 302 (deploy, same
   again, a second build, `--ref previous`).
2. **`run` ensures the runtime runs** (start if stopped, error if nothing
   deployed; a PEX that is not the deployed one is refused). *Built
   2026-11-12* (`fabric/__main__.py`: `_connect_runtime`,
   `_refuse_not_deployed`).
3. **Following detached from the request** (`follow`, `--detach`). *Built
   2026-11-12:* `runtime/jobs.py`, `Service.detached` (`IrApplyReq`),
   `FollowReq` / `JobListReq` in the runtime protocol; `fabric follow`,
   `fabric jobs`, `ir-apply --detach`; Ctrl-C ends only the following.
   Tried on VM 302: the client killed mid-run, the run finished and was
   followed to its end.
4. **Images in their own unit**, first in their scope; a container unit
   no longer builds its image. *Built 2026-11-12* (`deployer/ir_common.py`).
5. **The background job** (§3–§4): `fabric/services/apply.py`
   (`split_plan`, `_start_background`, `_run_background`), `IrApplyReq`
   `background`/`inputs`/`imageJobs`, `IrApplyRes.background`;
   `--image-jobs`. **Boot replay** in the runtime (`fabric/boot.py`: boot
   id, the allocator state file — `Addresses.resolve(remembered=…)`); the
   `boot` unit is gone from the plan, the runtime removes the old
   `ovn-fabric-reconcile.service` once where it finds it. *Built
   2026-11-12*, tried on VM 302: a run done in 6 s with 7 units in the
   background; a reboot replayed by the runtime, every address back on its
   number. *Built 2026-11-13:* `fabric status` (`StatusReq`), keeping
   containers alive (`fabric/supervise.py`, `applied` in the state file);
   on 302 a killed workload and a removed Tailscale container were back
   within 6 s on their addresses, a crash loop backed off, a delete with
   `--keep-deployed` was not undone.
6. **Replacing a previous run's background part** (keep / release, §5).
   *Built 2026-11-12* (unit-tested; on 302 the jobs end too fast to
   overlap).

Steps 1–3 are independent of 4–6 and come first.

## Open questions

None left. Decided with Meno (2026-11-12): 10 stored runtimes are kept;
images are built and pulled all in parallel, bounded by `--image-jobs`.

Related: ADR 0007 (one host service; this is its steps 4 and 5), ADR 0011
(addresses; a workload's NIC addresses come from the run's metadata).
