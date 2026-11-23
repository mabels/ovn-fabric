---
layout: page
title: Development
---

# Development

## Checks

`scripts/check.sh` runs what CI runs:

```sh
./scripts/check.sh                 # everything
./scripts/check.sh --skip-codegen  # when the schemas didn't change
./scripts/check.sh --ts-only       # deno fmt, lint, check, test, smoke
./scripts/check.sh --py-only       # black, ruff, codegen, mypy --strict, unittest
```

The Python tools live in a build venv:

```sh
python3 -m venv .venv-build
.venv-build/bin/pip install black ruff mypy -r requirements.txt
```

## Generated code

Don't edit these by hand; regenerate them:

| file | from |
| --- | --- |
| `protocol/generated.py`, `protocol/raw_generated.py` | `deno run -A src/cli.ts generate-pytypes` (schema in `src/protocol.ts`) |
| `runtime/generated.py` | `deno run -A runtime/schema/generate.ts` |
| `fabric/generated.py` | `deno run -A fabric/schema/generate.ts` |
| `fabric/rawir_learned.py` | `python3 -m primate learn` on a real system |

## Testing on real hosts

- `scripts/local-qemu-test.sh` and `scripts/cloud-init-test.sh` boot a QEMU
  VM and deploy into it.
- `scripts/proxmox-fedora-vm.sh` creates a test VM on Proxmox.
- `scripts/ec2-pair/` launches pairs of hosts on EC2 across an OS and
  architecture matrix, checks Geneve and egress between them, then deploys
  and tests.

## Publishing

`package.json` and `deno.json` carry a `0.0.0` placeholder version. Tag a
commit `vX.Y.Z` and push it: CI patches the version from the tag and
publishes to npm and JSR through OIDC trusted publishing.

## This site

The site is built from `docs/` with Jekyll, and the API reference with
`deno doc --html`, by `.github/workflows/pages.yml` on every push to
`main`. To preview locally:

```sh
bundle install
bundle exec jekyll serve --source docs
deno doc --html --name="ovn-fabric" --output=_site/api src/mod.ts
```
