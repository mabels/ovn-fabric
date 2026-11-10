from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any
from unittest import mock

from fabric.primitives.net import AddrPrimitive
from fabric.primitives.ovn import OVNNbCtlLs, OVNNbCtlLsp, OVNNbCtlNat
from fabric.primitives.ovs import OVSBridge, OVSMapping

from . import __main__ as primate_main
from . import discover, learn, module, samples, system
from .samples import Sample


def _addr_snapshots(netns: str, link: str, addr: str, ifindex: int) -> tuple[Any, Any, Any]:
    """What an address on a dummy link in a netns looks like in RawIR."""
    ip, length = addr.split("/")
    before = {
        f"netns:{netns}": {"id": f"netns:{netns}", "kind": "netns", "linkRefs": []},
        f"link:{netns}|{link}": {"id": f"link:{netns}|{link}", "kind": "link", "ifindex": ifindex},
    }
    added = {
        f"addr:{netns}|{link}|{addr}": {
            "id": f"addr:{netns}|{link}|{addr}",
            "kind": "addr",
            "linkRef": f"link:{netns}|{link}",
            "address": ip,
            "prefixlen": int(length),
            "family": "ipv4",
        }
    }
    after = {**before, **added}
    return before, after, dict(before)


class LearnTest(unittest.TestCase):
    def test_markers_become_placeholders_and_volatile_fields_drop_out(self) -> None:
        runs = []
        for netns, link, addr, ifindex in (
            ("pma0aaaaaa", "pma1bbbbbb", "10.251.18.33/27", 7),
            ("pmb0cccccc", "pmb1dddddd", "10.252.18.65/26", 9),
        ):
            before, after, undone = _addr_snapshots(netns, link, addr, ifindex)
            markers = {"netns": netns, "link": link, "addr": addr}
            runs.append(learn.observe(before, after, undone, markers))
        template = learn.template(*runs)
        self.assertEqual(
            template["added"],
            {
                "addr:{netns}|{link}|{addr}": {
                    "id": "addr:{netns}|{link}|{addr}",
                    "kind": "addr",
                    "linkRef": "link:{netns}|{link}",
                    "address": "{addr|ip}",
                    "prefixlen": "{addr|len}",
                    "family": "ipv4",
                }
            },
        )
        self.assertEqual(template["leftover"], [])

    def test_what_the_live_system_does_meanwhile_is_no_part_of_it(self) -> None:
        sample: dict[str, Any] = {
            "added": {"ovn.ls:{name}": {"name": "{name}"}},
            "changed": {},
            "leftover": [],
        }
        noisy = {
            **sample,
            "added": {**sample["added"], "ovn.lr:router-x": {"name": "router-x"}},
        }
        self.assertEqual(set(learn.template(noisy, sample)["added"]), {"ovn.ls:{name}"})
        broken: dict[str, Any] = {"added": {}, "changed": {}, "leftover": []}
        with self.assertRaises(learn.Unexplained):
            learn.template(sample, broken)  # the sample's own node in one run only

    def test_a_parameter_only_in_what_it_takes_away_is_kept(self) -> None:
        template = {
            "class": "LinkMovePrimitive",
            "params": {"host": "str", "netns": "str", "name": "str", "to": "str"},
            "fixed": {},
            "added": {"link:{to}|{name}": {"ifname": "{name}"}},
            "changed": {},
            "removed": ["link:{netns}|{name}"],
            "volatile": [],
            "leftover": [],
        }
        namespace: dict[str, Any] = {}
        exec(
            compile(module.render({"link_move": template}, "a test"), "generated", "exec"),
            namespace,
        )
        footprint = namespace["link_move"](host="h", netns="a", name="eth0", to="b")
        self.assertEqual(footprint["removed"], ["link:a|eth0"])

    def test_what_it_takes_away_is_recorded(self) -> None:
        before = {"link:pma0aaaaaa|pma1bbbbbb": {"ifname": "pma1bbbbbb"}, "netns:x": {}}
        after = {"link:pma2cccccc|pma1bbbbbb": {"ifname": "pma1bbbbbb"}}
        markers = {"netns": "pma0aaaaaa", "name": "pma1bbbbbb", "to": "pma2cccccc"}
        seen = learn.observe(before, after, before, markers)
        self.assertEqual(seen["removed"], ["link:{netns}|{name}"])  # netns:x: not its own
        self.assertEqual(set(seen["added"]), {"link:{to}|{name}"})

    def test_delete_that_does_not_undo_is_reported(self) -> None:
        before, after, _ = _addr_snapshots("pma0aaaaaa", "pma1bbbbbb", "10.251.18.33/27", 7)
        seen = learn.observe(before, after, after, {"netns": "pma0aaaaaa"})
        self.assertEqual(seen["leftover"], ["addr:pma0aaaaaa|pma1bbbbbb|10.251.18.33/27"])


