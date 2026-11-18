#!/bin/sh
# The local QEMU pair of local-qemu-test.sh, but the target is deployed by
# nothing but a cloud-init file (`fabric ir-apply cloudinit`):
#   tester              -> OVN socket listener, SSH 127.0.0.1:2201, set up as there
#   ovn-fabric-debian   -> OVN socket connector, SSH 127.0.0.1:2202,
#                          OVN NIC ovnkern, user-data: the given file
#
#   python -m fabric ir-apply cloudinit --host net-test-hh-c -o user-data < ir.json
#   scripts/cloud-init-test.sh user-data
#   scripts/cloud-init-test.sh ir.json     # an IR: made into that file first
#                                          # (--host $GUEST_HOSTNAME, $WORK/user-data)
#
# The target's user-data is the file, unchanged, as one part of a multipart
# user-data; a second part (a shell script, which cloud-init runs before the
# file's runcmd) names the OVN NIC and lets root in with this test's ssh
# keys. ACCESS=0 boots the file alone: no ssh then, only the serial log.
#
# Then it waits for cloud-init on the target to finish and shows how it ended
# (`cloud-init status`). Meanwhile the target's serial console (its boot,
# cloud-init and fabric's output) is shown live, every line `[target] …`;
# SHOW_BOOT=0 shows only the tail of cloud-init-output.log at the end.
#
# Requirements: qemu-system-x86_64, qemu-img, cloud-localds, ssh-keygen.
# WORK=/some/dir (default /root/qemu-test); GUEST_HOSTNAME is the target's
# hostname from meta-data (default net-test-hh-c; a file made with --host
# sets its own).
set -eu

if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "usage: $0 CLOUD-INIT-FILE|IR.json   (cloud-init from: fabric ir-apply cloudinit -o FILE)" >&2
  exit 2
fi
GIVEN=$(CDPATH='' cd -- "$(dirname "$1")" && pwd)/$(basename "$1")
USER_DATA_FILE=$GIVEN

ROOT=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd)
WORK=${WORK:-/root/qemu-test}
IMAGE=${IMAGE:-$WORK/debian-13-genericcloud-amd64.qcow2}
OVN_PORT=${OVN_PORT:-9000}
TESTER_PORT=${TESTER_PORT:-2201}
TARGET_PORT=${TARGET_PORT:-2202}
GUEST_HOSTNAME=${GUEST_HOSTNAME:-net-test-hh-c}
ACCESS=${ACCESS:-1}
SHOW_BOOT=${SHOW_BOOT:-1}
if [ -z "${ACCEL:-}" ]; then
  if [ -w /dev/kvm ]; then
    ACCEL=kvm
  else
    ACCEL=tcg
  fi
fi
TESTER_MEMORY=${TESTER_MEMORY:-512}
TARGET_MEMORY=${TARGET_MEMORY:-2048}
# The fabric run (packages, images) happens inside cloud-init: give it time.
TARGET_TIMEOUT=${TARGET_TIMEOUT:-1800}

# An IR (JSON: a node list or an IrCollection) becomes the cloud-init file
# here, for the target's hostname — with the build venv, as
# fabric-install.sh does (the PEX is bundled from this checkout).
case $(tr -d ' \t\r\n' <"$GIVEN" | head -c 1) in
  '[' | '{')
    mkdir -p "$WORK"
    USER_DATA_FILE="$WORK/user-data"
    PY="$ROOT/.venv-build/bin/python"
    [ -x "$PY" ] || python3 -m venv "$ROOT/.venv-build"
    "$PY" -m pip install --quiet --disable-pip-version-check -r "$ROOT/requirements.txt" >&2
    echo "$GIVEN is an IR: making $USER_DATA_FILE (--host $GUEST_HOSTNAME)" >&2
    (cd "$ROOT" && unset LC_ALL && "$PY" -m fabric ir-apply cloudinit \
      --host "$GUEST_HOSTNAME" --put "$GIVEN" -o "$USER_DATA_FILE")
    ;;
esac

if [ "$ACCESS" = "1" ] && [ "$(head -n 1 "$USER_DATA_FILE")" != "#cloud-config" ]; then
  echo "$USER_DATA_FILE does not start with #cloud-config (ACCESS=0 boots it as it is)" >&2
  exit 2
fi

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing $1" >&2
    exit 1
  }
}
need qemu-system-x86_64
need qemu-img
need cloud-localds
need ssh-keygen

