#!/bin/sh
# Simulated ISP for the network test.
#
# Two modes:
#   --use-netns  (default) a separate outer NIC is moved into ns-isp, which
#                DHCPs it, NATs, and advertises the WAN prefix. Needs a second
#                NIC (QEMU second cloud NIC, or a second EC2 ENI).
#   --use-host   single upstream NIC (EC2): keep the upstream in the host
#                namespace, put VLAN 2280 on the L2 trunk, give it the WAN
#                gateway addresses, forward, and MASQUERADE out the upstream.
#
# Options:
#   --trunk DEV   L2/OVN trunk parent (default: QEMU OVN MAC, else veth-l2)
#   --outer DEV   upstream interface (default: MAC in netns mode, else the
#                 default-route interface)
#
#   test-isp.sh [--use-netns|--use-host] [--trunk DEV] [--outer DEV] [up|down]
set -eu

ISP_NS=ns-isp
OVN_MAC=52:54:00:00:01:01
ISP_MAC=52:54:00:00:03:01

WAN_VLAN=2280
WAN_GW=192.168.132.1/24
WAN_V6_ADDR=fd00::1/64
WAN_V6_PREFIX=fd00::/64

MODE=netns
TRUNK=
OUTER=
ACTION=up

while [ $# -gt 0 ]; do
  case "$1" in
    --use-netns) MODE=netns ;;
    --use-host) MODE=host ;;
    --trunk) TRUNK=$2; shift ;;
    --outer) OUTER=$2; shift ;;
    up|down) ACTION=$1 ;;
    *)
      echo "usage: $0 [--use-netns|--use-host] [--trunk DEV] [--outer DEV] [up|down]" >&2
      exit 2
      ;;
  esac
  shift
done

iface_by_mac() {
  ip -o link | awk -v mac="$1" 'tolower($0) ~ tolower(mac) {gsub(":","",$2); print $2; exit}'
}

# The upstream is whatever the default route actually uses; no naming needed.
# Fall back to `ip route get` for setups where the default is split/policy-routed.
default_route_iface() {
  ip route show default 2>/dev/null |
    awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i+1); exit }}'
}
upstream_iface() {
  dev=$(default_route_iface)
  [ -n "$dev" ] && { echo "$dev"; return; }
  ip route get 1.1.1.1 2>/dev/null |
    awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i+1); exit }}'
}

detect_trunk() {
  if [ -n "$TRUNK" ]; then
    echo "$TRUNK"
    return
  fi
  dev=$(iface_by_mac "$OVN_MAC")
  [ -n "$dev" ] || dev=veth-l2
  [ -n "$dev" ] || {
    echo "no L2 trunk found (pass --trunk DEV)" >&2
    exit 1
  }
  echo "$dev"
}

# ── host mode: route between VLAN 2280 on the trunk and the upstream ──
host_down() {
  PARENT=${TRUNK:-veth-l2}
  WAN_IFACE="$PARENT.$WAN_VLAN"
  OUT=${OUTER:-$(upstream_iface)}
  pkill -f "radvd.*$WAN_IFACE" 2>/dev/null || true
  [ -n "$OUT" ] && iptables -t nat -D POSTROUTING -s 192.168.132.0/24 -o "$OUT" -j MASQUERADE 2>/dev/null || true
  [ -n "$OUT" ] && ip6tables -t nat -D POSTROUTING -s fd00::/64 -o "$OUT" -j MASQUERADE 2>/dev/null || true
  ip link del "$WAN_IFACE" 2>/dev/null || true
  echo "host ISP down: $WAN_IFACE"
}

