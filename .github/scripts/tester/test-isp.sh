#!/bin/sh
# Simulated ISP for the QEMU tester: a WAN VLAN on the OVN trunk plus
# forwarding/NAT to the host NAT interface, and optional RA/SLAAC.
set -eu

ISP_NS=ns-isp
HOST_MAC=52:54:00:00:00:01
OVN_MAC=52:54:00:00:01:01

iface_by_mac() {
  ip -o link | awk -v mac="$1" 'tolower($0) ~ tolower(mac) {gsub(":","",$2); print $2; exit}'
}

if [ "${1:-up}" = "down" ]; then
  ip netns del "$ISP_NS" 2>/dev/null || true
  ip link del isp-root 2>/dev/null || true
  echo "ns-isp removed"
  exit 0
fi

WAN_PARENT="$(iface_by_mac "$OVN_MAC")"
HOST_IF="$(iface_by_mac "$HOST_MAC")"
[ -n "$WAN_PARENT" ] || { echo "OVN NIC ($OVN_MAC) not found" >&2; exit 1; }
[ -n "$HOST_IF" ] || { echo "host NIC ($HOST_MAC) not found" >&2; exit 1; }

WAN_VLAN=2280
WAN_IFACE="$WAN_PARENT.$WAN_VLAN"
WAN_GW=192.168.132.1/24
WAN_V6_ADDR=fd00::1/64
WAN_V6_PREFIX=fd00::/64
OUTER=isp-out
ROOT_OUTER=isp-root

ip netns del "$ISP_NS" 2>/dev/null || true
ip link del "$ROOT_OUTER" 2>/dev/null || true
ip link del "$WAN_IFACE" 2>/dev/null || true

ip netns add "$ISP_NS"
ip link add link "$WAN_PARENT" name "$WAN_IFACE" type vlan id "$WAN_VLAN"
ip link set "$WAN_IFACE" netns "$ISP_NS"

ip link add "$ROOT_OUTER" type veth peer name "$OUTER"
ip link set "$OUTER" netns "$ISP_NS"
ip addr add 10.200.0.1/30 dev "$ROOT_OUTER"
ip link set "$ROOT_OUTER" up

sysctl -w net.ipv4.ip_forward=1 >/dev/null
iptables -t nat -A POSTROUTING -o "$HOST_IF" -j MASQUERADE 2>/dev/null || true
iptables -A FORWARD -i "$ROOT_OUTER" -o "$HOST_IF" -j ACCEPT 2>/dev/null || true
iptables -A FORWARD -i "$HOST_IF" -o "$ROOT_OUTER" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true

ip netns exec "$ISP_NS" ip link set lo up
ip netns exec "$ISP_NS" ip link set "$OUTER" up
ip netns exec "$ISP_NS" ip addr add 10.200.0.2/30 dev "$OUTER"
ip netns exec "$ISP_NS" ip route add default via 10.200.0.1
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

echo "ns-isp up: $WAN_GW + $WAN_V6_ADDR on $WAN_IFACE (parent $WAN_PARENT), egress $OUTER via $ROOT_OUTER -> $HOST_IF"
