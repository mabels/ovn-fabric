# deployer/ir_to_shell.py — the shell-script generator: every emitter
# lives in deployer/ir_common.py (shared with the all-in-one Python
# generator, deployer/ir_to_python.py); this module's only job is the
# top-level split into the two script families `build_scripts()` returns
# — the cluster-wide ovn-nbctl script (run once, from whichever chassis
# reaches the shared NB DB) and one per-host OVS/netns script (run on
# the host itself) — for the requested action. See ir_common.py's own
# header for the full design rationale.

from __future__ import annotations

from protocol import generated as pt

from .ir_common import Action, _emit_cluster_script, _emit_host_script

# re-exported so `from deployer.ir_to_shell import generate_python_deployer`
# (and the tests' `mod.generate_python_deployer`) keeps working without
# caring which front-end module owns it (2026-08-19).
from .ir_to_python import generate_python_deployer  # noqa: E402  (re-export)

__all__ = [
    "Action",
    "build_scripts",
    "generate_python_deployer",
]


def build_scripts(nodes: list[pt.Model], action: Action) -> tuple[str, dict[str, str]]:
    if action not in ("create", "delete"):
        raise ValueError(f'action must be "create" or "delete", got {action!r}')

    host_scripts = {
        node.key.host: _emit_host_script(node, nodes, action)
        for node in nodes
        if node.kind == "infra.host"
    }
    return _emit_cluster_script(nodes, action), host_scripts
