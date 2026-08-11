#!/usr/bin/env python3.12
import argparse
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from hpc_runtime import require_controller_python

require_controller_python()

from hpc_account_store import AccountStoreError, get_account, load_store, sync_legacy_token


ROOT = Path(__file__).resolve().parent


def run_step(label, command, allow_fail=False):
    print(f"[step] {label}", flush=True)
    result = subprocess.run(command, cwd=ROOT, text=True, check=False)
    if result.returncode and not allow_fail:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")
    return result.returncode


def python_cmd(*parts):
    return [sys.executable, *map(str, parts)]


def resolve_account(name):
    selected, _ = get_account(name)
    return selected


def validate_account(account):
    code = run_step(
        f"validate saved token for {account}",
        python_cmd(ROOT / "hpc_accounts.py", "validate", account),
        allow_fail=True,
    )
    return code == 0


def sync_legacy_if_default(account):
    if load_store().get("default") != account:
        return False
    sync_legacy_token(account)
    return True


def refresh_account(
    account,
    *,
    headless,
    timeout,
    fresh_page,
    clear_existing_token=False,
    clear_auth_session=False,
):
    command = python_cmd(
        ROOT / "hpc_accounts.py",
        "refresh",
        account,
        "--browser",
        "playwright",
        "--timeout",
        str(timeout),
    )
    if headless:
        command.append("--headless")
    if fresh_page:
        command.append("--fresh-page")
    if clear_existing_token:
        command.append("--clear-existing-token")
    if clear_auth_session:
        command.append("--clear-auth-session")
    return run_step(
        f"refresh {account} token with {'headless' if headless else 'visible'} Playwright",
        command,
        allow_fail=True,
    )


def verify_real_portal_call(enabled, account):
    if not enabled:
        return
    run_step(
        "verify token with a real portal job-list call",
        python_cmd(
            ROOT / "hpc_jobs.py",
            "list",
            "--size",
            "3",
            "--auth-account",
            account,
        ),
    )


def safe_label(value):
    text = str(value or "all").strip() or "all"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "all"


