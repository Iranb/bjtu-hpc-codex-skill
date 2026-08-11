#!/usr/bin/env python3.12
import argparse
import getpass
import json
import os
import re
import sys
import warnings
import webbrowser
from pathlib import Path

from hpc_runtime import require_controller_python

require_controller_python()

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*")

from hpc_account_store import (
    AccountStoreError,
    DEFAULT_ACCOUNTS_FILE,
    DEFAULT_CREDENTIALS_FILE,
    DEFAULT_LEGACY_TOKEN_FILE,
    account_summary,
    credential_for_account,
    delete_account,
    get_account,
    list_account_summaries,
    load_store,
    profile_dir_for,
    save_account_token,
    set_default_account,
    sync_legacy_token,
    upsert_account,
)
from hpc_account_migration import (
    account_migration_is_encrypted,
    export_account_migration,
    import_account_migration,
)
from hpc_refresh_token import (
    DEFAULT_LOGIN_NAME,
    INVALID_TOKEN_CODES,
    PORTAL_URL,
    read_token,
    read_token_from_playwright,
    validate_token,
)
from hpc_token_identity import verify_token_identity


DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_ACCOUNT = os.getenv("HPC_ACCOUNT")
DEFAULT_PORTAL_USER = os.getenv("HPC_PORTAL_USER", DEFAULT_LOGIN_NAME)


def redact_message(value):
    text = "" if value is None else str(value)
    return re.sub(r"(atoken is invalid: )[^\s}]+", r"\1<redacted>", text)


def validation_summary(validation):
    if not isinstance(validation, dict):
        return {"ok": False, "message": redact_message(validation)}
    http_status = validation.get("http_status")
    ok = validation.get("code") not in INVALID_TOKEN_CODES
    if isinstance(http_status, int) and http_status >= 400:
        ok = False
    return {
        "ok": ok,
        "code": validation.get("code"),
        "http_status": http_status,
        "success": validation.get("success"),
        "message": redact_message(
            validation.get("msg") or validation.get("message") or validation.get("raw")
        ),
    }


def apply_discovered_identity(name, entry, identity, *, enabled=True):
    if not enabled:
        return entry
    updates = {
        key: value
        for key, value in identity.items()
        if value and not str(entry.get(key) or "").strip()
    }
    if not updates:
        return entry

    entry = upsert_account(
        name,
        portal_user=updates.get("portal_user"),
        cluster=updates.get("cluster"),
        account=updates.get("account"),
    )
    summary = account_summary(name, entry, is_default=(load_store().get("default") == name))
    print(
        "[ok] discovered account metadata: "
        f"portal_user={summary.get('portal_user') or '-'} "
        f"cluster={summary.get('cluster') or '-'} "
        f"account={summary.get('account') or '-'}"
    )
    return entry


def print_table(rows):
    if not rows:
        print("no saved auth accounts")
        return
    headers = ["default", "name", "portal_user", "cluster", "account", "token", "token_updated_at"]
    widths = [7, 18, 12, 10, 12, 7, 25]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        values = [
            "*" if row["default"] else "",
            row["name"],
            row.get("portal_user") or "-",
            row.get("cluster") or "-",
            row.get("account") or "-",
            "yes" if row.get("has_token") else "no",
            row.get("token_updated_at") or "-",
        ]
        print("  ".join(str(value).ljust(width)[:width] for value, width in zip(values, widths)))


