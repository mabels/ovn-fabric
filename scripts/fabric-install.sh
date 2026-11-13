#!/bin/sh
# Deploy the runtime to a host (ADR 0012 §1): build the bundle from this
# checkout and make it the host's deployed runtime — a thin wrapper around
# `python3 -m fabric deploy`, which copies it into the host's runtime store
# (/var/lib/ovn-fabric/runtime, by hash), links the unit's executable to it,
# restarts the service and checks it answers with that hash (else it goes
# back to what ran before).
#
#   scripts/fabric-install.sh root@192.168.128.31
#   scripts/fabric-install.sh root@192.168.128.31 --ref previous   # switch back
#
# On the host:
#   /var/lib/ovn-fabric/runtime/             the stored runtimes (the last 10 kept)
#   /usr/local/lib/ovn-fabric/fabric.pex     → the deployed one
#   /etc/systemd/system/ovn-fabric.service   the one service
#   /run/ovn-fabric.sock                     where it listens
# Its host (which of an IR's hosts it is) is the machine's hostname.
set -eu

if [ $# -lt 1 ]; then
  echo "usage: $0 user@host [--ref previous|sha256:…]" >&2
  exit 2
fi
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# ssh would forward this machine's LC_ALL — a locale the host may not have.
unset LC_ALL

# The build venv: `python3 -m fabric` imports what the runtime imports
# (requirements.txt, e.g. mabels-ipaddress) — on a host the PEX carries it,
# here the venv does. pex itself is installed there by the bundle step.
PY="$ROOT/.venv-build/bin/python"
[ -x "$PY" ] || python3 -m venv "$ROOT/.venv-build"
"$PY" -m pip install --quiet --disable-pip-version-check -r "$ROOT/requirements.txt" >&2
cd "$ROOT"
exec "$PY" -m fabric deploy "$@"
