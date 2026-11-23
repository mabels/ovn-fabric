#!/usr/bin/env bash
# OVN shared bridge — ovs-0 (192.168.128.40): CLUSTER CENTER + chassis.
# Runs the NB/SB databases and ovn-northd, defines the logical switch
# "shared" with one port per host plus a router port 10.99.0.1, and
# attaches itself to it. The router r0 also gets an uplink leg
# 192.168.132.120/24 (VLAN 1280, wired on ovs-1 + ovs-2, HA) with a default route to
# 192.168.132.1 and SNAT of 10.99.0.0/24.
#
#   ./ovs-0.sh up     install, start central, define switch, attach shared0
#   ./ovs-0.sh down   detach shared0, delete the switch, leave OVN
#
# Run this one FIRST, then ovs-1.sh, ovs-2.sh, ovs-3.sh on their hosts.
#
# Underlay:      ovs-0 .40   ovs-1 .41   ovs-2 .42   ovs-3 .43  (192.168.128.0/24)
# Shared bridge: 10.99.0.0/24, each host gets 10.99.0.<last underlay octet>
# Firewall:      TCP 6641/6642 to ovs-0, UDP 6081 (Geneve) between all hosts
set -euo pipefail

NAME=ovs-0
LOCAL_IP=192.168.128.40
CENTRAL_IP=192.168.128.40
SWITCH=shared
PORT=shared0
MTU=1442                     # 1500 - 58 bytes Geneve overhead
MY_ADDR=10.99.0.40/24
# shared0 lives in its own netns "shared0" with a default route via r0
PORT_NS=shared0
PORT_GW=10.99.0.1
PORT_HELPER=/usr/local/sbin/shared0-netns-up
PORT_UNIT=shared0-netns.service
OLD_NETWORK_FILE=/etc/systemd/network/50-shared0.network   # earlier variant
MY_MAC=0a:00:0a:63:00:40

# distributed OVN logical router with one port on the shared bridge
ROUTER=r0
RTR_PORT=r0-shared           # router side (lrp)
RTR_LSP=shared-r0            # switch side (lsp, type=router)
RTR_MAC=0a:00:0a:63:00:01
RTR_ADDR=10.99.0.1/24
SHARED_NET=10.99.0.0/24

# second leg of r0: the real uplink, VLAN 1280 on ovs-1 (.41) and ovs-2 (.42).
# OVN reaches it through the localnet switch "uplink"; the leg is a
# distributed gateway port, active on one of UPLINK_CHASSIS, default route to
# 192.168.132.1, SNAT of the shared bridge.
UPLINK_SWITCH=uplink
UPLINK_LOCALNET=uplink-localnet
UPLINK_NET=uplink              # network_name; = ovn-bridge-mappings key on ovs-1
# HA gateway chassis for r0-uplink: "<chassis> <priority>", highest active.
# Every chassis with VLAN 1280 wired (ovs-1.sh, ovs-2.sh) belongs in here.
# BFD on the Geneve tunnels detects a dead gateway; the next one takes over
# cr-r0-uplink and sends GARP for 192.168.132.120. A returning higher
# priority chassis takes over again (preemption).
UPLINK_CHASSIS=(
  "ovs-1 20"
  "ovs-2 10"
)
UPLINK_RTR_PORT=r0-uplink      # router side (lrp)
UPLINK_RTR_LSP=uplink-r0       # switch side (lsp, type=router)
UPLINK_RTR_MAC=0a:00:c0:a8:84:78
UPLINK_RTR_ADDR=192.168.132.120/24
UPLINK_GW=192.168.132.1

# every host on the shared bridge: "<lsp-name> <mac> <ip>"
HOSTS=(
  "ovs-0 0a:00:0a:63:00:40 10.99.0.40"
  "ovs-1 0a:00:0a:63:00:41 10.99.0.41"
  "ovs-2 0a:00:0a:63:00:42 10.99.0.42"
  "ovs-3 0a:00:0a:63:00:43 10.99.0.43"
)

install_deps() {
  # Ubuntu 26.04: OVS + OVN packages and the tools the script uses
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y iproute2 kmod procps openvswitch-switch openvswitch-common ovn-common ovn-host ovn-central
  OVS_SVC=openvswitch-switch
  CENTRAL_SVC=ovn-central
  CONTROLLER_SVC=ovn-host
  systemctl enable --now "$OVS_SVC"
  ovs-vsctl --timeout=30 show >/dev/null   # wait until ovsdb-server answers
}

down() {
  ovs-vsctl --if-exists del-port br-int "$PORT"
  systemctl disable --now "$PORT_UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$PORT_UNIT" "$PORT_HELPER"
  systemctl daemon-reload
  ip netns del "$PORT_NS" 2>/dev/null || true
  ovn-nbctl --if-exists lr-del "$ROUTER"
  ovn-nbctl --if-exists ls-del "$SWITCH"
  ovn-nbctl --if-exists ls-del "$UPLINK_SWITCH"
  systemctl stop ovn-host || true
  echo "$NAME: shared bridge removed"
}

