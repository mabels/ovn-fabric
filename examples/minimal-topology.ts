// examples/minimal-topology.ts — the smallest possible DEPLOYABLE
// topology under the current (router-endpoint) model: one OVN central,
// one chassis, and one logical router with a client-facing LAN leg and
// an upstream leg. Copy this file as a starting point for your own
// config — see the README for the full model (collision domains,
// kernel/tunnel router endpoints, workloads, etc).
//
// This file is also used as a CI smoke test (see
// .github/workflows/ci.yaml), so keep it runnable. It is deliberately
// small, but unlike a bare single-host sketch it actually hydrates and
// emits through the Python deployer: an OVN cluster needs a central
// chassis to point every chassis's ovn-remote at, and that chassis
// needs a real address to build that remote from.

import { defineNetwork, IPv4 } from "../src/mod.ts";

export const network = defineNetwork("minimal", (net) => {
  const central = net.sshHost(
    "central",
    { ipv4: IPv4.parse("10.99.0.1/32") },
    "root",
    undefined,
    { role: { kind: "central" } },
  );
  const chassis = net.sshHost(
    "chassis",
    { ipv4: IPv4.parse("10.99.0.2/32") },
    "root",
    undefined,
    { role: { kind: "chassis" } },
  );

  const lan = net.collisionDomain("lan");
  const upstream = net.collisionDomain("upstream");

  const router = net.defineOvnRouter("router-lan", (router) => ({
    routingDomains: [],
    endpoints: [
      router.ovnRouterEndpoint({
        l2Segment: lan,
        ipaddrs: [IPv4.parse("192.168.10.1/24")],
        ifaces: [
          {
            host: chassis,
            iface: { kind: "vlan", vlanParent: "eth1", vlanId: 10 },
          },
        ],
        services: [{ kind: "ipv6.slaac" }, { kind: "ipv6.ra" }],
      }),
      router.ovnRouterEndpoint({
        l2Segment: upstream,
        ipaddrs: [IPv4.parse("10.0.0.2/30")],
      }),
    ],
  }));

  return { hosts: [central, chassis], routers: [router] };
});