def fetch_token(name, entry, args):
    if args.manual:
        return getpass.getpass("Paste DESKTOP_PARA_ATOKEN: ").strip()

    if args.browser == "playwright":
        profile_dir = Path(args.profile_dir).expanduser() if args.profile_dir else profile_dir_for(name, entry)
        credential = credential_for_account(name)
        credential_name = str(credential.get("login_name") or "").strip()
        credential_password = str(credential.get("login_password") or "")
        login_name = args.login_name if args.login_name is not None else (credential_name or entry.get("portal_user", ""))
        login_password = os.getenv(args.login_password_env, "") or credential_password
        if args.headless:
            print(
                f"Running Playwright headless for {name}. "
                "This requires that account profile to already be logged in."
            )
        else:
            print(f"A Playwright Chromium window will open for auth account {name}.")
            if login_name or login_password:
                print(
                    f"If CAS login appears, saved fields from {DEFAULT_CREDENTIALS_FILE} "
                    "will be pre-filled; enter the captcha/verification code and submit."
                )
            print(
                "Finish CAS login there, wait for the HPC portal page to load, then close "
                "the window; this script will continue automatically."
            )
        return read_token_from_playwright(
            profile_dir,
            args.headless,
            args.timeout,
            login_name,
            login_password,
            fresh_page=args.fresh_page,
            clear_existing_token=args.clear_existing_token,
            clear_auth_session=args.clear_auth_session,
        )

    if not args.no_open:
        webbrowser.open(PORTAL_URL)
    if not args.skip_wait:
        input("If the browser asks you to log in, finish login first, then press Enter here.")
    return read_token(args.browser).strip()


def refresh_one(name, args):
    name, entry = get_account(name)
    token = fetch_token(name, entry, args)
    if not token:
        raise AccountStoreError("token is empty. Make sure the portal is open and logged in.")

    summary = None
    if not args.no_validate:
        summary = validation_summary(validate_token(token, timeout=10))
        if not summary["ok"]:
            raise AccountStoreError(f"token validation failed: {summary}")

    identity = verify_token_identity(
        name,
        token,
        entry,
        cluster=getattr(args, "cluster", None),
        account=getattr(args, "account", None),
    )
    entry = save_account_token(name, token, validation=summary)
    entry = apply_discovered_identity(
        name,
        entry,
        identity,
        enabled=getattr(args, "discover_account", True),
    )
    if args.profile_dir:
        entry = upsert_account(name, profile_dir=args.profile_dir)
    legacy_path = None
    if args.sync_legacy_token:
        legacy_path = sync_legacy_token(name)

    print(f"[ok] saved token for auth account {name}")
    if legacy_path:
        print(f"[ok] synced legacy token file: {legacy_path}")
    return account_summary(name, entry, is_default=(load_store().get("default") == name))


def handle_add(args):
    portal_user = args.portal_user or (args.name if args.name.isdigit() else None)
    account = args.account
    if account is None and not args.refresh:
        account = DEFAULT_ACCOUNT
        if not account:
            raise AccountStoreError(
                "cluster account is required without --refresh; pass --account or set HPC_ACCOUNT"
            )
    upsert_account(
        args.name,
        portal_user=portal_user,
        cluster=args.cluster,
        account=account,
        profile_dir=args.profile_dir,
        set_default=args.set_default,
    )
    print(f"[ok] saved auth account {args.name}")
    if not args.refresh and args.account is None and DEFAULT_ACCOUNT:
        print(
            "[warn] cluster account defaulted to "
            f"{DEFAULT_ACCOUNT}; pass --account or run refresh to discover the real binding.",
            file=sys.stderr,
        )
    if args.refresh:
        refresh_one(args.name, args)
    return 0


def handle_list(args):
    rows = list_account_summaries()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print_table(rows)
    return 0


def handle_export_json(args):
    passphrase = None
    if args.encrypt or args.include_tokens or args.include_credentials:
        passphrase = getpass.getpass("Migration encryption passphrase (12+ characters): ")
        confirmation = getpass.getpass("Confirm migration passphrase: ")
        if passphrase != confirmation:
            raise AccountStoreError("migration passphrases do not match")
    result = export_account_migration(
        args.output,
        names=args.name,
        include_tokens=args.include_tokens,
        include_credentials=args.include_credentials,
        encrypt=args.encrypt,
        passphrase=passphrase,
        overwrite=args.force,
    )
    print(
        f"[ok] exported {len(result['accounts'])} auth account(s) to {result['path']} "
        f"(tokens={'yes' if result['includes_tokens'] else 'no'}, "
        f"credentials={'yes' if result['includes_credentials'] else 'no'}, "
        f"encrypted={'yes' if result['encrypted'] else 'no'}, mode=0600)"
    )
    if result["includes_tokens"] or result["includes_credentials"]:
        print("[info] token/CAS secrets are encrypted inside the JSON envelope; keep the passphrase separate", file=sys.stderr)
    return 0


