#!/bin/sh
# Client netns on the OVN trunk VLANs, matching the fleet's segment setup.
set -eu

OVN_MAC=52:54:00:00:01:01

iface_by_mac() {
  ip -o link | awk -v mac="$1" 'tolower($0) ~ tolower(mac) {gsub(":","",$2); print $2; exit}'
}

if [ "${1:-up}" = "down" ]; then
  for vlan in 1128 1129 1130 1131 128 129 130 131; do
    ip netns del "ns-$vlan" 2>/dev/null || true
  done
  echo "segment netns removed"
  exit 0
fi

VLAN_PARENT="$(iface_by_mac "$OVN_MAC")"
[ -n "$VLAN_PARENT" ] || { echo "OVN NIC ($OVN_MAC) not found" >&2; exit 1; }
ip link set dev "$VLAN_PARENT" up

for vlan in 1128 1129 1130 1131; do
  seg=$((vlan - 1000))
  ns="ns-$vlan"
  dev="$VLAN_PARENT.$vlan"
  ip netns del "$ns" 2>/dev/null || true
  ip link del "$dev" 2>/dev/null || true
  ip netns add "$ns"
  ip link add link "$VLAN_PARENT" name "$dev" type vlan id "$vlan"
  ip link set "$dev" netns "$ns"
  ip netns exec "$ns" ip addr add "192.168.$seg.99/24" dev "$dev"
  ip netns exec "$ns" ip link set "$dev" up
  ip netns exec "$ns" ip route add default via "192.168.$seg.1" dev "$dev"
  echo "$ns: 192.168.$seg.99/24, default via 192.168.$seg.1"
done

for vlan in 128 129 130 131; do
  seg=$vlan
  ns="ns-$vlan"
  dev="$VLAN_PARENT.$vlan"
  ip netns del "$ns" 2>/dev/null || true
  ip link del "$dev" 2>/dev/null || true
  ip netns add "$ns"
  ip link add link "$VLAN_PARENT" name "$dev" type vlan id "$vlan"
  ip link set "$dev" netns "$ns"
  ip netns exec "$ns" ip addr add "192.168.$seg.99/24" dev "$dev"
  ip netns exec "$ns" ip link set "$dev" up
  ip netns exec "$ns" ip route add default via "192.168.$seg.1" dev "$dev"
  echo "$ns: 192.168.$seg.99/24, default via 192.168.$seg.1"
done
