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


class ExternalIdWriteTest(unittest.TestCase):
    def test_set_external_id_builds_the_real_ovs_vsctl_command(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.set_external_id("ovn-encap-ip", "10.0.0.1")
        run.assert_called_once_with(
            ["ovs-vsctl", "set", "open_vswitch", ".", "external-ids:ovn-encap-ip=10.0.0.1"], None
        )

    def test_remove_external_id_takes_just_the_key(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.remove_external_id("ovn-encap-ip")
        run.assert_called_once_with(
            ["ovs-vsctl", "remove", "open_vswitch", ".", "external-ids", "ovn-encap-ip"], None
        )


class BridgeAndPortWriteTest(unittest.TestCase):
    def test_add_br_has_no_may_exist(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_br("br-home")
        run.assert_called_once_with(["ovs-vsctl", "add-br", "br-home"], None)

    def test_del_br_uses_if_exists(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.del_br("br-home")
        run.assert_called_once_with(["ovs-vsctl", "--if-exists", "del-br", "br-home"], None)

    def test_set_bridge_fail_mode_defaults_to_standalone(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.set_bridge_fail_mode("br-home")
        run.assert_called_once_with(
            ["ovs-vsctl", "set", "bridge", "br-home", "fail-mode=standalone"], None
        )

    def test_add_port_has_no_may_exist(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_port("br-home", "ens18.128")
        run.assert_called_once_with(["ovs-vsctl", "add-port", "br-home", "ens18.128"], None)

    def test_del_port_uses_if_exists(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.del_port("br-home", "ens18.128")
        run.assert_called_once_with(
            ["ovs-vsctl", "--if-exists", "del-port", "br-home", "ens18.128"], None
        )


if __name__ == "__main__":
    unittest.main()
