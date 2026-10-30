# ADR 0007: One host service on an event runtime, instead of a generated flat-Python deployer

Status: accepted (2026-10-30) — architecture only, no code changed yet.

## Context

Today a host is deployed like this:

```text
topology (TS) → generator → IR → ir_to_python → flat generated Python (PEX) → execute
```

The generated program has two independent lifecycles on the same host:

- the **CLI run** (`--action create|delete`) builds its own job tree: package
  install, the synchronous scopes, the cluster pass, then exits;
- the **service** (`ovn-fabric.service`), started by the CLI, ran the job tree
  (`--job … up`) and, most recently, the one-shot "lazy" pass (image
  pulls/builds, then containers and their injection).

Both own parts of the same state and coordinate only through
`systemctl start/stop`, files and process exit. Every recent problem came
from that split:

- two package jobs that can collide on the dpkg lock;
- images handled twice (`image:*` jobs and lazy `DockerImage`/`DockerPull`);
- stopping the service *is* the delete (ExecStop rewinds everything), so a
  reboot is a teardown, and a power loss leaves state that a strict create
  then fails on;
- "lazy" operations depend on bridges and router netns only through queue
  position, not through a declared dependency;
- the CLI returns before the fabric is up, so its exit says nothing about the
  deploy;
- no restart signal after a package upgrade (ovs/ovn/docker), no way for the
  CLI or another tool to observe progress.

The event mechanism of the current runtime (`runtime/service.py`) is also too
weak to carry a long-lived system:

- events are untyped dicts without an envelope (no id, source, correlation,
  auth), broadcast to every job, in one process only;
- `queue=True` is a state store disguised as an event log: append-only, it
  cannot take a fact back (`needs-restart`, `image deleted`) and grows forever;
- the design is edge-triggered ("A happened, so run B"), which breaks on a
  missed or reordered event, a restart or a reboot;
- a failing hook is logged and swallowed; there is no supervision policy.

The job tree itself (task/process ownership, start/stop, stop kills what a
job started) is a good kernel and is kept.

## Decision

### 1. Two tiers: a generic runtime, and ovn-fabric on top of it

```text
runtime  (generic, tiny — knows nothing about hosts, packages or networks)
  ├─ job tree     supervision, task/process ownership, start/stop, restart policy
  ├─ bus          state · events · requests, all as envelopes
  ├─ registry     admits services onto the bus, one supervised job each
  └─ transports   local (in-process) · unix socket — same semantics

ovn-fabric
  ├─ generator (TS)   topology → IR
  ├─ IR               arktype schema → JSON Schema → Python codegen
  └─ ovn-runtime      the host program, built on the runtime
       ├─ RAWIR       observes the host → raw snapshot (observed)
       ├─ IR store    the desired IR, as last applied
       ├─ merger      IR × RAWIR → plan (ADR 0006)
       ├─ executor    runs the plan as lifecycle primitives
       ├─ sysdeps     packages: priority apt queue, restart notices
       ├─ images      watch / pull / build
       ├─ workloads   containers + their network, kept alive
       └─ fabric      the request surface: fabric.create / fabric.delete / status
```

The dependency is one-way: the runtime never imports ovn-fabric code
(`deployer`, `ladops`, the IR types). This is enforced by a test while both
live in this repository; the runtime can later move to its own repository as
a mechanical step.

The runtime needs its own name (`runtime` is already the Python package
directory and `src/runtime.ts`); to be chosen.

### 2. One system-wide service

A host runs exactly one long-lived process, `ovn-runtime`, started by
systemd. It starts everything it needs from there: its services, their
supervised jobs and the transports. systemd does what systemd does well
(start at boot, journald, later the watchdog); everything else —
dependencies, ordering, restarts, progress — is the runtime's.

Stopping the service is **not** a delete. Delete is an explicit request.

### 3. The bus: three message kinds, one envelope

Every message is an envelope:

```text
Envelope { id, kind, subject, source, time, revision,
           correlation, causation, auth, data }
```

| | **state** — "what is" | **event** — "what happened" | **request → reply/progress** |
|---|---|---|---|
| examples | `sysdep/ovs = ready`, `image/x = building`, `service/fabric = ready` | logs, progress, "apt-get started" | `fabric.create(ir)`, `sysdep.install(pkg, prio)` |
| storage | retained: subject → latest value + revision | not stored | correlated by `correlation` |
| writer | **exactly one owner per subject** (enforced) | anyone | routed to the owner of the request type |
| subscriber gets | a wake-up, then reads the current state | each message, in order | progress events and one final reply |
| slow subscriber | coalesces — latest value wins, no backlog | bounded queue, drop-oldest, drop count reported | — |

Consequences of this model:

- **Level-triggered.** Components reconcile against the current state of the
  subjects they read. A missed, duplicated or reordered notification, a late
  start or a restart all converge the same way. No replay is ever needed.
