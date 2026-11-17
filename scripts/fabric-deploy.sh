#!/bin/sh
# Deploy to a host, over ssh: install (or update) the runtime there, then
# run a fabric command on it — by default `ir-apply create`, the IR on stdin.
#
#   scripts/fabric-deploy.sh root@192.168.128.31 < py.ir                    # ir-apply create
#   scripts/fabric-deploy.sh root@192.168.128.31 --port 2202 ir-apply create < py.ir
#   scripts/fabric-deploy.sh root@192.168.128.31 ir-apply delete < py.ir    # rewind it
#   scripts/fabric-deploy.sh root@192.168.128.31 ir-apply create --dry-run < py.ir
#   deno run -A src/cli.ts generate-ir … | scripts/fabric-deploy.sh root@192.168.128.31
set -eu

if [ $# -lt 1 ]; then
  echo "usage: $0 user@host [--port N] [fabric command …] < ir.json   (default: ir-apply create)" >&2
  exit 2
fi
TARGET=$1
shift
PORT=
if [ $# -gt 0 ] && [ "$1" = "--port" ]; then
  [ $# -ge 2 ] || {
    echo "--port needs a value" >&2
    exit 2
  }
  PORT=$2
  shift 2
fi
[ $# -gt 0 ] || set -- ir-apply create
DIR=$(dirname "$0")

# The installation must not read stdin: it is the IR.
if [ -n "$PORT" ]; then
  "$DIR/fabric-install.sh" "$TARGET" --port "$PORT" </dev/null
  exec "$DIR/fabric-remote.sh" "$TARGET" --port "$PORT" "$@"
fi
"$DIR/fabric-install.sh" "$TARGET" </dev/null
exec "$DIR/fabric-remote.sh" "$TARGET" "$@"
