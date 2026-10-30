from __future__ import annotations

import json
import unittest
from typing import Any

from . import generated as g
from .message import (
    Msg,
    ProtocolError,
    decode,
    encode,
    from_parts,
    header,
    register_bodies,
    to_parts,
)
from .services import sysdeps, version

register_bodies(version.BODIES)


def _header(**fields: Any) -> dict[str, Any]:
    return {"kind": "MsgHeader", "id": "m1", "time": 1.0, "source": "cli", **fields}


class TransposeTest(unittest.TestCase):
    def test_a_valid_collection_becomes_one_structure(self) -> None:
        msg = from_parts([_header(reqId="r1"), {"kind": "VersionReq"}])
        self.assertEqual(msg.kind, "VersionReq")
        self.assertEqual(msg.role, "request")
        self.assertEqual(msg.req_id, "r1")
        self.assertIsInstance(msg.body, g.VersionReq)

    def test_round_trip_writes_a_fixed_order(self) -> None:
        msg = Msg(
            header=header("rt", req_id="r1"),
            body=g.VersionRes(kind="VersionRes", runtime="0.1.0", protocol=1),
        )
        parts = to_parts(msg)
        self.assertEqual([part["kind"] for part in parts], ["MsgHeader", "VersionRes"])
        self.assertNotIn("parentReqId", parts[0])  # absent optionals are left out
        self.assertEqual(from_parts(parts), msg)
        self.assertEqual(decode(encode(msg)), msg)

    def test_part_order_on_the_wire_does_not_matter(self) -> None:
        msg = from_parts([{"kind": "VersionReq"}, _header(reqId="r1")])
        self.assertEqual(msg.kind, "VersionReq")

    def test_an_unknown_header_is_ignored(self) -> None:
        msg = from_parts(
            [_header(reqId="r1"), {"kind": "TraceHeader", "x": 1}, {"kind": "VersionReq"}]
        )
        self.assertEqual(msg.kind, "VersionReq")

    def test_enums_and_numbers_are_typed(self) -> None:
        msg = from_parts([_header(), {"kind": "LogMsg", "level": "warn", "message": "m"}])
        self.assertIs(msg.body.level, g.LogLevel.warn)
        self.assertIsInstance(msg.header.time, float)

    def test_invalid_collections(self) -> None:
        cases: list[tuple[object, str]] = [
            ({"kind": "VersionReq"}, "bad-message"),
            ([_header(reqId="r1"), "x"], "bad-part"),
            ([_header(reqId="r1")], "bad-message"),
            ([_header(reqId="r1"), {"kind": "VersionReq"}, {"kind": "LogMsg"}], "bad-message"),
            ([_header(reqId="r1"), {"kind": "LaunchReq"}], "unknown-body"),
            ([_header(), _header(), {"kind": "VersionReq"}], "duplicate-part"),
            ([{"kind": "VersionReq"}], "missing-part"),
            ([_header(), {"kind": "VersionReq"}], "missing-req-id"),
            ([_header(reqId="r1", extra=1), {"kind": "VersionReq"}], "bad-part"),
            ([_header(reqId="r1", time="now"), {"kind": "VersionReq"}], "bad-part"),
            ([_header(), {"kind": "LogMsg", "level": "loud", "message": "m"}], "bad-part"),
            ([_header(reqId="r1"), {"kind": "VersionRes", "runtime": "x"}], "bad-part"),
        ]
        for raw, code in cases:
            with self.subTest(raw=json.dumps(raw)):
                with self.assertRaises(ProtocolError) as caught:
                    from_parts(raw)
                self.assertEqual(caught.exception.code, code)

    def test_an_error_keeps_the_req_id_to_answer_with(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            from_parts([_header(reqId="r9"), {"kind": "LaunchReq"}])
        self.assertEqual(caught.exception.req_id, "r9")

    def test_lists_and_nested_parts(self) -> None:
        register_bodies(sysdeps.BODIES)
        res = g.ResolveSystemDepsRes(
            kind="ResolveSystemDepsRes",
            deps=[
                g.DepStatus(dep="curl", state=g.SystemDepState.ready),
                g.DepStatus(dep="nope", state=g.SystemDepState.failed, message="E: not found"),
            ],
        )
        msg = Msg(header=header("rt", req_id="r1"), body=res)
        parts = to_parts(msg)
        self.assertEqual(
            parts[1]["deps"],
            [
                {"dep": "curl", "state": "ready"},
                {"dep": "nope", "state": "failed", "message": "E: not found"},
            ],
        )
        self.assertEqual(from_parts(parts), msg)
        bad = [_header(reqId="r1"), {"kind": "ResolveSystemDepsReq", "deps": "curl"}]
        with self.assertRaises(ProtocolError):
            from_parts(bad)

    def test_bad_json(self) -> None:
        with self.assertRaises(ProtocolError) as caught:
            decode(b"[{")
        self.assertEqual(caught.exception.code, "bad-json")


if __name__ == "__main__":
    unittest.main()
