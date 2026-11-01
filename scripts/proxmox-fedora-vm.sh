#!/bin/sh
# Create a Fedora Cloud VM on a Proxmox host, as a fabric test host — no
# installer: Fedora's Cloud Base image, configured on first boot by
# cloud-init (hostname, root's SSH keys, a static address, NIC names).
#
#   scripts/proxmox-fedora-vm.sh root@192.168.129.14 302 net-test-hh-c \
#     192.168.128.32/24 192.168.128.1 keys.pub
#
# Three NICs on the bridge, named in the guest by their MAC like a test
# container's: `ovnkern` (the VLAN trunk), `host` (the address), `ovnxdp`.
# The VMID must be free. Then: scripts/fabric-install.sh root@<address>.
#
# Environment:
#   PVE_STORAGE  disks and the cloud-init drive, default datapool
#   PVE_BRIDGE   default vmbr1
#   VM_MEMORY    MiB, default 1024     VM_CORES  default 2
#   VM_DISK      default 8G
set -eu

if [ $# -ne 6 ]; then
  echo "usage: $0 root@pve VMID NAME ADDRESS/PREFIX GATEWAY AUTHORIZED_KEYS_FILE" >&2
  exit 2
fi
PVE=$1 VMID=$2 NAME=$3 ADDRESS=$4 GATEWAY=$5 KEYS=$6
STORAGE=${PVE_STORAGE:-datapool}
BRIDGE=${PVE_BRIDGE:-vmbr1}
MEMORY=${VM_MEMORY:-1024}
CORES=${VM_CORES:-2}
DISK=${VM_DISK:-8G}

RELEASE=44
IMAGE=Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2
CHECKSUM=Fedora-Cloud-44-1.7-x86_64-CHECKSUM
BASE=https://download.fedoraproject.org/pub/fedora/linux/releases/$RELEASE/Cloud/x86_64/images
CACHE=/var/lib/vz/template/cache
SNIPPETS=/var/lib/vz/snippets   # the `local` storage, content type snippets

# One MAC per NIC (Proxmox's prefix), so the guest can name them.
mac() { od -An -N3 -tx1 /dev/urandom | tr -s ' \n' ' ' | sed 's/^ //; s/ $//; s/ /:/g; s/^/bc:24:11:/'; }
MAC_KERN=$(mac) MAC_HOST=$(mac) MAC_XDP=$(mac)

keys=$(grep '^ssh-' "$KEYS" | sed 's/^/      - /')
[ -n "$keys" ] || { echo "no ssh- keys in $KEYS" >&2; exit 2; }

user_data=$(cat <<EOF
#cloud-config
hostname: $NAME
preserve_hostname: false
disable_root: false
ssh_pwauth: false
users:
  - name: root
    ssh_authorized_keys:
$keys
EOF
)

# The VM's identity: its hostname comes from here (Fedora's cloud-init
# prefers it over the user data's `hostname:`).
meta_data=$(cat <<EOF
instance-id: $NAME-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
local-hostname: $NAME
EOF
)

network=$(cat <<EOF
version: 2
ethernets:
  ovnkern:
    match: {macaddress: "$MAC_KERN"}
    set-name: ovnkern
  host:
    match: {macaddress: "$MAC_HOST"}
    set-name: host
    addresses: [$ADDRESS]
    routes: [{to: default, via: $GATEWAY}]
    nameservers: {addresses: [$GATEWAY]}
  ovnxdp:
    match: {macaddress: "$MAC_XDP"}
    set-name: ovnxdp
EOF
)

echo "== $NAME: VM $VMID on $PVE ($ADDRESS)"
# shellcheck disable=SC2087  # expanded here on purpose: the values travel
ssh -o BatchMode=yes "$PVE" sh -eu <<EOF
if qm status $VMID >/dev/null 2>&1 || pct status $VMID >/dev/null 2>&1; then
  echo "VMID $VMID is in use" >&2; exit 1
fi
cd $CACHE
if [ ! -f $IMAGE ]; then
  echo "== downloading $IMAGE"
  curl -fsSL -o $IMAGE.part $BASE/$IMAGE
  curl -fsSL -o $CHECKSUM $BASE/$CHECKSUM
  grep -E "^SHA256 \\($IMAGE\\)" $CHECKSUM | sed 's/^SHA256 (\(.*\)) = \(.*\)$/\2  \1.part/' | sha256sum -c -
  mv $IMAGE.part $IMAGE
fi

mkdir -p $SNIPPETS
cat > $SNIPPETS/$VMID-user.yaml <<'USER'
$user_data
USER
cat > $SNIPPETS/$VMID-network.yaml <<'NET'
$network
NET
cat > $SNIPPETS/$VMID-meta.yaml <<'META'
$meta_data
META

echo "== creating the VM"
qm create $VMID --name $NAME --ostype l26 --cpu host --cores $CORES --memory $MEMORY \
  --net0 virtio=$MAC_KERN,bridge=$BRIDGE \
  --net1 virtio=$MAC_HOST,bridge=$BRIDGE \
  --net2 virtio=$MAC_XDP,bridge=$BRIDGE \
  --scsihw virtio-scsi-single \
  --scsi0 $STORAGE:0,import-from=$CACHE/$IMAGE,discard=on \
  --ide2 $STORAGE:cloudinit --boot order=scsi0 \
  --serial0 socket --vga serial0 --onboot 0 \
  --cicustom user=local:snippets/$VMID-user.yaml,network=local:snippets/$VMID-network.yaml,meta=local:snippets/$VMID-meta.yaml \
  > /tmp/qm-create-$VMID.log 2>&1 || { cat /tmp/qm-create-$VMID.log >&2; exit 1; }
grep -v '^transferred ' /tmp/qm-create-$VMID.log || true   # without the import's progress
rm -f /tmp/qm-create-$VMID.log
qm resize $VMID scsi0 $DISK
qm start $VMID
EOF

host=${ADDRESS%/*}
printf '== waiting for ssh on %s ' "$host"
for _ in $(seq 1 60); do
  if ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new \
    "root@$host" true 2>/dev/null; then
    echo ok
    exec ssh -o BatchMode=yes "root@$host" \
      'hostname; . /etc/os-release; echo "$PRETTY_NAME"; ip -br link | grep -v "^lo"'
  fi
  printf .
  sleep 5
done
echo " no answer after 5 minutes (qm terminal $VMID on the host shows the console)" >&2
exit 1
