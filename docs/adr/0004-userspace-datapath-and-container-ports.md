# ADR 0004: userspace datapath (DPDK / AF_XDP) and container-facing ports

Status: research — options, a step-by-step PoC, and the open questions.
Nothing implemented in the generator yet; the PoC still has to be run on
`net-test-hh-b` (or, for DPDK, on the Proxmox host — see *Where to test*).

## Context

Every external leg `ovn-fabric` builds today is a **kernel** interface: a
VLAN sub-interface, a physical NIC, a veth pair, or a WireGuard/ZeroTier
device moved into a netns. OVS is used in its default `system` datapath,
so packets cross the kernel's OVS module and the host network stack.

The next step is to let external traffic use OVS's **userspace datapath**
— DPDK or AF_XDP — instead of the kernel datapath. That only pays off if
the container-facing leg is also understood, because a userspace bridge
and a container are not automatically connected: the container needs a
device that OVS's userspace datapath can own, and that device has to be
movable into the container's network namespace.

Terminology, because the shorthand is easy to lose:

- **DPDK** (Data Plane Development Kit), not `dpkg`. A userspace poll-mode
  driver that takes a NIC away from the kernel (usually binding it to
  `vfio-pci`) and moves packets without interrupts or the kernel stack.
- **AF_XDP** (address-family XDP), often shortened to XDP. An AF_XDP
  socket plus an XDP program gives a userspace process zero-copy (or
  copy-mode) access to a **kernel-owned** NIC queue. It is not the same
  thing as plain XDP (an eBPF hook) and OVS has no "XDP datapath" — it has
  netdev-datapath ports of type `afxdp`.
- **netdev datapath**: OVS's userspace datapath, selected per bridge with
  `datapath_type=netdev` instead of the default `system`.
- **OVN is unaffected at the control-plane level**: `ovn-controller`
  programs OpenFlow into `ovs-vswitchd`; whether the datapath behind it is
  kernel, DPDK or AF_XDP is an OVS concern. The integration points are
  `br-int`'s datapath type, the segment/localnet bridges, and the
  underlay routing.

## The two legs are separate decisions

| Leg | What it is | Realistic options |
| --- | --- | --- |
| **External / uplink** | The host device that faces the wire | kernel NIC (today), `type=dpdk` (kernel bypass), `type=afxdp` (kernel driver retained), SR-IOV VF passed to a VM/container |
| **Container** | The device the container sees as `eth0` | TAP on a netdev bridge, vhost-user(-client) into a microVM, veth on the kernel datapath, SR-IOV VF passthrough |

Choosing DPDK for the uplink says nothing about the container leg. A
plain container still needs a kernel-visible device; the only practical
one on a netdev bridge is a **TAP** (or a virtio-net device via
vhost-user, which a plain container does not have).

That distinction is the main finding of this note: "the container has a
DPDK interface" is not a thing. Either the *host's* uplink is DPDK/AF_XDP
and the container gets a TAP, or the *container itself* runs a DPDK/AF_XDP
application and owns a port directly (then OVS is not switching it).

## What OVS's netdev datapath can attach

From the OVS docs (`howto/dpdk`, `howto/userspace-tunneling`,
`topics/dpdk/vhost-user`):

- `type=dpdk` with `options:dpdk-devargs=<pci|class=eth,mac=...>` — a NIC
  bound to `vfio-pci`/`uio`.
- `type=dpdkvhostuserclient` (preferred) / `dpdkvhostuser` — a virtio
  backend for QEMU/libvirt, and for VDUSE (`options:vhost-server-path=...`).
- `type=tap` / a TAP device added by name — the simple userspace-to-kernel
  leg. The userspace-tunneling guide explicitly shows
  `ovs-vsctl add-port br-int tap0`.
- `type=afxdp` — AF_XDP sockets on kernel-managed queues.
- Kernel netdevs: the userspace-tunneling guide puts a kernel `eth1` on a
  `datapath_type=netdev` `br-phy`, so a netdev bridge can also carry a
  plain kernel device (used as the underlay/routing leg). It does **not**
  make that device's traffic bypass the kernel.

A bridge is one datapath instance: `datapath_type` is per bridge. Patch
ports connect bridges inside the same datapath; a `system` bridge and a
`netdev` bridge therefore cannot be patched together (to verify on OVS
3.7.1 — if true, mixing them needs a veth pair, one end in each bridge,
and the netdev side loses any bypass benefit on that hop).

