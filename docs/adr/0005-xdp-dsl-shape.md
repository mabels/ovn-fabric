# ADR 0005: XDP in the topology DSL — `xdp` interfaces and VLAN parents

Status: idea only — config/DSL sketch, no generator change.

## Context

We want the test chassis (`net-test-hh-b`) to carry five OVN segment VLANs
over AF_XDP: `HOME_NETWORK` (1128), `NEIGHBOR_NETWORK` (1130),
`CONTROL_PLANE_NETWORK` (2143), `US_NETWORK` (1131), `DMZ_NETWORK` (1129).

A NIC owned by AF_XDP cannot also be the parent of working kernel VLAN
sub-interfaces: XDP runs before the kernel's VLAN stack, so
`parent.<vlan>` devices stop seeing traffic. The VLAN split has to move
into OVS. The test container therefore has two parents:

- `ovnxdp` — the five OVN segments, AF_XDP in OVS;
- `ovnkern` — the kernel-router WAN VLANs (`voda-avm` 2280, `voda-modem`
  2281, `starlink` 2282), which live in netns, not OVS.

## The interface shapes

The one structural change: `vlanParent` becomes an interface **descriptor**
instead of a name. That lets the same `vlan` view ride either a plain NIC
(kernel VLAN, as today) or an XDP port (OVS VLAN).

```ts
type XdpMode = "best-effort" | "native-with-zerocopy" | "native" | "generic";

type IfaceParent =
  | { kind: "physical"; name: string }
  | { kind: "xdp"; name: string; mode?: XdpMode; nRxq?: number };

export type InterfaceKind =
  | { kind: "vlan"; vlanParent: IfaceParent; vlanId: number; ifaceName?: string }
  | { kind: "physical"; name: string }
  | { kind: "xdp"; name: string; mode?: XdpMode; nRxq?: number }
  | /* the existing veth / wireguard / zerotier / dummy variants */;
```

Defaults are resolved in `src/ir.ts`, not in the deployer — same rule as
`shortName`, `mac`, `gatewayChassis` and `routes`:

- `mode`: `"best-effort"` (OVS tries zero-copy → native → generic);
- `nRxq`: `1`.

A topology can therefore write just `{ kind: "xdp", name: "ovnxdp" }`.

## Test runtime

```ts
function runtimeArgs() {
  if (runtime().args["test"]) {
    const prod = prodRuntimeArgs();

    const XDP_PARENT = {
      kind: "xdp",
      name: "ovnxdp",
      mode: "generic", // veth in the LXC: copy mode, not zero-copy
      nRxq: 1,
    } as const;
    const KERN_PARENT = { kind: "physical", name: "ovnkern" } as const;

    return {
      ...prod,
      CHASSIS_NAME: "net-test-hh-b",
      CHASSIS_IPV4: IPv4.parse("192.168.128.31"),

      HOME_NETWORK: { kind: "vlan", vlanParent: XDP_PARENT, vlanId: 1128 },
      NEIGHBOR_NETWORK: { kind: "vlan", vlanParent: XDP_PARENT, vlanId: 1130 },
      CONTROL_PLANE_NETWORK: { kind: "vlan", vlanParent: XDP_PARENT, vlanId: 2143 },
      US_NETWORK: { kind: "vlan", vlanParent: XDP_PARENT, vlanId: 1131 },
      DMZ_NETWORK: { kind: "vlan", vlanParent: XDP_PARENT, vlanId: 1129 },

      VODA_AVM_NETWORK: { kind: "vlan", vlanParent: KERN_PARENT, vlanId: 2280 },
      VODA_MODEM_NETWORK: { kind: "vlan", vlanParent: KERN_PARENT, vlanId: 2281 },
      STARLINK_NETWORK: { kind: "vlan", vlanParent: KERN_PARENT, vlanId: 2282 },
    };
  }
  return prodRuntimeArgs();
}
```

Prod keeps the same descriptor shape with a physical parent, so the config
does not branch on the datapath:

```ts
const BASE_PARENT = { kind: "physical", name: CHASSIS_BASE_IFACE } as const;
HOME_NETWORK: BASE_PARENT,
NEIGHBOR_NETWORK: { kind: "vlan", vlanParent: BASE_PARENT, vlanId: 130 },
// ...
```

## How the generator would realize the two cases

| Config | Realization |
| --- | --- |
| `vlan` on `physical` | `ip link add link <parent> name <parent>.<vid> type vlan id <vid>`; one OVS bridge per segment (today) |
| `vlan` on `xdp` | no `<parent>.<vid>` device; one netdev trunk bridge with `<parent>` as `type=afxdp`, and one OVS leg per segment carrying `tag=<vid>` (or the trunk port carrying `trunks=[...]`) |

The `xdp` port is emitted once per `name`, however many segments reference
it:

```sh
ovs-vsctl add-br br-xdp-trunk -- set bridge br-xdp-trunk datapath_type=netdev
ovs-vsctl add-port br-xdp-trunk ovnxdp -- \
  set Interface ovnxdp type=afxdp \
      options:xdp-mode=generic options:n_rxq=1
```

Each segment then gets a `tag=1128`/`1130`/`2143`/`1131`/`1129` leg. The
exact leg kind (patch port, TAP, or veth into the existing segment bridge)
is the generator's choice and is what the current ADR 0004 investigation
has to settle.

## Alternative considered

`kind: "xdp"` with the VLAN on the same object:

```ts
{ kind: "xdp", name: "ovnxdp", vlan: 1128, mode: "generic", nRxq: 1 }
```

It reads compactly, but the parent descriptor (`name`/`mode`/`nRxq`) is
repeated for every segment, and two segments that disagree on `mode` make
the "one port" grouping ambiguous. Keeping `vlan` as its own view with an
`IfaceParent` reference avoids both problems and uses the same word for
the physical and XDP cases.

## Open questions

- [ ] Which leg connects a tagged XDP segment to the existing segment
      bridge: patch port (if `system`/`netdev` patch ports are rejected,
      this is out), TAP, or veth?
- [ ] Does `datapath_type=netdev` have to cover `br-int` too, or can the
      XDP trunk be a separate bridge bridged to the system side by a veth?
- [ ] Should `mode`/`nRxq` be overridable per segment, or only per parent?
- [ ] Do the kernel-router WAN VLANs stay on `ovnkern` permanently, or does
      the kernel router also move onto the userspace datapath later?
