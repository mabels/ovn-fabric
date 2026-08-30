#!/bin/sh
# test-segments.sh — three client netns on the home/neighbor/usa OVN
# segments, so you can exercise the tunnel routers end to end:
#
#   ns-1128 -> 192.168.128.99/24 (home),     default via 192.168.128.1
#   ns-1130 -> 192.168.130.99/24 (neighbor), default via 192.168.130.1
#   ns-1131 -> 192.168.131.99/24 (usa),      default via 192.168.131.1
#
# Each client is a vlan sub-interface of the host's eth0 on the segment's
# vlan — same tagged L2 the deployer's eth0.<vlan> bridge ports live on,
# so the client reaches the segment's OVN localnet bridge.
#
# usage: ./test-segments.sh up    (or: ./test-segments.sh down)
#
# NOTE: if the topology is already applied, the deployer has moved
# eth0.<vlan> into the segment bridges — run this BEFORE applying, or
# delete the bridge's own eth0.<vlan> ports first (or use the veth-into-
# bridge variant instead).

set -eu

VLAN_PARENT=eth0

if [ "${1:-up}" = "down" ]; then
  for vlan in 1128 1130 1131; do
    ip netns del "ns-$vlan" 2>/dev/null || true
  done
  echo "segment netns removed"
  exit 0
fi

for vlan in 1128 1130 1131; do
  seg=$((vlan - 1000))          # 1128->128, 1130->130, 1131->131
  ns="ns-$vlan"
  dev="eth0.$vlan"
  ip netns add "$ns"
  ip link add link "$VLAN_PARENT" name "$dev" type vlan id "$vlan"
  ip link set "$dev" netns "$ns"
  ip netns exec "$ns" ip addr add "192.168.$seg.99/24" dev "$dev"
  ip netns exec "$ns" ip link set "$dev" up
  ip netns exec "$ns" ip route add default via "192.168.$seg.1" dev "$dev"
  echo "ns-$vlan: 192.168.$seg.99/24, default via 192.168.$seg.1"
done
