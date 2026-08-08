# reconciler/test_netns.py — stdlib unittest for the shared netns
# plumbing every net-related reconciler (linux_net today, iptables/ovn/
# ovs later) is built on.

from __future__ import annotations

import unittest
from unittest import mock

from reconciler import netns as mod

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
        run.assert_called_once_with(["ip", "netns", "list"], check=True, capture_output=True, text=True)
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
        self.assertEqual(mod.netns_scope("host:test", None), "host:test|netns:*global*")

    def test_real_namespace_is_a_sub_scope_of_host(self) -> None:
        scope = mod.netns_scope("host:test", "ns-uplink-voda-avm")
        self.assertEqual(scope, "host:test|netns:ns-uplink-voda-avm")
        self.assertTrue(scope.startswith("host:test|"))


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


if __name__ == "__main__":
    unittest.main()