- **Single writer.** A component never writes another component's subject;
  it sends a request. This is the bus form of "one owner per object".
- **Correlation flows automatically.** Inside a request handler the runtime
  holds the request's correlation in a `contextvar`; every log, progress and
  event emitted there carries it. A client following its request sees exactly
  its own logs.
- `auth` exists from day one and stays empty for now (see 6).

### 4. The registry and the service declaration

A service declares what it is; the registry does the wiring:

```python
svc = Service(
    name="fabric",
    provides=["fabric/state"],                       # owned subjects
    needs=["sysdep/ovs", "sysdep/ovn", "sysdep/ip"], # watched subjects
    restart=Restart(backoff=1, max_backoff=60),
)

@svc.main
async def main(s: Bound) -> None:
    s.publish(FabricState(phase="waiting", missing=s.missing()))
    await s.wait_ready()                 # all `needs` ready
    s.log.info("dependencies ready")
    s.publish(FabricState(phase="idle"))
    s.accept("fabric.create")            # requests are answered from now on
    async for change in s.changes():     # a dependency may fall back
        ...

@svc.handles("fabric.create", busy="queue")
async def create(s: Bound, req: Request[CreateFabric]) -> CreateResult:
    ...
```

On `register`, the registry:

1. checks the name is unique, that no other service owns its `provides`, and
   that its protocol version is compatible;
