#!/bin/sh
# tools/wipe-ovs-ovn.sh — reset a real OVS/OVN installation to a
# freshly-installed state: stop every relevant daemon, delete OVS's own
# database (every bridge/port/interface/external-id) and — only if this
# host is currently running the central control plane — OVN's NB/SB
# databases too, then restart everything so each service's own init
# script recreates an empty, schema-only database, same as right after
# `apt install`.
#
# Central vs chassis-only is auto-detected from whether
# ovn-ovsdb-server-nb.service is ACTIVE on this host right now (not
# just installed — a chassis-only host can have the ovn-central
# package present without ever running it), so this one script works
# unmodified on either kind of node.
#
# system-id is preserved explicitly, not left to regenerate — confirmed
# live, 2026-08-11: a naive wipe of conf.db loses the persisted
# system-id, OVS generates a fresh random UUID on restart, and since
# system-id IS the chassis's real identity in OVN's SB DB
# (Chassis.name), every `ovn-nbctl lrp-set-gateway-chassis ... <old
# name> ...` pin already baked into a previously-deployed script
# silently starts referencing a chassis that no longer exists — nothing
# schedules anywhere, with no error anywhere in the pipeline pointing
# at why. If a Chassis row already registered under a wrong/stale
# identity before this script got a chance to restore system-id (e.g.
# a previous manual attempt), delete it by hand afterward:
#   ovn-sbctl chassis-del <stale-name>
# then `systemctl restart ovn-controller` once more.
#
# Deliberately does NOT re-apply chassis registration (ovn-remote/
# ovn-encap-ip/ovn-encap-type/ovn-cms-options/ovn-bridge-mappings) or
# the OVN cluster topology itself — both live in the databases this
# script just wiped and need a real redeploy
# (deployer/cli.py's generated host-local + cluster scripts), not
# something this tool guesses at.
#
# Usage: copy to the target host and run as root, directly there:
#   ./wipe-ovs-ovn.sh          # asks for confirmation first
#   ./wipe-ovs-ovn.sh --yes    # skips confirmation

set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root" >&2
  exit 1
fi

HOSTNAME_NOW="$(hostname -s)"

HAS_CENTRAL=0
if systemctl is-active --quiet ovn-ovsdb-server-nb.service 2>/dev/null; then
  HAS_CENTRAL=1
fi

if [ "${1:-}" != "--yes" ]; then
  echo "This will WIPE the OVS database on $HOSTNAME_NOW (every bridge/port/interface/external-id)."
  if [ "$HAS_CENTRAL" = "1" ]; then
    echo "This host is currently running ovn-central: OVN's shared NB/SB databases will be wiped too —"
    echo "that affects the ENTIRE cluster's logical topology, every chassis, not just this host."
  fi
  printf "Type this host's name (%s) to confirm: " "$HOSTNAME_NOW"
  read -r ANSWER
  if [ "$ANSWER" != "$HOSTNAME_NOW" ]; then
    echo "aborted — input did not match" >&2
    exit 1
  fi
fi

SYSTEM_ID="$(ovs-vsctl get open_vswitch . external_ids:system-id 2>/dev/null | tr -d '"')"
if [ -z "$SYSTEM_ID" ]; then
  echo "warning: could not read a current system-id — nothing to preserve, a fresh one will be generated" >&2
else
  echo "preserving system-id: $SYSTEM_ID"
fi

echo "stopping services..."
systemctl stop ovn-controller
if [ "$HAS_CENTRAL" = "1" ]; then
  systemctl stop ovn-northd ovn-ovsdb-server-nb ovn-ovsdb-server-sb
fi
systemctl stop ovs-vswitchd ovsdb-server

echo "deleting OVS database..."
rm -f /var/lib/openvswitch/conf.db

if [ "$HAS_CENTRAL" = "1" ]; then
  echo "deleting OVN NB/SB databases..."
  rm -f /var/lib/ovn/ovnnb_db.db /var/lib/ovn/ovnsb_db.db \
    /var/lib/ovn/.ovnnb_db.db.~lock~ /var/lib/ovn/.ovnsb_db.db.~lock~
fi

echo "starting services..."
systemctl start ovsdb-server ovs-vswitchd

if [ -n "$SYSTEM_ID" ]; then
  echo "restoring system-id..."
  ovs-vsctl set open_vswitch . external_ids:system-id="$SYSTEM_ID"
  echo "$SYSTEM_ID" >/etc/openvswitch/system-id.conf
fi

if [ "$HAS_CENTRAL" = "1" ]; then
  systemctl start ovn-ovsdb-server-nb ovn-ovsdb-server-sb ovn-northd
fi
systemctl start ovn-controller

echo "done. $HOSTNAME_NOW is back to a freshly-installed OVS/OVN state (system-id: ${SYSTEM_ID:-<freshly generated>})."
echo "Chassis registration and the OVN topology are gone along with the databases — redeploy this network's"
echo "generated cluster + host-local scripts (deployer/cli.py) to bring it back."
