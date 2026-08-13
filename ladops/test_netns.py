# ladops/test_netns.py — stdlib unittest for the shared netns plumbing
# every net-related ladops module (linux_net, iptables, ovn, ovs) is
# built on, and reconciler/cli.py's namespace sweep uses directly.

from __future__ import annotations

import unittest
from unittest import mock

from ladops import netns as mod

# real `ip netns list` output includes a "(id: N)" suffix per line —
# only the leading name is the actual namespace `ip netns exec` expects.
FAKE_NETNS_LIST = """\
ns-uplink-zerotier (id: 5)
ns-uplink-mullvad-us (id: 4)
ns-uplink-mullvad-de (id: 3)
ns-uplink-starlink (id: 2)
ns-uplink-voda-modem (id: 1)
ns-uplink-voda-avm (id: 0)
"""


class ListNetnsTest(unittest.TestCase):
    def test_strips_the_id_suffix(self) -> None:
        with mock.patch.object(
            mod.subprocess, "run", return_value=mock.Mock(stdout=FAKE_NETNS_LIST)
        ) as run:
            names = mod.list_netns()
        run.assert_called_once_with(
            ["ip", "netns", "list"], check=True, capture_output=True, text=True
        )
        self.assertEqual(
            names,
            [
                "ns-uplink-zerotier",
                "ns-uplink-mullvad-us",
                "ns-uplink-mullvad-de",
                "ns-uplink-starlink",
                "ns-uplink-voda-modem",
                "ns-uplink-voda-avm",
            ],
        )

    def test_empty_output_is_no_namespaces_not_one_blank_entry(self) -> None:
        with mock.patch.object(mod.subprocess, "run", return_value=mock.Mock(stdout="")):
            self.assertEqual(mod.list_netns(), [])


class NetnsScopeTest(unittest.TestCase):
    def test_global_is_the_literal_asterisk_global(self) -> None:
        self.assertEqual(
            mod.netns_scope({"host": "test"}, None), {"host": "test", "netns": "*global*"}
        )

    def test_real_namespace_is_a_sub_scope_of_host(self) -> None:
        scope = mod.netns_scope({"host": "test"}, "ns-uplink-voda-avm")
        self.assertEqual(scope, {"host": "test", "netns": "ns-uplink-voda-avm"})

    def test_does_not_mutate_the_base_scope_dict(self) -> None:
        base = {"host": "test"}
        mod.netns_scope(base, "ns-uplink-voda-avm")
        self.assertEqual(base, {"host": "test"})


class ScopeIdTest(unittest.TestCase):
    def test_host_only_scope(self) -> None:
        self.assertEqual(mod.scope_id({"host": "test"}), "host:test")

    def test_host_and_netns_scope(self) -> None:
        self.assertEqual(
            mod.scope_id({"host": "test", "netns": "ns-uplink-voda-avm"}),
            "host:test|netns:ns-uplink-voda-avm",
        )

    def test_matches_the_output_of_netns_scope(self) -> None:
        scope = mod.netns_scope({"host": "test"}, None)
        self.assertEqual(mod.scope_id(scope), "host:test|netns:*global*")


class RunTest(unittest.TestCase):
    def test_global_runs_argv_directly(self) -> None:
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.run(["ip", "-j", "addr", "show"], None)
        run.assert_called_once_with(
            ["ip", "-j", "addr", "show"], check=True, capture_output=True, text=True
        )

    def test_real_namespace_is_wrapped_in_ip_netns_exec(self) -> None:
        with mock.patch.object(mod.subprocess, "run") as run:
            mod.run(["ip", "-j", "addr", "show"], "ns-uplink-voda-avm")
        run.assert_called_once_with(
            ["ip", "netns", "exec", "ns-uplink-voda-avm", "ip", "-j", "addr", "show"],
            check=True,
            capture_output=True,
            text=True,
        )


# add_netns/delete_netns/add_if_to_netns/delete_if_to_netns are never
# run for real here (or anywhere in this session) — this project's only
# real router is live production infrastructure; these are verified by
# asserting the exact argv built for `mod.run`, not by executing it.
class NetnsExecArgvTest(unittest.TestCase):
    def test_global_leaves_argv_unwrapped(self) -> None:
        argv = ["ip", "link", "add", "dummy-left", "type", "dummy"]
        self.assertEqual(mod.netns_exec_argv(argv, None), argv)

    def test_real_namespace_is_wrapped_in_ip_netns_exec(self) -> None:
        self.assertEqual(
            mod.netns_exec_argv(["ip", "link", "set", "dummy-left", "up"], "ns-kernel-0"),
            ["ip", "netns", "exec", "ns-kernel-0", "ip", "link", "set", "dummy-left", "up"],
        )


class WriteTest(unittest.TestCase):
    def test_add_netns_argv(self) -> None:
        self.assertEqual(
            mod.add_netns_argv("ns-uplink-test"), ["ip", "netns", "add", "ns-uplink-test"]
        )

    def test_add_netns_builds_the_real_ip_command(self) -> None:
        with mock.patch.object(mod, "run") as run:
            mod.add_netns("ns-uplink-test")
        run.assert_called_once_with(["ip", "netns", "add", "ns-uplink-test"], None)

    def test_delete_netns_argv(self) -> None:
        self.assertEqual(
            mod.delete_netns_argv("ns-uplink-test"), ["ip", "netns", "delete", "ns-uplink-test"]
        )

    def test_delete_netns_builds_the_real_ip_command(self) -> None:
        with mock.patch.object(mod, "run") as run:
            mod.delete_netns("ns-uplink-test")
        run.assert_called_once_with(["ip", "netns", "delete", "ns-uplink-test"], None)


if __name__ == "__main__":
    unittest.main()
