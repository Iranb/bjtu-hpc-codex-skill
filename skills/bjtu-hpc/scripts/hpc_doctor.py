#!/usr/bin/env python3.12
"""Structured readiness checks for the BJTU HPC helper workspace."""

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from hpc_runtime import require_controller_python

require_controller_python()

from hpc_account_store import (
    AccountStoreError,
    DEFAULT_ACCOUNTS_FILE,
    DEFAULT_BROWSER_PROFILE_ROOT,
    DEFAULT_LEGACY_TOKEN_FILE,
    get_account,
    list_account_summaries,
    profile_dir_for,
)
from hpc_refresh_token import INVALID_TOKEN_CODES, redact_token_text, validate_token


ROOT = Path(__file__).resolve().parent
REQUIRED_SCRIPTS = [
    "hpc_account_migration.py",
    "hpc_account_store.py",
    "hpc_accounts.py",
    "hpc_credentials.py",
    "hpc_doctor.py",
    "hpc_jobs.py",
    "hpc_pending_reason.py",
    "hpc_portal_api.py",
    "hpc_queue_summary.py",
    "hpc_refresh_flow.py",
    "hpc_refresh_token.py",
    "hpc_resource_history.py",
    "hpc_runtime.py",
    "hpc_token_identity.py",
    "hpc_winscp_info.py",
]
OPTIONAL_FULL_WORKSPACE_SCRIPTS = [
    "hpc_upload.py",
    "hpc_submit.py",
    "dataset_upload_progress.py",
    "hpc_mcp_server.py",
]
PYTHON_MODULES = ["requests", "paramiko", "playwright", "mcp", "jsonschema", "cryptography"]
COMMANDS = ["ssh", "screen", "tar"]


def ok_item(**extra: Any) -> dict[str, Any]:
    return {"ok": True, **extra}


def fail_item(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


def module_check(name: str) -> dict[str, Any]:
    spec = importlib.util.find_spec(name)
    return ok_item(origin=spec.origin) if spec else fail_item("module not importable")


def command_check(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return ok_item(path=path) if path else fail_item("command not found on PATH")


def run_probe(command: list[str], timeout: int) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return fail_item(f"timed out after {timeout}s")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-1000:],
        "stderr_tail": proc.stderr[-1000:],
    }


def redacted_validation(validation: dict[str, Any]) -> dict[str, Any]:
    message = validation.get("msg") or validation.get("message") or validation.get("raw")
    ok = validation.get("code") not in INVALID_TOKEN_CODES
    http_status = validation.get("http_status")
    if isinstance(http_status, int) and http_status >= 400:
        ok = False
    safe_message = redact_token_text(message) if message else None
    return {
        "ok": ok,
        "code": validation.get("code"),
        "http_status": http_status,
        "success": validation.get("success"),
        "message": str(safe_message)[:300] if safe_message else None,
    }


def auth_report(args: argparse.Namespace) -> dict[str, Any]:
    token_file = args.token_file.expanduser()
    selected_name = args.auth_account or os.getenv("HPC_AUTH_ACCOUNT")
    account_error = None
    selected_summary = None
    token_source = None
    profile_dir = None

    try:
        summaries = list_account_summaries()
    except Exception as error:
        summaries = []
        account_error = str(error)

    if selected_name:
        try:
            resolved_name, entry = get_account(selected_name)
            selected_name = resolved_name
            selected_summary = {
                "name": resolved_name,
                "portal_user": entry.get("portal_user"),
                "cluster": entry.get("cluster"),
                "account": entry.get("account"),
                "has_token": bool(str(entry.get("token") or "").strip()),
                "token_updated_at": entry.get("token_updated_at"),
                "token_validated_at": entry.get("token_validated_at"),
            }
            profile_dir = profile_dir_for(resolved_name, entry)
        except AccountStoreError as error:
            account_error = str(error)
    elif summaries:
        default_rows = [row for row in summaries if row.get("default")]
        if default_rows:
            selected_name = default_rows[0]["name"]
            selected_summary = default_rows[0]
            profile_dir = Path(str(default_rows[0].get("profile_dir") or ""))

    env_token = os.getenv("HPC_PARA_ATOKEN")
    if env_token:
        token_source = "HPC_PARA_ATOKEN"
    elif selected_name and selected_summary and selected_summary.get("has_token"):
        token_source = f"auth_account:{selected_name}"
    elif token_file.is_file() and token_file.read_text(encoding="utf-8").strip():
        token_source = str(token_file)

    validation = None
    token = None
    if not args.no_validate:
        try:
            token = load_token_for_doctor(env_token, token_file, selected_name)
            if token:
                validation = redacted_validation(validate_token(token, timeout=args.timeout))
        except Exception as error:
            validation = fail_item(str(error))

    return {
        "ok": bool(token_source) and (validation is None or validation.get("ok")),
        "accounts_file": str(DEFAULT_ACCOUNTS_FILE),
        "accounts_file_exists": DEFAULT_ACCOUNTS_FILE.exists(),
        "legacy_token_file": str(token_file),
        "legacy_token_file_exists": token_file.exists(),
        "selected_auth_account": selected_name,
        "selected_account": selected_summary,
        "account_error": account_error,
        "token_present": bool(token_source),
        "token_source": token_source,
        "validation": validation,
        "browser_profile_root": str(DEFAULT_BROWSER_PROFILE_ROOT),
        "selected_browser_profile": str(profile_dir) if profile_dir else None,
        "selected_browser_profile_exists": profile_dir.exists() if profile_dir else None,
    }


