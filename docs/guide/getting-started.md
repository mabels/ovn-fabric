---
layout: page
title: Getting started
---

# Getting started

ovn-fabric has two halves:

- **A TypeScript DSL** (`@adviser/ovn-fabric`). You describe the network
  (hosts, collision domains, routers, tunnels, workloads) in a
  `topology.ts`, and the CLI turns it into a desired-state **IR**: a JSON
  document with one `{id, kind, key, data}` node per host, logical switch,
  router port, route, workload and so on.
- **A host runtime** (`fabric`). This is one Python service per host, shipped
  as a single PEX. You hand it the IR and it makes the host match: OVS
  bridges and VLANs, the OVN logical topology, kernel-router namespaces,
  WireGuard, and docker workloads.

## 1. Install the CLI

Through npx (no Deno install needed):

```sh
npx -p @adviser/ovn-fabric ovn-fabric generate-ir topology.ts > ir.json
```

Through Deno, from JSR:

```sh
deno run -A jsr:@adviser/ovn-fabric/cli generate-ir topology.ts > ir.json
deno install -g -A -n ovn-fabric jsr:@adviser/ovn-fabric/cli   # permanent command
```

## 2. Write a topology

Start from
[`examples/minimal-topology.ts`](https://github.com/mabels/ovn-fabric/blob/main/examples/minimal-topology.ts):
one OVN central, one chassis, and one router with a LAN leg (VLAN 10 on
the chassis's `eth1`) and an upstream leg.

```ts
import { defineNetwork, IPv4 } from "jsr:@adviser/ovn-fabric";

export const network = defineNetwork("minimal", (net) => {
  const central = net.sshHost({
    name: "central",
    address: { ipv4: IPv4.parse("10.99.0.1/32") },
    user: "root",
    ovn: { role: { kind: "central" } },
  });
  const chassis = net.sshHost({
    name: "chassis",
    address: { ipv4: IPv4.parse("10.99.0.2/32") },
    user: "root",
    ovn: { role: { kind: "chassis" } },
  });

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
```

An OVN cluster needs one `central` host, which every chassis points its
`ovn-remote` at, plus at least one `chassis`. The [topology guide](topology.html)
explains every building block.

## 3. Look at what it declares

```sh
ovn-fabric generate topology.ts          # a summary of what the topology declares
ovn-fabric generate-ir topology.ts       # the IR, as JSON
ovn-fabric serve-graph topology.ts       # a page drawing segments, routers and workloads
```

## 4. Deploy it

From a checkout of the repository, `scripts/fabric-deploy.sh` installs the
runtime on a host over ssh and applies the IR there:

```sh
ovn-fabric generate-ir topology.ts \
  | scripts/fabric-deploy.sh root@HOST ir-apply run
```

The host's hostname picks which of the IR's hosts it is. Before the first
deploy, check the [host requirements](hosts.html). In particular, the NICs
that the topology names must exist under those names. [Deploying](deploying.html)
covers the rest: the CLI on the host, `create`/`delete`/`run`, and what
happens at boot.
