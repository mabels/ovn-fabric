// src/ir.ts — toIR(NetworkDefinition): the DESIRED-state half of the IR
// this project has been building toward since ADR 0002 (docs/adr/
// 0002-intermediate-representation.md). The reconciler package (Python,
// this repo's sibling `reconciler`/`ladops` work) already produces this
// exact {id, kind, key, data} envelope shape from LIVE router state —
// this is the same shape, produced from topology.ts's DECLARED state
// instead, so the two sides can eventually be diffed against each
// other (ADR 0002 § Decision, point 5 — a plain function over the
// union of both key sets, not an "engine").
//
// Plain TypeScript objects, not ArkType schemas: the reconciler side
// never needed schema validation either (ladops/reconciler are all
// plain dicts, no runtime validation library at all) — this data is
// internal, produced by this project's own generator, not adversarial
// input. ArkType (ADR 0002 § Decision, point 4; § "Type stability
// across the TypeScript/Python boundary") is a real, separate follow-up
// for when cross-language type generation is actually needed, not a
// prerequisite for toIR() to exist and be useful.
//
// Scope, deliberately: only what the CURRENT primitives (CollisionDomain,
// Router/RouterEndpoint, Host — see types.ts) can produce a DESIRED-state
// fact for. Kernel-side NAT is covered as of 2026-08-22: a kernel
// router's `kernel.*.masq` services (RouterEndpointService, types.ts)
// become an IMPLEMENTATION-ABSTRACT `security.group` node
// (securityGroupToIR below) carrying the POSTROUTING MASQUERADE rules
// for its real-world-facing interface. Real-interface discovery
// (DHCP/static) still has no home in this shape yet — not because it's
// forgotten, but because there's nothing yet to extract it FROM. Static
// routes (RoutingDomain, see computeRoutes/computeInterconnectRoutes
// below) and IPv6 RA/SLAAC (see RouterEndpointService, types.ts,
// resolved below) both ARE covered. toIR() walks allCollisionDomains/
// allRouters/allHosts.
//
// Split by node kind into src/ir/; this module re-exports the entry point.

// The IR node types are the ones INFERRED from the protocol schema
// (src/protocol.ts) — not a loose `{key, data: Record<string, unknown>}`
// envelope: every builder below declares the exact node type it returns,
// so emitting something the protocol doesn't declare fails to compile.
export type { IRNode } from "./protocol.ts";
export { toIR } from "./ir/to-ir.ts";
