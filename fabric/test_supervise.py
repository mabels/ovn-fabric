from __future__ import annotations

import unittest

from .supervise import BASE, HEALTHY, MAX, Supervisor


class SupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Supervisor()
        self.s.want({"dns": "workload: dns", "ts": "kernel router: ts"})

    def test_what_is_not_running_is_due(self) -> None:
        states = {"dns": "exited", "ts": "running", "other": "exited"}
        self.assertEqual(self.s.due(states, 0.0), ["dns"])
        # docker has none: missing, due too
        self.assertEqual(self.s.due({"ts": "running"}, 0.0), ["dns"])

    def test_one_that_keeps_dying_waits_longer_each_time(self) -> None:
        down = {"dns": "exited", "ts": "running"}
        waits = []
        now = 0.0
        for _ in range(10):
            self.assertEqual(self.s.due(down, now), ["dns"])
            self.s.started(["dns"], now)
            self.assertEqual(self.s.due(down, now + 1), [])  # not at once again
            waits.append(self.s.restarts["dns"].next_at - now)
            now = self.s.restarts["dns"].next_at
        self.assertEqual(waits[:3], [BASE, 2 * BASE, 4 * BASE])
        self.assertEqual(waits[-1], MAX)

    def test_running_long_enough_starts_over(self) -> None:
        self.s.started(["dns"], 0.0)
        self.s.started(["dns"], 10.0)
        self.s.healthy({"dns": "running"}, 10.0 + HEALTHY)
        self.assertEqual(self.s.restarts["dns"].count, 0)
        self.assertEqual(self.s.due({"dns": "exited", "ts": "running"}, 10.0 + HEALTHY), ["dns"])

    def test_no_longer_wanted_is_forgotten(self) -> None:
        self.s.started(["dns"], 0.0)
        self.s.want({})
        self.assertEqual((self.s.restarts, self.s.due({}, 0.0)), ({}, []))


if __name__ == "__main__":
    unittest.main()
