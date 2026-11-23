import type { OvnRouterEndpoint } from "./router-endpoint.ts";
import type { RoutingDomain } from "./routing.ts";

// ── OVN per-router (Logical_Router.options) options ─────────────────
// The author-facing half of a router's own OVN options
// (net.defineOvnRouter()'s `options`). Every field is optional; what is
// left unset is defaulted by resolveOvnRouterOptions below, so a stored
// Router always carries the FINAL value (Router.options).
export interface OvnRouterOptions {
  /** Seconds without traffic after which the router forgets a learned
   * MAC_Binding and re-resolves it (OVN's
   * Logical_Router.options:mac_binding_age_threshold). OVN's own default
   * is 0 — keep a binding FOREVER, never re-probe it — which black-holes
   * a neighbour that came back with a different MAC (confirmed live
   * 2026-10-11). Defaults to DEFAULT_MAC_BINDING_AGE_THRESHOLD; an
   * explicit 0 restores OVN's keep-forever behaviour. */
  readonly macBindingAgeThreshold?: number;
}

export const DEFAULT_MAC_BINDING_AGE_THRESHOLD = 300;

/** Fill every unset OvnRouterOptions field with its default — also when
 * `options` itself is omitted — and fail fast on a value OVN would
 * reject (the column is an integer in 0..4294967295). */
export function resolveOvnRouterOptions(
  routerName: string,
  options?: OvnRouterOptions,
): Required<OvnRouterOptions> {
  const macBindingAgeThreshold = options?.macBindingAgeThreshold ??
    DEFAULT_MAC_BINDING_AGE_THRESHOLD;
  if (
    !Number.isInteger(macBindingAgeThreshold) ||
    macBindingAgeThreshold < 0 || macBindingAgeThreshold > 0xffffffff
  ) {
    throw new Error(
      `router "${routerName}": options.macBindingAgeThreshold must be an ` +
        `integer number of seconds in 0..4294967295, got ` +
        `${macBindingAgeThreshold}`,
    );
  }
  return { macBindingAgeThreshold };
}

export interface Router {
  readonly name: string;
  // Always the OVN side — even a kernelRouterEndpoint()/tunnelRouterEndpoint()
  // input is consumed into a plain OvnRouterEndpoint by its builder (the
  // KernelRouter/TunnelRouterEndpoint shapes are INPUT types, never a
  // stored Router's own endpoint).
  //
  // A router owns an ARRAY of endpoints, not a fixed left/right pair
  // (2026-09-16) — every port, addressed by its own EndpointBase.name
  // rather than by its position. At least two are required
  // (defineOvnRouter() rejects a lone endpoint: a router exists to join
  // domains, one leg joins nothing). Stored SORTED by `name`, so the IR
  // and the deployer emit deterministically regardless of declaration
  // order.
  readonly endpoints: readonly OvnRouterEndpoint[];
  /** RoutingDomains (net.routingDomain()) this router participates in
   * — object references, not names, matching every other cross-
   * reference in this file (l2Segment, gatewayChassis, ...), not a
   * string that could typo/drift out of sync with what was actually
   * declared. See RoutingDomain's own doc comment for how a domain's
   * routes actually resolve per-router. A router participates if THIS
   * names it OR any endpoint's own RouterEndpointBase.routingDomains
   * does (per-endpoint membership, 2026-08-23). */
  readonly routingDomains?: readonly RoutingDomain[];
  /** Routers this router IMPLIES (derived, not author-declared) — e.g. a
   * tunnelRouterEndpoint's internally-created `<name>-upstream` peer.
   * Always an array (possibly empty). The config author lists only the
   * TOP-level router in defineNetwork()'s returned routers[]; define.ts
   * flattens each router's subRouters when assembling allRouters, so a
   * tunnel's upstream peer never needs listing (2026-09-08). */
  readonly subRouters: readonly Router[];
  /** This router's OVN options, FULLY RESOLVED (defaults filled in by
   * resolveOvnRouterOptions at defineOvnRouter() time) — serialized into
   * the router's own `ovn.lr` IR node (src/ir.ts's routerToIR). */
  readonly options: Required<OvnRouterOptions>;
}
