---
layout: page
title: Troubleshooting
---

# Troubleshooting

## Where to look

```sh
fabric status                 # last apply, background job, kept-alive containers
fabric jobs                   # recent jobs
fabric follow JOB             # the full log of one job
journalctl -u ovn-fabric      # the service itself
```

A failed run names the **first failing unit** and prints what that unit
did, command by command. Every unit that depends on it is listed as
`skipped — needs …, which failed`. Fix the first failure; the skipped units
usually follow.

```text
interface: lan (ovnkern.10): failed — … Cannot find device "ovnkern"
router: router-lan: skipped — needs interface: lan (ovnkern.10), which failed
```

A unit shown as plain `skipped`, without a reason, needed no change.

## Common failures

### `dependencies not ready: <name> failed`

A package install failed before any unit ran, so nothing was applied.
Usually a package name doesn't exist on that distro. Check what apt or dnf
says on the host (`apt-get install <pkg>`), and replace the package with the
host's `dependencyOverrides` if needed.

### `Cannot find device "<nic>"`

The topology names a NIC the host doesn't have under that name. Rename the
NIC, or change the topology. See
[the uplink NIC must exist under its name](hosts.html#the-uplink-nic-must-exist-under-its-name).

### `RTNETLINK answers: Network is down` (after a reboot)

The NIC is there but down, so the VLANs on it can't come up. Something on
the host has to bring it up at boot. With NetworkManager, give it a profile
without addresses instead of making it unmanaged; see
[Host requirements](hosts.html#with-networkmanager-raspberry-pi-os-desktop-distros).

### `- is not JSON` when running `ir-apply` over ssh

With nothing piped in, ssh hands the command an empty stdin, which counts
as "the IR on stdin". Name the IR and close stdin:

```sh
ssh root@HOST 'fabric ir-apply run deployed </dev/null'
```

### Containers stay `missing`

Their images are still being built by the background job. Check
`fabric status` and `fabric follow <background job>`. A container that
keeps exiting is brought back with an increasing delay. Its own log is in
`docker logs <name>`.

### `setlocale: LC_ALL: cannot change locale`

ssh forwards your local `LC_ALL` to the host, and the host doesn't have that
locale. It's harmless. The deploy scripts unset `LC_ALL` themselves.

## Looking at the live state

```sh
ip netns list                       # kernel routers' namespaces (ns-…)
ip -br link                         # VLANs, veths, bridges
ovs-vsctl show                      # OVS bridges and ports
ovn-nbctl show; ovn-sbctl show      # logical topology and chassis (on the central)
```
