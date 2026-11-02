# ADR 0008: Primitive → RawIR templates, learned on a real system

Status: proposed (2026-11-01)

## Context

ADR 0006 reconciles the running system against the desired state: a
plan from the raw inventory (RawIR, collected by `raw_reconciler`) and
the desired objects. To compute that plan, the desired side must be in
RawIR terms too — **without applying anything**. We must know, for
example, that `add_addr("host", "192.168.128.32/24")` in netns `ns-x`
shows up in RawIR as one `addr` node with a given id and fields, before
the command ever runs. Without that prediction there is no diff, only
"apply and see".

Today every primitive declares its raw nodes by hand (`RawNodeTemplate`
in `as_primitive()`), next to its commands. Nothing checks those
declarations against what `raw_reconciler` actually observes: ids,
field names, value spellings (MAC case, IPv6 compression, a prefix vs.
its network) and the defaults the kernel, OVS or OVN fill in can all
differ, and a single mismatch makes every run see a change that is not
there. The two package systems we now deploy to (apt, dnf — see the
host's `packageManager`) can differ as well.

## Decision

### 1. A generated library: one template per primitive

`predict(primitive) -> [raw node]` is a library backed by **templates**,
one per primitive kind (and per call shape, § 4): the raw nodes the
primitive produces, with its parameters as placeholders —

```text
ovn_nbctl_ls(name)    → { id: "ovn.ls:{name}",              kind: "ovn.ls", data: { name: "{name}", … } }
add_addr(link, addr)  → { id: "addr:{netns}|{link}|{addr}",  kind: "addr",   data: { prefix: "{addr|ip}", … } }
```

The templates are **data, generated**, checked into the repository and
reviewed like code. The library can be recreated at any time by running
the generator again; nothing in it is written by hand.

`predict` needs no system access: it only fills the placeholders.

### 2. The templates are learned by applying on a real system

A generator ("learner") runs once — on a dev system or in CI, never on a
production host — and for every primitive and sample call:

1. collects RawIR (`raw_reconciler`), applies the primitive, collects
   again; the difference is what the primitive produced;
2. calls it with **marked values** (a switch `zz-ls-a1`, an address
   `10.251.17.33/27`, a MAC `02:5e:00:aa:bb:01`) and searches the new
   nodes for them: where a marker shows up (id, field, reference) the
   template gets the parameter's placeholder;
3. tries a fixed list of known **transforms** when a marker does not
   come back verbatim (lowercase, canonical IPv6, prefix → network, …)
   and records the one that matched (`{addr|ip}`); a value it cannot
   explain is an error for a human, not a guess;
4. runs every call **twice with different markers**: a field that
   changes without any parameter changing is volatile (a UUID, an
   ifindex) and is left out of comparisons; one that stays the same is a
   constant default and goes into the template as a fixed value;
5. deletes the primitive and checks that the difference is gone again
   (create and delete are each other's inverse in RawIR).

Everything is applied in isolation: a throwaway netns, a scratch OVS
bridge, OVN objects with a test prefix; the learner removes all of it.

### 3. The only hand-written input: sample calls per primitive

Optional parameters and lists change the number of nodes (a router port
with or without a gateway chassis; zero, one or two addresses). Each
primitive gets a small list of **sample calls** covering its shapes; the
learner learns one template per shape and merges them. This list is the
only hand-written input of the library.

### 4. Variants per package manager

The learner runs on an apt and on a dnf system (ADR 0007's host types).
Where the templates differ, they are recorded per `packageManager`;
where they agree, once.

### 5. CI keeps the library honest

CI runs the learner on a disposable VM and compares its output with the
checked-in templates. A new distro or OVN version, or a changed
primitive, shows up as a diff of the templates — not as a wrong
prediction at deploy time.

### 6. What has no template

Processes started into a running system (`docker exec` apps, dhclient,
wg-quick's runtime state) cannot be observed as stable RawIR. They get no
template: when anything they depend on changes, they are restarted.

### 7. Scope of this ADR

Only the Primitive → RawIR conversion. How the plan uses it — the `run`
action (create and delete stay as they are; `run` converges a host to its
IR, and is what the host service does at start, replacing ADR 0007 § 8's
"delete, then create"), the per-node flags (applied / needs apply /
to remove / removed), the exclude list (loopback, the host's management
NIC; our Docker containers by name, not all of Docker) and updating in
place — belongs to ADR 0006 and is amended there once this library
exists.

## Consequences

### Positive

- The diff has a desired side without applying anything.
- Predictions are what the system really reports, per distro, including
  its spellings and defaults — not what we believe it reports.
- The hand-written `RawNodeTemplate` declarations in the primitives can
  go once every primitive has a template.
- A library that is regenerated, not maintained: changes in the
  underlying tools surface as reviewable diffs.

### Negative / costs

- The learner needs a disposable system with root (a VM): CI needs one.
- Sample calls must cover every shape of a primitive; a shape not
  sampled has no template (the library refuses to predict it).
- Transforms are a closed list: a new spelling needs a new transform.

## Alternatives considered

- **Declarations by hand only** (today). Unverified; drifts from what
  the system reports, silently.
- **Learn at deploy time by applying.** Needs the change applied to know
  its result — no diff before acting.
- **Derive RawIR from the command lines** (parse `ip addr add …`).
  Brittle, and blind to what the system fills in or respells.

## Implementation slices

1. The learner's core (collect, apply, diff, marker search, transforms,
   volatile detection, delete check) and four primitives covering the
   main shapes: netns, address, OVS bridge + port, OVN logical switch —
   run on the Fedora test VM (302); review the templates.
2. Every primitive, with its sample calls; apt and dnf runs.
3. `predict()` and a conformance test: on a test system, predicted ==
   observed for every sample.
4. CI: the learner on a disposable VM, diffed against the checked-in
   templates.
