"""A small IR for the runtime's tests: a central host, a chassis, a router
with a leg on `home` and one on `backbone`, and a kernel router with a WAN
side — in the logical form (ADR 0011): an `ip.net` per collision domain,
every address a LogicalIP, the WAN's next hop an AddrRef pinned on its line.

Not a test module itself (no `test_` prefix): shared by fabric/test_plan.py
and fabric/services/test_apply.py.
"""

from __future__ import annotations

from protocol import generated as pt
from protocol.hydrate import hydrate_as


def _ls_interface(host: str, iface: dict[str, object]) -> pt.OvnLsInterface:
    return hydrate_as(pt.OvnLsInterface, {"host": host, "iface": iface})


def _kr_interface(host: str, iface: dict[str, object]) -> pt.KernelRouterInterface:
    return hydrate_as(pt.KernelRouterInterface, {"host": host, "iface": iface})


def _net(domain: str, ipv4: str) -> pt.IpNetNode:
    return pt.IpNetNode(
        id=f"ipnet:{domain}",
        kind="ip.net",
        key=pt.IpNetKey(domain=domain),
        data=pt.IpNetData(
            ipv4=ipv4,
            allocator=pt.Allocator(
                ipv4=pt.AllocatorStrategy.low_high, ipv6=pt.AllocatorStrategy.low_high
            ),
        ),
    )


def _ip(domain: str, owner: str, host: int | None = None) -> pt.LogicalIP:
    return pt.LogicalIP(net=f"ipnet:{domain}", owner=owner, host=host)


LRP_HOME = "ovnrouter:router-home|lrp:lrp-router-home-left"
LRP_BACKBONE = "ovnrouter:router-home|lrp:lrp-router-home-right"
KR_LEFT = "kernelrouter:kernel-0|side:left"
KR_RIGHT = "kernelrouter:kernel-0|side:right"

NODES: list[pt.Model] = [
    _net("ls:home", "192.168.1.0/24"),
    _net("ls:backbone", "172.22.0.0/16"),
    _net("ls:transit-kernel-0", "10.99.0.0/28"),
    _net(KR_RIGHT, "192.168.132.0/24"),
    pt.InfraHostNode(
        id="host:central-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="central-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.1",
            ovnRole=pt.OvnRole.central,
            encapIp="10.0.0.1",
            os=pt.HostOs(name="ubuntu", version="26.04", packageManager=pt.PackageManager.apt),
            reconcile=pt.HostReconcile(hostInterfaces=["lo"]),
        ),
    ),
    pt.InfraHostNode(
        id="host:chassis-1",
        kind="infra.host",
        key=pt.InfraHostKey(host="chassis-1"),
        data=pt.InfraHostData(
            connectAddress="10.0.0.2",
            ovnRole=pt.OvnRole.chassis,
            encapIp="10.0.0.2",
            os=pt.HostOs(name="ubuntu", version="26.04", packageManager=pt.PackageManager.apt),
            reconcile=pt.HostReconcile(hostInterfaces=["lo"]),
        ),
    ),
    pt.OvnLsNode(
        id="ls:home",
        kind="ovn.ls",
        key=pt.OvnLsKey(name="home"),
        data=pt.OvnLsData(
            interfaces=[
                _ls_interface(
                    host="chassis-1",
                    iface={
                        "kind": "vlan",
                        "vlanParent": {"kind": "physical", "name": "eth0"},
                        "vlanId": 129,
                        "bridge": "br-home",
                    },
                ),
            ],
        ),
    ),
    pt.OvnLsNode(
        id="ls:backbone",
        kind="ovn.ls",
        key=pt.OvnLsKey(name="backbone"),
        data=pt.OvnLsData(interfaces=[]),
    ),
    pt.OvnLrNode(
        id="ovnrouter:router-home",
        kind="ovn.lr",
        key=pt.OvnLrKey(name="router-home"),
        data=pt.OvnLrData(options=pt.OvnLrOptions(mac_binding_age_threshold=300)),
    ),
    pt.OvnLrpNode(
        id=LRP_HOME,
        kind="ovn.lrp",
        key=pt.OvnLrpKey(ovnrouter="router-home", name="lrp-router-home-left"),
        data=pt.OvnLrpData(
            l2Segment="ls:home",
            addresses=[_ip("ls:home", LRP_HOME, 1)],
            mac="00:00:00:00:01:01",
            gatewayChassis="host:chassis-1",
        ),
    ),
    pt.OvnLrpNode(
        id=LRP_BACKBONE,
        kind="ovn.lrp",
        key=pt.OvnLrpKey(ovnrouter="router-home", name="lrp-router-home-right"),
        data=pt.OvnLrpData(
            l2Segment="ls:backbone",
            # allocated by the runtime: the backbone's first free host
            addresses=[_ip("ls:backbone", LRP_BACKBONE)],
            mac="00:00:00:00:01:02",
            gatewayChassis=None,
        ),
    ),
    pt.KernelRouterNode(
        id="kernelrouter:kernel-0",
        kind="kernel.router",
        key=pt.KernelRouterKey(name="kernel-0", side=None),
        data=pt.KernelRouterData(host="host:chassis-1"),
    ),
    pt.KernelRouterNode(
        id=KR_LEFT,
        kind="kernel.router",
        key=pt.KernelRouterKey(name="kernel-0", side=pt.Side.left),
        data=pt.KernelRouterData(
            host="host:chassis-1", ipaddrs=[_ip("ls:transit-kernel-0", KR_LEFT, 2)]
        ),
    ),
    pt.KernelRouterNode(
        id=KR_RIGHT,
        kind="kernel.router",
        key=pt.KernelRouterKey(name="kernel-0", side=pt.Side.right),
        data=pt.KernelRouterData(
            host="host:chassis-1",
            ipaddrs=[_ip(KR_RIGHT, KR_RIGHT, 93)],
            routes=[
                pt.KernelRouterRoute(
                    dst="0.0.0.0/0",
                    # the upstream router: carried by nothing of the fabric
                    via=pt.AddrRef(
                        net=f"ipnet:{KR_RIGHT}",
                        owner=f"{KR_RIGHT}|route:0.0.0.0/0|via",
                        family=pt.Family.ipv4,
                        host=1,
                    ),
                )
            ],
            ifaces=[
                _kr_interface(
                    host="chassis-1",
                    iface={
                        "kind": "vlan",
                        "vlanParent": {"kind": "physical", "name": "eth0"},
                        "vlanId": 2280,
                    },
                ),
            ],
        ),
    ),
]
