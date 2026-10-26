# ADR 0006: Plan-driven reconciliation from raw inventory vs desired IR

Status: proposed

## Context

The deployer currently emits a self-installing script. After removing
`--may-exist`, two problems became visible:

1. OVS/OVN/netns state survives reboots, so a blind create pass fails on
   objects that already exist.
2. The running system contains both objects from the topology and objects
   added manually or left behind by older revisions. The deployer has no
   way to distinguish them.

The raw reconciler now produces a flat, referenced graph of live facts
(`RawSnapshot`), and the generator produces the desired-state IR from the
topology. Both are keyed graphs, but they are not the same model:

- IR carries intent and generator-resolved metadata.
- Raw carries observed runtime facts and no ownership by itself.

What is needed is a planner that compares the two and produces **create**,
**delete**, **keep**, **drift**, and **ignore** operations. The planner is
independent of both the raw graph's internal shape and the IR's full
schema; both are projected into a common keyed surface first.

## Decision

### 1. A plan, not a create script

Every apply run computes:

```text
PlanOp =
  | { op: "create", key, desired }
  | { op: "delete", key, actual }
  | { op: "keep",   key, desired, actual }
  | { op: "drift",  key, desired, actual }
  | { op: "ignore", key, actual }
```

Only `create` and `delete` are executed. `keep`, `drift`, and `ignore`
are report-only.

The plan is deterministic for the same desired IR, raw snapshot, and scope.

### 2. Three-way comparison

For each managed object kind:

```text
desired = keys from the topology IR
actual  = keys from the raw snapshot
owned   = actual keys inside the managed scope
```

Then:

```text
desired - actual                 -> create
(actual ∩ owned) - desired       -> delete
desired ∩ actual                 -> keep (see § 3)
actual - owned                   -> ignore
```

### 3. Never update an existing object

An existing object is never mutated. If its key exists:

- if the projected data matches → `keep`;
- if the projected data differs → `drift`, no write.

Repairing drift means deleting the object and creating it again in a
separate, explicit operation. That is not an automatic update path.

This keeps create-only semantics and makes repeated runs idempotent:
second run = no creates, no deletes, no writes.

### 4. Managed scope is explicit

Deletion is only allowed inside a declared scope. Outside it, objects are
ignored even if they look related.

Scope may be defined by any combination of:

- host identity;
- object name prefixes (`br-*`, `ns-*`, `ovn-fabric-*`);
- netns names declared by the topology;
- OVN LS/LR/LRP names declared by the topology;
- ownership tags (`external_ids:ovn-fabric:managed=true`);
- a per-host managed-object manifest.

Everything inside scope and not desired is deleted. Everything outside
scope is untouched.

### 5. Ownership tags are the preferred long-term marker

Scope by name is sufficient to start, but naming is not ownership.
The applier should write ownership tags on create:

```text
OVS Bridge/Port/Interface.external_ids:
  ovn-fabric:managed = "true"
  ovn-fabric:host    = "<host>"
  ovn-fabric:owner   = "<IR node id>"

OVN LS/LR/LRP/LSP/Route/NAT/ACL.external_ids:
  ovn-fabric:managed = "true"
  ovn-fabric:host    = "<host>"
  ovn-fabric:owner   = "<IR node id>"
```

Kernel-side objects have no generic tag store; their ownership must be
carried by the OVS-side veth/port or by an iptables comment.

Pre-existing untagged objects are handled by scope, not by assuming they
are managed.

### 6. Deletes are dependency-ordered and guarded

Deletes run only after the plan has verified:

- the object is inside managed scope;
- the object is not referenced by any desired object;
- referrers are deleted before their dependencies.

Adds run referenced-before-referrer. Deletes run the reverse.

A desired object whose key collides with an actual object is never counted
as both `keep` and `create`.

### 7. The plan is independent of the raw/IR internal schemas

The planner consumes projections, not the full graphs:

```text
desired projection: (kind, key, comparable data)
actual  projection: (kind, key, comparable data)
```

This keeps the planner stable while raw or IR evolve. A new kind is added
by defining:

1. its stable key;
2. its comparable data projection;
3. its create/delete operation;
4. its scope predicate.

