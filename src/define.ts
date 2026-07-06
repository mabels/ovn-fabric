// src/define.ts — defineNetwork(): a Vite-defineConfig / Jest-describe
// style builder. Lets config/topology.ts read as a declaration rather
// than a set of raw object literals, and validates as it goes (e.g. a
// segment can only reference an uplink that was already declared in
// the same defineNetwork call).

import type { UplinkBuilder } from "./factories.ts";
import {
  FixedUplink,
  type Host,
  localHost,
  type Segment,
  sshHost,
  type Uplink,
  type UplinkSelector,
} from "./types.ts";

export interface NetworkDefinition {
  readonly name: string;
  readonly allUplinks: readonly Uplink[];
  readonly allSegments: readonly Segment[];
}

/**
 * net.uplink(name, uplinkVlan({...})) / net.uplink(name, uplinkPhysical({...})) —
 * the second argument is an UplinkBuilder: a function of `slot` that a
 * factory in factories.ts returned. NetworkBuilder assigns the next
 * free slot (0-4095, see addressing.ts transferNet()) and calls it —
 * config/topology.ts never sees or chooses a slot itself, so two
 * uplinks can never collide on the same transfer-link block.
 */

type SegmentSpec = Omit<Segment, "name">;

/**
 * The builder context passed into defineNetwork's callback. Each method
 * both registers the declared thing and returns a handle to it, so later
 * calls in the same callback can reference earlier ones directly
 * (`net.segment("home", { uplink: avm, ... })`), the same way Jest's
 * `describe`/`it` or Vite's defineConfig read top-to-bottom.
 */
export class NetworkBuilder {
  private readonly uplinksByName = new Map<string, Uplink>();
  private readonly usedUplinkIds = new Set<number>();
  private nextSlot = 0;
  private readonly segmentsByName = new Map<string, Segment>();
  private readonly usedSegmentIds = new Set<number>();
  private readonly hostsByName = new Map<string, Host>();

  /** Declare a host reachable via SSH. Returns a handle for reuse. */
  sshHost(name: string, address: string, user: string): Host {
    if (this.hostsByName.has(name)) {
      throw new Error(`host "${name}" declared more than once`);
    }
    const host = sshHost(name, address, user);
    this.hostsByName.set(name, host);
    return host;
  }

  /** Declare the generator's own host — no SSH needed. */
  localHost(name: string): Host {
    if (this.hostsByName.has(name)) {
      throw new Error(`host "${name}" declared more than once`);
    }
    const host = localHost(name);
    this.hostsByName.set(name, host);
    return host;
  }

  uplink(name: string, builder: UplinkBuilder): Uplink {
    if (this.uplinksByName.has(name)) {
      throw new Error(`uplink "${name}" declared more than once`);
    }

    // Passed as an allocator, not a single slot: most uplinks call this
    // once (their own front-door transfer link), but one with a
    // `backdoor` (see Backdoor, types.ts) calls it again for the
    // backdoor's own, separate slot — same sequential pool either way,
    // so nothing declared via this builder can ever collide on a slot.
    const allocSlot = (): number => {
      if (this.nextSlot > 4095) {
        throw new Error(
          "no transfer-link slots remaining (4096 max, see addressing.ts " +
            "transferNet()) — this network has more uplinks (and/or " +
            "backdoors) than the 10.99.0.0/16 transfer-link space can " +
            "address",
        );
      }
      return this.nextSlot++;
    };

    const spec = builder(allocSlot, name);

    const id = spec.addresses[0]?.id();
    if (id !== undefined && this.usedUplinkIds.has(id)) {
      const existing = [...this.uplinksByName.entries()]
        .find(([, u]) => u.addresses[0]?.id() === id)?.[0];
      throw new Error(
        `uplink "${name}" reuses id ${id}, ` +
          `already used by uplink "${existing}"`,
      );
    }

    // Same "must already be declared via this builder" fail-fast as
    // segment()'s uplink check below — a backdoor's `via` has to be a
    // real, already-registered uplink, not an arbitrary object that
    // happens to match the Uplink shape (and, since it must be
    // declared BEFORE this one, its own emitUplinkTransfer/netns setup
    // is guaranteed to already exist by the time this uplink's backdoor
    // is emitted — see generate-ovn.ts, scriptForHost).
    if (
      spec.backdoor !== undefined &&
      !this.uplinksByName.has(spec.backdoor.via.name)
    ) {
      throw new Error(
        `uplink "${name}" has a backdoor via "${spec.backdoor.via.name}", ` +
          `which was not declared via net.uplink() before this uplink`,
      );
    }

    const uplink: Uplink = { name, ...spec };

    this.uplinksByName.set(name, uplink);
    if (id !== undefined) this.usedUplinkIds.add(id);
    return uplink;
  }

  segment(name: string, spec: SegmentSpec): Segment {
    if (this.segmentsByName.has(name)) {
      throw new Error(`segment "${name}" declared more than once`);
    }
    const id = spec.addresses[0]?.id();
    if (id !== undefined && this.usedSegmentIds.has(id)) {
      const existing = [...this.segmentsByName.entries()]
        .find(([, s]) => s.addresses[0]?.id() === id)?.[0];
      throw new Error(
        `segment "${name}" reuses id ${id}, ` +
          `already used by segment "${existing}"`,
      );
    }

    // undefined means "no uplink yet, deliberately isolated" (see
    // Segment.uplink, types.ts) — not every segment resolves to
    // something, so this must be checked before the "resolve" in
    // probe below, which throws on undefined.
    const selector: UplinkSelector | undefined = spec.uplink === undefined
      ? undefined
      : ("resolve" in spec.uplink ? spec.uplink : new FixedUplink(spec.uplink));

    // fail fast: if an uplink WAS given, it must have been declared via
    // this same builder, not an arbitrary object that happens to match
    // the Uplink shape.
    if (selector !== undefined) {
      const resolved = selector.resolve();
      if (!this.uplinksByName.has(resolved.name)) {
        throw new Error(
          `segment "${name}" references an uplink ("${resolved.name}") ` +
            `that was not declared via net.uplink() in this defineNetwork call`,
        );
      }
    }

    const segment: Segment = { name, ...spec, uplink: selector };

    this.segmentsByName.set(name, segment);
    if (id !== undefined) this.usedSegmentIds.add(id);
    return segment;
  }

  /** @internal used by defineNetwork to extract the final declarations */
  build(name: string): NetworkDefinition {
    return {
      name,
      allUplinks: [...this.uplinksByName.values()],
      allSegments: [...this.segmentsByName.values()],
    };
  }
}

export function defineNetwork(
  name: string,
  build: (net: NetworkBuilder) => void,
): NetworkDefinition {
  const builder = new NetworkBuilder();
  build(builder);
  return builder.build(name);
}
