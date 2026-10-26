from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence

from deployer.lifecycle import LifecycleAction, LifecycleController
from deployer.primitives_ovn import OVNNbCtlLs, OVNNbCtlLsp


class FakeJob:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[str, ...], bool]] = []

    async def run_cmd(
        self,
        label: str,
        argv: Sequence[str],
        abort_on_error: bool = True,
        background: bool = False,
    ) -> bool:
        self.commands.append((label, tuple(argv), abort_on_error))
        return True

    async def capture_cmd(
        self, label: str, argv: Sequence[str], abort_on_error: bool = True
    ) -> str:
        return ""


class OvnnPrimitiveTest(unittest.TestCase):
    def test_ls_adds_typed_command_and_raw_node(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(job, LifecycleAction.CREATE, "h")
            await lc.run(OVNNbCtlLs("backbone"))
            self.assertEqual(job.commands[0][1], ("ovn-nbctl", "ls-add", "backbone"))
            self.assertEqual(lc.graph.nodes["ovn.ls:backbone"]["kind"], "ovn.ls")

        asyncio.run(run())

    def test_lsp_emits_type_addresses_and_options(self) -> None:
        async def run() -> None:
            job = FakeJob()
            lc = LifecycleController(job, LifecycleAction.CREATE, "h")
            await lc.run(
                OVNNbCtlLsp(
                    switch="backbone",
                    name="lsp-localnet",
                    type="localnet",
                    addresses=("unknown",),
                    options=(("network_name", "net-backbone"),),
                )
            )
            argv = [c[1] for c in job.commands]
            self.assertIn(("ovn-nbctl", "lsp-add", "backbone", "lsp-localnet"), argv)
            self.assertIn(("ovn-nbctl", "lsp-set-type", "lsp-localnet", "localnet"), argv)
            self.assertIn(
                ("ovn-nbctl", "lsp-set-options", "lsp-localnet", "network_name=net-backbone"),
                argv,
            )
            node = lc.graph.nodes["ovn.lsp:backbone|lsp-localnet"]
            self.assertEqual(node["switchRef"], "ovn.ls:backbone")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
