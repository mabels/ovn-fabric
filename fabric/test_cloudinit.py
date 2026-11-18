from __future__ import annotations

import base64
import json
import unittest

from runtime.message import plain

from . import cloudinit
from .ir_fixture import NODES

yaml = None
try:
    import yaml  # type: ignore[import-untyped,no-redef]
except ImportError:  # only the round trip needs it
    pass


def _ir() -> dict[str, object]:
    return {"kind": "IrCollection", "collection": [plain(n) for n in NODES]}


class CloudInitTest(unittest.TestCase):
    def test_the_ir_hosts(self) -> None:
        collection = _ir()["collection"]
        assert isinstance(collection, list)
        self.assertEqual(cloudinit.ir_hosts(collection), ["central-1", "chassis-1"])

    def test_the_script_installs_then_runs_the_ir(self) -> None:
        text = cloudinit.script("chassis-1", "lab")
        install = text.index(f"python3 {cloudinit.PEX} runtime-install")
        apply = text.index(f"ir-apply run --put {cloudinit.IR} --name lab --host chassis-1")
        self.assertLess(install, apply)
        self.assertNotIn("--host", cloudinit.script(None, None))

    def test_it_starts_as_cloud_config_and_names_the_host(self) -> None:
        text = cloudinit.render(b"pex", _ir(), "chassis-1")
        self.assertTrue(text.startswith("#cloud-config\n"))
        self.assertIn('hostname: "chassis-1"', text)
        self.assertNotIn("hostname", cloudinit.render(b"pex", _ir()))

    @unittest.skipIf(yaml is None, "no PyYAML")
    def test_every_file_comes_back_exactly(self) -> None:
        pex = bytes(range(256)) * 50
        ir = _ir()
        assert yaml is not None
        doc = yaml.safe_load(cloudinit.render(pex, ir, "central-1", "lab"))
        files = {f["path"]: f for f in doc["write_files"]}
        self.assertEqual(base64.b64decode(files[cloudinit.PEX]["content"]), pex)
        self.assertEqual(json.loads(files[cloudinit.IR]["content"]), ir)
        self.assertEqual(files[cloudinit.SCRIPT]["content"], cloudinit.script("central-1", "lab"))
        self.assertEqual(doc["runcmd"], [[cloudinit.SCRIPT]])


if __name__ == "__main__":
    unittest.main()