## Container-leg options

1. **TAP on a netdev bridge** — the plain-container answer.
   `ip tuntap add` (or let QEMU/OVS create it), `ovs-vsctl add-port`,
   `ip link set … netns <pid>`. Simple, works with any container runtime
   that can run with `--network none`. Cost: the container leg still
   crosses the kernel TUN/TAP layer, so the bypass applies to the external
   leg only. Single queue unless multiqueue TAP is configured.
2. **vhost-user-client + VDUSE** — a virtio-net device provided to the
   *host kernel* by a userspace backend, without QEMU. OVS documents the
   VDUSE backend (`options:vhost-server-path=/dev/vduse/vduse0`). This is
   the closest thing to a full-bypass container leg (virtqueue, no
   TUN/TAP), but it is advanced and version-dependent; must be investigated
   separately.
3. **vhost-user + microVM** (QEMU/Kata/Firecracker) — the well-documented
   path; not a plain container, but the right answer if the workload can
   be a microVM.
4. **veth** — only meaningful on the `system` datapath (today). On a
   netdev bridge it is just a kernel netdev port and buys nothing.
5. **SR-IOV VF / device passthrough** — the container owns the device;
   OVS/OVN are not in the path. Useful as a performance baseline and as
   the "no software switch" comparison, not as an OVS feature.

## PoC A — DPDK uplink, TAP attached to a container

Target: Ubuntu 26.04, OVS 3.7.1 (the test chassis), one spare NIC that the
kernel is allowed to lose. Do **not** bind the management NIC.

### Prerequisites

```sh
# IOMMU on (GRUB), then reboot:
#   intel_iommu=on iommu=pt        (AMD: amd_iommu=on iommu=pt)

# DPDK-enabled OVS (Ubuntu package replaces the kernel-datapath build):
apt-get install -y openvswitch-switch-dpdk dpdk

# 1 GiB of 2 MiB hugepages, mounted:
echo 1024 > /proc/sys/vm/nr_hugepages
mkdir -p /dev/hugepages
mountpoint -q /dev/hugepages || mount -t hugetlbfs hugetlbfs /dev/hugepages

# Which NIC, and what is it bound to?
dpdk-devbind.py --status
lspci -nn | grep -i ethernet
```

Bind the spare NIC away from the kernel (example PCI address `0000:01:00.0`):

```sh
ip link set enp1s0 down
dpdk-devbind.py --bind=vfio-pci 0000:01:00.0
```

### Turn on OVS's DPDK init and create a netdev bridge

```sh
ovs-vsctl set Open_vSwitch . other_config:dpdk-init=true
ovs-vsctl set Open_vSwitch . other_config:dpdk-socket-mem="1024"
# Optional: pin the EAL/PMD cores away from the housekeeping CPUs:
# ovs-vsctl set Open_vSwitch . other_config:dpdk-lcore-mask=0x2
# ovs-vsctl set Open_vSwitch . other_config:pmd-cpu-mask=0xc

systemctl restart ovs-vswitchd
# OVS 3.x exposes dpdk_initialized/dpdk_version when the build supports
# DPDK; older builds only log "EAL:..." on startup.
ovs-vsctl get Open_vSwitch . other_config
ovs-vsctl get Open_vSwitch . dpdk_initialized dpdk_version 2>/dev/null || true

ovs-vsctl add-br br-dpdk -- set bridge br-dpdk datapath_type=netdev
ovs-vsctl add-port br-dpdk dpdk0 -- \
  set Interface dpdk0 type=dpdk options:dpdk-devargs=0000:01:00.0

# A DPDK port has no kernel interface; configure L2/L3 on the bridge:
ovs-vsctl set Interface dpdk0 options:mtu_request=1500
ip link set br-dpdk up
```

### Give the container a TAP on that bridge

```sh
ip tuntap add dev tap-c1 mode tap
ip link set tap-c1 up
ovs-vsctl add-port br-dpdk tap-c1

# Optional but important for OVN port security later: pin the MAC.
ip link set tap-c1 address 02:00:00:00:c1:01

# Start the container with no runtime networking at all:
docker run -d --network none --name c1 <image> sleep infinity
pid=$(docker inspect -f '{{.State.Pid}}' c1)

mkdir -p /var/run/netns
ln -sfn "/proc/$pid/ns/net" /var/run/netns/c1
ip link set tap-c1 netns c1
ip netns exec c1 ip link set tap-c1 name eth0
ip netns exec c1 ip link set eth0 up
ip netns exec c1 ip addr add 192.0.2.2/24 dev eth0
ip netns exec c1 ip route add default via 192.0.2.1
```

