#!/bin/sh
# Run the runtime with fabric's services (python3 -m fabric) in a throwaway
# Debian container with Python, the checkout mounted read-only, using the
# HOST's docker daemon (its socket is mounted in — docker-outside-of-docker,
# no daemon inside the container).
#
#   scripts/rt-docker.sh                      start the server (foreground, Ctrl-C stops it)
#   scripts/rt-docker.sh serve --log wire=debug
#   scripts/rt-docker.sh version              ask the running server
#   scripts/rt-docker.sh resolve curl jq
#   scripts/rt-docker.sh docker-run --image busybox -- echo hi
#   python3 -m deployer.containers py.ir --host H | scripts/rt-docker.sh docker-run --spec - --name N
#   scripts/rt-docker.sh ir-put < py.ir   (stored IRs live in .rt-files/state, across restarts)
#   scripts/rt-docker.sh ir-list
#   scripts/rt-docker.sh serve --host H   then   scripts/rt-docker.sh ir-apply create < py.ir
#
# Environment:
#   RT_IMAGE   default python:3.12-slim
#   RT_NAME    default rt
#   RT_HOST    the container's hostname: which of an IR's hosts the runtime is
#              (e.g. RT_HOST=net-test-hh-b); default: docker's random one
#   RT_DOCKER  the host's docker socket, default /var/run/docker.sock
#   RT_FILES   where containers' files are written, default ./.rt-files — the
#              HOST daemon resolves bind mounts on the host, so this directory
#              is mounted at the identical path inside the container
set -eu

IMAGE=${RT_IMAGE:-python:3.12-slim}
NAME=${RT_NAME:-rt}
DOCKER_SOCKET=${RT_DOCKER:-/var/run/docker.sock}
SOCKET=/tmp/ovn-fabric.sock   # /src is read-only
ROOT=$(cd "$(dirname "$0")/.." && pwd)
FILES=${RT_FILES:-$ROOT/.rt-files}

for schema in runtime fabric; do
  if [ ! -f "$ROOT/$schema/generated.py" ]; then
    echo "$schema/generated.py is missing: run 'deno run -A $schema/schema/generate.ts' first" >&2
    exit 1
  fi
done

command=${1:-serve}
[ $# -gt 0 ] && shift

if [ "$command" = serve ]; then
  mkdir -p "$FILES"
  FILES=$(cd "$FILES" && pwd -P)   # the real path: what the host daemon sees
  # More grace than the runtime gives its own containers (docker stop -t 10),
  # so it can stop and remove them before it is killed.
  exec docker run --rm -it --name "$NAME" --stop-timeout 30 \
    ${RT_HOST:+--hostname "$RT_HOST"} \
    -v "$ROOT":/src:ro -w /src \
    -v "$DOCKER_SOCKET":/var/run/docker.sock \
    -v "$FILES":"$FILES" \
    "$IMAGE" \
    python3 -m fabric --socket "$SOCKET" serve --files-root "$FILES" --state-root "$FILES/state" "$@"
fi

# The client runs inside the container without a TTY (stdin may be a
# heredoc), so Ctrl-C would only end the local docker CLI and leave the
# client — and its request — running. Pass the interrupt on to it instead.
# docker exec runs in the background so that `wait` is interrupted and the
# trap runs (a shell skips its INT trap when the foreground child exits
# normally on SIGINT, which the docker CLI does); stdin is handed over
# explicitly (a background job would get /dev/null).
PIDFILE=/tmp/rt-client.$$
interrupted=0
forward() {
  interrupted=1
  docker exec "$NAME" sh -c "kill -INT \$(cat $PIDFILE) 2>/dev/null" || true
}
trap forward INT TERM
exec 3<&0
docker exec -i "$NAME" sh -c 'pidfile=$1; shift; echo $$ > "$pidfile"; exec python3 -m fabric "$@"' \
  sh "$PIDFILE" --socket "$SOCKET" "$command" "$@" <&3 &
child=$!
status=0
wait "$child" || status=$?
while kill -0 "$child" 2>/dev/null; do  # interrupted: wait for it to end
  wait "$child" || status=$?
done
docker exec "$NAME" rm -f "$PIDFILE" 2>/dev/null || true
[ "$interrupted" = 1 ] && exit 130
exit "$status"