### 8. Drift is reported, not repaired

`drift` covers:

- same key, different comparable data;
- desired object exists outside managed scope;
- owned object has no desired counterpart but cannot be safely deleted
  because a desired object references it;
- raw object cannot be classified into an IR kind.

Drift never triggers an implicit update or force-delete.

### 9. Mutations are primitives, the snapshot is a journal

`run_cmd` is not the right boundary for applied-state reflection. A
command has no semantic identity, no inverse, and no place to attach the
runtime UUID/PID it created. Every first-class mutation becomes a
`Primitive`:

```text
Primitive
  key          semantic key, e.g. "ovn.lrp:router-home|lrp-home"
  nodes        one or more RawNodeTemplate objects (semantic ids/refs)
  create       commands to create the object
  delete       commands to remove it
  probes       post-create/live probes that add runtime ids
```

A primitive may create more than one raw node (a veth pair, an LRP plus
its router-port LSP, a Docker container plus its netns). All graph
references inside those templates are semantic ids known before apply:

```text
ovn.lrp:<router>|<port>
ovn.nat:<router>|<type>|<externalIp>|<logicalIp>
ovn.acl:<switch>|<direction>|<priority>|<match>|<action>
```

UUIDs, container ids, PIDs and ifindexes are **never part of an id or a
reference**. They are optional `runtime` annotations attached after a
successful create:

```json
"runtime": {
  "provider": "ovn",
  "table": "Logical_Router_Port",
  "uuid": "0e47adc4-..."
}
```

If an object already exists (create-only `keep`), the primitive runs its
probe against live state and journals the node without issuing a create
command.

### 10. One in-memory graph, one dump

The apply run builds an in-memory `RawJournal`:

```text
nodes: dict[semantic_id, raw node]
```

Primitives upsert their rendered nodes; deletes remove them. Nothing is
written per primitive or per command.

At the end of `--action create`, `--action delete`, or `--action run`:

1. decorate nodes with `host` and `time`;
2. validate every `*Ref`/`*Refs` against the journal's id set;
3. serialize a `RawSnapshot` with deterministic ordering;
4. write it atomically:

```text
/var/lib/ovn-fabric/raw-snapshot.json.tmp -> rename -> raw-snapshot.json
```

If validation fails, do not write a seemingly valid snapshot. Merge the
missing targets from a live collection, or write a separately named
partial result. Never silently drop dangling references.

The dumped artifact is a **raw snapshot**, not the desired IR:

```text
IR            desired topology input
RawSnapshot   applied/live fact graph output
RawJournal    in-memory builder for one RawSnapshot
```

The journal is seeded with kept/live nodes so the output is a complete
managed graph, not only the nodes created in this run.

## Consequences

### Positive

- Create/delete are explicit; no `--may-exist` needed.
- Reboot and repeated apply are safe: existing objects are `keep`.
- Manual additions inside managed scope are pruned; outside scope are
  ignored.
- Existing configuration is not silently changed.
- The same planner can be used by `--action run`, `--action create`, and
  a future `--plan` CLI.
- Raw and IR can evolve independently behind their projections.

### Negative / costs

- A changed desired object stays wrong until an operator deletes it; this
  is intentional but needs clear drift output.
- Safe deletion requires a complete scope definition and reference checks.
- Shared OVN objects need host/owner tags; otherwise a per-host plan can
  delete another host's objects.
- The plan is only as complete as the raw inventory; missing live facts
  can cause incorrect `create` or `ignore`.

## Alternatives considered

- **`--may-exist` / idempotent writers.** Rejected: hides duplicate
  objects, stale metadata, and manual drift.
- **Always update existing objects.** Rejected: contradicts the
  create-only requirement and makes every apply rewrite live state.
- **Delete everything not in desired IR.** Rejected: destroys unmanaged
  objects outside this deployment's scope.
- **Full raw → IR round-trip before planning.** Rejected for now: the raw
  graph cannot reconstruct generator-only metadata (`domain`, `metric`,
  `securityGroups`, ownership) without tags. Planning directly on keyed
  projections avoids blocking on that reconstruction.

## Implementation slices

### Slice 0: contract and fixtures