mkdir -p "$WORK"
if [ ! -f "$IMAGE" ]; then
  echo "downloading Debian cloud image to $IMAGE" >&2
  python3 - "$IMAGE" <<'PY'
import sys
import urllib.request

url = "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
urllib.request.urlretrieve(url, sys.argv[1])
PY
fi
IMAGE=$(CDPATH='' cd -- "$(dirname "$IMAGE")" && pwd)/$(basename "$IMAGE")

KEY="$WORK/id_ed25519"
if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -N '' -C local-qemu-test -f "$KEY" >/dev/null
fi
PUB=$(cat "$KEY.pub")
USER_PUB='ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDIQpC2scaVXEaNuwtq4n6Vtht2WHYxtDFKe44JNFEsZGyQjyL9c2qkmQQGCF+2g3HrIPDTCCCWQ3GUiXGAlQ0/rf6sLqcm4YMXt+hgHU5VeciUIDEySCKdCPC419wFPBw6oKdcN1pLoIdWoF4LRDcjcrKKAlkdNJ/oLnl716piLdchABO9NXGxBpkLsJGK8qw390O1ZqZMe9wEAL9l/A1/49v8LfzELp0/fhSmiXphTVI/zNVIp/QIytXzRg74xcYpBjHk1TQZHuz/HYYsWwccnu7vYaTDX0CCoAyEt599f9u+JQ4oW0qyLO0ie7YcmR6nGEW4DMsPcfdqqo2VyYy4ix3U5RI2JcObfP0snYwPtAdVeeeReXi3c/E7bGLeCcwdFeFBfHSA9PDGxWVlxh/oCJaE7kP7eBhXNjN05FodVdNczKI5T9etfQ9VHILFrvpEREg1+OTiI58RmwjxS5ThloqXvr/nZzhIwTsED0KNW8wE4pjyotDJ8jaW2d7oVIMdWqE2M9Z1sLqDDdhHdVMFxk6Hl2XfqeqO2Jnst7qzbHAN/S3hvSwysixWJEcLDVG+cg1KRwz4qafCU5oHSp8aNNOk4RZozboFjac17nOmfPfnjC/LLayjSkEBZ+eFi+njZRLDN92k3PvHYFEB3USbHYzICsuDcf+L4cslX03g7w== men.abels@adviser.com'

if [ "$ACCEL" = "kvm" ]; then
  ACCEL_ARGS="-accel kvm -cpu host"
else
  ACCEL_ARGS="-accel tcg,thread=multi -cpu max"
fi
echo "work=$WORK accel=$ACCEL tester=${TESTER_MEMORY}MiB target=${TARGET_MEMORY}MiB" >&2

SSH_OPTS="-o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=2"
on() {
  port=$1
  shift
  # shellcheck disable=SC2086
  ssh -i "$KEY" -p "$port" $SSH_OPTS root@127.0.0.1 "$@"
}

