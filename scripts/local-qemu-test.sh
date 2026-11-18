#!/bin/sh
# Start the local QEMU pair used by the CI smoke test:
#   tester              -> OVN socket listener, SSH 127.0.0.1:2201
#   ovn-fabric-debian   -> OVN socket connector, SSH 127.0.0.1:2202,
#                          guest hostname net-test-hh-c, OVN NIC ovnkern
#
# Requirements: qemu-system-x86_64, qemu-img, cloud-localds, ssh-keygen.
# Run from the repository root, or pass WORK=/some/dir (default /root/qemu-test).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
WORK=${WORK:-/root/qemu-test}
IMAGE=${IMAGE:-$WORK/debian-13-genericcloud-amd64.qcow2}
OVN_PORT=${OVN_PORT:-9000}
TESTER_PORT=${TESTER_PORT:-2201}
TARGET_PORT=${TARGET_PORT:-2202}
if [ -z "${ACCEL:-}" ]; then
  if [ -w /dev/kvm ]; then
    ACCEL=kvm
  else
    ACCEL=tcg
  fi
fi
TESTER_MEMORY=${TESTER_MEMORY:-512}
TARGET_MEMORY=${TARGET_MEMORY:-1024}

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
IMAGE=$(CDPATH= cd -- "$(dirname "$IMAGE")" && pwd)/$(basename "$IMAGE")

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

  cat >"$dir/user-data" <<EOF
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

start_vm tester tester "$TESTER_PORT" \
  52:54:00:00:00:01 52:54:00:00:01:01 "" listen "$TESTER_MEMORY" 52:54:00:00:03:01
start_vm ovn-fabric-debian net-test-hh-c "$TARGET_PORT" \
  52:54:00:00:00:02 52:54:00:00:01:02 ovnkern connect "$TARGET_MEMORY" ""

echo
echo "waiting for cloud-init on both guests..."
for spec in "$TESTER_PORT:tester" "$TARGET_PORT:ovn-fabric-debian"; do
  port=${spec%%:*}
  name=${spec#*:}
  deadline=$(( $(date +%s) + 600 ))
  ready=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ssh -i "$KEY" -p "$port" \
      -o BatchMode=yes \
      -o IdentitiesOnly=yes \
      -o StrictHostKeyChecking=no \
      -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=2 \
      root@127.0.0.1 'test -f /var/run/qemu-ready' >/dev/null 2>&1; then
      echo "$name ready"
      ready=1
      break
    fi
    sleep 2
  done
  if [ "$ready" -ne 1 ]; then
    echo "$name did not become ready" >&2
    tail -80 "$WORK/$name/serial.log" >&2 || true
    exit 1
  fi
done

echo
if [ "${SETUP_TESTER:-1}" = "1" ]; then
  echo "installing tester tools and starting netns setup..."
  ssh -i "$KEY" -p "$TESTER_PORT" \
    -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    root@127.0.0.1 'export DEBIAN_FRONTEND=noninteractive; apt-get update; apt-get install -y iptables radvd iputils-ping dnsutils dhcpcd-base curl ca-certificates'
  scp -i "$KEY" -P "$TESTER_PORT" \
    -o IdentitiesOnly=yes -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "$ROOT/.github/scripts/tester/test-segments.sh" \
    "$ROOT/.github/scripts/tester/test-isp.sh" \
    root@127.0.0.1:/root/
  ssh -i "$KEY" -p "$TESTER_PORT" \
    -o BatchMode=yes -o IdentitiesOnly=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    root@127.0.0.1 'chmod +x /root/test-segments.sh /root/test-isp.sh; /root/test-segments.sh up; /root/test-isp.sh up'
fi

echo
echo "tester:"
echo "  ssh -i $KEY -p $TESTER_PORT root@127.0.0.1"
echo "target:"
echo "  ssh -i $KEY -p $TARGET_PORT root@127.0.0.1"
echo
echo "next, for the target:"
echo "  TOPOLOGY_SECRETS_TS=... deno run -A src/cli.ts generate-ir --runtime ctx=test-c examples/topology.cluster-tenants.ts | scripts/fabric-deploy.sh root@127.0.0.1 --port $TARGET_PORT ir-apply run --wait-background"
