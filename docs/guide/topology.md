---
layout: page
title: Topology
---

# Writing a topology

A topology is a TypeScript module that exports the result of
`defineNetwork(name, (net) => { … })`. Everything comes from one import,
`@adviser/ovn-fabric` (or `jsr:@adviser/ovn-fabric`). The full API, with
every field's documentation, is in the [API reference]({{ '/reference.html' | relative_url }}).

One `defineNetwork` is **one OVN cluster**: one shared NB/SB control plane,
with one `central` host and any number of `chassis` hosts.

## Building blocks

### Hosts

`net.sshHost({ name, address, user, … })` declares a host reached over ssh,
and `net.localHost({ name, … })` the machine running the generator. Every
router endpoint names the host its interfaces live on, so a multi-chassis
topology is a configuration change, not a redesign.

| field | meaning |
| --- | --- |
| `address` | `{ fqdn?, ipv4?, ipv6? }`, at least one of them |
| `ovn` | `{ role: { kind: "central" } }` or `{ role: { kind: "chassis" } }`; optional `encapIp`/`encapType` (Geneve by default). Without `ovn`, the host is not part of the cluster. |
| `os` | `{ distro, version?, platform? }`. The distro is `ubuntu`, `debian`, `fedora` or `rocky`; the platform is `generic` or `rpi`. Anything left out is discovered on the host. |
| `dependencyOverrides` | replacement packages for an abstract dependency (`ovn`, `docker`, `traceroute`, …): a package at a version, a package file already on the host, or an extra repository |
| `reconcile.hostInterfaces` | the host's own interfaces the runtime must leave alone, such as `["host", "lo"]` |
| `monitoring.ipfix` | IPFIX flow export from the host's bridges: `{ target, sampling?, … }` |

### Collision domains

`net.collisionDomain(name)` is a bare OVN logical switch (L2 only).
`net.backbone(name)` declares the one cluster-wide backbone switch. Router
endpoints join a domain through their `l2Segment`.

### Routers and endpoints

`net.defineOvnRouter(name, (router) => ({ routingDomains, endpoints }))`
is one OVN `Logical_Router` with an array of endpoints (at least two). A
port is identified by its own derived name, never by its position.

- `router.ovnRouterEndpoint({ … })` is an OVN router port: `l2Segment`, its
  `ipaddrs`, optional `ifaces` (the real NIC or VLAN on a host that bridges
  the domain into the physical world), `routes`, `mac`, and `services`.
- `router.kernelRouterEndpoint({ … })` puts a **kernel router** behind the
  endpoint: a real Linux network namespace forwarding between OVN and a real
  uplink (a WAN VLAN, DHCP, masquerade).
- `router.tunnelRouterEndpoint({ … })` does the same for a tunnel (WireGuard,
  ZeroTier) with a transit to the mesh on one side and an upstream leg on
  the other.

### Interfaces

The `iface` of an endpoint binding is one of these kinds:

| kind | what it is |
| --- | --- |
| `vlan` | a VLAN on a parent NIC (`vlanParent`, `vlanId`). The parent is a NIC name, `{ kind: "physical", name }`, or `{ kind: "xdp", name, mode?, nRxq? }` for AF_XDP. |
| `physical` | an untagged NIC |
| `xdp` | a NIC owned by OVS's userspace datapath through AF_XDP. All AF_XDP ports go on one netdev bridge, `br-xdp`, and the VLAN split happens in OVS. |
| `veth` | a veth pair owned by a kernel router's namespace |
| `bridge-port` | a port on an existing bridge |
| `wireguard`, `zerotier` | tunnels, through `wg-quick` or a confined ZeroTier client |
| `dummy` | a placeholder with no backing device |

### Endpoint services

The `services` of an endpoint add behaviour to it:

| service | effect |
| --- | --- |
| `ipv6.slaac`, `ipv6.ra` | IPv6 router advertisements and SLAAC on the segment |
| `ovn.ipv4.masq`, `ovn.ipv6.masq` | SNAT of the router's other subnets to this endpoint's address |
| `ovn.ipv4.block-ingress`, `ovn.ipv6.block-ingress` | stateful filter: new connections coming in on this endpoint to the router's other subnets are dropped, except what `allow` lists; replies to outbound connections pass |
| `kernel.ipv4.masq`, `kernel.ipv6.masq` | masquerade in a kernel router |
| `kernel.app.dhcp-client`, `kernel.app.wireguard`, `kernel.app.zerotier`, `kernel.app.docker` | an application inside a kernel router's namespace |

### Routing domains

`net.routingDomain(name, { routes?, metric? })` is a membership and
route-distribution tag. Routers join it through `routingDomains`.

- Without `routes`, the domain's destinations are **calculated** from what
  its participants actually resolve (anchor routes and interconnect), minus
  infrastructure transit, backdoor and backbone prefixes.
- An explicit non-empty `routes` array overrides that.

The routes reach every participating router, next-hopped at a peer's
address on a shared domain, and every workload on a participating segment,
next-hopped at that segment's gateway. `metric` ranks domains; lower wins.

### Workloads

`net.service(name, …)` declares a reusable, network-free workload: an image
or a build, a command, and files. `ep.attachTo(service, { ipaddrs, routes? })`
gives it one NIC on an endpoint's segment, the same model as a k8s pod. A
NIC's routes are its explicit `routes`; otherwise it gets the routes of the
routing domains its router takes part in. Each workload becomes a docker
container on its host.

### Tenants, WANs and the platform

Larger sites build on the primitives above:

- `net.internet({ core })` declares the shared side every tenant leaves to.
- `net.wan(name, …)` declares a physical uplink on it.
- `net.tenant(name, …)` declares an isolated group of cables and routers.
- `net.createPlatform(name, spec)` declares the appliance's own service
  platform: control plane, DNS, ingress and publishing.

See ADR 0009 (tenants and switchable WANs) and the living example,
[`examples/topology.cluster-tenants.ts`](https://github.com/mabels/ovn-fabric/blob/main/examples/topology.cluster-tenants.ts).

### Addresses

`net.ipNet({ ipv4?, ipv6?, allocator? })` declares a net. An address on it
is either **pinned**, with `.host(n)` (a number, `"first"` or `"last"`), or
left to the runtime with `.allocated()`. The runtime places allocated
addresses on the host and remembers them across reboots (ADR 0011). The
allocator is `low-high`, `high-low` or `random`. MAC addresses are derived
from the resolved IPv4 unless an endpoint pins `mac`.

## Parameters and secrets

A topology can take parameters from the run that loads it. `generate`,
`generate-ir` and `serve-graph` accept a repeatable `--runtime key=val`,
and the topology reads it, together with the process environment, through
`runtime()`:

```ts
import { runtime } from "@adviser/ovn-fabric";

const rt = runtime();
const debug = rt.args["debug"] === "1"; // --runtime debug=1
const site = rt.env["SITE"] ?? "hh";    // SITE=... in the environment
```

The living example keeps everything site-specific (keys, names, addresses,
which interface carries which network) in a separate secrets module. It
passes the path in with `--runtime secrets=…` and loads it with
`getSecrets(ctx)`. The shape of that file is the template in
`@adviser/ovn-fabric/secret-types`.

```sh
ovn-fabric generate-ir \
  --runtime secrets=./topology.secrets.ts --runtime ctx=prod \
  topology.ts > ir.json
```