def handle_import_json(args):
    passphrase = None
    if account_migration_is_encrypted(args.input):
        passphrase = getpass.getpass("Migration decryption passphrase: ")
    result = import_account_migration(
        args.input,
        on_conflict=args.on_conflict,
        use_exported_default=args.use_exported_default,
        passphrase=passphrase,
    )
    print(
        f"[ok] imported {len(result['imported'])} auth account(s); "
        f"skipped={len(result['skipped'])} default={result['default'] or '-'}"
    )
    if result["included_tokens"]:
        print("[warn] imported tokens may have expired; run `hpc_accounts.py validate --all` on this computer", file=sys.stderr)
    if args.sync_legacy_token and result.get("default"):
        try:
            token_path = sync_legacy_token(result["default"], token_file=args.token_file)
        except AccountStoreError as error:
            print(f"[warn] import succeeded but legacy token was not synced: {error}", file=sys.stderr)
        else:
            print(f"[ok] synced imported default token to legacy cache: {token_path}")
    print("[info] browser profiles are intentionally not imported; Playwright will create target-local profiles")
    return 0


def handle_import_legacy(args):
    token_path = args.token_file.expanduser()
    if not token_path.is_file():
        raise AccountStoreError(f"legacy token file not found: {token_path}")
    token = token_path.read_text(encoding="utf-8").strip()
    if not token:
        raise AccountStoreError(f"legacy token file is empty: {token_path}")

    try:
        _, existing_entry = get_account(args.name)
    except AccountStoreError:
        existing_entry = None
    existing_token = str((existing_entry or {}).get("token") or "").strip()
    if existing_token and existing_token != token and not args.force:
        existing_summary = validation_summary(validate_token(existing_token, timeout=10))
        if existing_summary["ok"]:
            try:
                verify_token_identity(args.name, existing_token, existing_entry)
            except AccountStoreError:
                print(
                    "[warn] existing token is generically valid but does not match this "
                    "account identity; allowing identity-checked repair",
                    file=sys.stderr,
                )
            else:
                raise AccountStoreError(
                    "refusing to overwrite an existing valid account token from the legacy "
                    "token file. Pass --force only if the legacy token is known to be newer."
                )

    portal_user = args.portal_user or (args.name if args.name.isdigit() else None)
    candidate = dict(existing_entry or {})
    for key, value in {
        "portal_user": portal_user,
        "cluster": args.cluster,
        "account": args.account,
    }.items():
        if value is not None:
            candidate[key] = value
    summary = None
    if not args.no_validate:
        summary = validation_summary(validate_token(token, timeout=10))
        if not summary["ok"]:
            raise AccountStoreError(f"token validation failed: {summary}")
    identity = verify_token_identity(
        args.name,
        token,
        candidate,
        cluster=args.cluster,
        account=args.account,
    )
    entry = upsert_account(
        args.name,
        portal_user=portal_user,
        cluster=args.cluster,
        account=args.account,
        profile_dir=args.profile_dir,
        set_default=args.set_default,
    )
    entry = save_account_token(args.name, token, validation=summary)
    apply_discovered_identity(
        args.name,
        entry,
        identity,
        enabled=getattr(args, "discover_account", True),
    )
    print(f"[ok] imported legacy token into auth account {args.name}")
    return 0


def handle_refresh(args):
    if args.all:
        store = load_store()
        names = sorted(store["accounts"])
        if not names:
            print("no saved auth accounts")
            return 0
        for name in names:
            refresh_one(name, args)
        return 0
    if not args.name:
        raise AccountStoreError("refresh requires NAME unless --all is set.")
    refresh_one(args.name, args)
    return 0


