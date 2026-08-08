# ladops/test_ovs.py — stdlib unittest, no pytest.
#
# Fixture is synthetic but shape-accurate, modeled on real `ovs-vsctl -f
# json list Interface` rows captured from the router (patch ports and a
# real internal bridge port were both present in the real capture).

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("ladops.ovs")

FAKE_INTERFACES = [
    {
        "name": "patch-lsp-uplink-voda-modem-transfer-localnet-to-br-int",
        "type": "patch",
        "admin_state": "up",
        "link_state": "up",
        "ofport": 1,
    },
    {
        "name": "br-bd-4",
        "type": "internal",
        "admin_state": "down",
        "link_state": "down",
        "ofport": 65534,
    },
]


def _fake_list_table(argv, table):
    assert argv == ["ovs-vsctl"]
    assert table == "Interface"
    return FAKE_INTERFACES


class ListInterfacesTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "list_table", side_effect=_fake_list_table)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.interfaces = mod.list_interfaces()

    def test_no_facts_lost(self) -> None:
        self.assertEqual(len(self.interfaces), len(FAKE_INTERFACES))

    def test_type_and_state_carried_through(self) -> None:
        iface = next(i for i in self.interfaces if i["name"].startswith("patch-"))
        self.assertEqual(iface["type"], "patch")
        self.assertEqual(iface["adminState"], "up")
        self.assertEqual(iface["linkState"], "up")
        self.assertEqual(iface["ofport"], 1)


if __name__ == "__main__":
    unittest.main()