class ModuleTest(unittest.TestCase):
    def test_the_generated_library_predicts_from_a_primitive(self) -> None:
        template = {
            "class": "AddrPrimitive",
            "params": {"host": "str", "netns": "str | None", "link": "str", "addr": "str"},
            "fixed": {},
            "kinds": {"netns": "name", "link": "name", "addr": "cidr"},
            "added": {
                "addr:{netns}|{link}|{addr}": {
                    "linkRef": "link:{netns}|{link}",
                    "address": "{addr|ip}",
                    "prefixlen": "{addr|len}",
                    "family": "ipv4",
                }
            },
            "changed": {},
            "volatile": [],
            "leftover": [],
        }
        source = module.render({"addr": template}, "a test")
        namespace: dict[str, Any] = {}
        exec(compile(source, "generated", "exec"), namespace)
        predicted = namespace["rawir"](
            AddrPrimitive(host="h", netns="ns-x", link="eth0", addr="192.168.128.32/24")
        )
        self.assertEqual(
            predicted["added"],
            {
                "addr:ns-x|eth0|192.168.128.32/24": {
                    "linkRef": "link:ns-x|eth0",
                    "address": "192.168.128.32",
                    "prefixlen": 24,
                    "family": "ipv4",
                }
            },
        )
        with self.assertRaises(namespace["NotLearned"]):
            namespace["rawir"](OVSBridge("br-x"))  # no footprint learned for it
        with self.assertRaisesRegex(namespace["NotLearned"], "learned only with IPv4"):
            namespace["rawir"](
                AddrPrimitive(host="h", netns="ns-x", link="eth0", addr="fd00::1/64")
            )

    def test_a_fixed_field_refuses_other_values(self) -> None:
        template = {
            "class": "OVSBridge",
            "params": {"name": "str", "datapath": "str | None", "fail_mode": "str"},
            "fixed": {"datapath": None, "fail_mode": "standalone"},
            "added": {"ovs.bridge:{name}": {"name": "{name}"}},
            "changed": {},
            "volatile": [],
            "leftover": [],
        }
        namespace: dict[str, Any] = {}
        exec(
            compile(module.render({"ovs_bridge": template}, "a test"), "generated", "exec"),
            namespace,
        )
        self.assertIn("ovs.bridge:br-x", namespace["rawir"](OVSBridge("br-x"))["added"])
        with self.assertRaisesRegex(namespace["NotLearned"], "datapath='netdev'"):
            namespace["rawir"](OVSBridge("br-x", datapath="netdev"))

    def test_a_free_field_may_be_unset_or_set(self) -> None:
        # It leaves no trace in RawIR; a marked field that may be None does.
        template = {
            "class": "OVNNbCtlNat",
            "params": {
                "router": "str",
                "type": "str",
                "external_ip": "str",
                "logical_ip": "str",
                "reset": "bool",
                "gateway_port": "str | None",
            },
            "fixed": {"type": "snat", "reset": False},
            "free": ["gateway_port"],
            "added": {"ovn.nat:{router}|{external_ip}|{logical_ip}": {"type": "snat"}},
            "changed": {},
            "volatile": [],
            "leftover": [],
        }
        namespace: dict[str, Any] = {}
        exec(
            compile(module.render({"ovn_nb_ctl_nat": template}, "a test"), "generated", "exec"),
            namespace,
        )
        for port in (None, "lrp-x"):
            nat = OVNNbCtlNat("r", "snat", "10.0.0.1", "10.1.0.0/24", gateway_port=port)
            self.assertIn("ovn.nat:r|10.0.0.1|10.1.0.0/24", namespace["rawir"](nat)["added"])

    def test_shapes_of_one_class_are_tried_in_order_pairs_by_key(self) -> None:
        params = {
            "switch": "str",
            "name": "str",
            "type": "str | None",
            "addresses": "tuple[str, ...]",
            "options": "tuple[tuple[str, str], ...]",
        }
        plain = {
            "class": "OVNNbCtlLsp",
            "params": params,
            "fixed": {"type": None, "addresses": ("unknown",), "options": ()},
            "added": {"ovn.lsp:{switch}|{name}": {"options": {}}},
            "changed": {},
            "volatile": ["uuid"],
            "leftover": [],
        }
        localnet = {
            **plain,
            "fixed": {"type": "localnet", "addresses": ("unknown",)},
            "pairs": {"options": ["network_name"]},
            "added": {"ovn.lsp:{switch}|{name}": {"options": {"network_name": "{options_0}"}}},
            "volatile": ["tag"],
        }
        source = module.render(
            {"ovn_nb_ctl_lsp": plain, "ovn_nb_ctl_lsp_localnet": localnet}, "a test"
        )
        namespace: dict[str, Any] = {}
        exec(compile(source, "generated", "exec"), namespace)
        rawir, not_learned = namespace["rawir"], namespace["NotLearned"]
        self.assertEqual(
            rawir(OVNNbCtlLsp("ls", "p", "localnet", ("unknown",), (("network_name", "phys"),))),
            {
                "added": {"ovn.lsp:ls|p": {"options": {"network_name": "phys"}}},
                "changed": {},
                "removed": [],
            },
        )
        self.assertIn(
            "ovn.lsp:ls|p", rawir(OVNNbCtlLsp("ls", "p", addresses=("unknown",)))["added"]
        )
        with self.assertRaisesRegex(not_learned, "keys \\['other'\\]"):
            rawir(OVNNbCtlLsp("ls", "p", "localnet", ("unknown",), (("other", "x"),)))
        self.assertEqual(namespace["VOLATILE"]["OVNNbCtlLsp"], ["uuid", "tag"])

    def test_an_each_field_applies_one_element_to_all(self) -> None:
        template = {
            "class": "OVSMapping",
            "params": {"mappings": "tuple[str, ...]"},
            "fixed": {},
            "kinds": {"network": "name", "bridge": "name"},
            "each": {"mappings": "{network}:{bridge}"},
            "added": {},
            "changed": {
                "ovs.bridge:{bridge}": {
                    "externalIds": {"from": {}, "to": {"ovn-bridge-mappings": "{network}"}}
                }
            },
            "volatile": [],
            "leftover": [],
        }
        namespace: dict[str, Any] = {}
        source = module.render({"ovs_mapping": template}, "a test")
        exec(compile(source, "generated", "exec"), namespace)
        footprint = namespace["rawir"](OVSMapping(("home:br-home", "usa:br-usa")))
        self.assertEqual(
            footprint["changed"],
            {
                "ovs.bridge:br-home": {
                    "externalIds": {"from": {}, "to": {"ovn-bridge-mappings": "home"}}
                },
                "ovs.bridge:br-usa": {
                    "externalIds": {"from": {}, "to": {"ovn-bridge-mappings": "usa"}}
                },
            },
        )
        with self.assertRaisesRegex(namespace["NotLearned"], "is no"):
            namespace["rawir"](OVSMapping(("no-separator",)))


