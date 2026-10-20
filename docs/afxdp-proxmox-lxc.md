---
layout: page
title: AF_XDP in a Proxmox LXC
---

# AF_XDP in a Proxmox LXC

A small, hands-on note for making OVS inside a Proxmox **LXC container** use
AF_XDP on the container's uplink. This is the first slice of the larger
userspace-datapath work in [ADR 0004](adr/0004-userspace-datapath-and-container-ports.md).

## Read this first

In a Proxmox LXC the container's `eth0` is a **veth**; its peer lives on the
Proxmox host. AF_XDP on a veth can only run in **copy / generic (SKB) mode**,
never zero-copy, and some kernels/drivers refuse it entirely. So:

- this gets you a **functional** AF_XDP path, not a performance one;
- for zero-copy, run OVS on the **Proxmox host** with the physical NIC, or in
  a **VM** with a passed-through NIC — not in the LXC;
- OVS taking over `eth0` removes the container's normal uplink, so give the
  container a second interface (`eth1`) for management/SSH.
- The veth's VLAN offload must be disabled on **both ends** for tagged
  traffic — see step 4.
- Proxmox VE ships stock `lxc-pve`; the `veth.vlan_*` keys from PR #4737
  are **not** in it. On Proxmox, use the `.link`/udev persistence in step 4;
  the LXC keys only work with a patched/upstream LXC.

## 1. Check what the host and container can do

On the Proxmox host:

```sh
uname -r
grep -E 'CONFIG_XDP_SOCKETS|CONFIG_BPF_SYSCALL' /boot/config-$(uname -r)
lspci -nn | grep -i ethernet
ethtool -i <physical-nic>          # driver; XDP support is driver-dependent
pct config <ctid>                  # unprivileged? caps? features?
```

AF_XDP needs `CONFIG_XDP_SOCKETS=y` and a driver that supports XDP. The LXC
needs `CAP_BPF`/`CAP_NET_ADMIN` and an unrestricted `RLIMIT_MEMLOCK`. An
unprivileged container usually cannot load XDP programs; use a privileged
one for the PoC. In `/etc/pve/lxc/<ctid>.conf`:

```ini
unprivileged: 0
features: nesting=1
lxc.apparmor.profile: unconfined
lxc.cap.drop:
lxc.prlimit.memlock: unlimited
```

Changing `unprivileged` is not always possible in place — create a fresh
privileged container if Proxmox refuses. Also disable the Proxmox firewall
on the test NIC while debugging.

## 2. Prepare the container

Give the container a second NIC for management (Proxmox: `net1`), then work
on `net0` (`eth0`) as the future AF_XDP uplink.

```sh
apt-get update
apt-get install -y iproute2 ethtool libbpf1 libxdp1 xdp-tools
apt-get install -y openvswitch-switch

ulimit -l unlimited
```

Check that this OVS build knows `afxdp` without touching `eth0` yet (a
distro build may not have it):

```sh
ldd "$(command -v ovs-vswitchd)" | grep -E 'libbpf|libxdp' \
  || echo "no libbpf/libxdp linked"
strings "$(command -v ovs-vswitchd)" | grep -m1 -i afxdp || true
```

The real proof is step 5; if adding the port says `unknown interface type
afxdp`, the build lacks `--enable-afxdp` — rebuild OVS or use a build that
has it.

## 3. Prove AF_XDP works before involving OVS

Do not debug OVS and the driver at the same time.

```sh
# XDP itself (generic/SKB mode is what a veth can do). Use a program
# that returns XDP_PASS, or you will cut the container's own uplink:
xdp-loader load -m skb eth0 <xdp-pass-prog.o>
xdp-loader status
xdp-loader unload eth0 -m skb

# AF_XDP specifically: xdpsock from the kernel's bpf selftests (or your
# distro's equivalent). -S = skb/copy, -N = native, -Z = zero-copy.
xdpsock -i eth0 -q 0 -S
```

If `xdpsock -S` fails on the veth, stop: OVS's `generic` mode will fail the
same way. Fall back to the host or a VM.

## 4. Disable VLAN offload on both ends of the veth

For VLAN-tagged traffic this is required, and it is easy to miss. The veth
driver enables 802.1Q RX/TX offload by default. With RX offload on, the
kernel moves the VLAN tag into `skb` metadata; AF_XDP only sees the untagged
frame, so OVS's `trunks=`/`tag=` never matches and the frames are dropped.
A VM's tap has `rx-vlan-offload: off [fixed]`; a veth does not.

Each end of the pair has its own feature flags, so disable it on **both**:

```sh
# container side (the AF_XDP port)
ethtool -K eth0 rxvlan off txvlan off

# host side: the veth<vmid>i<index> peer Proxmox created for that NIC
ethtool -K veth301i2 rxvlan off txvlan off
```

Verify both:

```sh
ethtool -k eth0      | grep -E 'rx-vlan-offload|tx-vlan-offload'
ethtool -k veth301i2 | grep -E 'rx-vlan-offload|tx-vlan-offload'
```

With the patched LXC from PR #4737 the same setting is expressed in the
container config and applied to both ends:

