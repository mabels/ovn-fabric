# ladops/test_ovn.py — stdlib unittest, no pytest.
#
# Fixtures are synthetic but shape-accurate, modeled directly on real
# `ovn-nbctl -f json list Logical_Router`/`Logical_Router_Port` output
# captured from the router: router-home legitimately owns more than one
# LRP, the exact case that broke the ADR's literal `router:<scope>|lrp`
# key (no per-port identity) before reconciler/ovn/reconcile.py's own
# key construction was fixed to include it.
#
# add_lrp/delete_lrp are never run for real here (or anywhere in this
# session) — this project's only real router is live production
# infrastructure; these are verified by asserting the exact argv built
# for `ovn-nbctl`, not by executing it.

from __future__ import annotations

import importlib
import unittest
from unittest import mock

mod = importlib.import_module("ladops.ovn")

FAKE_ROUTERS = [
    {"_uuid": "r-home", "name": "router-home", "ports": ["lrp-home-uuid", "lrp-home-bb-uuid"]},
    {"_uuid": "r-usa", "name": "router-usa", "ports": ["lrp-usa-uuid"]},
]

FAKE_PORTS = [
    {
        "_uuid": "lrp-home-uuid",
        "name": "lrp-home",
        "mac": "00:00:c0:a8:80:01",
        "networks": ["192.168.128.1/24", "fd00:192:168:128::1/64"],
        "gateway_chassis": ["effd37ab-685f-4c33-8c67-43017f4c7c52"],
    },
    {
        "_uuid": "lrp-home-bb-uuid",
        "name": "lrp-home-bb",
        "mac": "00:00:0a:50:08:01",
        "networks": ["10.80.8.1/16"],
        "gateway_chassis": ["effd37ab-685f-4c33-8c67-43017f4c7c52"],
    },
    {
        "_uuid": "lrp-usa-uuid",
        "name": "lrp-usa",
        "mac": "00:00:c0:a8:83:01",
        "networks": ["192.168.131.1/24"],
        "gateway_chassis": [],
    },
    {
        "_uuid": "lrp-orphan-uuid",
        "name": "lrp-orphan",
        "mac": "00:00:00:00:00:00",
        "networks": [],
        "gateway_chassis": [],
    },
]


def _fake_list_table(argv, table):
    assert argv == ["ovn-nbctl"]
    if table == "Logical_Router":
        return FAKE_ROUTERS
    if table == "Logical_Router_Port":
        return FAKE_PORTS
    raise AssertionError(f"unexpected table: {table}")


class ListLrpsTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(mod, "list_table", side_effect=_fake_list_table)
        self.addCleanup(patcher.stop)
        patcher.start()
        self.lrps = mod.list_lrps()

    def test_a_router_with_multiple_lrps_reports_each_one(self) -> None:
        names = {lrp["name"] for lrp in self.lrps if lrp["router"] == "router-home"}
        self.assertEqual(names, {"lrp-home", "lrp-home-bb"})

    def test_mac_networks_and_gateway_chassis_carried_through(self) -> None:
        lrp = next(item for item in self.lrps if item["name"] == "lrp-home")
        self.assertEqual(lrp["mac"], "00:00:c0:a8:80:01")
        self.assertEqual(lrp["networks"], ["192.168.128.1/24", "fd00:192:168:128::1/64"])
        self.assertEqual(lrp["gatewayChassis"], ["effd37ab-685f-4c33-8c67-43017f4c7c52"])

    def test_a_port_not_attached_to_any_router_is_skipped_not_crashed_on(self) -> None:
        self.assertEqual(len(self.lrps), 3)
        self.assertFalse(any(lrp["name"] == "lrp-orphan" for lrp in self.lrps))


class WriteTest(unittest.TestCase):
    def test_add_lrp_builds_the_real_ovn_nbctl_command(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.add_lrp("router-home", "lrp-home", "00:00:c0:a8:80:01", ["192.168.128.1/24"])
        run.assert_called_once_with(
            [
                "ovn-nbctl",
                "lrp-add",
                "router-home",
                "lrp-home",
                "00:00:c0:a8:80:01",
                "192.168.128.1/24",
            ],
            None,
        )

    def test_delete_lrp_uses_if_exists(self) -> None:
        with mock.patch.object(mod, "run_in_netns") as run:
            mod.delete_lrp("lrp-home")
        run.assert_called_once_with(["ovn-nbctl", "--if-exists", "lrp-del", "lrp-home"], None)

    def test_lr_add_has_no_may_exist(self) -> None:
        self.assertEqual(mod.lr_add_argv("router-home"), ["ovn-nbctl", "lr-add", "router-home"])

    def test_lr_del_uses_if_exists(self) -> None:
        self.assertEqual(
            mod.lr_del_argv("router-home"), ["ovn-nbctl", "--if-exists", "lr-del", "router-home"]
        )

    def test_ls_add_has_no_may_exist(self) -> None:
        self.assertEqual(mod.ls_add_argv("home"), ["ovn-nbctl", "ls-add", "home"])

    def test_ls_del_uses_if_exists(self) -> None:
        self.assertEqual(mod.ls_del_argv("home"), ["ovn-nbctl", "--if-exists", "ls-del", "home"])

    def test_lsp_add_router_bundles_four_calls(self) -> None:
        self.assertEqual(
            mod.lsp_add_router_argv("home", "lsp-router-home-left", "lrp-router-home-left"),
            [
                ["ovn-nbctl", "lsp-add", "home", "lsp-router-home-left"],
                ["ovn-nbctl", "lsp-set-type", "lsp-router-home-left", "router"],
                ["ovn-nbctl", "lsp-set-addresses", "lsp-router-home-left", "router"],
                [
                    "ovn-nbctl",
                    "lsp-set-options",
                    "lsp-router-home-left",
                    "router-port=lrp-router-home-left",
                ],
            ],
        )

    def test_lsp_add_localnet_bundles_four_calls(self) -> None:
        self.assertEqual(
            mod.lsp_add_localnet_argv("home", "lsp-home-localnet", "net-home"),
            [
                ["ovn-nbctl", "lsp-add", "home", "lsp-home-localnet"],
                ["ovn-nbctl", "lsp-set-type", "lsp-home-localnet", "localnet"],
                ["ovn-nbctl", "lsp-set-addresses", "lsp-home-localnet", "unknown"],
                ["ovn-nbctl", "lsp-set-options", "lsp-home-localnet", "network_name=net-home"],
            ],
        )

    def test_lsp_del_uses_if_exists(self) -> None:
        self.assertEqual(
            mod.lsp_del_argv("lsp-home-localnet"),
            ["ovn-nbctl", "--if-exists", "lsp-del", "lsp-home-localnet"],
        )


if __name__ == "__main__":
    unittest.main()
