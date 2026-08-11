#!/usr/bin/env python3
"""Install and manage the BJTU HPC dashboard as a macOS LaunchAgent."""

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_LABEL = os.getenv("HPC_DASHBOARD_LABEL", "com.example.bjtu-hpc-dashboard")
DEFAULT_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{DEFAULT_LABEL}.plist"
DEFAULT_PYTHON = Path(os.getenv("HPC_DASHBOARD_PYTHON", sys.executable)).expanduser()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_AGE_WARNING_SECONDS = 5 * 86400
STDOUT_LOG = Path("/tmp/bjtu_hpc_transfer_web.out.log")
STDERR_LOG = Path("/tmp/bjtu_hpc_transfer_web.err.log")
SECRET_KEY_RE = re.compile(r"(TOKEN|PASSWORD|SECRET|COOKIE|CERT|AUTH|CREDENTIAL|PRIVATE[_-]?KEY)", re.IGNORECASE)
LONG_SECRET_RE = re.compile(r"(?<![A-Za-z0-9._~-])[A-Za-z0-9._~-]{96,}(?![A-Za-z0-9._~-])")


def gui_target() -> str:
    return f"gui/{os.getuid()}"


def service_target(label: str) -> str:
    return f"{gui_target()}/{label}"


def run_launchctl(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["launchctl", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=check,
    )


def redact_launchctl_status(text: str) -> str:
    clean_lines = []
    for line in text.splitlines():
        clean = line
        if SECRET_KEY_RE.search(clean):
            clean = re.sub(r"(=>\s*).*$", r"\1<redacted>", clean)
            clean = re.sub(r"(=\s*).*$", r"\1<redacted>", clean)
        clean = LONG_SECRET_RE.sub("<redacted-secret>", clean)
        clean_lines.append(clean)
    return "\n".join(clean_lines)


def dashboard_args(args) -> list[str]:
    command = [
        str(args.python),
        str(ROOT / "hpc_transfer_web.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--token-guardian",
        "--guardian-accounts",
        args.guardian_accounts,
        "--guardian-interval-seconds",
        str(args.guardian_interval_seconds),
        "--guardian-refresh-every-seconds",
        str(args.guardian_refresh_every_seconds),
        "--guardian-refresh-timeout-seconds",
        str(args.guardian_refresh_timeout_seconds),
        "--guardian-failure-notify-threshold",
        str(args.guardian_failure_notify_threshold),
        "--guardian-age-warning-seconds",
        str(args.guardian_age_warning_seconds),
        "--guardian-visible-refresh-timeout-seconds",
        str(args.guardian_visible_refresh_timeout_seconds),
    ]
    if args.refresh_headless:
        command.append("--refresh-headless")
    if args.guardian_no_notifications:
        command.append("--guardian-no-notifications")
    if args.guardian_auto_visible_refresh:
        command.append("--guardian-auto-visible-refresh")
    return command


def plist_payload(args) -> dict:
    return {
        "Label": args.label,
        "ProgramArguments": dashboard_args(args),
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(args.stdout_log),
        "StandardErrorPath": str(args.stderr_log),
        "EnvironmentVariables": {
            "PATH": f"{args.python.parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
    }


def write_plist(args) -> None:
    args.plist.parent.mkdir(parents=True, exist_ok=True)
    payload = plist_payload(args)
    with args.plist.open("wb") as file:
        plistlib.dump(payload, file, sort_keys=False)
    os.chmod(args.plist, 0o644)
    print(f"[ok] wrote {args.plist}")


def bootout(args) -> None:
    result = run_launchctl(["bootout", gui_target(), str(args.plist)])
    if result.returncode == 0:
        print(f"[ok] unloaded {args.label}")
        return
    result = run_launchctl(["bootout", service_target(args.label)])
    if result.returncode == 0:
        print(f"[ok] unloaded {args.label}")


def is_loaded(args) -> bool:
    return run_launchctl(["print", service_target(args.label)]).returncode == 0


def bootstrap(args) -> int:
    result = run_launchctl(["bootstrap", gui_target(), str(args.plist)])
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return result.returncode
    print(f"[ok] loaded {args.label}")
    return 0


def kickstart(args) -> int:
    result = run_launchctl(["kickstart", "-k", service_target(args.label)])
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return result.returncode
    print(f"[ok] started {args.label}")
    return 0


def install(args) -> int:
    write_plist(args)
    bootout(args)
    code = bootstrap(args)
    if code != 0:
        return code
    return kickstart(args)


def uninstall(args) -> int:
    bootout(args)
    if args.plist.exists():
        args.plist.unlink()
        print(f"[ok] removed {args.plist}")
    return 0


def start(args) -> int:
    if not args.plist.exists():
        write_plist(args)
    if not is_loaded(args):
        code = bootstrap(args)
        if code != 0:
            return code
    return kickstart(args)


def stop(args) -> int:
    bootout(args)
    print(f"[ok] stopped {args.label}")
    return 0


def print_status(args) -> int:
    result = run_launchctl(["print", service_target(args.label)])
    if result.returncode == 0:
        print(redact_launchctl_status(result.stdout).strip())
    else:
        print(redact_launchctl_status(result.stderr.strip() or result.stdout.strip()), file=sys.stderr)

    url = f"http://{args.host}:{args.port}/api/token-guardian/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        guardian = payload.get("guardian") or {}
        accounts = ",".join(guardian.get("accounts_filter") or []) or "all"
        print(
            "[dashboard] "
            f"guardian_running={guardian.get('running')} "
            f"accounts={accounts} "
            f"last_cycle={guardian.get('last_cycle_finished_at') or '-'}"
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"[dashboard] status probe failed: {error}", file=sys.stderr)
    return 0 if result.returncode == 0 else result.returncode


def show_plist(args) -> int:
    print(plistlib.dumps(plist_payload(args), sort_keys=False).decode("utf-8"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the BJTU HPC dashboard LaunchAgent.")
    parser.add_argument("command", choices=["install", "uninstall", "start", "stop", "restart", "status", "plist"])
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--plist", type=Path, default=DEFAULT_PLIST)
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--guardian-accounts", default="all")
    parser.add_argument("--guardian-interval-seconds", type=int, default=300)
    parser.add_argument("--guardian-refresh-every-seconds", type=int, default=1800)
    parser.add_argument("--guardian-refresh-timeout-seconds", type=int, default=60)
    parser.add_argument("--guardian-failure-notify-threshold", type=int, default=3)
    parser.add_argument("--guardian-age-warning-seconds", type=int, default=DEFAULT_AGE_WARNING_SECONDS)
    parser.add_argument("--guardian-visible-refresh-timeout-seconds", type=int, default=900)
    parser.add_argument("--guardian-no-notifications", action="store_true")
    parser.add_argument("--guardian-auto-visible-refresh", action="store_true")
    parser.add_argument("--refresh-headless", action="store_true")
    parser.add_argument("--stdout-log", type=Path, default=STDOUT_LOG)
    parser.add_argument("--stderr-log", type=Path, default=STDERR_LOG)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    args.plist = args.plist.expanduser()
    args.python = args.python.expanduser()
    if args.command == "install":
        return install(args)
    if args.command == "uninstall":
        return uninstall(args)
    if args.command == "start":
        return start(args)
    if args.command == "stop":
        return stop(args)
    if args.command == "restart":
        write_plist(args)
        bootout(args)
        code = bootstrap(args)
        return code if code != 0 else kickstart(args)
    if args.command == "status":
        return print_status(args)
    if args.command == "plist":
        return show_plist(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
