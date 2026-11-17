from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import runtime_store


class CommandTest(unittest.TestCase):
    def test_fabric_runs_the_deployed_pex_on_the_runtime_s_socket(self) -> None:
        text = runtime_store.command_text()
        self.assertIn(f"exec python3 {runtime_store.LINK} --socket {runtime_store.SOCKET}", text)
        self.assertIn(f"PEX_ROOT={runtime_store.PEX_ROOT}", text)
        self.assertTrue(text.rstrip().endswith('"$@"'))

    def test_it_is_written_executable_but_never_over_one_not_ours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "bin" / "fabric"
            with mock.patch.object(runtime_store, "COMMAND", command):
                runtime_store._write_command()
                self.assertEqual(command.read_text(), runtime_store.command_text())
                self.assertTrue(os.access(command, os.X_OK))
                command.write_text("#!/bin/sh\necho someone else's\n")
                with mock.patch("sys.stderr"):
                    runtime_store._write_command()
                self.assertIn("someone else's", command.read_text())


class DeployTest(unittest.TestCase):
    def test_ref_uses_the_ssh_port(self) -> None:
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            code = runtime_store.deploy("root@127.0.0.1", None, "previous", 2202)
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0][:4], ["ssh", "-p", "2202", "root@127.0.0.1"])

    def test_pex_copy_uses_the_scp_and_ssh_ports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pex = Path(tmp) / "fabric.pex"
            pex.write_bytes(b"pex")
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
                code = runtime_store.deploy("root@127.0.0.1", pex, None, 2202)
        self.assertEqual(code, 0)
        scp, ssh = (call.args[0] for call in run.call_args_list)
        self.assertEqual(scp[:4], ["scp", "-q", "-P", "2202"])
        self.assertEqual(ssh[:4], ["ssh", "-p", "2202", "root@127.0.0.1"])


if __name__ == "__main__":
    unittest.main()