def load_token_for_doctor(env_token: str | None, token_file: Path, auth_account: str | None) -> str | None:
    if env_token:
        return env_token.strip()
    if auth_account:
        try:
            _, entry = get_account(auth_account)
        except AccountStoreError:
            return None
        token = str(entry.get("token") or "").strip()
        if token:
            return token
    if token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    return None


def deep_report(args: argparse.Namespace) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    if not args.source_host:
        checks["source_host"] = fail_item("pass --source-host or set HPC_SOURCE_HOST")
        return checks
    checks["source_host"] = run_probe(
        [
            "ssh",
            "-n",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={args.timeout}",
            "-o",
            "ConnectionAttempts=1",
            args.source_host,
            "true",
        ],
        timeout=args.timeout + 2,
    )
    return checks


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    scripts = {
        name: ok_item(path=str(ROOT / name)) if (ROOT / name).exists() else fail_item("missing")
        for name in REQUIRED_SCRIPTS
    }
    optional_scripts = {
        name: ok_item(path=str(ROOT / name)) if (ROOT / name).exists() else fail_item("not bundled")
        for name in OPTIONAL_FULL_WORKSPACE_SCRIPTS
    }
    report = {
        "success": True,
        "workspace": {
            "root": str(ROOT),
            "cwd": str(Path.cwd()),
            "scripts": scripts,
            "optional_full_workspace_scripts": optional_scripts,
        },
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
            "platform": platform.platform(),
        },
        "dependencies": {name: module_check(name) for name in PYTHON_MODULES},
        "commands": {name: command_check(name) for name in COMMANDS},
        "auth": auth_report(args),
        "deep_checks": {},
        "recommendations": [],
    }
    if args.deep:
        report["deep_checks"] = deep_report(args)

    recommendations = report["recommendations"]
    for name, item in report["dependencies"].items():
        if not item["ok"]:
            recommendations.append(f"Install Python dependency: {name}")
    if not report["auth"]["token_present"]:
        recommendations.append("Create or refresh auth with hpc_refresh_flow.py or hpc_accounts.py.")
    elif report["auth"].get("validation") and not report["auth"]["validation"].get("ok"):
        recommendations.append("Token validation failed; refresh the selected auth account.")
    if not all(item["ok"] for item in scripts.values()):
        recommendations.append("Restore the complete bundled scripts directory.")

    report["success"] = not recommendations
    return report


def print_text(report: dict[str, Any]) -> None:
    print(f"workspace: {report['workspace']['root']}")
    print(f"python: {report['python']['executable']} ({report['python']['version']})")
    print("dependencies:")
    for name, item in report["dependencies"].items():
        status = "ok" if item["ok"] else "missing"
        print(f"  {name}: {status}")
    auth = report["auth"]
    print("auth:")
    print(f"  selected: {auth.get('selected_auth_account') or '-'}")
    print(f"  token: {'yes' if auth.get('token_present') else 'no'} ({auth.get('token_source') or '-'})")
    validation = auth.get("validation")
    if validation:
        status = "ok" if validation.get("ok") else "invalid"
        print(f"  validation: {status} code={validation.get('code')}")
    if report["recommendations"]:
        print("recommendations:")
        for item in report["recommendations"]:
            print(f"  - {item}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose BJTU HPC helper readiness without printing secrets.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_LEGACY_TOKEN_FILE)
    parser.add_argument("--no-validate", action="store_true", help="Do not call the portal token self-check endpoint.")
    parser.add_argument("--deep", action="store_true", help="Run slow optional SSH/source-host probes.")
    parser.add_argument("--source-host", default=os.getenv("HPC_SOURCE_HOST", ""))
    parser.add_argument("--timeout", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    report = build_report(parse_args())
    if "--json" in sys.argv[1:]:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text(report)
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