- Freeze canonical semantic keys per raw kind.
- Define the managed-scope predicate per kind.
- Capture a raw snapshot and a desired IR from the same deployed host.
- Add golden fixtures for OVS bridge/port, OVN LS/LR/LRP, route, NAT/ACL.

### Slice 1: OVS primitives, no UUIDs

- Add `Command`, `RawNodeTemplate`, `Primitive`, `RawJournal`.
- Implement create/keep/delete for OVS bridge, port, interface.
- Journal nodes from primitives and kept live nodes.
- Dump a validated raw snapshot at the end of `--action create`/`delete`.
- Acceptance: second apply produces no creates, no deletes.

### Slice 2: OVN objects with name-based probes

- Add LS/LR/LRP/LSP primitives.
- Probe runtime UUIDs by natural name after create.
- Keep semantic refs (`routerRef`, `switchRef`) unchanged.
- Acceptance: snapshot contains UUIDs; keys do not.
- Add create-only planning for these kinds.

### Slice 3: routes, NAT, ACL

- Canonical keys by semantic tuple.
- Probes by `(router, prefix, nexthop, policy)` and
  `(router, type, external_ip, logical_ip)` and
  `(switch, direction, priority, match, action)`.
- Journal `routeRefs`, `natRefs`, `aclRefs`.

### Slice 4: kernel/netns and iptables

- Add netns, link, addr, route, rule primitives.
- Kernel has no metadata store: ownership comes from scope and from the
  OVS-side veth/port or iptables comment.
- Probe ifindex/MAC only as runtime metadata.

### Slice 5: Docker

- Primitive captures `docker run`'s container id and probes the pid.
- Container netns is a normal raw netns node; the container node points
  at it with `netnsRef`.

### Slice 6: planner and prune

- Project desired IR and actual raw snapshot to `(kind, key, data)`.
- Implement `create`, `delete`, `keep`, `drift`, `ignore`.
- Wire plan output into `--action create/delete/run`.
- Delete only inside scope; protect desired references.
- Add `--plan` dry-run.

### Slice 7: hardening

- Atomic journal writes and partial-result policy.
- Validate no dangling refs before dump.
- Seed journal from live state for kept objects.
- Add a full round-trip test: deploy -> snapshot -> plan -> empty create
  set -> unchanged snapshot.

## Implementation status (2026-10-26)

Done:

- `deployer/lifecycle.py`: `Command`, `RawNodeTemplate`, `Primitive`,
  `Runnable`, `RawGraphBuilder`, `LifecycleController`.
- Primitive command stream: `Primitive.create/delete` execute through
  `LifecycleController.run_cmd`; probes attach runtime ids.
- Raw journal: one in-memory graph, validated refs, one atomic dump.
- OVS bridge primitives are generated as `PRIMITIVES[host]` and wired into
  the generated `create_action` / `delete_action`; bridge commands were
  removed from the static binding bodies.
- `deployer/primitives_ovn.py`: typed `OVNNbCtlLs`, `OVNNbCtlLsp` handles.
- `deployer/primitives_host.py`: `HostPrimitive` factory with
  `host.netns(None|name)` and `netns.add_if(...)`.
- Tests cover primitive command streams, raw nodes, and generated source.
- `--explode DIR` on the generated PEX and `ovn-fabric explode`.

Not done yet:

- Only OVS bridges are journaled at apply time. Bridge ports, VLANs,
  veths, addresses, routes, OVS mappings, iptables, services and containers
  still execute as raw commands.
- `--action run` is not lifecycle-controller driven.
- Cluster OVN commands (LR/LSP/LRP/routes/NAT/ACL), kernel router bodies,
  and Docker/service bodies have not been converted to primitives.
- The journal is not seeded from live state, so kept objects are absent
  from the snapshot until their primitives run.
- The raw snapshot is therefore intentionally partial.

Next slices:

1. Convert all cluster OVN emitters to typed primitives and one LC per
   cluster action.
2. Convert host bindings (ports, VLANs, veths, mappings) and seed the
   journal with kept nodes.
3. Convert kernel router/netns/address/route/iptables primitives.
4. Convert services, Docker, and image lifecycle.
5. Wire `--action run` and add `--plan`.
