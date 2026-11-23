---
layout: page
title: Deploying
---

# Deploying

Each host runs **one service**, the fabric runtime (`ovn-fabric.service`),
from a single PEX. Deploying has two steps:

1. Put the runtime on the host.
2. Hand it an IR to apply.

## From your checkout: `scripts/fabric-deploy.sh`

`scripts/fabric-deploy.sh` does both steps over ssh. It deploys the runtime,
then runs a `fabric` command on the host with the IR on stdin (default:
`ir-apply create`):

```sh
ovn-fabric generate-ir topology.ts | scripts/fabric-deploy.sh root@HOST ir-apply run
scripts/fabric-deploy.sh root@HOST ir-apply run --dry-run < ir.json   # what it would do
scripts/fabric-deploy.sh root@HOST ir-apply delete < ir.json          # rewind it
scripts/fabric-deploy.sh root@HOST --port 2202 ir-apply run < ir.json # another ssh port
```

### Deploying the runtime

`scripts/fabric-install.sh` (a wrapper around `python3 -m fabric deploy`)
does the following:

1. Builds the PEX, `dist/fabric.pex`.
2. Copies it into the host's runtime store, keyed by its hash.
3. Points the service at it and restarts the service.
4. Checks that the service answers with that hash. If it doesn't, the host
   goes back to the runtime that ran before.

```sh
scripts/fabric-install.sh root@HOST                  # deploy this checkout
scripts/fabric-install.sh root@HOST --ref previous   # switch back
```

On the host:

```text
/var/lib/ovn-fabric/runtime/             stored runtimes (the last 10 are kept)
/usr/local/lib/ovn-fabric/fabric.pex     -> the deployed one
/etc/systemd/system/ovn-fabric.service   the service
/run/ovn-fabric.sock                     where it listens
/var/lib/ovn-fabric/allocator.json       allocated addresses, kept across reboots
```

The host's **hostname** says which of the IR's hosts it is (`--host`
overrides that).

## On the host: the `fabric` CLI

On the host, the deployed PEX is the CLI. It talks to the runtime over the
socket. Every request runs as a **job** inside the runtime, so a dropped
ssh session doesn't stop it:

| command | what it does |
| --- | --- |
| `fabric status` | the deployed IR, the last apply, its background job, the containers kept alive |
| `fabric jobs` | running and recent jobs |
| `fabric follow JOB` | a job's log so far, then live |
| `fabric ir-list`, `ir-get`, `ir-put`, `ir-delete` | the stored IRs |
| `fabric ir-apply ACTION [ref]` | apply a stored IR (or the IR on stdin) |
| `fabric runtime-list` | the stored runtimes |
| `fabric version` | the running runtime's version and hash |

### `ir-apply`: create, delete, run

- **`create`** makes everything the IR describes.
- **`delete`** rewinds it.
- **`run`** converges: it compares the IR with what is live, creates what is
  missing, replaces what differs, and removes leftovers once every unit is
  done. Only `run` looks at the live state.

Useful options:

| option | |
| --- | --- |
| `deployed` (as `ref`) | the IR applied last: `fabric ir-apply run deployed` |
| `--dry-run` | describe, run nothing |
| `--no-cleanup` | `run`: keep leftovers |
| `--detach` | leave the run going; `fabric follow <req>` attaches again |
| `--wait-background` | wait for images and containers too, and fail if they fail |
| `--command-timeout SECONDS` | per-command limit (default 300) |
| `--image-jobs N` | images built or pulled at once (default: all) |

Over ssh with nothing piped in, give the IR explicitly and close stdin:

```sh
ssh root@HOST 'fabric ir-apply run deployed </dev/null'
```

## What the runtime does for you

- **Packages.** It installs what the topology needs (OVS/OVN, iproute2,
  nftables, docker, wireguard-tools, …) for the host's distro and platform.
  See [Host requirements](hosts.html).
- **Images in the background.** Docker images are built or pulled by a
  background job. A run is done as soon as the network is up, and workloads
  start when their images are ready.
- **Boot replay.** After a reboot, the runtime replays the deployed IR
  (`run`, without cleanup). Allocated addresses come back on the same
  numbers.
- **Kept-alive containers.** While an IR is applied, a container of it that
  died or was removed is brought back. A container that keeps dying waits
  longer before each retry.

## Without ssh: cloud-init

`fabric ir-apply cloudinit` writes cloud-init user-data that carries the PEX
and the IR, for hosts that should come up already deployed. See
`scripts/cloud-init-test.sh` for an end-to-end example on QEMU.
