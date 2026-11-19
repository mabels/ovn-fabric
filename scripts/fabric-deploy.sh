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
PEX=
while [ $# -gt 0 ]; do
  case "$1" in
    --port)
      [ $# -ge 2 ] || { echo "--port needs a value" >&2; exit 2; }
      PORT=$2
      shift 2
      ;;
    --pex)
      [ $# -ge 2 ] || { echo "--pex needs a value" >&2; exit 2; }
      PEX=$2
      shift 2
      ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || set -- ir-apply create
DIR=$(dirname "$0")

# The installation must not read stdin: it is the IR.
# --pex deploys a prebuilt, per-run PEX (parallel deploys must not share
# dist/fabric.pex); it is an install option, not a remote command option.
if [ -n "$PORT" ] && [ -n "$PEX" ]; then
  "$DIR/fabric-install.sh" "$TARGET" --port "$PORT" --pex "$PEX" </dev/null
  exec "$DIR/fabric-remote.sh" "$TARGET" --port "$PORT" "$@"
elif [ -n "$PORT" ]; then
  "$DIR/fabric-install.sh" "$TARGET" --port "$PORT" </dev/null
  exec "$DIR/fabric-remote.sh" "$TARGET" --port "$PORT" "$@"
elif [ -n "$PEX" ]; then
  "$DIR/fabric-install.sh" "$TARGET" --pex "$PEX" </dev/null
  exec "$DIR/fabric-remote.sh" "$TARGET" "$@"
fi
"$DIR/fabric-install.sh" "$TARGET" </dev/null
exec "$DIR/fabric-remote.sh" "$TARGET" "$@"
