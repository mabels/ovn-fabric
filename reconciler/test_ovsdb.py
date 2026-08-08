# reconciler/test_ovsdb.py — stdlib unittest for the shared OVSDB JSON
# decoding both reconciler/ovn and reconciler/ovs are built on.

from __future__ import annotations

import json
import unittest
from unittest import mock

from reconciler import ovsdb as mod


class DecodeTest(unittest.TestCase):
    def test_scalars_pass_through(self) -> None:
        self.assertEqual(mod.decode("hello"), "hello")
        self.assertEqual(mod.decode(42), 42)
        self.assertIsNone(mod.decode(None))

    def test_uuid(self) -> None:
        self.assertEqual(mod.decode(["uuid", "abc-123"]), "abc-123")

    def test_empty_and_populated_set(self) -> None:
        self.assertEqual(mod.decode(["set", []]), [])
        self.assertEqual(mod.decode(["set", ["10.99.0.1/28", "fe80::1/64"]]), ["10.99.0.1/28", "fe80::1/64"])

    def test_set_of_uuids_decodes_each_element(self) -> None:
        self.assertEqual(
            mod.decode(["set", [["uuid", "a"], ["uuid", "b"]]]),
            ["a", "b"],
        )

    def test_map(self) -> None:
        self.assertEqual(
            mod.decode(["map", [["hosting-chassis", "effd37ab"], ["reside-on-redirect-chassis", "true"]]]),
            {"hosting-chassis": "effd37ab", "reside-on-redirect-chassis": "true"},
        )

    def test_a_plain_two_element_list_that_isnt_a_tagged_atom_passes_through(self) -> None:
        # real OVSDB tags are always "uuid"/"set"/"map" as the first
        # element — anything else two-element-shaped (shouldn't occur in
        # practice, but the tag check must be specific, not "any 2-list")
        self.assertEqual(mod.decode(["not-a-tag", "value"]), ["not-a-tag", "value"])


FAKE_LIST_OUTPUT = json.dumps(
    {
        "data": [
            [["uuid", "u1"], "lrp-home", ["set", ["192.168.128.1/24"]]],
            [["uuid", "u2"], "lrp-usa", ["set", []]],
        ],
        "headings": ["_uuid", "name", "networks"],
    }
)


class ListTableTest(unittest.TestCase):
    def test_rows_are_keyed_by_real_heading_names_with_atoms_decoded(self) -> None:
        with mock.patch.object(mod, "run_in_netns", return_value=mock.Mock(stdout=FAKE_LIST_OUTPUT)) as run:
            rows = mod.list_table(["ovn-nbctl"], "Logical_Router_Port")
        run.assert_called_once_with(["ovn-nbctl", "-f", "json", "list", "Logical_Router_Port"], None)
        self.assertEqual(
            rows,
            [
                {"_uuid": "u1", "name": "lrp-home", "networks": ["192.168.128.1/24"]},
                {"_uuid": "u2", "name": "lrp-usa", "networks": []},
            ],
        )


if __name__ == "__main__":
    unittest.main()
