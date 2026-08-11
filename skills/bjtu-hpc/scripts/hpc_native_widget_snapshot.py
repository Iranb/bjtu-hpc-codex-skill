#!/usr/bin/env python3
"""Write redacted BJTU HPC queue snapshots for the WidgetKit extension."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PYTHON = sys.executable
DEFAULT_SLURM_SOURCE_DIR = os.getenv(
    "HPC_MONITOR_SLURM_SOURCE_DIR", str(Path(__file__).resolve().parent)
)
DEFAULT_SLURM_DIR = str(Path.home() / "Library" / "BJTUHPCNativeWidget" / "slurm_runtime")
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765/"
DEFAULT_ACCOUNT_CAP = 4
DEFAULT_EXTENSION_BUNDLE_ID = os.getenv(
    "HPC_NATIVE_WIDGET_BUNDLE_ID", "com.example.bjtu-hpc-native-widget.widget"
)
DEFAULT_HISTORY_LOG_NAME = "hpc_resource_history.jsonl"


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if minimum is not None:
        value = max(minimum, value)
    return value


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def clean_reason(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text or "-"


def dashboard_api_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def default_snapshot_path(extension_bundle_id: str = DEFAULT_EXTENSION_BUNDLE_ID) -> Path:
    return (
        Path.home()
        / "Library"
        / "Containers"
        / extension_bundle_id
        / "Data"
        / "Library"
        / "Application Support"
        / "BJTUHPCNativeWidget"
        / "snapshot.json"
    )


def run_queue_summary(
    python_path: str,
    slurm_dir: str,
    accounts: str | None,
    timeout: int,
    all_partitions: bool,
) -> tuple[dict[str, Any] | None, str | None, int]:
    script = str(Path(slurm_dir) / "hpc_queue_summary.py")
    command = [
        python_path,
        script,
        "--json",
        "--timeout",
        str(timeout),
        "--cap",
        str(env_int("HPC_MONITOR_ACCOUNT_CAP", DEFAULT_ACCOUNT_CAP, minimum=1)),
    ]
    if env_bool("HPC_MONITOR_RECORD_HISTORY", False):
        history_log = os.getenv("HPC_MONITOR_HISTORY_LOG") or str(
            Path(DEFAULT_SLURM_SOURCE_DIR) / "work" / DEFAULT_HISTORY_LOG_NAME
        )
        command.extend(["--history-log", history_log])
    if accounts:
        command.extend(["--accounts", accounts])
    if all_partitions:
        command.append("--all-partitions")

    attempts = env_int("HPC_MONITOR_QUERY_ATTEMPTS", 3, minimum=1)
    last_error = "queue summary produced no JSON"
    last_returncode = -1
    for attempt in range(attempts):
        try:
            proc = subprocess.run(
                command,
                cwd=slurm_dir,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(timeout * 2 + 15, 45),
                check=False,
            )
        except Exception as error:  # noqa: BLE001 - snapshot should preserve failures.
            last_error = str(error)
            last_returncode = -1
        else:
            last_returncode = proc.returncode
            try:
                payload = json.loads(proc.stdout)
            except Exception as error:  # noqa: BLE001
                last_error = proc.stderr.strip() or proc.stdout.strip() or str(error)
            else:
                # Queue history is best-effort and may emit a warning on stderr
                # even when the live queue/resource payload was produced.
                if proc.returncode == 0:
                    return payload, None, proc.returncode
                return (
                    payload,
                    proc.stderr.strip() or f"queue summary exited with {proc.returncode}",
                    proc.returncode,
                )
        if attempt + 1 < attempts:
            time.sleep(1)
    return None, last_error, last_returncode


def fetch_guardian_status(dashboard_url: str, timeout: int = 3) -> tuple[dict[str, Any] | None, str | None]:
    try:
        with urllib.request.urlopen(
            dashboard_api_url(dashboard_url, "/api/token-guardian/status"),
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return None, str(error)
    return payload.get("guardian") or {}, None


def sanitize_guardian(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in {
                "stdout",
                "stderr",
                "command",
                "cmd",
                "token",
                "cookie",
                "cookies",
                "password",
                "account",
                "portal_user",
                "user",
                "profile",
                "profile_dir",
            }:
                continue
            clean[key_text] = sanitize_guardian(item)
        return clean
    if isinstance(value, list):
        return [sanitize_guardian(item) for item in value]
    if isinstance(value, str) and len(value) > 240:
        return value[:237] + "..."
    return value


def sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return payload
    clean_accounts: list[dict[str, Any]] = []
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        clean_accounts.append(
            {
                "name": account.get("name"),
                "error": account.get("error"),
                "has_token": account.get("has_token"),
                "summary": account.get("summary") or {},
                "jobs": [
                    {
                        "job_id": job.get("job_id"),
                        "state": job.get("state"),
                        "reason": job.get("reason"),
                        "name": job.get("name"),
                    }
                    for job in (account.get("jobs") or [])
                    if isinstance(job, dict)
                ],
            }
        )
    resources = payload.get("cluster_resources")
    clean_resources = None
    if isinstance(resources, dict):
        clean_resources = {
            "error": resources.get("error"),
            "summary": resources.get("summary") or {},
            "nodes": resources.get("nodes") or [],
            "excluded_reserved_nodes": resources.get("excluded_reserved_nodes") or [],
        }
    return {
        "checked_at_local": payload.get("checked_at_local"),
        "accounts": clean_accounts,
        "cluster_resources": clean_resources,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def load_previous_payload(path: Path) -> dict[str, Any] | None:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else None
    if not isinstance(payload, dict) or not payload.get("checked_at_local"):
        return None
    if not isinstance(payload.get("accounts"), list):
        return None
    if not isinstance(payload.get("cluster_resources"), dict):
        return None
    return payload


def stable_signature(snapshot: dict[str, Any]) -> str:
    payload = snapshot.get("payload") or {}
    resources = payload.get("cluster_resources") or {}
    cluster = {
        "error": resources.get("error"),
        "summary": resources.get("summary") or {},
        "excluded_reserved_nodes": sorted(resources.get("excluded_reserved_nodes") or []),
        "nodes": sorted(
            (
                {
                    "name": node.get("name"),
                    "state": node.get("state"),
                    "cpu_alloc": as_int(node.get("cpu_alloc")),
                    "cpu_total": as_int(node.get("cpu_total")),
                    "cpu_free": as_int(node.get("cpu_free")),
                    "gpu_alloc": as_int(node.get("gpu_alloc")),
                    "gpu_total": as_int(node.get("gpu_total")),
                    "gpu_free": as_int(node.get("gpu_free")),
                }
                for node in resources.get("nodes") or []
                if isinstance(node, dict)
            ),
            key=lambda item: str(item.get("name") or ""),
        ),
    }
    accounts = []
    for account in payload.get("accounts") or []:
        if not isinstance(account, dict):
            continue
        jobs = []
        for job in account.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            resources = job.get("resources") or {}
            jobs.append(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "name": job.get("name") or "",
                    "state": str(job.get("state") or "").upper(),
                    "reason": clean_reason(job.get("reason")),
                    "node": job.get("node") or job.get("nodelist") or "",
                    "cpus": job.get("cpus") or job.get("ncpus") or resources.get("num_cpus"),
                    "gpus": job.get("gpus") or job.get("ngpus") or resources.get("gpu_count"),
                }
            )
        accounts.append(
            {
                "name": account.get("name") or "",
                "error": account.get("error") or "",
                "has_token": account.get("has_token"),
                "summary": account.get("summary") or {},
                "jobs": sorted(jobs, key=lambda item: item["job_id"]),
            }
        )

    guardian = snapshot.get("guardian") or {}
    guardian_accounts = []
    for name, row in sorted((guardian.get("accounts") or {}).items()):
        if not isinstance(row, dict):
            continue
        guardian_accounts.append(
            {
                "name": name,
                "status": row.get("status"),
                "attention_required": row.get("attention_required"),
                "attention_reason": row.get("attention_reason"),
                "headless_failure_count": row.get("headless_failure_count"),
                "age_warning": row.get("age_warning"),
                "needs_visible_login": row.get("needs_visible_login"),
                "visible_status": (row.get("visible_refresh") or {}).get("status"),
            }
        )
    visible_refreshes = []
    for name, row in sorted((guardian.get("visible_refreshes") or {}).items()):
        if not isinstance(row, dict):
            continue
        visible_refreshes.append(
            {
                "name": name,
                "status": row.get("status"),
                "returncode": row.get("returncode"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
            }
        )
    return json.dumps(
        {
            "returncode": snapshot.get("returncode"),
            "error": snapshot.get("error"),
            "guardian_error": snapshot.get("guardian_error"),
            "cluster": cluster,
            "accounts": sorted(accounts, key=lambda item: item["name"]),
            "guardian": {
                "auto_visible_refresh": guardian.get("auto_visible_refresh"),
                "notifications_enabled": guardian.get("notifications_enabled"),
                "accounts": guardian_accounts,
                "visible_refreshes": visible_refreshes,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_has_activity(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    for account in payload.get("accounts") or []:
        summary = (account or {}).get("summary") or {}
        if as_int(summary.get("total")) > 0 or account.get("error"):
            return True
    resources = payload.get("cluster_resources") or {}
    summary = resources.get("summary") or {}
    return as_int(summary.get("gpu_alloc")) > 0 or as_int(summary.get("cpu_alloc")) > 0


def guardian_visible_refresh_running(guardian: dict[str, Any] | None) -> bool:
    if not guardian:
        return False
    for row in (guardian.get("visible_refreshes") or {}).values():
        if isinstance(row, dict) and row.get("status") == "running":
            return True
    for row in (guardian.get("accounts") or {}).values():
        visible = row.get("visible_refresh") if isinstance(row, dict) else None
        if isinstance(visible, dict) and visible.get("status") == "running":
            return True
    return False


def next_interval(
    base_interval: int,
    active_interval: int,
    max_interval: int,
    busy_max_interval: int,
    stable_refreshes: int,
    changed: bool,
    payload: dict[str, Any] | None,
    returncode: int,
    guardian: dict[str, Any] | None,
) -> tuple[int, int]:
    if guardian_visible_refresh_running(guardian):
        return active_interval, 0
    if payload is None or returncode != 0 or changed:
        return base_interval, 0
    stable_refreshes += 1
    interval = min(max_interval, base_interval * max(1, stable_refreshes + 1))
    if payload_has_activity(payload):
        interval = min(interval, busy_max_interval)
    return interval, stable_refreshes


def reload_widget() -> None:
    subprocess.run(
        ["open", "-g", "bjtu-hpc-widget://reload"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def write_once(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    payload, error, returncode = run_queue_summary(
        args.python,
        args.slurm_dir,
        args.accounts,
        args.timeout,
        args.all_partitions,
    )
    stale_payload = False
    if payload is None:
        previous_payload = load_previous_payload(args.snapshot_path)
        if previous_payload is not None:
            payload = previous_payload
            stale_payload = True
    guardian, guardian_error = fetch_guardian_status(args.dashboard_url)
    snapshot = {
        "version": 1,
        "written_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "payload": sanitize_payload(payload),
        "stale_payload": stale_payload,
        "guardian": sanitize_guardian(guardian),
        "guardian_error": guardian_error,
        "error": error,
        "returncode": returncode,
    }
    atomic_write_json(args.snapshot_path, snapshot)
    return snapshot, stable_signature(snapshot)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--python", default=os.getenv("HPC_MONITOR_PYTHON", DEFAULT_PYTHON))
    parser.add_argument("--slurm-dir", default=os.getenv("HPC_MONITOR_SLURM_DIR", DEFAULT_SLURM_DIR))
    parser.add_argument("--accounts", default=os.getenv("HPC_MONITOR_ACCOUNTS") or None)
    parser.add_argument("--dashboard-url", default=os.getenv("HPC_MONITOR_DASHBOARD_URL", DEFAULT_DASHBOARD_URL))
    parser.add_argument("--snapshot-path", type=Path, default=Path(os.getenv("HPC_NATIVE_WIDGET_SNAPSHOT", default_snapshot_path())))
    parser.add_argument("--timeout", type=int, default=env_int("HPC_MONITOR_TIMEOUT", 45, minimum=10))
    parser.add_argument("--interval", type=int, default=env_int("HPC_MONITOR_INTERVAL", 60, minimum=15))
    parser.add_argument("--active-interval", type=int, default=env_int("HPC_MONITOR_ACTIVE_INTERVAL", 5, minimum=3))
    parser.add_argument("--busy-max-interval", type=int, default=env_int("HPC_MONITOR_BUSY_MAX_INTERVAL", 60, minimum=15))
    parser.add_argument("--max-interval", type=int, default=env_int("HPC_MONITOR_MAX_INTERVAL", 300, minimum=15))
    parser.add_argument("--all-partitions", action="store_true", default=env_bool("HPC_MONITOR_ALL_PARTITIONS", False))
    parser.add_argument("--no-reload", action="store_true")
    args = parser.parse_args()
    args.busy_max_interval = max(args.interval, args.busy_max_interval)
    args.max_interval = max(args.interval, args.max_interval)
    return args


def main() -> int:
    args = parse_args()
    last_signature: str | None = None
    stable_refreshes = 0
    while True:
        started = time.time()
        snapshot, signature = write_once(args)
        changed = last_signature is None or last_signature != signature
        last_signature = signature
        if not args.no_reload and (changed or guardian_visible_refresh_running(snapshot.get("guardian"))):
            reload_widget()
        interval, stable_refreshes = next_interval(
            args.interval,
            args.active_interval,
            args.max_interval,
            args.busy_max_interval,
            stable_refreshes,
            changed,
            snapshot.get("payload"),
            as_int(snapshot.get("returncode")),
            snapshot.get("guardian"),
        )
        checked_at = ((snapshot.get("payload") or {}).get("checked_at_local") or "-")
        print(
            "[native-widget] "
            f"checked_at={checked_at} rc={snapshot.get('returncode')} "
            f"changed={changed} next={interval}s "
            f"error={'yes' if snapshot.get('error') else 'no'} "
            f"guardian_error={'yes' if snapshot.get('guardian_error') else 'no'}",
            flush=True,
        )
        if args.once:
            return 0
        elapsed = time.time() - started
        time.sleep(max(1.0, interval - elapsed))


if __name__ == "__main__":
    raise SystemExit(main())
