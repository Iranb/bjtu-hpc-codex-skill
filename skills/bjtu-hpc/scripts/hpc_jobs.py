#!/usr/bin/env python3.12
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from hpc_runtime import require_controller_python

require_controller_python()

from hpc_account_store import apply_auth_account_defaults
from hpc_portal_api import (
    BASE_URL,
    DEFAULT_TOKEN_FILE,
    create_session,
    load_token,
    refresh_token,
    request_json,
)

DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_PORTAL_USER = os.getenv("HPC_PORTAL_USER", "")
TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED"}


def query_jobs(session, token, cluster, portal_user, keyword="", page=0, size=20, history=False):
    endpoint = "job/hist-jobs" if history else "job/list"
    params = {
        "pno": str(page),
        "psize": str(size),
        "cluster_id": cluster,
        "keyword": keyword or "",
        "order": "time",
        "reverse": "0",
        "user": portal_user,
    }
    url = f"{BASE_URL}/{endpoint}?{urlencode(params)}"
    return request_json(session, "GET", url, headers={"PARA_ATOKEN": token})


def cancel_job(session, token, platform_id, cluster):
    url = f"{BASE_URL}/job/{platform_id}/cancel"
    return request_json(
        session,
        "POST",
        url,
        headers={"PARA_ATOKEN": token},
        data={"cluster_id": cluster},
    )


def normalize_rows(data):
    if isinstance(data, dict):
        return data.get("data") or []
    return []


def row_matches(row, target):
    target = str(target)
    return target in {
        str(row.get("id") or ""),
        str(row.get("jobId") or ""),
        str(row.get("name") or ""),
    }


def select_target_row(rows, target):
    exact = [row for row in rows if row_matches(row, target)]
    if exact:
        return exact[0]
    return rows[0] if rows else None


def is_done(row):
    return row.get("done") == 1 or str(row.get("state") or "").upper() in TERMINAL_STATES


def format_time(value):
    if not value:
        return "-"
    try:
        return datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def compact(value, width):
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def print_table(rows, show_paths=False):
    headers = ["platform_id", "slurm_id", "state", "done", "nodes", "partition", "name", "submit"]
    widths = [11, 8, 10, 4, 14, 9, 34, 19]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        values = [
            row.get("id"),
            row.get("jobId"),
            row.get("state") or row.get("originState"),
            row.get("done"),
            row.get("nodes"),
            row.get("part"),
            row.get("name"),
            format_time(row.get("submit")),
        ]
        print("  ".join(compact(value, width).ljust(width) for value, width in zip(values, widths)))
        if show_paths:
            stdout = row.get("stdOutput") or "-"
            workdir = row.get("workDir") or "-"
            err = row.get("err") or ""
            print(f"  stdout: {stdout}")
            print(f"  workDir: {workdir}")
            if err:
                print(f"  err: {err}")


def print_json(rows):
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def load_auth(args):
    token_file = args.token_file.expanduser()
    auth_account = getattr(args, "auth_account", None)
    token = load_token(args.token, token_file, auth_account=auth_account)
    if args.refresh_token:
        token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=auth_account)
    if not token:
        print(
            f"Missing token. Run: python3 hpc_refresh_token.py or retry with --refresh-token. "
            f"You can also set HPC_PARA_ATOKEN / --token / --token-file {args.token_file}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


def add_common_auth_args(parser):
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"), help="Saved auth account name from hpc_accounts.py")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")


def handle_list(args, session, token):
    rows = []
    if args.scope in {"current", "both"}:
        rows.extend(
            normalize_rows(
                query_jobs(
                    session,
                    token,
                    args.cluster,
                    args.portal_user,
                    keyword=args.keyword,
                    page=args.page,
                    size=args.size,
                    history=False,
                )
            )
        )
    if args.scope in {"history", "both"}:
        rows.extend(
            normalize_rows(
                query_jobs(
                    session,
                    token,
                    args.cluster,
                    args.portal_user,
                    keyword=args.keyword,
                    page=args.page,
                    size=args.size,
                    history=True,
                )
            )
        )

    if args.json:
        print_json(rows)
    else:
        print_table(rows, show_paths=args.paths)
    return 0


