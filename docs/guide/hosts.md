---
layout: page
title: Host requirements
---

# Host requirements

## Supported systems

The runtime reads `/etc/os-release` (and the device tree, for a Raspberry
Pi) to pick the package manager and package names. A host's `os` in the
topology overrides what it finds.

| distro | manager | notes |
| --- | --- | --- |
| Ubuntu | apt | |
| Debian (and Raspberry Pi OS, `raspbian`) | apt | |
| Fedora | dnf | |
| Rocky | dnf | OVS/OVN from the CentOS NFV SIG repository, docker-ce from Docker's repository |

On a Raspberry Pi (`platform: "rpi"`, discovered when not declared) the
runtime also installs `raspi-config` and the firmware package: `raspi-firmware`
on Debian and `linux-firmware` on Ubuntu.

The host needs `python3`. Everything else (OVS, OVN, iproute2, nftables,
docker, wireguard-tools, …) is installed by the runtime for what the
topology uses. `dependencyOverrides` on a host replaces any of these with a
pinned version, a package file already on the host, or an extra repository.

## The uplink NIC must exist under its name

The topology names physical NICs: a VLAN's `vlanParent`, a `physical` or
`xdp` interface. ovn-fabric creates VLANs, bridges and namespaces on top of
those NICs, but **the NIC itself is the host's business**. It must:

1. **exist under exactly that name.** If the topology says `ovnkern` and
   the host calls its NIC `eth0`, the run fails with
   `Cannot find device "ovnkern"`, and everything that depends on it is
   skipped.
2. **be up after boot.** A VLAN can't come up on a parent that is down:
   `RTNETLINK answers: Network is down`.

Use the same NIC name on every chassis, so one topology fits all of them.
On a systemd host, a `.link` file renames the NIC by its MAC address at
every boot:

```ini
# /etc/systemd/network/10-ovnkern.link
[Match]
MACAddress=dc:a6:32:xx:xx:xx

[Link]
Name=ovnkern
```

Run `update-initramfs -u` afterwards if the initramfs carries udev rules.

### With NetworkManager (Raspberry Pi OS, desktop distros)

NetworkManager must bring the NIC **up** but must not put an address on it,
and must leave fabric's VLANs on it alone:

```sh
# up, without addresses
nmcli con add type ethernet ifname ovnkern con-name ovnkern \
  ipv4.method disabled ipv6.method disabled connection.autoconnect yes

# fabric owns the VLANs on it
cat > /etc/NetworkManager/conf.d/90-ovnkern-unmanaged.conf <<'EOF'
[keyfile]
unmanaged-devices=interface-name:ovnkern.*
EOF
systemctl reload NetworkManager
```

Don't make the NIC itself unmanaged: nothing would bring it up after a
reboot.

### Keep management off the trunk

Reach the host for ssh over another interface (a management NIC, or Wi-Fi on
a Pi), and list that interface in the host's `reconcile.hostInterfaces`, so
a `run` leaves it and its addresses alone:

```ts
net.sshHost({
  name: "chassis-1",
  address: { ipv4: IPv4.parse("192.0.2.10/32") },
  user: "root",
  ovn: { role: { kind: "chassis" } },
  reconcile: { hostInterfaces: ["wlan0", "lo"] },
});
```

## Network between hosts

- TCP 6641/6642 from every chassis to the central (OVN NB/SB)
- UDP 6081 (Geneve) between all chassis
- Every chassis reaches the central at its address in the topology. The
  Geneve endpoint is the host's IPv4/IPv6, or `ovn.encapIp` when tunnel
  traffic uses another network.