host_up() {
  PARENT=$(detect_trunk)
  OUT=${OUTER:-$(upstream_iface)}
  [ -n "$OUT" ] || {
    echo "no upstream interface (pass --outer DEV)" >&2
    exit 1
  }
  WAN_IFACE="$PARENT.$WAN_VLAN"

  ip link del "$WAN_IFACE" 2>/dev/null || true
  ip link add link "$PARENT" name "$WAN_IFACE" type vlan id "$WAN_VLAN"
  ip link set "$WAN_IFACE" up
  ip addr add "$WAN_GW" dev "$WAN_IFACE"
  ip -6 addr add "$WAN_V6_ADDR" dev "$WAN_IFACE"

  sysctl -w net.ipv4.ip_forward=1 >/dev/null
  sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null
  sysctl -w net.ipv6.conf.all.accept_ra=0 >/dev/null
  sysctl -w net.ipv6.conf.all.autoconf=0 >/dev/null
  sysctl -w "net.ipv6.conf.$WAN_IFACE.accept_ra=0" >/dev/null 2>&1 || true

  iptables -t nat -C POSTROUTING -s 192.168.132.0/24 -o "$OUT" -j MASQUERADE 2>/dev/null ||
    iptables -t nat -A POSTROUTING -s 192.168.132.0/24 -o "$OUT" -j MASQUERADE
  ip6tables -t nat -C POSTROUTING -s fd00::/64 -o "$OUT" -j MASQUERADE 2>/dev/null ||
    ip6tables -t nat -A POSTROUTING -s fd00::/64 -o "$OUT" -j MASQUERADE 2>/dev/null || true

  if command -v radvd >/dev/null 2>&1; then
    cat >"/tmp/radvd-$WAN_IFACE.conf" <<EOF
interface $WAN_IFACE {
  AdvSendAdvert on;
  prefix $WAN_V6_PREFIX {
    AdvOnLink on;
    AdvAutonomous on;
    AdvRouterAddr on;
  };
};
EOF
    pkill -f "radvd.*$WAN_IFACE" 2>/dev/null || true
    radvd -C "/tmp/radvd-$WAN_IFACE.conf"
  fi

  echo "host ISP up: $WAN_GW + $WAN_V6_ADDR on $WAN_IFACE (parent $PARENT), NAT out $OUT"
}

# ── netns mode: separate outer NIC inside ns-isp ──
netns_down() {
  ip netns del "$ISP_NS" 2>/dev/null || true
  echo "$ISP_NS removed"
}

netns_up() {
  WAN_PARENT=$(detect_trunk)
  OUT=${OUTER:-$(iface_by_mac "$ISP_MAC")}
  [ -n "$WAN_PARENT" ] || {
    echo "L2 trunk not found" >&2
    exit 1
  }
  [ -n "$OUT" ] || {
    echo "outer NIC ($ISP_MAC) not found; pass --outer DEV" >&2
    exit 1
  }

  WAN_IFACE="$WAN_PARENT.$WAN_VLAN"
  ip netns del "$ISP_NS" 2>/dev/null || true
  ip link del "$WAN_IFACE" 2>/dev/null || true

  ip link set "$WAN_PARENT" up
  ip netns add "$ISP_NS"
  ip link add link "$WAN_PARENT" name "$WAN_IFACE" type vlan id "$WAN_VLAN"
  ip link set "$WAN_IFACE" netns "$ISP_NS"

  ip addr flush dev "$OUT" 2>/dev/null || true
  ip link set "$OUT" down
  ip link set "$OUT" netns "$ISP_NS"

  ip netns exec "$ISP_NS" ip link set lo up
  ip netns exec "$ISP_NS" ip link set "$OUT" up
  ip netns exec "$ISP_NS" dhcpcd -w -4 "$OUT" || true
  ip netns exec "$ISP_NS" ip addr add "$WAN_GW" dev "$WAN_IFACE"
  ip netns exec "$ISP_NS" ip -6 addr add "$WAN_V6_ADDR" dev "$WAN_IFACE"
  ip netns exec "$ISP_NS" ip link set "$WAN_IFACE" up

  ip netns exec "$ISP_NS" sysctl -w net.ipv4.ip_forward=1 >/dev/null
  ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null
  ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.accept_ra=0 >/dev/null
  ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.autoconf=0 >/dev/null
  ip netns exec "$ISP_NS" iptables -t nat -A POSTROUTING -o "$OUT" -j MASQUERADE 2>/dev/null || true
  ip netns exec "$ISP_NS" ip6tables -t nat -A POSTROUTING -o "$OUT" -j MASQUERADE 2>/dev/null || true

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

  echo "ns-isp up: $WAN_GW + $WAN_V6_ADDR on $WAN_IFACE (parent $WAN_PARENT), egress $OUT"
}

case "$MODE:$ACTION" in
  host:down) host_down ;;
  host:up) host_up ;;
  netns:down) netns_down ;;
  netns:up) netns_up ;;
esac