2. creates one supervised job for the service (its `main` and every accepted
   request run as tasks in that job's scope);
3. hands the service a **bound view** of the bus: it may write only its
   `provides`, wait on its `needs`, answer its `handles`, and send requests
   to anyone;
4. publishes `service/<name>` as state (`registered → starting → ready |
   failed`, with provides/needs/handles as data) — discovery runs over the
   bus itself, from inside and outside.

The bus primitives (`publish`, `watch`, `get`, `event`, `listen`, `request`,
`reply`) stay internal; services and clients see use-case named calls
(`wait_ready`, `changes`, `publish`, `log`, `progress`, `ask`, `follow`,
`accept`/`refuse`).

### 5. Supervision on `asyncio.TaskGroup`

The job tree is rebuilt on the stdlib's structured concurrency
(`asyncio.TaskGroup`, Python ≥ 3.11): no task outlives its scope, and
cancelling a scope cancels and awaits everything in it. One-for-one
supervision wraps each child in a restart loop with backoff inside the
group; `CancelledError` is never swallowed; a child that exceeds its restart
budget escalates to its parent. Stopping a service cancels its scope — its
running handlers are cancelled with it and their `finally` blocks run (locks
released, state reset).

### 6. Transports: same structure, different interfaces

Inside and outside use the same bus model and the same calls; only the
transport differs.

| | local | unix (later maybe tcp+tls) |
|---|---|---|
| state | watchers are woken, read the store | on subscribe: snapshot of the matching subjects, then latest value per subject only |
| events | bounded queue per subscriber | the same per peer, drop count sent along |
| requests | direct call to the handler | routed frames; timeout and `cancel` are frames too |
| reconnect | — | resubscribe → fresh snapshot (level-triggered, nothing to replay) |
| identity | trusted (same process) | now: the uid from `SO_PEERCRED`, written into `auth` by the transport; later: signed envelopes |

Wire format: **newline-delimited JSON envelopes over a Unix socket**
(`/run/ovn-fabric.sock`). The bus is symmetric — both sides push state,
events and requests — which a call-oriented protocol (varlink) does not fit;
NDJSON needs no dependency and can be debugged with `socat`. Control frames:
`hello` (peer id, protocol version), `subscribe`, `unsubscribe`, `register`,
`bye`. A varlink adapter can be added later as one more transport.

A remote peer may register services too; its ownership is a **lease** on the
connection: when it drops, its subjects become `stale` and its
`service/<name>` becomes `gone`.

### 7. The CLI is a client

```python
async with connect("unix:///run/ovn-fabric.sock") as bus:
    req = await bus.ask("fabric.create", CreateFabric(ir=ir))   # returns at once
    async for msg in bus.follow(req):                           # logs, progress, reply
        show(msg)
```

Its only synchronous duty is bootstrap: install (or upgrade) the program and
its unit, start the service, wait for `service/fabric`. Create from the CLI,
at boot, or from a future tool all go through the same handler, its lock and
its logs.

### 8. Boot recovery: delete, then create — for now

At start `ovn-runtime` loads the IR store and sends itself `fabric.delete`
followed by `fabric.create` for the stored IR — the same behaviour as today,
now inside one process. Later the merger diffs the stored IR against RAWIR
instead (ADR 0006); no other part changes.

### 9. The generated flat-Python deployer is retired

`ir_to_python` (generate a program per topology, ship it, execute it) is
replaced by a fixed host program that receives the IR as data
(`fabric.create(ir)`). What carries over, moved into ovn-runtime:

- the lifecycle controller and the one-declaration-per-scope model (create
  forward, delete rewound), now driven by the executor from the plan;
- the typed primitives (fs, systemd, docker, netns/ip, OVS, OVN);
- the dependency logic (package resolution, apt retries) as the `sysdeps`
  component;
- the raw reconciler as RAWIR.

What becomes obsolete: the generated runtime template, the `JOB_BODIES` /
`HOSTS` / `CREATE_JOBS` tables, `--job` / `--action` passes, the unit's
ExecStart/ExecStop rewind, `DepWaiter` and the `queue=True` events, the
"lazy" phase (a workload simply `needs` its image and its network). Removing
that code is a separate, explicit step once the replacement works.

### 10. Types: arktype → JSON Schema → Python codegen, twice

The host side stays Python (stdlib only: available everywhere, small
footprint, runs on a Raspberry Pi or a small RISC-V board). Types keep the
existing pipeline:

- the **runtime** owns its schema: the envelope, the control frames and the
  built-in states (`service/*`);
- **ovn-fabric** owns the IR and its own states and requests (`SysdepState`,
  `ImageState`, `WorkloadState`, `CreateFabric`, …).

Messages from a transport are validated at the boundary (`fastjsonschema`,
pure Python, compiled from the same JSON Schema) and become the codegen'd
dataclasses inside.

## Consequences

### Positive

- One owner of host state: the apt queue, image watching and restart notices
  are in-process concerns, not cross-process coordination.
- Reboot, CLI create and (later) drift repair share one code path.
- Stopping the service no longer deletes the fabric.
- Synchronous and async work are the same mechanism with different `needs`;
  "lazy" disappears as a concept.
- The CLI sees real progress and a real result; other tools can join the
  same bus.
- The runtime is small, generic and testable without OVN; ovn-fabric is
  testable against a fake runtime.
- Auth can be added as a transport/bus policy without a schema change.

### Negative / costs

- A long-lived daemon must be robust: supervision, restart budgets and
  idempotent reconcile become mandatory, not optional.
- The IR store must persist the desired IR.
- CLI and daemon can differ in version; the protocol version is negotiated in
  `hello`, and each payload carries its schema version.
- Delete needs the daemon; a local purge (moved into its own module) is the
  fallback when it is not running.
- A substantial rework: the generator back end is replaced, and the deployer
  modules move into ovn-runtime.

## Alternatives considered

- **Keep the CLI lifecycle, tidy the edges** (lock files, file-based events,
  the service re-reading what the CLI did). Smallest change, but keeps two
  owners; every new need is cross-process coordination again.
- **TypeScript (Deno) for the host program.** One type system end to end and
  `deno compile` single binaries, but a large runtime and binary on small
  boards; Python is already everywhere.
- **Rust / Go.** Rust is the strongest and the most expensive; Go's type
  system fits a message union model worst. Performance is not a constraint
  here.
- **Libraries.** `anyio`/`trio` (structured concurrency), `mode` (services
  with supervisors), `pykka`/`thespian` (actors) — useful as design
  references, but each adds dependencies; the stdlib `TaskGroup` plus a small
  bus covers the need.
- **varlink / D-Bus as the bus.** Call-oriented or heavy; kept as possible
  extra transports, not as the model.

## Open questions

1. The runtime's name.
2. Minimum Python version: `TaskGroup` needs 3.11 (Debian 12); Ubuntu 22.04
   ships 3.10. Either require 3.11 or carry a small `TaskGroup` equivalent.
3. Subject and request naming convention (`<service>/<object>` for state,
   `<service>.<verb>` for requests).
4. Ownership of the cluster (OVN NB) objects: the central host's
   ovn-runtime only?
5. Where the IR store persists, and whether any other state survives a
   restart (default: none — owners republish on start).
6. Restart notices: which package upgrades trigger which re-reconcile.

## Implementation slices

1. **Runtime core:** envelope schema (arktype → Python), bus (state, events,
   requests, single writer, coalescing), registry, `TaskGroup` supervision,
   local transport. Tests without any OVN code; an import-boundary test.
2. **Unix transport and client:** NDJSON frames, `hello`/subscribe/
   register, `SO_PEERCRED` into `auth`, validation at the boundary; a minimal
   CLI client (`ask`, `follow`, `status`).
3. **ovn-runtime skeleton:** the `fabric` service with the IR store,
   `fabric.create`/`fabric.delete` (busy policy, correlated logs/progress);
   the systemd unit that runs it; the CLI bootstrap.
4. **sysdeps and images as services:** the priority apt queue, `sysdep/*`
   and `image/*` states; `fabric` and workloads `need` them.
5. **Executor:** the lifecycle controller and primitives moved in, driven by
   the stored IR (delete → create, per 8); workloads as their own services
   (`needs` image and network, kept alive).
6. **Merger and RAWIR:** ADR 0006's planner replaces delete → create.
7. **Retire the generated deployer** (separate decision, see 9).
