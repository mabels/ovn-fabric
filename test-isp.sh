#!/bin/sh
# test-isp.sh — simulate the ISP side so the mullvad/zerotier tunnels on
# the test router can egress to the real internet.
#
# Topology:
#   ns-isp (the "fake ISP"): 192.168.132.1/24 + <v6>::1/64 on ens18.2280
#     |  <- same physical vlan-2280 L2 as the voda-avm netns's WAN iface
#     |     so voda-avm's default (192.168.132.1) lands here
#     +-- RA / SLAAC on the WAN iface: advertises <v6prefix>/64 so WAN
#     |     clients auto-configure IPv6 (radvd)
#     +-- ens18 / eth0 outer leg (moved INTO ns-isp, DHCP-addressed)
#     +-- ip_forward + MASQUERADE (v4 + v6) out the outer leg
#
# usage: ./test-isp.sh up    (or: ./test-isp.sh down)
#
# Adjust WAN_PARENT/OUTER to your host; moving OUTER into ns-isp removes
# it from root — if your management session rides on it, it drops.
# Needs radvd (apt-get installs it) and a v6-capable egress for NAT6.

set -eu

ISP_NS=ns-isp
WAN_PARENT=ens18          # physical iface the WAN vlan is created on
WAN_VLAN=2280
WAN_IFACE="$WAN_PARENT.$WAN_VLAN"
WAN_GW=192.168.132.1/24
WAN_V6_ADDR=fd00::1/64       # static IPv6 on the WAN iface
WAN_V6_PREFIX=fd00::/64      # /64 the ISP advertises (SLAAC)
OUTER=ens18                # real uplink egress (v4 + optional v6 via dhclient)

if [ "${1:-up}" = "down" ]; then
  ip netns del "$ISP_NS" 2>/dev/null || true
  echo "ns-isp removed"
  exit 0
fi

ip netns add "$ISP_NS"

# Create the WAN vlan off the parent (ens18), then move the parent/OUTER
# into the netns and bring it UP first — ens18.2280 is a vlan that RIDES
# on ens18, so ens18 must be up before the WAN vlan carries traffic.
ip link add link "$WAN_PARENT" name "$WAN_IFACE" type vlan id "$WAN_VLAN"
ip link set "$WAN_IFACE" netns "$ISP_NS"
ip link set "$OUTER" netns "$ISP_NS"
ip netns exec "$ISP_NS" ip link set "$OUTER" up

# WAN leg — v4 gateway + static IPv6 router address (fd00::1).
ip netns exec "$ISP_NS" ip addr add "$WAN_GW" dev "$WAN_IFACE"
ip netns exec "$ISP_NS" ip -6 addr add "$WAN_V6_ADDR" dev "$WAN_IFACE"
ip netns exec "$ISP_NS" ip link set "$WAN_IFACE" up

# outer leg — DHCP-address the real uplink with dhcpcd (v4 + v6 in one
# call; daemonizes into ns-isp; NAT6 needs a v6-capable egress).
ip netns exec "$ISP_NS" dhcpcd "$OUTER"

# forwarding + NAT (v4 + v6) out the outer leg.
ip netns exec "$ISP_NS" sysctl -w net.ipv4.ip_forward=1
ip netns exec "$ISP_NS" sysctl -w net.ipv6.conf.all.forwarding=1
ip netns exec "$ISP_NS" iptables -t nat -A POSTROUTING -o "$OUTER" -j MASQUERADE
ip netns exec "$ISP_NS" ip6tables -t nat -A POSTROUTING -o "$OUTER" -j MASQUERADE

# RA / SLAAC on the WAN iface — radvd advertises the /64 so WAN clients
# (e.g. the voda-avm netns) auto-configure IPv6. radvd daemonizes into
# ns-isp; installs radvd if missing.
apt-get install -y radvd >/dev/null 2>&1
cat > /tmp/radvd-$ISP_NS.conf << EOF
interface $WAN_IFACE {
  AdvSendAdvert on;
  prefix ${WAN_V6_PREFIX} {
    AdvOnLink on;
    AdvAutonomous on;
    AdvRouterAddr on;
  };
};
EOF
ip netns exec "$ISP_NS" radvd -C /tmp/radvd-$ISP_NS.conf

echo "ns-isp up: $WAN_GW + $WAN_V6_ADDR on $WAN_IFACE, RA/SLAAC (${WAN_V6_PREFIX}) on, egress $OUTER via dhclient + MASQUERADE (v4+v6)"