The TAP's OVS side stays in the host namespace; moving the device into the
container's netns does not invalidate the TAP fd `ovs-vswitchd` already
holds (to verify explicitly).

### Verify

```sh
ovs-vsctl show
ovs-appctl dpif/show                 # br-dpdk: tap-c1 (tap), dpdk0 (dpdk)
ovs-appctl dpctl/dump-flows
ovs-ofctl dump-ports br-dpdk
ip netns exec c1 ping 192.0.2.1      # or the upstream gateway
```

### What this PoC does and does not prove

- Proves: a plain container can sit on a userspace bridge, and the
  external side is DPDK.
- Does not prove: kernel bypass on the container leg. Every packet still
  crosses TUN/TAP. If that matters, go to VDUSE/vhost-user or accept the
  TAP cost.

## PoC B — AF_XDP uplink

The container-specific, step-by-step version of this section lives in
[AF_XDP in a Proxmox LXC](../afxdp-proxmox-lxc.md). What follows is the
overview and the constraints.

AF_XDP keeps the kernel driver and attaches an XDP program; OVS uses it as
a netdev port (`type=afxdp`). This is the more realistic first experiment
on an LXC / virtual NIC, where DPDK's PCI passthrough usually is not
available. It requires a kernel with AF_XDP support, `libbpf`/`libxdp`
(and an OVS build with AF_XDP), plus `CAP_BPF`/`CAP_NET_ADMIN` and
`RLIMIT_MEMLOCK` in the container.

Sketch (exact options are version-dependent — check
`ovs-vsctl get Interface afxdp0 options` and the OVS release notes):

```sh
ovs-vsctl add-br br-xdp -- set bridge br-xdp datapath_type=netdev
ovs-vsctl add-port br-xdp afxdp0 -- set Interface afxdp0 type=afxdp
```

Limit: veth/NIC driver support for zero-copy varies; a virtual `eth0` in
an LXC is not a representative test of the uplink. Validate on the
Proxmox host's physical NIC, not inside the LXC's veth.

### veth: disable VLAN offload

For tagged traffic over a **veth**, the veth driver's `rx-vlan-offload`
(default on) moves the 802.1Q tag into `skb` metadata before the XDP hook,
so AF_XDP sees the untagged frame and OVS's `trunks=`/`tag=` never matches.
A TAP has `rx-vlan-offload: off [fixed]`, which is why the VM path works
untouched. Each end of a veth pair has its own flags, so disable it on
both:

```sh
ethtool -K <container-side-veth> rxvlan off txvlan off
ethtool -K <host-side-veth> rxvlan off txvlan off
```