class KeepTest(unittest.TestCase):
    def test_a_setting_is_cleared_for_the_sample_and_comes_back(self) -> None:
        with (
            mock.patch.object(samples, "list_connections", return_value=["ptcp:6641:0.0.0.0"]),
            mock.patch.object(system, "run") as run,
        ):
            restore = samples._keep_connection("nb")()
            run.assert_called_once_with(["ovn-nbctl", "del-connection"])
            restore()
        run.assert_called_with(["ovn-nbctl", "set-connection", "ptcp:6641:0.0.0.0"])

    def test_a_global_setting_is_restored_also_when_the_sample_fails(self) -> None:
        calls: list[str] = []

        def keep() -> Callable[[], None]:
            calls.append("saved")
            return lambda: calls.append("restored")

        sample = Sample(OVNNbCtlLs, keep=keep)
        with (
            mock.patch.object(system, "create", side_effect=system.CommandFailed("boom")),
            mock.patch.object(system, "delete"),
            self.assertRaises(system.CommandFailed),
        ):
            primate_main._observe(sample, 0, "h")
        self.assertEqual(calls, ["saved", "restored"])


class DiscoverTest(unittest.TestCase):
    def test_every_primitive_is_found_and_the_builder_is_not(self) -> None:
        found = {f.name: f for f in discover.primitives()}
        self.assertIn("OVNNbCtlLs", found)
        self.assertIn("AddrPrimitive", found)
        self.assertNotIn("HostPrimitive", found)
        self.assertTrue(found["OVSPort"].commands)
        self.assertFalse(found["DirectoryPrimitive"].commands)
        addr = {p.name: p for p in found["AddrPrimitive"].params}
        self.assertTrue(addr["addr"].required)


if __name__ == "__main__":
    unittest.main()
