"""One cloud-init user-data file that deploys a host from nothing:
`fabric ir-apply cloudinit` (ADR 0012 §1, without ssh).

It carries the runtime (the PEX) and the IR as write_files, and a script
that runcmd starts on first boot — the same two steps `fabric deploy` and
`fabric ir-apply run --put` do over ssh:

    /var/lib/ovn-fabric/cloud-init/fabric.pex   the runtime, removed once stored
    /var/lib/ovn-fabric/cloud-init/ir.json      the IR, kept
    /var/lib/ovn-fabric/cloud-init/apply.sh     runtime-install, then ir-apply run

`run`, not `create`: run converges, so running the script again (by hand,
after a failed boot) is safe. It returns once the network is done; the
background job (images, containers) goes on in the service.

With a host, cloud-init also sets the machine's hostname to it: the
runtime is the host its hostname names (boot replay included).

Everything is emitted as YAML by hand — no YAML library needed: scalars
JSON-quoted (valid YAML), file contents as literal blocks (the IR as
indented JSON, so the user-data stays readable; the PEX as base64).
"""

from __future__ import annotations

import base64
import json
import shlex

from .runtime_store import COMMAND, PEX_ROOT

DIR = "/var/lib/ovn-fabric/cloud-init"
PEX = f"{DIR}/fabric.pex"
IR = f"{DIR}/ir.json"
SCRIPT = f"{DIR}/apply.sh"


def ir_hosts(collection: list[dict[str, object]]) -> list[str]:
    """The IR's infra.hosts, by name."""
    hosts = []
    for node in collection:
        key = node.get("key")
        if node.get("kind") == "infra.host" and isinstance(key, dict):
            hosts.append(str(key.get("host")))
    return hosts


def script(host: str | None, name: str | None) -> str:
    apply = [str(COMMAND), "ir-apply", "run", "--put", IR]
    if name is not None:
        apply += ["--name", name]
    if host is not None:
        apply += ["--host", host]
    return "\n".join(
        [
            "#!/bin/sh",
            "# ovn-fabric: deploy the runtime, then apply the IR (fabric ir-apply cloudinit)",
            "set -eu",
            f"export PEX_ROOT={PEX_ROOT}",
            f"if [ -f {PEX} ]; then",
            f"  python3 {PEX} runtime-install",
            f"  rm -f {PEX}",
            "fi",
            " ".join(shlex.quote(a) for a in apply),
            "",
        ]
    )


def _q(value: str) -> str:
    return json.dumps(value)


def _block(text: str, indent: str) -> list[str]:
    """A literal block's lines: `text` is JSON or our script — no tabs, no
    trailing spaces, so it comes back exactly (with one final newline)."""
    return [f"{indent}{line}" if line else "" for line in text.rstrip("\n").split("\n")]


def _b64_block(data: bytes, indent: str) -> list[str]:
    text = base64.b64encode(data).decode("ascii")
    return [f"{indent}{text[i : i + 76]}" for i in range(0, len(text), 76)]


def render(pex: bytes, ir: object, host: str | None = None, name: str | None = None) -> str:
    """The #cloud-config: `ir` is the IR as JSON-able data (an IrCollection)."""
    lines = ["#cloud-config"]
    if host is not None:
        lines += [f"hostname: {_q(host)}", "preserve_hostname: false"]
    lines += [
        "write_files:",
        f"  - path: {_q(PEX)}",
        "    owner: root:root",
        '    permissions: "0600"',
        "    encoding: b64",
        "    content: |",
        *_b64_block(pex, "      "),
        f"  - path: {_q(IR)}",
        "    owner: root:root",
        '    permissions: "0600"',
        "    content: |",
        *_block(json.dumps(ir, indent=2), "      "),
        f"  - path: {_q(SCRIPT)}",
        "    owner: root:root",
        '    permissions: "0700"',
        "    content: |",
        *_block(script(host, name), "      "),
        "runcmd:",
        f"  - [{_q(SCRIPT)}]",
        "",
    ]
    return "\n".join(lines)