```ini
lxc.net.0.veth.vlan_rx_offload = 0
lxc.net.0.veth.vlan_tx_offload = 0
```

Without it, `ovs-appctl dpctl/dump-flows` shows only untagged flows ending in
`actions:drop`.

### Persistence

`ethtool -K` is per netdev. A container restart recreates the veth, so the
flags reset. Make it declarative with `systemd.link` instead.

Inside the container, `/etc/systemd/network/90-ovnxdp.link`:

```ini
[Match]
Name=ovnxdp

[Link]
ReceiveVLANCTAGHardwareAcceleration=no
TransmitVLANCTAGHardwareAcceleration=no
```

On the Proxmox host, `/etc/systemd/network/90-veth301i2.link`:

```ini
[Match]
OriginalName=veth301i2

[Link]
ReceiveVLANCTAGHardwareAcceleration=no
TransmitVLANCTAGHardwareAcceleration=no
```

Apply to an existing interface (or restart the CT):

```sh
udevadm control --reload
udevadm trigger --action=add /sys/class/net/ovnxdp
udevadm trigger --action=add /sys/class/net/veth301i2
```

Proxmox VE ships stock `lxc-pve`, which does **not** have the
`vlan_rx_offload`/`vlan_tx_offload` keys; they come from PR #4737 and need a
patched/upstream LXC. On stock Proxmox the `.link` files above are the
working route. With a patched LXC the keys are applied on every start and the
`.link` files are not needed.

## 5. Configure OVS (inside the container)

```sh
ovs-vsctl add-br br-xdp -- set bridge br-xdp datapath_type=netdev

# eth0 becomes the AF_XDP uplink. generic = copy mode, the only mode a
# veth can be expected to support; best-effort would try zero-copy first.
ovs-vsctl add-port br-xdp eth0 -- \
  set Interface eth0 type=afxdp \
      options:xdp-mode=generic \
      options:n_rxq=1

ovs-vsctl set Interface eth0 mtu_request=1500
ip link set br-xdp up
```

`xdp-mode` accepts `best-effort` (default), `native-with-zerocopy`,
`native`, and `generic`. Start with `generic` for a veth; use
`best-effort` on a real NIC and read the log to see which mode it chose.

## 6. Give a workload a port on that bridge

A plain container/workload still needs a kernel-visible device. On a netdev
bridge that is a **TAP**:

```sh
ip tuntap add dev tap-c1 mode tap
ip link set tap-c1 up
ip link set tap-c1 address 02:00:00:00:c1:01
ovs-vsctl add-port br-xdp tap-c1

# then move it into the workload's netns (docker --network none):
pid=$(docker inspect -f '{{.State.Pid}}' c1)
mkdir -p /var/run/netns && ln -sfn "/proc/$pid/ns/net" /var/run/netns/c1
ip link set tap-c1 netns c1
ip netns exec c1 ip link set tap-c1 name eth0
ip netns exec c1 ip link set eth0 up
```

The TAP leg still crosses TUN/TAP; only the uplink (`eth0`) is AF_XDP.

## 7. Verify

```sh
ovs-vsctl show
ovs-appctl dpif/show                 # br-xdp: eth0 (afxdp), tap-c1 (tap)
ovs-appctl dpctl/dump-flows
ovs-appctl dpif-netdev/pmd-stats-show
grep -i 'XDP mode' /var/log/openvswitch/ovs-vswitchd.log
```

You want `XDP mode will be in use: generic` (or `native`/`native-with-zerocopy`
on a real NIC). `Defaulted to ...` / "Failed to detect suitable XDP mode"
means the driver refused every mode.

## 8. Persistence

`ovn-fabric` does not emit `afxdp` ports yet — its binding emitter only knows
`vlan`, `veth` and `physical`. Until that changes, keep the `ovs-vsctl`
above in a small unit or a script run after `ovs-vswitchd`, and do not put it
in the generated deployer yet.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `xsk_socket__create failed (Operation not permitted)` | unprivileged LXC, seccomp/AppArmor, or missing `CAP_BPF`/`CAP_NET_ADMIN` |
| `No XDP program is loaded at ifindex …` | `libxdp`/`libbpf` too old, or the driver/build lacks XDP |
| `Failed to detect suitable XDP mode` | veth/driver refused native and generic; use the host/VM |
| `setrlimit(RLIMIT_MEMLOCK) failed` | raise `ulimit -l` / `lxc.prlimit.memlock` |
| `unknown interface type afxdp` | OVS built without `--enable-afxdp` |
| container loses SSH after step 5 | `eth0` is now OVS's; use `eth1` for management |
| tagged VLAN traffic is dropped; `dump-flows` shows only untagged flows with `actions:drop` | RX VLAN offload still on; disable `rxvlan`/`txvlan` on **both** veth ends (step 4) |

## What this does not cover

- Zero-copy AF_XDP (needs a physical NIC on the host or a passed-through NIC
  in a VM).
- OVN with `br-int` on the netdev datapath — that requires every bridge in
  the patch chain to be netdev, covered in ADR 0004.
- DPDK; that is the separate path in ADR 0004.
