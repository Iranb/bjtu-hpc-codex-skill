#!/usr/bin/env python3.12
import argparse
import getpass
import json
import sys

from hpc_runtime import require_controller_python

require_controller_python()

from hpc_account_store import (
    AccountStoreError,
    DEFAULT_CREDENTIALS_FILE,
    delete_account_credential,
    list_credential_summaries,
    upsert_account_credential,
)


def print_table(rows):
    if not rows:
        print("no saved login credentials")
        return
    headers = ["name", "login_name", "password", "updated_at"]
    widths = [18, 14, 8, 25]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        values = [
            row["name"],
            row.get("login_name") or "-",
            "yes" if row.get("has_password") else "no",
            row.get("updated_at") or "-",
        ]
        print("  ".join(str(value).ljust(width)[:width] for value, width in zip(values, widths)))


def handle_list(args):
    rows = list_credential_summaries()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows)
    return 0


def handle_set(args):
    password = args.password
    if password is None:
        password = getpass.getpass("CAS password: ")
    if not password:
        raise AccountStoreError("password is empty")
    upsert_account_credential(args.name, login_name=args.login_name, login_password=password)
    print(f"[ok] saved login credential for {args.name} at {DEFAULT_CREDENTIALS_FILE}")
    return 0


def handle_remove(args):
    if not args.yes:
        print("refusing to remove without --yes", file=sys.stderr)
        return 2
    delete_account_credential(args.name)
    print(f"[ok] removed login credential for {args.name}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Manage local BJTU CAS login credentials. Passwords are stored only in "
            f"{DEFAULT_CREDENTIALS_FILE} with file mode 0600 and are never printed."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List saved login credentials")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=handle_list)

    set_parser = subparsers.add_parser("set", help="Save or update one login credential")
    set_parser.add_argument("name")
    set_parser.add_argument("--login-name", required=True, help="CAS login name")
    set_parser.add_argument("--password", help=argparse.SUPPRESS)
    set_parser.set_defaults(func=handle_set)

    remove_parser = subparsers.add_parser("remove", help="Remove one saved login credential")
    remove_parser.add_argument("name")
    remove_parser.add_argument("--yes", action="store_true")
    remove_parser.set_defaults(func=handle_remove)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except AccountStoreError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
