# ADR 0010: Natural keys — recognising a single resource under a new name

Status: proposed (2026-11-10)

## Context

`run` identifies every RawIR node by its id, which contains names. Renaming
the topology (ADR 0009) renames the generated objects, so for `run` the old
object is a leftover and the new one is missing — harmless while the two
share nothing. It breaks where the new object needs a resource the old one
still holds and that can exist only once.

Seen on VM 302 (2026-11-10): the cable `usa-v2` became `usa-cable-lan`, so
its bridge got a new name (`br-41dc351e`). The VLAN `ovnkern.1131` was
still a port of the old bridge; adding it to the new one failed, and the old
bridge — a leftover — is only removed after a run that did not fail.

There is no state file and no rename declaration: the live RawIR and the
plan's footprints are all `run` has.

## Decision

### 1. A natural key for single resources

A **single resource** is a thing the kernel refuses to have twice with the
same value. Only these have a **natural key**. It is not stored: RawIR keeps
`id` (with its name) as its only key, and `naturalKey(node, live)` computes
the natural key from the node and the live graph around it:

```
id:                 link:*global*|ovnkern.1131        (stored: who it is, its name)
naturalKey(node, live) → vlan:nic:bc:24:11:e8:68:a8|1131 (computed: what it is)
```

| Single resource | naturalKey |
|---|---|
| physical NIC | `nic:<permanent MAC>` |
| VLAN | `vlan:<parent's naturalKey>\|<VLAN ID>` |
| veth (its pinned leg) | `veth:<MAC>` |
| WireGuard interface | `wg:<hash of its private key>` |
| ZeroTier instance | `zt:<node address>` (its container's label) |
| OVN route | `ovnroute:<router>\|<policy>\|<prefix>` |
| kernel route | `route:<netns>\|<table>\|<prefix>\|<metric>` |

A route is single per prefix in its router or netns (OVN refuses a
duplicate prefix, the kernel the same table, prefix and metric — whatever
the next hop). Its key is scoped by that container's name: the conflict is
only ever inside the same router or netns, which kept its name.

A tunnel's identity may run once: moved to another netns (a rename), the
old instance goes before the new one comes up; in the same netns with a
changed config, its decider restarts it (as before). Only a hash of the
WireGuard key reaches RawIR, and only ZeroTier's public node address.

Everything else has none: bridges, routers, OVS ports, netns, other
containers (their name is their key), addresses (the kernel allows one
address on two devices).

### 2. A natural key never contains a name

Not a NIC name (udev renames it), not a generated or shortened one
(`br-<hash>`, `veth-krn-<hash>`). Only hardware identity (the permanent MAC,
`permaddr` when the MAC was changed), protocol values (VLAN ID) and the
parent's natural key. No macvlan is used.

A veth's netns-side leg has a MAC derived from its addresses
(`macFromAddrsOrName`), so after a rename the new veth has the old one's
natural key. Without addresses it falls back to the name, and a renamed veth
simply counts as new.

### 3. A function, not an attribute

`naturalKey(node, live)` is one kind-keyed function: the kind selects which
fields make the key, and the live graph gives a parent's key (a VLAN needs
its NIC's). It is called for both sides, so they agree by construction:

- **live** nodes from the collector (the MAC is read there);
- **planned** nodes from a footprint: a parent named in the plan (e.g.
  `ovnkern`) is looked up in the live RawIR and its key taken from there.
  What a footprint cannot carry comes from the step itself: the hash of
  the private key in a WireGuard step's config, a container's
  `ovn-fabric.natural-key` label.

No schema change and no relearn: the collector and the footprints stay as
they are. A debug view (or the graph page) shows a key by calling it.

The topology keeps naming NICs by name; when a named NIC does not exist,
the run fails — as it should.

### 4. `run` releases before it claims

Before applying: for each node a step will create, look up live nodes with
the same natural key. One with a different id or a different parent that is
ours and a leftover is removed first, with what hangs off it, in reverse
order — then the plan is applied, then the remaining leftovers are cleaned
up as today.

A step that only changes fields of existing nodes (the bridge mappings)
runs when only some of its changes are live, instead of being left alone
as partly there: after a rename only some mappings are still right, and
the step writes the whole value again.

### 5. What release and cleanup never remove

- The planned node itself: a held node that *is* the node the plan claims
  is where it belongs.
- A container, by the cleanup after a run (`NEVER_CLEANED`): a workload goes
  only through `delete`, or through a release — another container with
  the same natural key under another name.
- What a step declares by hand when its footprint is not learned yet (a
  VLAN moved into a netns, before primate knew it): still its own, never a
  leftover.

Release only follows what attaches a resource directly — the nodes
pointing at it, and an OVS interface's port. A bridge or a netns holds more
than one resource: it stays a leftover for the cleanup at the end.

## Consequences

- A rename of the topology no longer blocks on a VLAN or a NIC; the gap is
  limited to the parts that move.
- A veth renamed with the same addresses is replaced, not duplicated: no two
  legs with one MAC on a segment.
- The collector reads permanent MACs, VLAN IDs, a VLAN's parent (in the
  root netns when the VLAN was moved), a WireGuard key's hash and a
  container's `ovn-fabric.natural-key` label; the key table is the only
  hand-written part.
- Tested on VM 302 (2026-11-10): the tenant topology (ADR 0009) run over the
  old draft without a delete — renamed VLANs, the WAN VLAN moved into a
  netns, the tunnels' veths and the Mullvad interface, a router's old
  default route released first; then `done`, and a second run a no-op.
- A container started before the label existed is not recognised; such
  containers are removed by hand.

## Notes

The name borrows the database term: a natural key identifies a row by its
values, where `id` (a surrogate key) identifies it by a name we chose.

Two NICs with one MAC on a host are a configuration error, not a case to
handle: ovn-fabric works with what it gets.
