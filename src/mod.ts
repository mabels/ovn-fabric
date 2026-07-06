// src/mod.ts — the library's single public entry point.
//
// Re-exports everything a topology.ts config needs to author a
// network: the defineNetwork builder, every uplink/segment factory,
// and the public types (Host, Uplink, Segment, the ManualUplink/
// FixedUplink/PriorityUplink selectors, etc). Import this one path —
// "@adviser/ovn-fabric" / "jsr:@adviser/ovn-fabric" — rather than
// reaching into src/define.ts / src/factories.ts / src/types.ts
// directly; those remain implementation modules, not a stable surface
// on their own. (The CLI itself is a separate export, "./cli" — see
// src/cli.ts and deno.json — since it's a runnable entrypoint, not
// something a config module needs to import.)
export * from "./define.ts";
export * from "./factories.ts";
export * from "./types.ts";
