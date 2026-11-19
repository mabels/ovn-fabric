#!/usr/bin/env bash
# Geneve L2 bridge demo — HOST B (192.168.128.33).
# Plain Open vSwitch + a Geneve tunnel to host A. No OVN, no ovn-fabric.
#
#   ./scripts/geneve-demo-b.sh up     create br-gen, tunnel to 192.168.128.31, addresses
#   ./scripts/geneve-demo-b.sh down   remove the VLAN subif and the bridge
#
# Untagged segment: 10.222.0.0/24      (A=.1, B=.2)
# Tagged  VLAN 4094: 10.222.94.0/24    (A=.1, B=.2) — the tag rides the tunnel.
set -euo pipefail

LOCAL_IP=192.168.128.33
PEER_IP=192.168.128.31
VNI=4099
BR=br-gen
TUN=gen0
MTU=1400
UNTAGGED=10.222.0.2/24
VLAN_ID=4094
TAGGED=10.222.94.2/24

down() {
  ip link del "$BR.$VLAN_ID" 2>/dev/null || true
  ovs-vsctl --if-exists del-br "$BR"
  echo "host B: br-gen removed"
}

up() {
  modprobe openvswitch 2>/dev/null || true
  modprobe geneve 2>/dev/null || true

  ovs-vsctl --if-exists del-br "$BR"
  ovs-vsctl add-br "$BR"
  # no controller -> act as a plain MAC-learning switch
  ovs-vsctl set bridge "$BR" fail-mode=standalone

  ovs-vsctl add-port "$BR" "$TUN" -- set interface "$TUN" \
    type=geneve \
    options:local_ip="$LOCAL_IP" \
    options:remote_ip="$PEER_IP" \
    options:key="$VNI" \
    mtu_request="$MTU"

  ip link set "$BR" up
  ip link set "$BR" mtu "$MTU"
  ip addr flush dev "$BR" 2>/dev/null || true
  ip addr add "$UNTAGGED" dev "$BR"

  ip link del "$BR.$VLAN_ID" 2>/dev/null || true
  ip link add link "$BR" name "$BR.$VLAN_ID" type vlan id "$VLAN_ID"
  ip link set "$BR.$VLAN_ID" up mtu "$MTU"
  ip addr flush dev "$BR.$VLAN_ID" 2>/dev/null || true
  ip addr add "$TAGGED" dev "$BR.$VLAN_ID"

  echo "host B up: $UNTAGGED | vlan$VLAN_ID $TAGGED -> geneve $PEER_IP vni $VNI"
  ovs-vsctl show
}

case "${1:-up}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 [up|down]" >&2; exit 2 ;;
esac
