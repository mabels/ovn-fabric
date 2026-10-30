#!/bin/sh
# Run a fabric client command on a host where the runtime is installed
# (scripts/fabric-install.sh), over ssh. stdin goes along, so an IR can be
# piped in.
#
#   scripts/fabric-remote.sh root@192.168.128.31 version
#   scripts/fabric-remote.sh root@192.168.128.31 ir-apply create < py.ir
#   scripts/fabric-remote.sh root@192.168.128.31 ir-list
#   scripts/fabric-remote.sh root@192.168.128.31 --log remote=debug ir-apply create < py.ir
set -eu

if [ $# -lt 2 ]; then
  echo "usage: $0 user@host <fabric command> [args…]" >&2
  exit 2
fi
TARGET=$1
shift

# ssh hands the remote side one command line: quote every argument.
quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}
args=""
for arg in "$@"; do
  args="$args $(quote "$arg")"
done

# A terminal for interactive use (Ctrl-C reaches the remote client); none
# when stdin is a pipe or a file (an IR), which must reach it unchanged.
tty=""
[ -t 0 ] && tty="-t"
# ssh would forward this machine's LC_ALL — a locale the host may not have.
unset LC_ALL
PEX=/usr/local/lib/ovn-fabric/fabric.pex
exec ssh $tty "$TARGET" \
  "if [ ! -f $PEX ]; then echo 'the runtime is not installed on $TARGET: run scripts/fabric-install.sh $TARGET' >&2; exit 2; fi
PEX_ROOT=/var/cache/ovn-fabric/pex exec python3 $PEX --socket /run/ovn-fabric.sock$args"
