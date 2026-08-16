"""Command-line entry point (plan §4.3): ``ledger validate <db>``.

Exit codes: 0 all checks passed, 1 findings reported, 2 usage error
(missing database file).
"""

import argparse
import os
import sys

from ledger.api import Ledger


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ledger", description="Obol double-entry ledger tools"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser(
        "validate",
        help="run every integrity check against a ledger database",
    )
    validate_parser.add_argument("database", help="path to the ledger SQLite file")
    args = parser.parse_args(argv)

    if not os.path.exists(args.database):
        print(f"error: no such database: {args.database}", file=sys.stderr)
        return 2
    with Ledger.open(args.database) as ledger:
        report = ledger.validate()
    print(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