The patched LXC (`network_veth_vlan_offload`, PR #4737) exposes this as
`lxc.net.[i].veth.vlan_rx_offload = 0` and `vlan_tx_offload = 0`, applied to
both ends. Proxmox VE ships stock `lxc-pve`, which does not have these keys
yet, so on Proxmox the `.link`/udev route is the working one.

## OVN integration

1. **`br-int` must be `datapath_type=netdev`** if any of its ports are
   DPDK/AF_XDP/vhost-user. `ovn-controller` creates `br-int` with the
   default type when it is missing, so create the bridge (or change its
   type) *before* starting `ovn-controller`, and restart it afterwards.
2. **Every bridge in the same patch chain must use the same datapath
   type.** OVN wires logical ports with patch ports between `br-int` and
   the segment bridges; those bridges must therefore be netdev too, and
   their ports must be port kinds the netdev datapath supports. This is
   the largest gap for `ovn-fabric` (see below).
3. **Geneve underlay routing moves into OVS.** With a DPDK uplink there is
   no kernel route to the remote chassis; OVS needs its own route and ARP
   entries:
   ```sh
   ovs-appctl ovs/route/add 192.168.128.0/24 br-phy
   ovs-appctl tnl/arp/set br-phy 192.168.128.31 <mac>
   ovs-appctl ovs/route/show
   ovs-appctl tnl/arp/show
   ```
   This is exactly the `br-phy` pattern in the userspace-tunneling guide.
4. **`ovn-controller` does not create DPDK ports.** The CMS owns the
   physical/vhost ports; OVN only sees the bridge and the patch/localnet
   ports. `ovn-bridge-mappings` points a logical localnet at a netdev
   bridge just like a system one.
5. **Logical ports are datapath-agnostic.** The OVN NB/SB model
   (`Logical_Switch_Port`, `Logical_Router_Port`, ACLs) does not change.
   What changes is how the chassis realizes the L2 leg.

## Implications for `ovn-fabric`

The generator currently assumes the kernel datapath everywhere:

- `_emit_iface_bindings` creates `br-<shortName>` with `add-br` (default
  `system`) and attaches VLAN sub-interfaces and veth root legs.
- Kernel routers move veth/VLAN/physical devices into netns; those devices
  are kernel netdevs.
- `br-int` is left at whatever `ovn-controller` creates.

A userspace profile is therefore not a one-line flag. The shape of the
change:

1. A per-network/per-host `datapath` setting (`system` default, `netdev`
   opt-in) carried in the IR.
2. `br-int` and every segment bridge emitted with
   `datapath_type=netdev` when the profile is on, and `ovn-controller`
   restarted if the bridge already existed.
3. A port-kind mapping per leg:
   - uplink physical NIC → `type=dpdk` (PCI/devargs in the topology) or
     `type=afxdp`;
   - container/workload NIC → TAP (plain container) or vhost-user
     (microVM);
   - kernel-router transit → needs a design (a veth on a netdev bridge is
     just a kernel netdev port; whether that is acceptable must be tested).
4. Underlay routes/ARP emitted for the DPDK/AF_XDP path.
5. Explicit failure at generation time for combinations that cannot work
   (e.g. a `system` bridge patched to a `netdev` bridge).

Recommended sequencing: prove PoC A and B by hand first, then add the IR
knob and the TAP container leg, and only then touch OVN's `br-int`.

## Where to test

`net-test-hh-b` is an Ubuntu 26.04 **LXC** on Proxmox (`eth0` plus the
`host` veth). DPDK needs a spare PCI NIC, IOMMU and hugepages; passing a
NIC into an LXC is not the same as passing it into a VM, and Proxmox may
not allow it. Practical split:

- **AF_XDP / TAP / OVS configuration**: can be explored inside the LXC,
  but the LXC's `eth0` is a veth — not a meaningful zero-copy uplink.
- **DPDK**: run on the Proxmox host itself, or in a VM with PCI
  passthrough, with a spare NIC. Do not bind the management NIC.

## Open questions to answer on the machine

- [ ] Does OVS 3.7.1 accept a `veth`/kernel netdev as a port on a
      `datapath_type=netdev` bridge (the `br-phy` example suggests yes)?
- [ ] Does OVS reject a patch port between a `system` and a `netdev`
      bridge? If yes, is a veth pair the only bridge between the two
      datapaths, and what does it cost?
- [ ] Does moving an OVS `tap` port into a container netns keep the
      userspace datapath's fd valid across `ip link set … netns`?
- [ ] AF_XDP: which `xdp-mode` (native/generic/skb) does this OVS build
      accept, and does it work on the Proxmox host's NIC?
- [ ] VDUSE: can a `dpdkvhostuserclient` port at `/dev/vduse/vduse0`
      present a virtio-net device to a container netns without QEMU?
- [ ] OVN with `br-int` netdev: do the existing patch ports and Geneve
      ports come up, and what MTU/underlay routing is required?
- [ ] What is the actual throughput/latency delta (TAP vs veth) for a
      container on the same host?

## References

- OVS, *Using Open vSwitch with DPDK*: <https://docs.openvswitch.org/en/latest/howto/dpdk/>
- OVS, *Connecting VMs Using Tunnels (Userspace)*: <https://docs.openvswitch.org/en/latest/howto/userspace-tunneling/>
- OVS, *DPDK vHost User Ports*: <https://docs.openvswitch.org/en/latest/topics/dpdk/vhost-user/>
- OVS, *DPDK Physical Ports*: <https://docs.openvswitch.org/en/latest/topics/dpdk/phy/>
- OVS FAQ, *Basic Configuration* (DPDK ports, tap ports): <https://docs.openvswitch.org/en/latest/faq/configuration/>
