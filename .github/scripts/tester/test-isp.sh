#!/bin/sh
# Simulated ISP for the QEMU tester: a WAN VLAN on the OVN trunk plus a
# dedicated second cloud NIC as the outer leg, forwarding/NAT, and RA/SLAAC.
set -eu

ISP_NS=ns-isp
OVN_MAC=52:54:00:00:01:01
ISP_MAC=52:54:00:00:03:01

iface_by_mac() {
  ip -o link | awk -v mac="$1" 'tolower($0) ~ tolower(mac) {gsub(":","",$2); print $2; exit}'
}

if [ "${1:-up}" = "down" ]; then
  ip netns del "$ISP_NS" 2>/dev/null || true
  echo "ns-isp removed"
  exit 0
fi

WAN_PARENT="$(iface_by_mac "$OVN_MAC")"
OUTER="$(iface_by_mac "$ISP_MAC")"
[ -n "$WAN_PARENT" ] || { echo "OVN NIC ($OVN_MAC) not found" >&2; exit 1; }
[ -n "$OUTER" ] || { echo "ISP cloud NIC ($ISP_MAC) not found" >&2; exit 1; }

WAN_VLAN=2280
WAN_IFACE="$WAN_PARENT.$WAN_VLAN"
WAN_GW=192.168.132.1/24
WAN_V6_ADDR=fd00::1/64
WAN_V6_PREFIX=fd00::/64

ip netns del "$ISP_NS" 2>/dev/null || true
ip link del "$WAN_IFACE" 2>/dev/null || true

ip link set "$WAN_PARENT" up
ip netns add "$ISP_NS"
ip link add link "$WAN_PARENT" name "$WAN_IFACE" type vlan id "$WAN_VLAN"
ip link set "$WAN_IFACE" netns "$ISP_NS"

ip addr flush dev "$OUTER" 2>/dev/null || true
ip link set "$OUTER" down
ip link set "$OUTER" netns "$ISP_NS"

ip netns exec "$ISP_NS" ip link set lo up
ip netns exec "$ISP_NS" ip link set "$OUTER" up
ip netns exec "$ISP_NS" dhcpcd -w -4 "$OUTER"
ip netns exec "$ISP_NS" ip addr add "$WAN_GW" dev "$WAN_IFACE"
ip netns exec "$ISP_NS" ip -6 addr add "$WAN_V6_ADDR" dev "$WAN_IFACE"
ip netns exec "$ISP_NS" ip link set "$WAN_IFACE" up

ip netns exec "$ISP_NS" sysctl -w net.ipv4.ip_forward=1 >/dev/null
ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null
ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.accept_ra=0 >/dev/null
ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.autoconf=0 >/dev/null
ip netns exec "$ISP_NS" iptables -t nat -A POSTROUTING -o "$OUTER" -j MASQUERADE 2>/dev/null || true
ip netns exec "$ISP_NS" ip6tables -t nat -A POSTROUTING -o "$OUTER" -j MASQUERADE 2>/dev/null || true

if command -v radvd >/dev/null 2>&1; then
  cat >"/tmp/radvd-$ISP_NS.conf" <<EOF
interface $WAN_IFACE {
  AdvSendAdvert on;
  prefix $WAN_V6_PREFIX {
    AdvOnLink on;
    AdvAutonomous on;
    AdvRouterAddr on;
  };
};
EOF
  ip netns exec "$ISP_NS" radvd -C "/tmp/radvd-$ISP_NS.conf"
fi

echo "ns-isp up: $WAN_GW + $WAN_V6_ADDR on $WAN_IFACE (parent $WAN_PARENT), egress $OUTER via dhcpcd + MASQUERADE"
