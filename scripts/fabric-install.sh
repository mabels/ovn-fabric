#!/bin/sh
# Install (or update) the runtime with fabric's services on a host, over ssh:
# build the bundle (python3 -m fabric bundle), copy it over, write the
# systemd unit, stop the running service, put the new bundle in place,
# start it again and check that it answers.
#
#   scripts/fabric-install.sh root@192.168.128.31
#
# On the host:
#   /usr/local/lib/ovn-fabric/fabric.pex     the bundle
#   /etc/systemd/system/ovn-fabric.service   the one service (python3 fabric.pex serve)
#   /run/ovn-fabric.sock                     where it listens
#   /var/lib/ovn-fabric                      its state (stored IRs, containers' files)
# Its host (which of an IR's hosts it is) is the machine's hostname.
set -eu

if [ $# -ne 1 ]; then
  echo "usage: $0 user@host" >&2
  exit 2
fi
TARGET=$1
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# ssh would forward this machine's LC_ALL — a locale the host may not have.
unset LC_ALL
BUNDLE="$ROOT/dist/fabric.pex"

echo "== building $BUNDLE" >&2
(cd "$ROOT" && python3 -m fabric bundle -o "$BUNDLE" >/dev/null)

echo "== copying it to $TARGET" >&2
scp -q "$BUNDLE" "$TARGET:/tmp/fabric.pex.new"

echo "== installing and restarting ovn-fabric.service on $TARGET" >&2
ssh "$TARGET" sh -s <<'REMOTE'
set -eu
PEX=/usr/local/lib/ovn-fabric/fabric.pex
SOCKET=/run/ovn-fabric.sock
export PEX_ROOT=/var/cache/ovn-fabric/pex
install -d /usr/local/lib/ovn-fabric /var/lib/ovn-fabric "$PEX_ROOT"

cat > /etc/systemd/system/ovn-fabric.service <<UNIT
[Unit]
Description=ovn-fabric runtime (python3 fabric.pex serve)
Wants=network-online.target
After=network-online.target

[Service]
Environment=PEX_ROOT=$PEX_ROOT
ExecStart=/usr/bin/env python3 $PEX --socket $SOCKET serve --state-root /var/lib/ovn-fabric --files-root /var/lib/ovn-fabric/containers
Restart=on-failure
RestartSec=5
# More grace than the runtime gives the containers it stops (10s each).
TimeoutStopSec=60
KillMode=mixed

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl stop ovn-fabric.service 2>/dev/null || true
install -m 0755 /tmp/fabric.pex.new "$PEX"
rm -f /tmp/fabric.pex.new
systemctl enable --quiet ovn-fabric.service
systemctl start ovn-fabric.service

# Up when it answers.
i=0
until python3 "$PEX" --socket "$SOCKET" version 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "ovn-fabric.service does not answer:" >&2
    systemctl --no-pager status ovn-fabric.service >&2 || true
    journalctl --no-pager -u ovn-fabric.service -n 20 >&2 || true
    exit 1
  fi
  sleep 1
done
REMOTE
