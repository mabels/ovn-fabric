# reconciler/host/test_reconcile.py — stdlib unittest, same reasoning
# as reconciler/linux_net/test_reconcile.py (no pytest, keep the "zero
# installs" principle consistent for tests too).

from __future__ import annotations

import datetime
import importlib
import unittest
from unittest import mock

mod = importlib.import_module("reconciler.host.reconcile")


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher_hostname = mock.patch.object(mod, "hostname", return_value="test-router")
        patcher_uname = mock.patch.object(mod, "_uname_a", return_value="Linux test-router 6.1.0 #1 SMP x86_64 GNU/Linux")
        self.addCleanup(patcher_hostname.stop)
        self.addCleanup(patcher_uname.stop)
        patcher_hostname.start()
        patcher_uname.start()

    def test_key_and_kind(self) -> None:
        nodes = mod.reconcile("host:test-router")
        node = nodes["host:test-router"]
        self.assertEqual(node["kind"], "infra.host")
        self.assertEqual(node["key"], "host:test-router")

    def test_data_carries_hostname_uname_and_timestamp(self) -> None:
        nodes = mod.reconcile("host:test-router")
        data = nodes["host:test-router"]["data"]
        self.assertEqual(data["name"], "test-router")
        self.assertEqual(data["unameA"], "Linux test-router 6.1.0 #1 SMP x86_64 GNU/Linux")
        # a real ISO-8601 timestamp with a UTC offset, not a naive one —
        # datetime.fromisoformat round-trips it and the offset is present
        parsed = datetime.datetime.fromisoformat(data["reconciledAt"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_no_real_subprocess_or_network_call_needed(self) -> None:
        # both hostname() and _uname_a() are mocked in setUp — this just
        # documents that reconcile() itself makes no other real calls,
        # so it's safe to run this test suite anywhere, not just on a
        # real router.
        mod.reconcile("host:test-router")

    def test_produces_nothing_for_a_real_namespace_pass(self) -> None:
        # host identity is per-host, not per-netns — cli.py's
        # orchestration loop calls every reconciler once per namespace
        # uniformly, so this must degrade to a no-op rather than
        # producing the same host node once per namespace.
        self.assertEqual(mod.reconcile("host:test-router", "ns-uplink-voda-avm"), {})


if __name__ == "__main__":
    unittest.main()
