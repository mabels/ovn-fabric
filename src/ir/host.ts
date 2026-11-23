// src/ir/host.ts — infra.host: a host (chassis) and its dependencies.

import type { NetworkDefinition } from "../define.ts";
import type { InfraHostNode } from "../protocol.ts";
import type { Host } from "../types.ts";
import { serviceNodes } from "./workloads.ts";

// ── infra.host ───────────────────────────────────────────────────────
// Matches the reconciler side's own infra.host kind (reconciler/host/
// reconcile.py) — but NOT the same data shape. The live side reports
// OBSERVED facts (uname -a, a reconcile timestamp) that have no
// "desired" analog — you can't declare what `uname -a` should say. The
// desired side instead reports the one thing that genuinely IS a
// declared intent: whether this host should run ovn-central or be a
// plain chassis (see OvnHostConfig, types.ts). A diff between the two
// sides' infra.host nodes is therefore only ever comparing key
// presence (does this host exist at all), never comparing `data`
// field-for-field — the two `data` shapes are deliberately incomparable.
// encapIp resolution matches OvnHostConfig.encapIp's own documented
// default (types.ts): explicit override, else address.ipv4, else
// address.ipv6 — never address.fqdn (the SB DB's Chassis.encaps.ip
// column is a real address, not a hostname, confirmed live 2026-08-09).
// undefined only when a host declares nothing but an fqdn — the
// shell-conversion step is what actually needs this to exist, so it's
// left optional here rather than throwing at IR-build time.
function resolveEncapIp(host: Host): string | undefined {
  return (host.ovn?.encapIp ?? host.address.ipv4 ?? host.address.ipv6)?.to_s();
}

// The ABSTRACT dependencies this Host needs (2026-08-23), derived from
// what the topology puts on it — ABSTRACT names, deliberately not
// distro package names, so the IR stays OS-agnostic: every host gets the
// base tooling (`arp`/net-tools, `ping`, `traceroute`,
// `python`/python-is-python3 — the everyday debugging commands), an
// OVN-cluster host needs `ovn`+`ovs`, any kernel router's netns needs
// `ip`+`iptables`, and the apps need their own (dhclient/dhcpcd,
// wireguard, docker). A zerotier app needs nothing on the host: zerotier
// runs in its container (2026-10-31). The runtime maps each abstract
// dependency to the host's distro's packages; there is deliberately NO
// deinstall.
export function hostDependencies(
  host: Host,
  network: NetworkDefinition,
): string[] {
  const deps = new Set<string>([
    "arp",
    "ping",
    "traceroute",
    "python",
  ]);
  if (host.ovn) {
    deps.add("ovn");
    deps.add("ovs");
  }
  const kernelRouters = network.allKernelRouters.filter(
    (kr) => kr.host.name === host.name,
  );
  if (kernelRouters.length > 0) {
    deps.add("ip");
    deps.add("iptables");
    for (const kr of kernelRouters) {
      for (const side of [kr.left, kr.right]) {
        for (const app of side.apps ?? []) {
          if (app.kind === "dhcp-client") {
            deps.add(app.style);
          } else if (app.kind === "wireguard") {
            deps.add("wireguard");
          } else if (app.kind === "docker") {
            deps.add("docker");
          }
        }
      }
    }
  }
  // Host-level docker workloads (`net.service` -> kernel.app.container)
  // are containers too: the host needs the docker engine even when it
  // has no kernel router apps.
  for (const service of serviceNodes(network)) {
    if (service.data.host === `host:${host.name}`) {
      deps.add("docker");
    }
  }
  return [...deps];
}

export function hostToIR(
  host: Host,
  dependencies: readonly string[],
): InfraHostNode {
  const id = `host:${host.name}`;
  const ipfix = host.monitoring?.ipfix;
  const overrides = host.dependencyOverrides;
  // An empty `os: {}` means "discover it": emitting it would fail the IR
  // schema, so drop it. A populated but invalid os still fails there.
  const os = host.os && Object.keys(host.os).length > 0 ? host.os : undefined;
  return {
    id,
    kind: "infra.host",
    key: { host: host.name },
    data: {
      connectAddress: host.connectAddress,
      ovnRole: host.ovn?.role.kind,
      encapIp: resolveEncapIp(host),
      // A declared target rides the IR; when the config leaves it unset,
      // the runtime discovers the host's OS itself.
      os,
      abstractDependencies: dependencies.length > 0 ? dependencies : undefined,
      // Concrete replacements for the distro translation, by abstract
      // name (e.g. local patched packages for this host).
      ...(overrides
        ? {
          dependencyOverrides: Object.fromEntries(
            Object.entries(overrides).map((
              [name, entries],
            ) => [name, [...entries]]),
          ),
        }
        : {}),
      // WHERE to export flows (types.ts IpfixExport -> protocol IpfixData)
      // — the per-leg WHETHER rides each ovn.ls interface instead (see
      // collisionDomainToIR's `flow`). `target` becomes `targets`, the
      // OVS IPFIX column name; the API's singular reads better in config.
      ipfix: ipfix
        ? {
          targets: ipfix.target,
          sampling: ipfix.sampling,
          cacheActiveTimeout: ipfix.cacheActiveTimeout,
          cacheMaxFlows: ipfix.cacheMaxFlows,
        }
        : undefined,
      // The host's own interfaces the reconcile leaves alone — the
      // loopback always (every netns has one), then the topology's.
      reconcile: {
        hostInterfaces: [
          ...new Set(["lo", ...(host.reconcile?.hostInterfaces ?? [])]),
        ],
      },
    },
  };
}
