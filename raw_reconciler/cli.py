from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import sys

from .collect import collect


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="raw-reconciler",
        description="Collect live host state as the raw graph snapshot.",
    )
    parser.add_argument("--host", default=None, help="override the host name")
    parser.add_argument("--output", default=None, help="write JSON here")
    parser.add_argument("--nice", action="store_true", help="pretty-print")
    args = parser.parse_args(argv)

    snapshot = collect(args.host)
    payload = dataclasses.asdict(snapshot)
    text = json.dumps(
        payload,
        indent=2 if args.nice else None,
        separators=None if args.nice else (",", ":"),
        default=lambda value: value.value if isinstance(value, enum.Enum) else value,
    )
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")
    else:
        try:
            sys.stdout.write(text + "\n")
        except BrokenPipeError:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
