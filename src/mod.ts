// src/mod.ts — the library's single public entry point.
//
// Re-exports everything a topology.ts config needs to author a
// network: the defineNetwork builder and the public types (Host,
// CollisionDomain, the router-endpoint shapes, the service/workload
// shapes, ...). Import this one path — "@adviser/ovn-fabric" /
// "jsr:@adviser/ovn-fabric" — rather than reaching into src/define.ts /
// src/types.ts directly; those remain implementation modules, not a
// stable surface on their own. (The CLI itself is a separate export,
// "./cli" — see src/cli.ts and deno.json — since it's a runnable
// entrypoint, not something a config module needs to import.)
export * from "./define.ts";
export * from "./ip.ts";
export * from "./types.ts";
// Only transitNetwork() (+ its own TransitEndpoint/TransitNetwork
// types) from addressing.ts — the rest (transferNet/
// macFromV4/macFromVlan/fnv1a32) are internal fold-rule helpers
// src/ir.ts calls itself; a config author never calls those directly
// the way they call transitNetwork() itself, e.g. to build the
// `transit` a router.kernelRouterEndpoint() call needs (define.ts).
export {
  type TransitEndpoint,
  type TransitNetwork,
  transitNetwork,
} from "./addressing.ts";