def handle_wait(args, session, token):
    last_state = None
    deadline = time.monotonic() + args.timeout if args.timeout else None
    while True:
        rows = normalize_rows(
            query_jobs(
                session,
                token,
                args.cluster,
                args.portal_user,
                keyword=args.target,
                page=0,
                size=args.size,
                history=False,
            )
        )
        row = select_target_row(rows, args.target)

        if row:
            state = f"{row.get('state')} origin={row.get('originState')} done={row.get('done')}"
            if state != last_state or args.verbose:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    f"name={row.get('name')} platform_id={row.get('id')} "
                    f"slurm_id={row.get('jobId')} state={row.get('state')} "
                    f"origin={row.get('originState')} done={row.get('done')} nodes={row.get('nodes')}",
                    flush=True,
                )
                last_state = state
            if is_done(row):
                print_table([row], show_paths=True)
                return 0 if str(row.get("originState") or row.get("state")).upper() == "COMPLETED" else 1
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] no matching job yet: {args.target}", flush=True)

        if deadline and time.monotonic() >= deadline:
            print(f"[timeout] job did not finish within {args.timeout}s", file=sys.stderr)
            return 124
        time.sleep(args.interval)


def handle_cancel(args, session, token):
    rows = normalize_rows(
        query_jobs(
            session,
            token,
            args.cluster,
            args.portal_user,
            keyword=args.target,
            page=0,
            size=args.size,
            history=False,
        )
    )
    row = select_target_row(rows, args.target)
    if not row:
        print(f"no matching job: {args.target}", file=sys.stderr)
        return 1

    platform_id = row.get("id")
    if not platform_id:
        print(f"job has no platform id: {row}", file=sys.stderr)
        return 1

    print_table([row], show_paths=args.paths)
    if args.dry_run:
        print("[dry-run] add --yes to cancel this job")
        return 0
    if not args.yes:
        print("refusing to cancel without --yes", file=sys.stderr)
        return 2

    result = cancel_job(session, token, platform_id, args.cluster)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1


def build_parser():
    parser = argparse.ArgumentParser(description="Query and wait for BJTU HPC web-submitted jobs.")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list", help="List current/history jobs")
    add_common_auth_args(list_parser)
    list_parser.add_argument("--keyword", default="", help="Filter by job name or id keyword")
    list_parser.add_argument("--scope", choices=["current", "history", "both"], default="current")
    list_parser.add_argument("--page", type=int, default=0)
    list_parser.add_argument("--size", type=int, default=20)
    list_parser.add_argument("--paths", action="store_true", help="Print stdout/workDir paths")
    list_parser.add_argument("--json", action="store_true")

    wait_parser = subparsers.add_parser("wait", help="Poll until a job finishes")
    add_common_auth_args(wait_parser)
    wait_parser.add_argument("target", help="Job name, SLURM job id, or platform id")
    wait_parser.add_argument("--interval", type=int, default=10)
    wait_parser.add_argument("--timeout", type=int, default=0, help="Seconds; 0 means no timeout")
    wait_parser.add_argument("--size", type=int, default=20)
    wait_parser.add_argument("--verbose", action="store_true")

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a queued/running job by platform id/name/job id")
    add_common_auth_args(cancel_parser)
    cancel_parser.add_argument("target", help="Job name, SLURM job id, or platform id")
    cancel_parser.add_argument("--size", type=int, default=20)
    cancel_parser.add_argument("--paths", action="store_true")
    cancel_parser.add_argument("--dry-run", action="store_true")
    cancel_parser.add_argument("--yes", action="store_true", help="Required to actually cancel")

    parser.set_defaults(command="list")
    return parser


def main():
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("-"):
        argv = ["list", *argv]
    args = build_parser().parse_args(argv)
    apply_auth_account_defaults(
        args,
        default_cluster=DEFAULT_CLUSTER,
        default_portal_user=DEFAULT_PORTAL_USER,
    )
    token = load_auth(args)
    session = create_session()
    session.headers.update({"Accept": "application/json, text/plain, */*"})

    if args.command == "wait":
        return handle_wait(args, session, token)
    if args.command == "cancel":
        return handle_cancel(args, session, token)
    return handle_list(args, session, token)


if __name__ == "__main__":
    raise SystemExit(main())
