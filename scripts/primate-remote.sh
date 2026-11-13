#!/bin/sh
# Run primate on a disposable test host, over ssh: build it as its own PEX
# (never part of the runtime's bundle), copy it there, run it as root.
# The generated library comes back on stdout, primate's log on stderr.
#
#   scripts/primate-remote.sh root@192.168.128.32 list
#   scripts/primate-remote.sh -o fabric/rawir_learned.py root@192.168.128.32 learn
#   scripts/primate-remote.sh root@192.168.128.32 learn --sample addr -v
#
# `learn` creates and deletes objects on that host (netns, links, OVS
# bridges, OVN switches — all named pm…): never on a production host.
#
# `-o FILE` writes stdout to FILE only once primate succeeded (formatted
# with black) — never `> FILE` onto a file of the repo: the shell empties
# it before the PEX is built from that very repo.
set -eu

OUT=""
if [ "${1:-}" = "-o" ]; then
  OUT=$2
  shift 2
fi
if [ $# -lt 2 ]; then
  echo "usage: $0 [-o FILE] user@host <primate command> [args…]" >&2
  exit 2
fi
TARGET=$1
shift
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PEX=$ROOT/dist/primate.pex

echo "== building $PEX" >&2
(cd "$ROOT" && python3 -c '
import sys
from pathlib import Path
from fabric.bundle import build_pex
build_pex(Path(sys.argv[1]), "primate",
          packages=("primate", "fabric", "raw_reconciler", "ladops", "protocol"))
' "$PEX") >&2
scp -q "$PEX" "$TARGET:/tmp/primate.pex"

quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}
args=""
for arg in "$@"; do
  args="$args $(quote "$arg")"
done
unset LC_ALL
run="PEX_ROOT=/tmp/primate-pex exec python3 /tmp/primate.pex$args"
if [ -z "$OUT" ]; then
  exec ssh -x "$TARGET" "$run"
fi
tmp=$(mktemp "${TMPDIR:-/tmp}/primate.XXXXXX")
trap 'rm -f "$tmp"' EXIT
ssh -x "$TARGET" "$run" > "$tmp"
"$ROOT/.venv-build/bin/black" -q --config "$ROOT/pyproject.toml" "$tmp"
chmod 644 "$tmp"
mv "$tmp" "$OUT"
trap - EXIT
echo "== wrote $OUT" >&2