# The shell script that brings the OVN NIC up under its name and lets root
# in — the tester's whole cloud-init, the target's extra part.
access_script() {
  ovn_mac=$1
  ovn_name=$2
  cat <<EOF
#!/bin/sh
if [ -n "$ovn_name" ]; then
  for i in /sys/class/net/*; do
    if [ "\$(cat "\$i/address")" = "$ovn_mac" ]; then
      iface=\$(basename "\$i")
      if [ "\$iface" != "$ovn_name" ]; then
        ip link set "\$iface" down || true
        ip link set "\$iface" name "$ovn_name" || true
      fi
      ip link set "$ovn_name" up || true
    fi
  done
fi
mkdir -p /root/.ssh
{
  printf '%s\n' "$PUB"
  printf '%s\n' "$USER_PUB"
} > /root/.ssh/authorized_keys
chmod 700 /root/.ssh
chmod 600 /root/.ssh/authorized_keys
systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
touch /var/run/qemu-ready
EOF
}

# The tester's user-data: hostname, root's keys, the access script.
tester_user_data() {
  guest=$1
  cat <<EOF
#cloud-config
hostname: $guest
preserve_hostname: false
users:
  - name: root
    ssh_authorized_keys:
      - $PUB
      - $USER_PUB
ssh_pwauth: false
disable_root: false
runcmd:
  - |
EOF
  access_script "$2" "$3" | sed 's/^/    /'
}

# The target's user-data: the given file as it is, and (ACCESS=1) the access
# script as a part of its own. A text/x-shellscript part runs in scripts-user
# before the file's runcmd (names sort: part-* < runcmd), so the NIC has its
# name before fabric applies the IR; it merges with nothing in the file.
target_user_data() {
  if [ "$ACCESS" != "1" ]; then
    cat "$USER_DATA_FILE"
    return
  fi
  boundary="==ovn-fabric-cloud-init-test-$$=="
  cat <<EOF
Content-Type: multipart/mixed; boundary="$boundary"
MIME-Version: 1.0

--$boundary
Content-Type: text/cloud-config; charset="us-ascii"
MIME-Version: 1.0
Content-Disposition: attachment; filename="fabric.cfg"

EOF
  cat "$USER_DATA_FILE"
  cat <<EOF

--$boundary
Content-Type: text/x-shellscript; charset="us-ascii"
MIME-Version: 1.0
Content-Disposition: attachment; filename="00-test-access.sh"

EOF
  access_script "$1" "$2"
  printf '\n--%s--\n' "$boundary"
}

start_vm() {
  name=$1
  guest=$2
  port=$3
  host_mac=$4
  ovn_mac=$5
  ovn_name=$6
  ovn_mode=$7
  memory=$8
  wan_mac=$9

  dir="$WORK/$name"
  rm -rf "$dir"
  mkdir -p "$dir"

  cat >"$dir/meta-data" <<EOF
instance-id: $name
local-hostname: $guest
EOF

  cat >"$dir/network-config" <<EOF
version: 2
ethernets:
  host:
    match: {macaddress: "$host_mac"}
    addresses: [10.0.2.15/24]
    routes: [{to: 0.0.0.0/0, via: 10.0.2.2}]
    nameservers: {addresses: [10.0.2.3]}
    dhcp4: false
    dhcp6: false
EOF
  if [ -n "$ovn_name" ]; then
    cat >>"$dir/network-config" <<EOF
  $ovn_name:
    match: {macaddress: "$ovn_mac"}
    set-name: $ovn_name
    dhcp4: false
    dhcp6: false
    accept-ra: false
EOF
  fi

  if [ "$name" = "tester" ]; then
    tester_user_data "$guest" "$ovn_mac" "$ovn_name" >"$dir/user-data"
  else
    target_user_data "$ovn_mac" "$ovn_name" >"$dir/user-data"
  fi

  WAN_ARGS=""
  if [ -n "$wan_mac" ]; then
    WAN_ARGS="-netdev user,id=wannet -device virtio-net-pci,netdev=wannet,mac=$wan_mac"
  fi

  cloud-localds --network-config="$dir/network-config" "$dir/seed.iso" "$dir/user-data" "$dir/meta-data"
  qemu-img create -f qcow2 -b "$IMAGE" -F qcow2 "$dir/overlay.qcow2" 12G >/dev/null

  # shellcheck disable=SC2086
  qemu-system-x86_64 \
    -name "$name" \
    -machine pc $ACCEL_ARGS \
    -smp 2 \
    -m "$memory" \
    -drive file="$dir/overlay.qcow2",if=virtio,format=qcow2 \
    -drive file="$dir/seed.iso",if=virtio,format=raw,readonly=on \
    -netdev user,id=hostnet,hostfwd=tcp:127.0.0.1:${port}-10.0.2.15:22 \
    -device virtio-net-pci,netdev=hostnet,mac="$host_mac" \
    -netdev socket,id=ovnnet,${ovn_mode}=127.0.0.1:${OVN_PORT} \
    -device virtio-net-pci,netdev=ovnnet,mac="$ovn_mac" \
    $WAN_ARGS \
    -display none \
    -serial "file:$dir/serial.log" \
    -monitor none \
    -daemonize \
    -pidfile "$dir/qemu.pid" \
    -no-reboot

  echo "$name: pid $(cat "$dir/qemu.pid"), ssh -i $KEY -p $port root@127.0.0.1"
}

# The tester listens on the OVN socket: it starts first.
start_vm tester tester "$TESTER_PORT" \
  52:54:00:00:00:01 52:54:00:00:01:01 "" listen "$TESTER_MEMORY" 52:54:00:00:03:01
start_vm ovn-fabric-debian "$GUEST_HOSTNAME" "$TARGET_PORT" \
  52:54:00:00:00:02 52:54:00:00:01:02 ovnkern connect "$TARGET_MEMORY" ""

wait_ssh() {
  port=$1
  name=$2
  deadline=$(($(date +%s) + 600))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if on "$port" 'test -f /var/run/qemu-ready' >/dev/null 2>&1; then
      echo "$name ready"
      return 0
    fi
    sleep 2
  done
  echo "$name did not become ready" >&2
  tail -80 "$WORK/$name/serial.log" >&2 || true
  exit 1
}

echo
echo "waiting for the tester..."
wait_ssh "$TESTER_PORT" tester

echo
if [ "${SETUP_TESTER:-1}" = "1" ]; then
  echo "installing tester tools and starting netns setup..."
  on "$TESTER_PORT" 'export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y iptables radvd iputils-ping dnsutils dhcpcd-base curl ca-certificates'
  # shellcheck disable=SC2086
  scp -i "$KEY" -P "$TESTER_PORT" $SSH_OPTS \
    "$ROOT/.github/scripts/tester/test-segments.sh" \
    "$ROOT/.github/scripts/tester/test-isp.sh" \
    root@127.0.0.1:/root/
  on "$TESTER_PORT" 'chmod +x /root/test-segments.sh /root/test-isp.sh; /root/test-segments.sh up; /root/test-isp.sh up'
fi

# The target's serial console, live, from its first line: the tail writes
# into a fifo, the reader prefixes each line (and drops the console's CR);
# stopping the tail ends the reader.
TARGET_SERIAL="$WORK/ovn-fabric-debian/serial.log"
CONSOLE_FIFO="$WORK/ovn-fabric-debian/console.fifo"
CONSOLE_PID=
show_console() {
  rm -f "$CONSOLE_FIFO"
  mkfifo "$CONSOLE_FIFO"
  cr=$(printf '\r')
  sed "s/$cr//g; s/^/[target] /" <"$CONSOLE_FIFO" &
  tail -n +1 -F "$TARGET_SERIAL" >"$CONSOLE_FIFO" 2>/dev/null &
  CONSOLE_PID=$!
}
stop_console() {
  if [ -n "$CONSOLE_PID" ]; then
    sleep 1 # the last lines
    kill "$CONSOLE_PID" 2>/dev/null || true
    wait 2>/dev/null || true
    CONSOLE_PID=
    rm -f "$CONSOLE_FIFO"
  fi
}
trap stop_console EXIT INT TERM

# The target runs the fabric deploy inside cloud-init: wait for its end.
echo
echo "waiting for cloud-init on the target (fabric runs inside it; up to ${TARGET_TIMEOUT}s)..."
[ "$SHOW_BOOT" = "1" ] && show_console
deadline=$(($(date +%s) + TARGET_TIMEOUT))
status=1
if [ "$ACCESS" = "1" ]; then
  wait_ssh "$TARGET_PORT" ovn-fabric-debian
  remaining=$((deadline - $(date +%s)))
  # cloud-init status --wait: 0 done, 1 error, 2 done with recoverable errors.
  # Its progress dots are not shown: the console is.
  if on "$TARGET_PORT" "timeout $remaining cloud-init status --wait" >/dev/null; then
    status=0
  fi
  stop_console
  echo
  on "$TARGET_PORT" 'cloud-init status --long' || true
  if [ "$SHOW_BOOT" != "1" ]; then
    echo
    echo "--- target: /var/log/cloud-init-output.log (tail) ---"
    on "$TARGET_PORT" 'tail -60 /var/log/cloud-init-output.log' || true
    echo "---"
  fi
else
  # No ssh: the console says when cloud-init's final stage is over.
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if grep -q "Cloud-init v\..* finished at" "$TARGET_SERIAL" 2>/dev/null; then
      break
    fi
    sleep 5
  done
  stop_console
  if [ "$SHOW_BOOT" != "1" ]; then
    echo "--- target: serial console (tail) ---"
    tail -60 "$TARGET_SERIAL" || true
    echo "---"
  fi
  if grep -q "Cloud-init v\..* finished at" "$TARGET_SERIAL" 2>/dev/null &&
    ! grep -qi "Failed to run module scripts.user\|scripts-user.*failed" "$TARGET_SERIAL"; then
    status=0
  fi
fi

echo
echo "tester:"
echo "  ssh -i $KEY -p $TESTER_PORT root@127.0.0.1"
echo "target (serial log: $TARGET_SERIAL):"
echo "  ssh -i $KEY -p $TARGET_PORT root@127.0.0.1"
if [ "$status" -eq 0 ]; then
  echo
  echo "cloud-init on the target: done"
else
  echo
  echo "cloud-init on the target: failed or timed out" >&2
fi
exit "$status"