up() {
  install_deps
  modprobe openvswitch 2>/dev/null || true
  modprobe geneve 2>/dev/null || true

  # --- central: NB/SB databases + northd, reachable from the other hosts
  systemctl enable --now "$CENTRAL_SVC"
  ovn-nbctl set-connection ptcp:6641:0.0.0.0
  ovn-sbctl set-connection ptcp:6642:0.0.0.0

  # --- chassis: join our own SB
  # ovs-ctl (--system-id=random) copies this file into external_ids:system-id
  # at every start; without it the chassis comes back as a random (or cloned)
  # UUID after a reboot and collides with its old encap ip.
  echo "$NAME" > /etc/openvswitch/system-id.conf
  ovs-vsctl set open_vswitch . \
    external_ids:system-id="$NAME" \
    external_ids:ovn-remote="tcp:$CENTRAL_IP:6642" \
    external_ids:ovn-encap-type=geneve \
    external_ids:ovn-encap-ip="$LOCAL_IP"
  systemctl enable --now "$CONTROLLER_SVC"

  # ovn-controller creates br-int; wait for it
  for _ in $(seq 1 30); do ovs-vsctl br-exists br-int && break; sleep 1; done
  ovs-vsctl br-exists br-int || { echo "$NAME: br-int did not appear" >&2; exit 1; }

  # --- the shared bridge: one logical switch, one port per host
  ovn-nbctl --may-exist ls-add "$SWITCH"
  for h in "${HOSTS[@]}"; do
    read -r lsp mac ip <<<"$h"
    ovn-nbctl --may-exist lsp-add "$SWITCH" "$lsp"
    ovn-nbctl lsp-set-addresses "$lsp" "$mac $ip"
  done

  # --- router endpoint 10.99.0.1 on the shared bridge
  ovn-nbctl --may-exist lr-add "$ROUTER"
  ovn-nbctl --may-exist lrp-add "$ROUTER" "$RTR_PORT" "$RTR_MAC" "$RTR_ADDR"
  ovn-nbctl --may-exist lsp-add "$SWITCH" "$RTR_LSP"
  ovn-nbctl lsp-set-type "$RTR_LSP" router
  ovn-nbctl lsp-set-addresses "$RTR_LSP" router
  ovn-nbctl lsp-set-options "$RTR_LSP" router-port="$RTR_PORT"

  # --- uplink 192.168.132.120 on VLAN 1280 (wired on ovs-1 by ovs-1.sh)
  ovn-nbctl --may-exist ls-add "$UPLINK_SWITCH"
  ovn-nbctl --may-exist lsp-add "$UPLINK_SWITCH" "$UPLINK_LOCALNET"
  ovn-nbctl lsp-set-type "$UPLINK_LOCALNET" localnet
  ovn-nbctl lsp-set-addresses "$UPLINK_LOCALNET" unknown
  ovn-nbctl lsp-set-options "$UPLINK_LOCALNET" network_name="$UPLINK_NET"
  ovn-nbctl --may-exist lrp-add "$ROUTER" "$UPLINK_RTR_PORT" "$UPLINK_RTR_MAC" "$UPLINK_RTR_ADDR"
  for gw in "${UPLINK_CHASSIS[@]}"; do
    read -r chassis prio <<<"$gw"
    ovn-nbctl lrp-set-gateway-chassis "$UPLINK_RTR_PORT" "$chassis" "$prio"
  done
  ovn-nbctl --may-exist lsp-add "$UPLINK_SWITCH" "$UPLINK_RTR_LSP"
  ovn-nbctl lsp-set-type "$UPLINK_RTR_LSP" router
  ovn-nbctl lsp-set-addresses "$UPLINK_RTR_LSP" router
  ovn-nbctl lsp-set-options "$UPLINK_RTR_LSP" router-port="$UPLINK_RTR_PORT"
  ovn-nbctl --may-exist lr-route-add "$ROUTER" 0.0.0.0/0 "$UPLINK_GW"
  ovn-nbctl --may-exist lr-nat-add "$ROUTER" snat "${UPLINK_RTR_ADDR%/*}" "$SHARED_NET"

  # --- attach this host
  ovs-vsctl --may-exist add-port br-int "$PORT" -- set interface "$PORT" \
    type=internal mac="\"$MY_MAC\"" external_ids:iface-id="$NAME" \
    mtu_request="$MTU"

  # shared0 goes into netns shared0. ovs-vswitchd (re)creates the internal
  # device in the root namespace (also at every boot), so a oneshot unit moves
  # it over and sets mtu, address and the default route via r0 inside the netns.
  if [ -e "$OLD_NETWORK_FILE" ]; then rm -f "$OLD_NETWORK_FILE"; networkctl reload; fi
  cat >"$PORT_HELPER" <<EOF
#!/bin/sh
# generated by $NAME.sh: puts $PORT into netns $PORT_NS (idempotent)
set -e
[ -e /run/netns/$PORT_NS ] || ip netns add $PORT_NS
ip -n $PORT_NS link set lo up
i=0
until ip -n $PORT_NS link show $PORT >/dev/null 2>&1; do
  if ip link show $PORT >/dev/null 2>&1; then
    ip link set $PORT netns $PORT_NS
  else
    i=\$((i + 1))
    [ \$i -lt 60 ] || { echo "$PORT did not appear" >&2; exit 1; }
    sleep 1
  fi
done
ip -n $PORT_NS link set $PORT mtu $MTU up
ip -n $PORT_NS addr replace $MY_ADDR dev $PORT
ip -n $PORT_NS route replace default via $PORT_GW dev $PORT
EOF
  chmod 755 "$PORT_HELPER"
  cat >"/etc/systemd/system/$PORT_UNIT" <<EOF
[Unit]
Description=$PORT in netns $PORT_NS, default via $PORT_GW
Wants=ovs-vswitchd.service
After=ovs-vswitchd.service openvswitch-switch.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$PORT_HELPER

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable "$PORT_UNIT"
  systemctl restart "$PORT_UNIT"

  echo "$NAME: central up, $PORT = $MY_ADDR in netns $PORT_NS, default via $PORT_GW"
  echo "router $ROUTER: $RTR_ADDR on $SWITCH"
  echo "check: ovn-sbctl show; ovn-nbctl show; ip netns exec $PORT_NS ping -c2 $PORT_GW"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
