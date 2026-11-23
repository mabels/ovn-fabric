---
layout: page
title: ovn-fabric
---

**ovn-fabric** is a declarative OVN/OVS network fabric. You describe a
network (hosts, collision domains, routers, kernel routers, VPN tunnels,
workloads) as plain TypeScript. A runtime on each host makes the host match
it, and keeps it that way across reboots.

```text
topology.ts ──▶ IR (JSON) ──▶ fabric runtime on each host ──▶ OVS · OVN · netns · WireGuard · docker
```

## Documentation

- [Getting started](guide/getting-started.html): install, a first topology,
  a first deploy
- [Topology](guide/topology.html): the building blocks of the DSL
- [API reference]({{ '/reference.html' | relative_url }}): every exported
  function and type, generated from the source
- [Deploying](guide/deploying.html): the runtime, the `fabric` CLI,
  `create`/`delete`/`run`, boot replay
- [Host requirements](guide/hosts.html): supported systems, NIC naming,
  NetworkManager
- [Troubleshooting](guide/troubleshooting.html): reading a failed run,
  common errors
- [Architecture](guide/architecture.html): data flow, packages, decision
  records
- [Development](guide/development.html): checks, generated code, testing,
  publishing

## Write-ups

- [Bridging OVN into a physical home network (or: how "what is OVS?" led here)](blog/ovn-fabric-writeup.html)
- [AF_XDP in a Proxmox LXC](afxdp-proxmox-lxc.html)

Source: [github.com/mabels/ovn-fabric](https://github.com/mabels/ovn-fabric) ·
Package: [`@adviser/ovn-fabric`](https://jsr.io/@adviser/ovn-fabric) on JSR
and npm · Apache-2.0
