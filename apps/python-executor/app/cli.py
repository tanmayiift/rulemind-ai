from __future__ import annotations

import argparse
import json
import sys

from .storage import Storage


def create_admin(args: argparse.Namespace) -> int:
    storage = Storage()
    try:
        user = storage.create_platform_admin_user(args.email, args.password, args.name)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps({"created": True, "user": user}, separators=(",", ":")))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin_parser = subparsers.add_parser("create-admin", help="Create the first platform admin user.")
    create_admin_parser.add_argument("--email", required=True)
    create_admin_parser.add_argument("--password", required=True)
    create_admin_parser.add_argument("--name", required=True)
    create_admin_parser.set_defaults(handler=create_admin)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