def run_capture_step(label, command, output_path):
    print(f"[step] {label}", flush=True)
    output_path = Path(output_path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")
    print(f"[ok] saved {output_path}", flush=True)


def run_post_status(args, account):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if args.after_jobs_keyword is not None:
        command = [
            *python_cmd(ROOT / "hpc_jobs.py", "list"),
            "--keyword",
            args.after_jobs_keyword,
            "--size",
            str(args.after_jobs_size),
            "--auth-account",
            account,
        ]
        if args.after_jobs_paths:
            command.append("--paths")
        run_step(
            f"post-login portal job status keyword={args.after_jobs_keyword or '<empty>'}",
            command,
        )

        if args.after_snapshot_dir:
            snapshot_dir = Path(args.after_snapshot_dir).expanduser()
            json_path = snapshot_dir / (
                f"bjtu_jobs_{timestamp}_{safe_label(args.after_jobs_keyword)}_after_login.json"
            )
            run_capture_step(
                "save post-login portal job status JSON",
                [
                    *python_cmd(ROOT / "hpc_jobs.py", "list"),
                    "--keyword",
                    args.after_jobs_keyword,
                    "--size",
                    str(args.after_jobs_size),
                    "--paths",
                    "--json",
                    "--auth-account",
                    account,
                ],
                json_path,
            )

    for target in args.after_pending_job:
        command = [
            *python_cmd(ROOT / "hpc_pending_reason.py", str(target)),
            "--auth-account",
            account,
        ]
        if args.after_pending_no_sinfo:
            command.append("--no-sinfo")
        run_step(f"post-login native Slurm status for {target}", command)


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Robust BJTU HPC token recovery flow: validate current token, try a short "
            "headless Playwright refresh, then fall back to a visible Playwright login."
        )
    )
    parser.add_argument(
        "account",
        nargs="?",
        default=os.getenv("HPC_AUTH_ACCOUNT"),
        help="Saved hpc_accounts.py auth account. Defaults to HPC_AUTH_ACCOUNT or the stored default.",
    )
    parser.add_argument("--force", action="store_true", help="Refresh even if the current token validates")
    parser.add_argument(
        "--visible-only",
        action="store_true",
        help=(
            "Prefer the visible browser fallback, but still validate the saved token and "
            "briefly probe the selected Playwright profile first."
        ),
    )
    parser.add_argument("--no-visible", action="store_true", help="Do not open the visible browser fallback")
    parser.add_argument("--headless-timeout", type=int, default=30)
    parser.add_argument(
        "--profile-probe-timeout",
        type=int,
        default=12,
        help=(
            "Seconds for a short headless probe of the selected Playwright profile before "
            "opening a visible window. This avoids duplicate popups when the user just logged in."
        ),
    )
    parser.add_argument(
        "--no-profile-probe-before-visible",
        action="store_true",
        help="Open the visible window without first probing the selected Playwright profile.",
    )
    parser.add_argument("--visible-timeout", type=int, default=600)
    parser.add_argument("--no-job-check", action="store_true", help="Skip final hpc_jobs.py verification")
    parser.add_argument(
        "--after-jobs-keyword",
        help="After a usable token is available, print hpc_jobs.py list for this keyword.",
    )
    parser.add_argument("--after-jobs-size", type=int, default=30)
    parser.add_argument("--after-jobs-paths", action="store_true")
    parser.add_argument(
        "--after-snapshot-dir",
        type=Path,
        help="Also save a post-login hpc_jobs.py JSON snapshot under this directory.",
    )
    parser.add_argument(
        "--after-pending-job",
        action="append",
        default=[],
        help="After login, run hpc_pending_reason.py for this Slurm job id or exact job name. Can be repeated.",
    )
    parser.add_argument(
        "--after-pending-no-sinfo",
        action="store_true",
        help="Pass --no-sinfo to post-login hpc_pending_reason.py checks.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        account = resolve_account(args.account)
    except AccountStoreError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 2

    try:
        if not args.force and validate_account(account):
            sync_legacy_if_default(account)
            verify_real_portal_call(not args.no_job_check, account)
            run_post_status(args, account)
            print(f"[ok] existing token for {account} is usable")
            return 0

        if not args.visible_only:
            refresh_account(
                account,
                headless=True,
                timeout=args.headless_timeout,
                fresh_page=False,
            )
            if validate_account(account):
                sync_legacy_if_default(account)
                verify_real_portal_call(not args.no_job_check, account)
                run_post_status(args, account)
                print(f"[ok] refreshed {account} token headlessly")
                return 0

        if not args.no_profile_probe_before_visible:
            refresh_account(
                account,
                headless=True,
                timeout=args.profile_probe_timeout,
                fresh_page=True,
            )
            if validate_account(account):
                sync_legacy_if_default(account)
                verify_real_portal_call(not args.no_job_check, account)
                run_post_status(args, account)
                print(f"[ok] refreshed {account} token from the existing Playwright profile")
                return 0

        if args.no_visible:
            print("[error] no usable token/profile was found and visible fallback is disabled", file=sys.stderr)
            return 1

        print(
            "[action] A Playwright Chromium window should open now. Finish CAS login there, "
            "wait for the HPC portal page to load, then close the window. If a window opens "
            "and closes immediately after a recent login, keep this command running; it is "
            "usually validating the token already persisted in the profile.",
            flush=True,
        )
        visible_code = refresh_account(
            account,
            headless=False,
            timeout=args.visible_timeout,
            fresh_page=True,
            clear_existing_token=True,
            clear_auth_session=True,
        )
        if visible_code != 0:
            print("[error] visible Playwright refresh failed; validate the saved token/profile, then rerun --visible-only if auth is still invalid", file=sys.stderr)
            return 1
        if validate_account(account):
            sync_legacy_if_default(account)
            verify_real_portal_call(not args.no_job_check, account)
            run_post_status(args, account)
            print(f"[ok] refreshed {account} token through visible Playwright")
            return 0
    except RuntimeError as error:
        print(f"[error] {error}", file=sys.stderr)
        return 1

    print("[error] visible refresh finished but the saved token did not validate", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
