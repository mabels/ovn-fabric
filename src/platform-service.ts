// platform-service.ts — the platform's BUILT-IN services ("platform.*") and
// the sentinel that removes one from the active set.
//
// Built-ins land ONE BY ONE (dns → ingress → ca → …). This module owns what
// they ARE — image, defaults, generated config, how they resolve into the
// admin NIC the platform attaches — while types.ts only carries their
// SHAPES and the topology-facing surface (PlatformSpec.platformServices,
// platform.cfgPlatformSvc()/delPlatformSvc()).
//
// Split out of types.ts so that file stays shape-only, and out of define.ts
// so the built-in definitions don't grow inside the builder.

import type {
  PlatformServiceCoreDns,
  PlatformServiceId,
  PlatformServiceOptions,
  PlatformServiceSpec,
  Service,
} from "./types.ts";

/** A forwarder-less CoreDNS answers nothing, so the built-in ships with a
 * sensible upstream: Cloudflare, matching the draft's dnsmasq
 * `--server=1.1.1.1`. Override with forwarders[] — e.g. "tls://1.1.1.1" or
 * "https://cloudflare-dns.com/dns-query" for encrypted upstreams, or the
 * ISP's resolvers. NOTE: forwarding needs EGRESS — the control-plane leg
 * must join a routing domain that carries a default (and masq), otherwise
 * CoreDNS has no route to 1.1.1.1. */
const DEFAULT_DNS_FORWARDERS: readonly string[] = ["1.1.1.1"];

const IGNORE: unique symbol = Symbol("platformSvc.IGNORE");

/** Settings for the platform's BUILT-IN services. Assign platformSvc.IGNORE
 * in PlatformSpec.platformServices — or call platform.delPlatformSvc(id) —
 * to DROP a built-in from the active set. A named, unique constant rather
 * than `false`, so the intent reads at the call site and can't be confused
 * with a falsy "disabled" value later. */
export const platformSvc = { IGNORE } as const;

/** Registry ORDER of the built-ins — the active set is instantiated in THIS
 * order (not in the order of the topology's object literal), so upgrades and
 * rollbacks are deterministic. Keep in step with PlatformServiceOptions. */
export const PLATFORM_SERVICE_IDS: readonly PlatformServiceId[] = ["dns"];

// ── built-in: dns (CoreDNS) ─────────────────────────────────────────
// The platform's own internal resolver: answers for the platform's local
// names and forwards everything else. Named `platform.dns` — built-ins live
// under the `platform.` prefix so they're unmistakable in the IR / deployer
// output and clearly not an author's service.
//
// Its config is a Corefile, generated here and carried through the IR as a
// FILE (Service.files) — section of the filesystem primitive the deployer
// writes on the host and bind-mounts read-only.
// TODO: the `zones` option (authoritative for the platform's own names)
// needs the published-name records, which aren't in the IR yet.
function renderCorefile(opts: PlatformServiceCoreDns): string[] {
  const lines = [".:53 {"];
  if (opts.forwarders && opts.forwarders.length > 0) {
    lines.push(`    forward . ${opts.forwarders.join(" ")}`);
  }
  lines.push("    cache 30", "}");
  return lines;
}

function coreDnsService(opts: PlatformServiceCoreDns): Service {
  return {
    name: "platform.dns",
    image: "coredns/coredns:latest",
    cmd: ["-conf", "/etc/coredns/Corefile"],
    files: [{ path: "/etc/coredns/Corefile", content: renderCorefile(opts) }],
    endpointRefs: [],
  };
}

/** A built-in's DEFAULT setting when the id isn't mentioned at all. ON by
 * default (the "batteries included" platform) — now that addresses are
 * ALLOCATED (+10 off the control-plane network), a built-in no longer needs
 * the author to declare anything to be runnable. Use platformSvc.IGNORE /
 * delPlatformSvc(id) to drop one. */
export function defaultPlatformServiceSetting(
  id: PlatformServiceId,
): PlatformServiceOptions[PlatformServiceId] | typeof IGNORE {
  switch (id) {
    case "dns":
      // Defaults only when the id is ABSENT: an explicit `dns: {}` means
      // "no forwarders" (options replace, they don't merge).
      return { forwarders: DEFAULT_DNS_FORWARDERS };
    default:
      return IGNORE;
  }
}

/** Resolve one built-in id + its setting into the admin NIC the platform
 * should attach (PlatformServiceSpec). IGNORE (or an id with no
 * implementation yet) → undefined = not in the active set. */
export function resolvePlatformService(
  id: PlatformServiceId,
  options: PlatformServiceOptions[PlatformServiceId],
): PlatformServiceSpec | undefined {
  switch (id) {
    case "dns": {
      // The id keys the map, so the value is already the right shape; the
      // widening is only because a switch doesn't narrow a separately-typed
      // union. One cast per built-in, right next to its definition.
      const dns = options as PlatformServiceCoreDns;
      return {
        service: coreDnsService(dns),
        ipaddrs: dns.ipaddrs,
        role: "dns",
      };
    }
    default:
      return undefined;
  }
}