def handle_validate(args):
    if args.all:
        store = load_store()
        names = sorted(store["accounts"])
    else:
        selected, _ = get_account(args.name)
        names = [selected]

    results = []
    ok = True
    for name in names:
        _, entry = get_account(name)
        token = str(entry.get("token") or "").strip()
        if not token:
            result = {"name": name, "ok": False, "message": "no saved token"}
        else:
            result = {"name": name, **validation_summary(validate_token(token, timeout=10))}
            if result.get("ok"):
                try:
                    verify_token_identity(name, token, entry)
                except (AccountStoreError, RuntimeError) as error:
                    result["ok"] = False
                    result["message"] = redact_message(error)
        ok = ok and bool(result.get("ok"))
        results.append(result)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for result in results:
            status = "ok" if result.get("ok") else "invalid"
            bits = [f"{result['name']}: {status}"]
            if result.get("code") is not None:
                bits.append(f"code={result.get('code')}")
            if result.get("message"):
                bits.append(str(result["message"]))
            print(" ".join(bits))
    return 0 if ok else 1


def handle_use(args):
    set_default_account(args.name)
    print(f"[ok] default auth account: {args.name}")
    if not args.no_sync_legacy_token:
        try:
            token_path = sync_legacy_token(args.name, token_file=args.token_file)
            print(f"[ok] synced legacy token file: {token_path}")
        except AccountStoreError as error:
            print(f"[warn] {error}", file=sys.stderr)
    return 0


def handle_remove(args):
    if not args.yes:
        print("refusing to remove without --yes", file=sys.stderr)
        return 2
    delete_account(args.name)
    print(f"[ok] removed auth account {args.name}")
    return 0


def add_refresh_options(parser):
    parser.add_argument("--browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--manual", action="store_true", help="Paste token manually instead of reading a browser")
    parser.add_argument("--profile-dir", type=Path, help="Playwright profile directory for this account")
    parser.add_argument("--fresh-page", action="store_true", help="Playwright mode: open a new portal page instead of reusing a restored tab")
    parser.add_argument(
        "--clear-existing-token",
        action="store_true",
        help="Playwright mode: remove DESKTOP_PARA_ATOKEN from the account profile before waiting for a fresh token",
    )
    parser.add_argument(
        "--clear-auth-session",
        action="store_true",
        help="Playwright mode: clear cookies from this account profile before opening CAS login",
    )
    parser.add_argument("--login-name", help="CAS login name prefill; defaults to the saved portal user when known")
    parser.add_argument("--login-password-env", default="HPC_LOGIN_PASSWORD")
    parser.add_argument("--no-open", action="store_true", help="Chrome/Safari mode: do not open the portal")
    parser.add_argument("--skip-wait", action="store_true", help="Chrome/Safari mode: do not wait for Enter")
    parser.add_argument("--no-validate", action="store_true")
    parser.add_argument(
        "--no-discover-account",
        dest="discover_account",
        action="store_false",
        default=True,
        help="Do not fill missing portal_user/cluster/account fields after identity verification",
    )
    parser.add_argument(
        "--sync-legacy-token",
        action="store_true",
        help=f"Also write this token to {DEFAULT_LEGACY_TOKEN_FILE}",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Manage multiple BJTU HPC portal auth accounts. Tokens are stored in "
            f"{DEFAULT_ACCOUNTS_FILE} with file mode 0600."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add or update an auth account profile")
    add_parser.add_argument("name")
    add_parser.add_argument("--portal-user", help="Portal login user")
    add_parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    add_parser.add_argument("--account", help="Cluster OS account; omit with --refresh to discover it")
    add_parser.add_argument("--set-default", action="store_true")
    add_parser.add_argument("--refresh", action="store_true", help="Fetch and save a token after adding")
    add_refresh_options(add_parser)
    add_parser.set_defaults(func=handle_add)

    list_parser = subparsers.add_parser("list", help="List saved auth account profiles")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=handle_list)

    export_parser = subparsers.add_parser(
        "export-json",
        help="Export portable account metadata and optional encrypted secrets to a private JSON file",
    )
    export_parser.add_argument("output", type=Path)
    export_parser.add_argument(
        "--name",
        action="append",
        help="Export one account alias; repeat for multiple aliases (default: all)",
    )
    export_parser.add_argument(
        "--include-tokens",
        action="store_true",
        help="Include saved portal tokens; secret-bearing exports are automatically encrypted",
    )
    export_parser.add_argument(
        "--include-credentials",
        action="store_true",
        help="Include saved CAS login names/passwords; secret-bearing exports are automatically encrypted",
    )
    export_parser.add_argument(
        "--encrypt",
        action="store_true",
        help="Encrypt the complete JSON payload even when exporting metadata only",
    )
    export_parser.add_argument("--force", action="store_true", help="Overwrite an existing regular output file")
    export_parser.set_defaults(func=handle_export_json)

    import_json_parser = subparsers.add_parser(
        "import-json",
        help="Import a private BJTU HPC account migration JSON without contacting the portal",
    )
    import_json_parser.add_argument("input", type=Path)
    import_json_parser.add_argument(
        "--on-conflict",
        choices=["error", "skip", "replace"],
        default="error",
        help="Alias conflict policy; default error makes no changes",
    )
    import_json_parser.add_argument(
        "--use-exported-default",
        action="store_true",
        help="Replace the target computer's current default with the exported default",
    )
    import_json_parser.add_argument(
        "--sync-legacy-token",
        action="store_true",
        help="Also mirror the resulting default account token to the legacy token file",
    )
    import_json_parser.add_argument("--token-file", type=Path, default=DEFAULT_LEGACY_TOKEN_FILE)
    import_json_parser.set_defaults(func=handle_import_json)

    import_parser = subparsers.add_parser("import-legacy", help="Import ~/.bjtu_hpc_token into a saved auth account")
    import_parser.add_argument("name")
    import_parser.add_argument("--token-file", type=Path, default=DEFAULT_LEGACY_TOKEN_FILE)
    import_parser.add_argument("--portal-user", help="Portal login user")
    import_parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    import_parser.add_argument("--account", help="Cluster OS account; omit to discover it")
    import_parser.add_argument("--profile-dir", type=Path, help="Playwright profile directory for this account")
    import_parser.add_argument("--set-default", action="store_true")
    import_parser.add_argument("--no-validate", action="store_true")
    import_parser.add_argument(
        "--no-discover-account",
        dest="discover_account",
        action="store_false",
        default=True,
        help="Do not update portal_user/cluster/account from the portal after importing",
    )
    import_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow the legacy token file to overwrite an existing valid account token.",
    )
    import_parser.set_defaults(func=handle_import_legacy)

    refresh_parser = subparsers.add_parser("refresh", help="Fetch and save token(s)")
    refresh_parser.add_argument("name", nargs="?")
    refresh_parser.add_argument("--all", action="store_true", help="Refresh every saved auth account")
    add_refresh_options(refresh_parser)
    refresh_parser.set_defaults(func=handle_refresh)

    validate_parser = subparsers.add_parser("validate", help="Validate saved token(s)")
    validate_parser.add_argument("name", nargs="?")
    validate_parser.add_argument("--all", action="store_true")
    validate_parser.add_argument("--json", action="store_true")
    validate_parser.set_defaults(func=handle_validate)

    use_parser = subparsers.add_parser("use", help="Set the default auth account")
    use_parser.add_argument("name")
    use_parser.add_argument("--token-file", type=Path, default=DEFAULT_LEGACY_TOKEN_FILE)
    use_parser.add_argument("--no-sync-legacy-token", action="store_true")
    use_parser.set_defaults(func=handle_use)

    remove_parser = subparsers.add_parser("remove", help="Remove an auth account profile")
    remove_parser.add_argument("name")
    remove_parser.add_argument("--yes", action="store_true")
    remove_parser.set_defaults(func=handle_remove)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        return args.func(args)
    except (AccountStoreError, TimeoutError, RuntimeError) as error:
        print(f"[error] {redact_message(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
