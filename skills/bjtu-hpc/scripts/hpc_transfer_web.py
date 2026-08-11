#!/usr/bin/env python3
"""Local web dashboard for BJTU HPC token and account auth management."""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from dataset_upload_progress import connect_cluster, stat_size
from hpc_account_store import (
    DEFAULT_CREDENTIALS_FILE,
    delete_account_credential,
    get_account,
    list_account_summaries,
    list_credential_summaries,
    sync_legacy_token,
    token_for_account,
    upsert_account_credential,
)
from hpc_transfer_app import (
    DEFAULT_CONFIG,
    DEFAULT_REMOTE_WORKDIR,
    DEFAULT_SOURCE_HOST,
    UploadTask,
    derive_archive_name,
    get_remote_task_state,
    load_tasks,
    save_tasks,
    upsert_task,
)
from hpc_refresh_token import (
    DEFAULT_TOKEN_FILE as DEFAULT_HPC_TOKEN_FILE,
    INVALID_TOKEN_CODES,
    redact_token_text,
    sync_auth_account_token,
    token_validation_failed,
    validate_token,
    write_token,
)
from hpc_token_identity import verify_token_identity


ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
ACTION_LOCK = threading.Lock()
ACTIONS: dict[str, dict] = {}
PROGRESS_LOCK = threading.Lock()
PROGRESS_HISTORY: dict[str, list[dict]] = {}
MAX_PROGRESS_SAMPLES = 12
TOKEN_GUARDIAN_LOCK = threading.RLock()
TOKEN_GUARDIAN_CYCLE_LOCK = threading.Lock()
TOKEN_GUARDIAN_STOP = threading.Event()
TOKEN_GUARDIAN_THREAD: threading.Thread | None = None
TOKEN_GUARDIAN_HEADLESS_PROCS: dict[str, subprocess.Popen] = {}
TOKEN_GUARDIAN_VISIBLE_PROCS: dict[str, subprocess.Popen] = {}
TOKEN_GUARDIAN_LOG = ROOT / "hpc_token_guardian.jsonl"
HEADLESS_WARMUP_BACKOFF_SECONDS = 6 * 3600
DEFAULT_AGE_WARNING_SECONDS = 5 * 86400
TOKEN_GUARDIAN_STATE: dict = {
    "running": False,
    "interval_seconds": 300,
    "refresh_every_seconds": 1800,
    "refresh_timeout_seconds": 60,
    "failure_notify_threshold": 3,
    "age_warning_seconds": DEFAULT_AGE_WARNING_SECONDS,
    "notifications_enabled": True,
    "auto_visible_refresh": False,
    "visible_refresh_timeout_seconds": 900,
    "accounts_filter": [],
    "last_started_at": None,
    "last_stopped_at": None,
    "last_cycle_started_at": None,
    "last_cycle_finished_at": None,
    "last_run_mode": None,
    "error": None,
    "accounts": {},
    "events": [],
    "visible_refreshes": {},
}
SECRETISH_TEXT_RE = re.compile(r"(?<![A-Za-z0-9._~-])[A-Za-z0-9._~-]{96,}(?![A-Za-z0-9._~-])")


@dataclass
class WebConfig:
    config: Path
    token_file: Path
    auto_refresh_token: bool
    refresh_browser: str
    refresh_headless: bool


def utc_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %z")


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, text: str, status: int = 200, content_type: str = "text/plain") -> None:
    body = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", f"{content_type}; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def action_snapshot(key: str) -> dict | None:
    with ACTION_LOCK:
        action = ACTIONS.get(key)
        return dict(action) if action else None


def set_action(key: str, **updates) -> None:
    with ACTION_LOCK:
        current = ACTIONS.setdefault(key, {})
        current.update(updates)


def run_background(key: str, command: list[str], *, env: dict[str, str] | None = None) -> None:
    def worker() -> None:
        set_action(
            key,
            status="running",
            command=command,
            started_at=utc_now(),
            finished_at=None,
            returncode=None,
            stdout="",
            stderr="",
            error=None,
        )
        try:
            proc = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, **env} if env else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            set_action(
                key,
                status="done" if proc.returncode == 0 else "failed",
                finished_at=utc_now(),
                returncode=proc.returncode,
                stdout=sanitize_guardian_text(proc.stdout)[-12000:],
                stderr=sanitize_guardian_text(proc.stderr)[-12000:],
            )
        except Exception as error:
            set_action(key, status="failed", finished_at=utc_now(), error=sanitize_guardian_text(error))

    thread = threading.Thread(target=worker, name=f"hpc-web-{key}", daemon=True)
    thread.start()


def task_from_payload(payload: dict) -> UploadTask:
    name = str(payload.get("name") or "").strip()
    if not name or "/" in name:
        raise ValueError("Task name is required and cannot contain '/'.")
    source_path = str(payload.get("source_path") or payload.get("source") or "").strip()
    dest_path = str(payload.get("dest_path") or payload.get("dest") or "").strip()
    if not source_path:
        raise ValueError("source_path is required.")
    if not dest_path:
        raise ValueError("dest_path is required.")
    total_bytes_raw = str(payload.get("total_bytes") or "").strip()
    total_bytes = None
    if total_bytes_raw:
        total_bytes = int(total_bytes_raw)
        if total_bytes < 0:
            raise ValueError("total_bytes cannot be negative.")

    return UploadTask(
        name=name,
        source_host=str(payload.get("source_host") or DEFAULT_SOURCE_HOST).strip(),
        source_path=source_path,
        dest_path=dest_path,
        pack=bool(payload.get("pack", True)),
        screen_name=str(payload.get("screen_name") or f"bjtu-{name}").strip(),
        remote_workdir=str(payload.get("remote_workdir") or DEFAULT_REMOTE_WORKDIR).strip(),
        total_bytes=total_bytes,
        auth_account=str(payload.get("auth_account") or "").strip() or None,
    )


def find_task(config_path: Path, name: str) -> dict | None:
    for task in load_tasks(config_path):
        if task["name"] == name:
            return task
    return None


def task_progress(task: dict) -> dict:
    payload = dict(task)
    payload["archive_name"] = derive_archive_name(UploadTask(**task))
    payload["launch"] = action_snapshot(f"upload:{task['name']}")
    if task.get("total_bytes"):
        payload["remote_state"] = None
        payload["remote_state_error"] = None
        payload["remote_state_skipped"] = "known total_bytes; using cluster-side .part progress"
    else:
        try:
            state = get_remote_task_state(task)
            payload["remote_state"] = state
            payload["remote_state_error"] = None
            payload["speed_estimate"] = speed_from_remote_state(state)
        except Exception as error:
            payload["remote_state"] = None
            payload["remote_state_error"] = str(error)
    return payload


def speed_from_remote_state(state: dict | None) -> dict | None:
    if not state:
        return None
    raw_speed = state.get("current_run_speed_bytes_s")
    if raw_speed is None:
        return None
    try:
        speed = max(0.0, float(raw_speed))
        present = int(state.get("present_bytes") or 0)
        total = int(state.get("total_bytes") or 0)
    except (TypeError, ValueError):
        return None
    remaining = max(total - present, 0) if total else None
    status_text = str(state.get("status") or "")
    eta = 0.0 if status_text == "complete" else (remaining / speed if remaining is not None and speed > 0 else None)
    return {
        "source": "remote_state_current_run",
        "speed_bytes_s": speed,
        "latest_speed_bytes_s": speed,
        "average_speed_bytes_s": speed,
        "remaining_bytes": remaining,
        "eta_seconds": eta,
        "samples": 1,
        "sampled_at": utc_now(),
        "note": "reported by source-side transfer worker",
    }


def speed_from_cluster_sample(task: dict, state: dict) -> dict:
    key = f"{task.get('name')}:{task.get('dest_path')}:{state.get('total_bytes')}"
    now = time.time()
    present = int(state.get("present_bytes") or 0)
    total = int(state.get("total_bytes") or 0)
    status_text = str(state.get("status") or "")
    remaining = max(total - present, 0) if total else None

    with PROGRESS_LOCK:
        history = PROGRESS_HISTORY.setdefault(key, [])
        if history and (present < int(history[-1]["present_bytes"]) or total != int(history[-1]["total_bytes"])):
            history = []
            PROGRESS_HISTORY[key] = history
        if not history or now - float(history[-1]["time"]) >= 1:
            history.append(
                {
                    "time": now,
                    "present_bytes": present,
                    "total_bytes": total,
                    "status": status_text,
                }
            )
            del history[:-MAX_PROGRESS_SAMPLES]
        samples = [dict(item) for item in history]

    estimate = {
        "source": "cluster_sftp_delta",
        "speed_bytes_s": None,
        "latest_speed_bytes_s": None,
        "average_speed_bytes_s": None,
        "remaining_bytes": remaining,
        "eta_seconds": None,
        "samples": len(samples),
        "sampled_at": utc_now(),
        "window_seconds": 0.0,
        "note": "waiting for another progress sample",
    }
    if status_text == "complete":
        estimate.update(
            {
                "speed_bytes_s": 0.0,
                "latest_speed_bytes_s": 0.0,
                "average_speed_bytes_s": 0.0,
                "eta_seconds": 0.0,
                "note": "upload complete",
            }
        )
        return estimate
    if len(samples) < 2:
        return estimate

    latest = samples[-1]
    previous = samples[-2]
    latest_dt = max(float(latest["time"]) - float(previous["time"]), 0.001)
    latest_delta = max(int(latest["present_bytes"]) - int(previous["present_bytes"]), 0)
    latest_speed = latest_delta / latest_dt

    first = samples[0]
    window_seconds = max(float(latest["time"]) - float(first["time"]), 0.001)
    window_delta = max(int(latest["present_bytes"]) - int(first["present_bytes"]), 0)
    average_speed = window_delta / window_seconds
    speed = average_speed if average_speed > 0 else latest_speed
    eta = remaining / speed if remaining is not None and speed > 0 else None
    note = "estimated from cluster-side upload byte growth" if speed > 0 else "no byte growth in the current sample window"
    estimate.update(
        {
            "speed_bytes_s": speed,
            "latest_speed_bytes_s": latest_speed,
            "average_speed_bytes_s": average_speed,
            "eta_seconds": eta,
            "window_seconds": window_seconds,
            "note": note,
        }
    )
    return estimate


def cluster_chunk_bytes(sftp, dest_path: str) -> int:
    try:
        entries = sftp.listdir_attr(dest_path + ".chunks")
    except OSError:
        return 0
    return sum(int(entry.st_size) for entry in entries if entry.filename.endswith(".chunk"))


def all_state(config: WebConfig) -> dict:
    return {
        "time": utc_now(),
        "config": {
            "path": str(config.config),
            "token_file": str(config.token_file.expanduser()),
            "auto_refresh_token": config.auto_refresh_token,
            "refresh_browser": config.refresh_browser,
            "refresh_headless": config.refresh_headless,
        },
        "token": token_status(config),
        "accounts": list_account_summaries(),
        "credentials": credentials_status(),
        "token_guardian": guardian_snapshot(),
    }


def progress_auth_args(config: WebConfig, auth_account: str | None) -> SimpleNamespace:
    rows = list_account_summaries()
    selected = None
    if auth_account:
        selected = next((row for row in rows if row.get("name") == auth_account), None)
    else:
        selected = next((row for row in rows if row.get("default")), None)
        auth_account = str(selected.get("name") or "") if selected else None
    return SimpleNamespace(
        cluster=(selected or {}).get("cluster") or os.getenv("HPC_CLUSTER", "cluster2"),
        account=(selected or {}).get("account") or os.getenv("HPC_ACCOUNT"),
        portal_user=(selected or {}).get("portal_user") or os.getenv("HPC_PORTAL_USER", ""),
        auth_account=auth_account or None,
        token=None,
        token_file=config.token_file,
        refresh_token=False,
        refresh_browser=config.refresh_browser,
        refresh_headless=config.refresh_headless,
    )


def enrich_cluster_progress(config: WebConfig, tasks: list[dict]) -> None:
    candidates = [task for task in tasks if task.get("dest_path") and task.get("total_bytes")]
    if not candidates:
        return

    grouped: dict[str | None, list[dict]] = {}
    for task in candidates:
        auth_account = str(task.get("auth_account") or "").strip() or None
        grouped.setdefault(auth_account, []).append(task)

    for auth_account, account_tasks in grouped.items():
        args = progress_auth_args(config, auth_account)
        try:
            client, sftp, info = connect_cluster(args)
        except Exception as error:
            for task in account_tasks:
                task["cluster_state_error"] = sanitize_guardian_text(error)
            continue

        try:
            for task in account_tasks:
                dest_path = task["dest_path"]
                total = int(task["total_bytes"])
                final_size = stat_size(sftp, dest_path)
                part_size = stat_size(sftp, dest_path + ".part")
                chunk_size = cluster_chunk_bytes(sftp, dest_path)
                if final_size == total:
                    status_text = "complete"
                    present = total
                elif part_size is not None or chunk_size > 0:
                    status_text = "partial"
                    present = min((part_size or 0) + chunk_size, total)
                elif final_size is not None:
                    status_text = "mismatch"
                    present = min(final_size, total)
                else:
                    status_text = "missing"
                    present = 0

                cluster_state = {
                    "source": "cluster_sftp",
                    "proxy": info.get("proxy"),
                    "status": status_text,
                    "present_bytes": present,
                    "total_bytes": total,
                    "final_size": final_size,
                    "part_size": part_size,
                    "chunk_size": chunk_size,
                    "percent": 100.0 * present / total if total else 100.0,
                    "checked_at": utc_now(),
                    "auth_account": args.auth_account,
                }
                speed_estimate = speed_from_cluster_sample(task, cluster_state)
                cluster_state["speed_estimate"] = speed_estimate
                task["cluster_state"] = cluster_state
                task["speed_estimate"] = speed_estimate
                if not task.get("remote_state"):
                    task["remote_state"] = cluster_state
        finally:
            sftp.close()
            client.close()


def fetch_jobs(config: WebConfig) -> dict:
    command = [
        sys.executable,
        str(ROOT / "hpc_jobs.py"),
        "list",
        "--json",
        "--scope",
        config.jobs_scope,
        "--size",
        str(config.jobs_size),
        "--refresh-browser",
        config.refresh_browser,
    ]
    if config.refresh_headless:
        command.append("--refresh-headless")

    proc = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0 and config.auto_refresh_token:
        retry = [*command, "--refresh-token"]
        proc = subprocess.run(retry, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if proc.returncode != 0:
        return {"ok": False, "rows": [], "error": proc.stderr.strip() or proc.stdout.strip()}

    try:
        rows = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        return {"ok": False, "rows": [], "error": f"invalid hpc_jobs.py JSON: {error}"}
    rows = sorted(rows, key=lambda row: row.get("submit") or 0, reverse=True)[: config.jobs_size]
    return {"ok": True, "rows": rows, "error": None}


def launch_task(config: WebConfig, name: str, archive_name: str | None = None) -> dict:
    task = find_task(config.config, name)
    if not task:
        raise KeyError(f"task not found: {name}")

    key = f"upload:{name}"
    running = action_snapshot(key)
    if running and running.get("status") == "running":
        return running

    command = [
        sys.executable,
        str(ROOT / "hpc_transfer_app.py"),
        "--config",
        str(config.config),
        "--refresh-browser",
        config.refresh_browser,
    ]
    if config.refresh_headless:
        command.append("--refresh-headless")
    if not config.auto_refresh_token:
        command.append("--no-auto-refresh-token")
    command.extend(["run", name])
    if archive_name:
        command.extend(["--archive-name", archive_name])

    run_background(key, command)
    return action_snapshot(key) or {"status": "starting"}


def read_saved_token(path: Path) -> str:
    path = path.expanduser()
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 14:
        return "*" * len(token)
    return f"{token[:6]}...{token[-6:]}"


def token_status(config: WebConfig, *, validate: bool = False) -> dict:
    token_file = config.token_file.expanduser()
    token = read_saved_token(token_file)
    result = {
        "path": str(token_file),
        "exists": token_file.is_file(),
        "length": len(token),
        "masked": mask_token(token),
        "mtime": None,
        "validation": None,
        "refresh": action_snapshot("token-refresh"),
        "save": action_snapshot("token-save"),
    }
    if token_file.is_file():
        result["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S %z", time.localtime(token_file.stat().st_mtime))
    if validate and token:
        try:
            validation = validate_token(token, timeout=10)
            result["validation"] = {
                "ok": not token_validation_failed(validation),
                "code": validation.get("code"),
                "success": validation.get("success"),
                "message": redact_token_text(
                    validation.get("msg") or validation.get("message") or validation.get("raw")
                ),
            }
            if result["validation"]["ok"]:
                default_row = next(
                    (row for row in list_account_summaries() if row.get("default")),
                    None,
                )
                if default_row:
                    name = str(default_row["name"])
                    _, entry = get_account(name)
                    verify_token_identity(name, token, entry)
        except Exception as error:
            result["validation"] = {"ok": False, "error": str(error)}
    return result


def selected_saved_account(payload: dict) -> dict:
    name = str(payload.get("account") or payload.get("auth_account") or "").strip()
    if not name:
        raise ValueError("refresh requires an explicit saved account")
    row = next((item for item in list_account_summaries() if item.get("name") == name), None)
    if not row:
        raise ValueError(f"unknown saved account: {name}")
    return row


def refresh_token(config: WebConfig, payload: dict | None = None) -> dict:
    payload = payload or {}
    key = "token-refresh"
    running = action_snapshot(key)
    if running and running.get("status") == "running":
        return running

    browser = str(payload.get("browser") or config.refresh_browser)
    if browser not in {"playwright", "chrome", "safari"}:
        raise ValueError(f"unsupported browser: {browser}")
    timeout = int(payload.get("timeout") or 180)
    selected = selected_saved_account(payload)
    account = str(selected["name"])
    login_name = str(payload.get("login_name") or "").strip()
    login_password = str(payload.get("login_password") or "")
    headless = bool(payload.get("headless", config.refresh_headless))

    command = [
        sys.executable,
        str(ROOT / "hpc_accounts.py"),
        "refresh",
        account,
        "--browser",
        browser,
        "--timeout",
        str(timeout),
        "--fresh-page",
    ]
    if login_name:
        command.extend(["--login-name", login_name])
    if headless:
        command.append("--headless")
    elif browser == "playwright":
        command.extend(["--clear-existing-token", "--clear-auth-session"])
    if selected.get("default"):
        command.append("--sync-legacy-token")
    env = {"HPC_LOGIN_PASSWORD": login_password} if login_password else None
    run_background(key, command, env=env)
    return action_snapshot(key) or {"status": "starting"}


def save_token(config: WebConfig, payload: dict) -> dict:
    selected = selected_saved_account(payload)
    account = str(selected["name"])
    token = str(payload.get("token") or "").strip()
    if not token:
        raise ValueError("token is required.")
    validation = None
    if bool(payload.get("validate", True)):
        validation = validate_token(token, timeout=10)
        if token_validation_failed(validation):
            raise ValueError(f"token validation failed: {redact_token_text(validation)}")
    synced_account = sync_auth_account_token(token, validation, account, strict=True)
    legacy_synced = bool(selected.get("default"))
    if legacy_synced:
        write_token(config.token_file, token)
    set_action(
        "token-save",
        status="done",
        finished_at=utc_now(),
        returncode=0,
        stdout=(
            f"token saved to auth account {account} and {config.token_file.expanduser()}"
            if legacy_synced
            else f"token saved to auth account {account}; global legacy token unchanged"
        ),
        stderr="",
        validation=validation,
        synced_auth_account=synced_account,
        legacy_synced=legacy_synced,
    )
    return token_status(config, validate=True)


def credentials_status() -> dict:
    return {
        "path": str(DEFAULT_CREDENTIALS_FILE),
        "rows": list_credential_summaries(),
        "save": action_snapshot("credentials-save"),
        "delete": action_snapshot("credentials-delete"),
    }


def save_credential(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    login_name = str(payload.get("login_name") or "").strip()
    login_password = str(payload.get("login_password") or "")
    if not name:
        raise ValueError("credential name is required.")
    if not login_name:
        raise ValueError("login_name is required.")
    if not login_password:
        raise ValueError("login_password is required.")
    upsert_account_credential(name, login_name=login_name, login_password=login_password)
    set_action(
        "credentials-save",
        status="done",
        finished_at=utc_now(),
        returncode=0,
        stdout=f"credential saved for {name} at {DEFAULT_CREDENTIALS_FILE}",
        stderr="",
    )
    return credentials_status()


def remove_credential(name: str) -> dict:
    if not name:
        raise ValueError("credential name is required.")
    delete_account_credential(name)
    set_action(
        "credentials-delete",
        status="done",
        finished_at=utc_now(),
        returncode=0,
        stdout=f"credential removed for {name}",
        stderr="",
    )
    return credentials_status()


def parse_account_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def seconds_since_account_time(value: str | None) -> int | None:
    parsed = parse_account_time(value)
    if not parsed:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def parse_guardian_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S %z",):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            pass
    return parse_account_time(text)


def seconds_since_guardian_time(value: str | None) -> int | None:
    parsed = parse_guardian_time(value)
    if not parsed:
        return None
    return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


def headless_warmup_due(previous_state: dict, *, backoff_seconds: int = HEADLESS_WARMUP_BACKOFF_SECONDS) -> bool:
    last = previous_state.get("last_headless_attempt_at")
    if not last:
        last = ((previous_state.get("refresh") or {}) if isinstance(previous_state.get("refresh"), dict) else {}).get(
            "finished_at"
        )
    if not last:
        return True
    elapsed = seconds_since_guardian_time(last)
    return elapsed is None or elapsed >= backoff_seconds


def sanitize_guardian_text(value) -> str:
    text = redact_token_text("" if value is None else str(value))
    return SECRETISH_TEXT_RE.sub("<redacted-secret>", text)


def sanitize_guardian_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_guardian_text(value)


def normalize_guardian_accounts(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"all", "*"}:
            return []
        raw = re.split(r"[\s,]+", text)
    elif isinstance(value, list):
        raw = value
    else:
        raw = [value]
    names = []
    for item in raw:
        name = str(item or "").strip()
        if name and name.lower() not in {"all", "*"} and name not in names:
            names.append(name)
    return names


def clamp_seconds(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def clamp_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def parse_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def guardian_runtime_options(payload: dict, current: dict | None = None) -> dict:
    current = current or TOKEN_GUARDIAN_STATE
    return {
        "interval_seconds": clamp_seconds(
            payload.get("interval_seconds"),
            int(current.get("interval_seconds") or 300),
            60,
            86400,
        ),
        "refresh_every_seconds": clamp_seconds(
            payload.get("refresh_every_seconds"),
            int(current.get("refresh_every_seconds") or 1800),
            300,
            604800,
        ),
        "refresh_timeout_seconds": clamp_seconds(
            payload.get("refresh_timeout_seconds"),
            int(current.get("refresh_timeout_seconds") or 60),
            15,
            600,
        ),
        "failure_notify_threshold": clamp_int(
            payload.get("failure_notify_threshold"),
            int(current.get("failure_notify_threshold") or 3),
            1,
            100,
        ),
        "age_warning_seconds": clamp_seconds(
            payload.get("age_warning_seconds"),
            int(current.get("age_warning_seconds") or DEFAULT_AGE_WARNING_SECONDS),
            3600,
            604800,
        ),
        "notifications_enabled": parse_bool(
            payload.get("notifications_enabled"),
            bool(current.get("notifications_enabled", True)),
        ),
        "auto_visible_refresh": parse_bool(
            payload.get("auto_visible_refresh"),
            bool(current.get("auto_visible_refresh", False)),
        ),
        "visible_refresh_timeout_seconds": clamp_seconds(
            payload.get("visible_refresh_timeout_seconds"),
            int(current.get("visible_refresh_timeout_seconds") or 900),
            60,
            3600,
        ),
    }


def applescript_quote(value: object) -> str:
    return sanitize_guardian_text(value).replace("\\", "\\\\").replace('"', '\\"')


def macos_notify(title: str, message: str, subtitle: str = "BJTU HPC") -> dict:
    if sys.platform != "darwin":
        return {"ok": False, "error": "macOS notifications are unavailable on this platform"}
    script = (
        f'display notification "{applescript_quote(message)}" '
        f'with title "{applescript_quote(title)}" '
        f'subtitle "{applescript_quote(subtitle)}"'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
            check=False,
        )
    except Exception as error:
        return {"ok": False, "error": sanitize_guardian_text(error)}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stderr": sanitize_guardian_text(proc.stderr)[-1000:],
    }


def notify_guardian_attention(
    name: str,
    account_state: dict,
    reason: str,
    *,
    notifications_enabled: bool,
) -> None:
    if not reason:
        account_state["last_notification_key"] = None
        return
    previous = {}
    with TOKEN_GUARDIAN_LOCK:
        previous = dict((TOKEN_GUARDIAN_STATE.get("accounts") or {}).get(name) or {})
    notify_key = f"{reason}:{account_state.get('token_updated_at') or '-'}"
    account_state["last_notification_key"] = previous.get("last_notification_key")
    account_state["last_notification_at"] = previous.get("last_notification_at")
    if previous.get("last_notification_key") == notify_key:
        return
    account_state["last_notification_key"] = notify_key
    account_state["last_notification_at"] = utc_now()
    if not notifications_enabled:
        account_state["notification"] = {"ok": False, "skipped": "disabled"}
        guardian_event({"event": "notification_skipped", "account": name, "reason": reason})
        return
    failures = int(account_state.get("headless_failure_count") or 0)
    age = int(account_state.get("token_age_seconds") or 0)
    age_hours = round(age / 3600, 1)
    if reason == "needs_visible_login":
        message = f"{name} token is invalid and needs visible CAS login."
    elif reason == "headless_failures":
        message = f"{name} headless refresh failed {failures} times; visible login may be needed."
    elif reason == "token_age":
        message = f"{name} token age is {age_hours}h; refresh it before expiry."
    else:
        message = f"{name} token needs attention: {reason}."
    result = macos_notify("BJTU HPC token attention", message)
    account_state["notification"] = result
    guardian_event(
        {
            "event": "notification_sent" if result.get("ok") else "notification_failed",
            "account": name,
            "reason": reason,
            "error": result.get("error") or result.get("stderr") or "",
        }
    )


def guardian_event(event: dict) -> None:
    clean = {key: sanitize_guardian_value(value) for key, value in event.items()}
    clean.setdefault("time", utc_now())
    with TOKEN_GUARDIAN_LOCK:
        events = TOKEN_GUARDIAN_STATE.setdefault("events", [])
        events.append(clean)
        del events[:-40]
    try:
        with TOKEN_GUARDIAN_LOG.open("a", encoding="utf-8") as file:
            file.write(json.dumps(clean, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def update_guardian_account(name: str, account_state: dict) -> None:
    with TOKEN_GUARDIAN_LOCK:
        accounts = TOKEN_GUARDIAN_STATE.setdefault("accounts", {})
        accounts[name] = dict(account_state)


def guardian_snapshot() -> dict:
    with TOKEN_GUARDIAN_LOCK:
        return {
            **{key: value for key, value in TOKEN_GUARDIAN_STATE.items() if key not in {"accounts", "events"}},
            "accounts": {key: dict(value) for key, value in TOKEN_GUARDIAN_STATE.get("accounts", {}).items()},
            "events": [dict(event) for event in TOKEN_GUARDIAN_STATE.get("events", [])],
            "log_path": str(TOKEN_GUARDIAN_LOG),
        }


def guardian_validation_summary(name: str, token: str | None) -> dict:
    if not token:
        return {"ok": False, "message": "no saved token"}
    validation = validate_token(token, timeout=10)
    summary = {
        "ok": not token_validation_failed(validation),
        "code": validation.get("code"),
        "http_status": validation.get("http_status"),
        "success": validation.get("success"),
        "message": sanitize_guardian_text(
            validation.get("msg") or validation.get("message") or validation.get("raw")
        ),
    }
    if not summary["ok"]:
        return summary
    try:
        _, entry = get_account(name)
        verify_token_identity(name, token, entry)
    except Exception as error:
        summary["ok"] = False
        summary["message"] = sanitize_guardian_text(error)
    return summary


def refresh_guardian_account_snapshot(name: str, *, visible_refresh: dict | None = None) -> dict:
    """Refresh one Guardian account row from the saved account store without launching browsers."""
    try:
        rows = {str(item.get("name") or ""): item for item in list_account_summaries()}
    except Exception as error:
        guardian_event(
            {
                "event": "account_snapshot_refresh_failed",
                "account": name,
                "error": sanitize_guardian_text(error),
            }
        )
        return {}
    row = rows.get(name)
    if not row:
        guardian_event({"event": "account_snapshot_refresh_failed", "account": name, "error": "account not found"})
        return {}
    with TOKEN_GUARDIAN_LOCK:
        previous_state = dict((TOKEN_GUARDIAN_STATE.get("accounts") or {}).get(name) or {})
        refresh_every_seconds = int(TOKEN_GUARDIAN_STATE.get("refresh_every_seconds") or 1800)
        failure_notify_threshold = int(TOKEN_GUARDIAN_STATE.get("failure_notify_threshold") or 3)
        age_warning_seconds = int(TOKEN_GUARDIAN_STATE.get("age_warning_seconds") or DEFAULT_AGE_WARNING_SECONDS)

    age_seconds = seconds_since_account_time(row.get("token_updated_at"))
    account_state = {
        "name": name,
        "default": bool(row.get("default")),
        "portal_user": row.get("portal_user"),
        "cluster": row.get("cluster"),
        "account": row.get("account"),
        "has_token": bool(row.get("has_token")),
        "token_updated_at": row.get("token_updated_at"),
        "token_age_seconds": age_seconds,
        "checked_at": utc_now(),
        "refresh": None,
        "needs_visible_login": False,
        "headless_failure_count": int(previous_state.get("headless_failure_count") or 0),
        "attention_required": False,
        "attention_reason": "",
    }
    try:
        validation = guardian_validation_summary(name, token_for_account(name))
    except Exception as error:
        validation = {"ok": False, "message": sanitize_guardian_text(error)}
    account_state["validation"] = validation
    account_state["stale"] = age_seconds is None or age_seconds >= refresh_every_seconds
    account_state["refresh_reason"] = "stale" if account_state["stale"] else ""

    if validation.get("ok"):
        account_state["status"] = "valid"
        account_state["headless_failure_count"] = 0
    else:
        account_state["status"] = "needs_visible_login"
        account_state["needs_visible_login"] = True

    age_warning = (
        account_state.get("token_age_seconds") is None
        or int(account_state.get("token_age_seconds") or 0) >= age_warning_seconds
    )
    account_state["age_warning"] = age_warning
    attention_reason = ""
    if account_state.get("needs_visible_login"):
        attention_reason = "needs_visible_login"
    elif int(account_state.get("headless_failure_count") or 0) >= failure_notify_threshold:
        attention_reason = "headless_failures"
    elif age_warning:
        attention_reason = "token_age"
    account_state["attention_required"] = bool(attention_reason)
    account_state["attention_reason"] = attention_reason
    account_state["last_notification_key"] = previous_state.get("last_notification_key")
    account_state["last_notification_at"] = previous_state.get("last_notification_at")

    current_visible = visible_refresh if visible_refresh is not None else visible_refresh_status(name)
    if current_visible:
        account_state["visible_refresh"] = current_visible
    update_guardian_account(name, account_state)
    guardian_event(
        {
            "event": "account_snapshot_refreshed",
            "account": name,
            "status": account_state["status"],
            "token_updated_at": account_state.get("token_updated_at"),
        }
    )
    return account_state


def run_guardian_headless_refresh(
    name: str,
    timeout_seconds: int,
    *,
    clear_existing_token: bool = False,
) -> dict:
    command = [
        sys.executable,
        str(ROOT / "hpc_accounts.py"),
        "refresh",
        name,
        "--browser",
        "playwright",
        "--headless",
        "--fresh-page",
        "--timeout",
        str(timeout_seconds),
    ]
    if clear_existing_token:
        command.insert(-2, "--clear-existing-token")
    started = utc_now()
    proc = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        TOKEN_GUARDIAN_HEADLESS_PROCS[name] = proc
        stdout_raw, stderr_raw = proc.communicate(timeout=timeout_seconds + 45)
        stdout = sanitize_guardian_text(stdout_raw)[-4000:]
        stderr = sanitize_guardian_text(stderr_raw)[-4000:]
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "started_at": started,
            "finished_at": utc_now(),
            "clear_existing_token": clear_existing_token,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired as error:
        if proc is not None:
            proc.kill()
            stdout_raw, stderr_raw = proc.communicate()
        else:
            stdout_raw = error.stdout
            stderr_raw = error.stderr
        return {
            "ok": False,
            "returncode": None,
            "started_at": started,
            "finished_at": utc_now(),
            "clear_existing_token": clear_existing_token,
            "stdout": sanitize_guardian_text(stdout_raw)[-4000:],
            "stderr": sanitize_guardian_text(stderr_raw)[-4000:],
            "error": f"headless refresh timed out after {timeout_seconds + 45}s",
        }
    except Exception as error:
        return {
            "ok": False,
            "returncode": None,
            "started_at": started,
            "finished_at": utc_now(),
            "clear_existing_token": clear_existing_token,
            "stdout": "",
            "stderr": "",
            "error": sanitize_guardian_text(error),
        }
    finally:
        if proc is not None and TOKEN_GUARDIAN_HEADLESS_PROCS.get(name) is proc:
            TOKEN_GUARDIAN_HEADLESS_PROCS.pop(name, None)


def stop_guardian_headless_refresh(name: str) -> bool:
    proc = TOKEN_GUARDIAN_HEADLESS_PROCS.get(name)
    if proc is None or proc.poll() is not None:
        TOKEN_GUARDIAN_HEADLESS_PROCS.pop(name, None)
        return False
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    TOKEN_GUARDIAN_HEADLESS_PROCS.pop(name, None)
    guardian_event({"event": "headless_refresh_stopped_for_visible", "account": name, "pid": proc.pid})
    return True


def update_visible_refresh_state(name: str, **updates) -> dict:
    with TOKEN_GUARDIAN_LOCK:
        refreshes = TOKEN_GUARDIAN_STATE.setdefault("visible_refreshes", {})
        state = dict(refreshes.get(name) or {})
        state.update({key: sanitize_guardian_value(value) for key, value in updates.items()})
        refreshes[name] = state
        return dict(state)


def pid_is_running(pid) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        os.kill(pid_int, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def visible_refresh_status(name: str) -> dict:
    with TOKEN_GUARDIAN_LOCK:
        state = dict((TOKEN_GUARDIAN_STATE.get("visible_refreshes") or {}).get(name) or {})
    proc = TOKEN_GUARDIAN_VISIBLE_PROCS.get(name)
    if proc is not None and proc.poll() is None:
        state["status"] = "running"
        state["pid"] = proc.pid
    elif state.get("status") == "running":
        pid = state.get("pid")
        if pid_is_running(pid):
            state = update_visible_refresh_state(
                name,
                status="orphaned",
                finished_at=utc_now(),
                returncode=None,
                error="visible refresh process is no longer tracked by guardian; retry login if needed",
            )
            guardian_event({"event": "visible_refresh_orphaned", "account": name, "pid": pid})
        else:
            state = update_visible_refresh_state(
                name,
                status="stale",
                finished_at=utc_now(),
                returncode=None,
                error="visible refresh state was running but no process is active",
            )
            guardian_event({"event": "visible_refresh_stale", "account": name, "pid": pid})
    return state


def visible_refresh_recently_started(state: dict, cooldown_seconds: int = 1800) -> bool:
    started_epoch = state.get("started_epoch")
    try:
        started = float(started_epoch)
    except (TypeError, ValueError):
        return False
    status = str(state.get("status") or "")
    return status in {"running", "failed", "timeout"} and time.time() - started < cooldown_seconds


def launch_guardian_visible_refresh(name: str, reason: str, timeout_seconds: int, *, force: bool = False) -> dict:
    current = visible_refresh_status(name)
    if current.get("status") == "running":
        return current
    if not force and visible_refresh_recently_started(current):
        current["skipped"] = "cooldown"
        return current
    stop_guardian_headless_refresh(name)

    command = [
        sys.executable,
        str(ROOT / "hpc_refresh_flow.py"),
        name,
        "--visible-only",
        "--force",
        "--no-profile-probe-before-visible",
        "--visible-timeout",
        str(timeout_seconds),
        "--no-job-check",
    ]
    try:
        proc = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as error:
        state = update_visible_refresh_state(
            name,
            status="failed",
            reason=reason,
            started_at=utc_now(),
            finished_at=utc_now(),
            error=sanitize_guardian_text(error),
        )
        guardian_event({"event": "visible_refresh_launch_failed", "account": name, "reason": reason, "error": error})
        return state

    TOKEN_GUARDIAN_VISIBLE_PROCS[name] = proc
    state = update_visible_refresh_state(
        name,
        status="running",
        pid=proc.pid,
        reason=reason,
        command=command,
        started_at=utc_now(),
        started_epoch=time.time(),
        finished_at=None,
        returncode=None,
        stdout="",
        stderr="",
        error=None,
    )
    guardian_event({"event": "visible_refresh_started", "account": name, "reason": reason, "pid": proc.pid})
    macos_notify(
        "BJTU HPC token login opened",
        f"Complete CAS login and captcha for {name}, then close the browser window.",
    )

    def watcher() -> None:
        try:
            stdout, stderr = proc.communicate(timeout=timeout_seconds + 120)
            status = "done" if proc.returncode == 0 else "failed"
            visible_state = update_visible_refresh_state(
                name,
                status=status,
                finished_at=utc_now(),
                returncode=proc.returncode,
                stdout=sanitize_guardian_text(stdout)[-4000:],
                stderr=sanitize_guardian_text(stderr)[-4000:],
            )
            refresh_guardian_account_snapshot(name, visible_refresh=visible_state)
            guardian_event(
                {
                    "event": "visible_refresh_finished",
                    "account": name,
                    "status": status,
                    "returncode": proc.returncode,
                    "error": stderr or "",
                }
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            visible_state = update_visible_refresh_state(
                name,
                status="timeout",
                finished_at=utc_now(),
                returncode=None,
                stdout=sanitize_guardian_text(stdout)[-4000:],
                stderr=sanitize_guardian_text(stderr)[-4000:],
                error=f"visible refresh timed out after {timeout_seconds + 120}s",
            )
            refresh_guardian_account_snapshot(name, visible_refresh=visible_state)
            guardian_event({"event": "visible_refresh_timeout", "account": name})
        finally:
            TOKEN_GUARDIAN_VISIBLE_PROCS.pop(name, None)

    threading.Thread(target=watcher, name=f"hpc-visible-refresh-{name}", daemon=True).start()
    return state


def selected_guardian_accounts(accounts_filter: list[str]) -> list[dict]:
    rows = list_account_summaries()
    if not accounts_filter:
        return rows
    wanted = set(accounts_filter)
    return [row for row in rows if row.get("name") in wanted]


def run_guardian_cycle(
    config: WebConfig,
    *,
    accounts_filter: list[str] | None = None,
    refresh_every_seconds: int = 1800,
    refresh_timeout_seconds: int = 60,
    failure_notify_threshold: int = 3,
    age_warning_seconds: int = DEFAULT_AGE_WARNING_SECONDS,
    notifications_enabled: bool = True,
    auto_visible_refresh: bool = False,
    visible_refresh_timeout_seconds: int = 900,
    force_refresh: bool = False,
    run_mode: str = "loop",
) -> dict:
    if not TOKEN_GUARDIAN_CYCLE_LOCK.acquire(blocking=False):
        guardian_event({"event": "cycle_skipped", "reason": "another cycle is already running"})
        return guardian_snapshot()

    accounts_filter = accounts_filter or []
    with TOKEN_GUARDIAN_LOCK:
        TOKEN_GUARDIAN_STATE.update(
            {
                "last_cycle_started_at": utc_now(),
                "last_cycle_finished_at": None,
                "last_run_mode": run_mode,
                "error": None,
            }
        )
    guardian_event({"event": "cycle_started", "mode": run_mode, "force_refresh": force_refresh})
    try:
        rows = selected_guardian_accounts(accounts_filter)
        if accounts_filter and not rows:
            guardian_event({"event": "cycle_warning", "message": "no matching accounts"})

        for row in rows:
            name = str(row.get("name") or "")
            if not name:
                continue
            with TOKEN_GUARDIAN_LOCK:
                previous_state = dict((TOKEN_GUARDIAN_STATE.get("accounts") or {}).get(name) or {})
            age_seconds = seconds_since_account_time(row.get("token_updated_at"))
            account_state = {
                "name": name,
                "default": bool(row.get("default")),
                "portal_user": row.get("portal_user"),
                "cluster": row.get("cluster"),
                "account": row.get("account"),
                "has_token": bool(row.get("has_token")),
                "token_updated_at": row.get("token_updated_at"),
                "token_age_seconds": age_seconds,
                "checked_at": utc_now(),
                "refresh": None,
                "needs_visible_login": False,
                "headless_failure_count": int(previous_state.get("headless_failure_count") or 0),
                "attention_required": False,
                "attention_reason": "",
            }
            current_visible = visible_refresh_status(name)
            if current_visible.get("status") == "running":
                account_state["status"] = previous_state.get("status") or "visible_login_running"
                account_state["validation"] = previous_state.get("validation") or {}
                account_state["stale"] = age_seconds is None or age_seconds >= refresh_every_seconds
                account_state["age_warning"] = (
                    age_seconds is None or age_seconds >= age_warning_seconds
                )
                account_state["refresh_reason"] = "visible_login_running"
                account_state["visible_refresh"] = current_visible
                update_guardian_account(name, account_state)
                guardian_event(
                    {
                        "event": "refresh_deferred_for_visible_login",
                        "account": name,
                        "pid": current_visible.get("pid"),
                    }
                )
                continue

            try:
                current_token = token_for_account(name)
                initial = guardian_validation_summary(name, current_token)
            except Exception as error:
                current_token = None
                initial = {"ok": False, "message": sanitize_guardian_text(error)}
            account_state["validation"] = initial

            initial_ok = bool(initial.get("ok"))
            stale = age_seconds is None or age_seconds >= refresh_every_seconds
            age_warning = age_seconds is None or age_seconds >= age_warning_seconds
            warmup_due = initial_ok and age_warning and headless_warmup_due(previous_state)
            should_refresh = force_refresh or not initial_ok or warmup_due
            account_state["stale"] = stale
            account_state["age_warning"] = age_warning
            account_state["refresh_reason"] = (
                "force"
                if force_refresh
                else "invalid"
                if not initial_ok
                else "token_age_warmup"
                if warmup_due
                else ""
            )

            if should_refresh:
                refresh_kind = "recovery" if not initial_ok else "warmup"
                clear_existing_token = not initial_ok
                guardian_event(
                    {
                        "event": "refresh_started",
                        "account": name,
                        "reason": account_state["refresh_reason"],
                        "kind": refresh_kind,
                        "clear_existing_token": clear_existing_token,
                    }
                )
                refresh = run_guardian_headless_refresh(
                    name,
                    refresh_timeout_seconds,
                    clear_existing_token=clear_existing_token,
                )
                account_state["refresh"] = refresh
                account_state["last_headless_attempt_at"] = refresh.get("finished_at")
                if refresh.get("ok") and row.get("default"):
                    try:
                        sync_legacy_token(name, token_file=config.token_file)
                        account_state["legacy_synced"] = True
                    except Exception as error:
                        account_state["legacy_sync_error"] = sanitize_guardian_text(error)
                try:
                    refreshed_token = token_for_account(name)
                    final = guardian_validation_summary(name, refreshed_token)
                except Exception as error:
                    refreshed_token = None
                    final = {"ok": False, "message": sanitize_guardian_text(error)}
                token_changed = bool(refreshed_token and refreshed_token != current_token)
                account_state["token_changed"] = token_changed
                try:
                    refreshed_rows = {item.get("name"): item for item in list_account_summaries()}
                    refreshed_row = refreshed_rows.get(name) or {}
                    if refreshed_row.get("token_updated_at"):
                        account_state["token_updated_at"] = refreshed_row.get("token_updated_at")
                    if refreshed_row.get("token_validated_at"):
                        account_state["token_validated_at"] = refreshed_row.get("token_validated_at")
                except Exception:
                    pass
                account_state["validation"] = final
                account_state["checked_at"] = utc_now()
                if final.get("ok"):
                    if refresh.get("ok"):
                        account_state["status"] = "refreshed" if token_changed else "kept_alive"
                    else:
                        account_state["status"] = "valid"
                        account_state["headless_unavailable"] = True
                        account_state["headless_unavailable_reason"] = (
                            refresh.get("error") or refresh.get("stderr") or ""
                        )
                    account_state["headless_failure_count"] = 0
                else:
                    account_state["status"] = "needs_visible_login"
                    account_state["needs_visible_login"] = True
                    account_state["headless_failure_count"] = (
                        int(previous_state.get("headless_failure_count") or 0) + 1
                    )
                guardian_event(
                    {
                        "event": "refresh_finished",
                        "account": name,
                        "kind": refresh_kind,
                        "ok": refresh.get("ok"),
                        "final_ok": final.get("ok"),
                        "status": account_state["status"],
                        "error": refresh.get("error") or refresh.get("stderr") or "",
                    }
                )
            else:
                if initial_ok:
                    account_state["status"] = "valid"
                    account_state["headless_failure_count"] = 0
                    guardian_event({"event": "validated", "account": name, "ok": True})
                else:
                    account_state["status"] = "needs_visible_login"
                    account_state["needs_visible_login"] = True
                    account_state["headless_failure_count"] = (
                        int(previous_state.get("headless_failure_count") or 0) + 1
                    )
                    guardian_event({"event": "validated", "account": name, "ok": False})

            account_state["token_age_seconds"] = seconds_since_account_time(
                account_state.get("token_updated_at")
            )
            age_warning = (
                account_state.get("token_age_seconds") is None
                or int(account_state.get("token_age_seconds") or 0) >= age_warning_seconds
            )
            account_state["age_warning"] = age_warning
            attention_reason = ""
            if account_state.get("needs_visible_login"):
                attention_reason = "needs_visible_login"
            elif int(account_state.get("headless_failure_count") or 0) >= failure_notify_threshold:
                attention_reason = "headless_failures"
            elif age_warning:
                attention_reason = "token_age"
            account_state["attention_required"] = bool(attention_reason)
            account_state["attention_reason"] = attention_reason
            if attention_reason:
                notify_guardian_attention(
                    name,
                    account_state,
                    attention_reason,
                    notifications_enabled=notifications_enabled,
                )
            else:
                account_state["last_notification_key"] = None
                account_state["last_notification_at"] = None
            if auto_visible_refresh and attention_reason:
                account_state["visible_refresh"] = launch_guardian_visible_refresh(
                    name,
                    attention_reason,
                    visible_refresh_timeout_seconds,
                )
            else:
                current_visible = visible_refresh_status(name)
                if current_visible:
                    account_state["visible_refresh"] = current_visible
            update_guardian_account(name, account_state)

        with TOKEN_GUARDIAN_LOCK:
            TOKEN_GUARDIAN_STATE["last_cycle_finished_at"] = utc_now()
        guardian_event({"event": "cycle_finished", "mode": run_mode, "accounts": len(rows)})
    except Exception as error:
        message = sanitize_guardian_text(error)
        with TOKEN_GUARDIAN_LOCK:
            TOKEN_GUARDIAN_STATE["error"] = message
            TOKEN_GUARDIAN_STATE["last_cycle_finished_at"] = utc_now()
        guardian_event({"event": "cycle_failed", "error": message})
    finally:
        TOKEN_GUARDIAN_CYCLE_LOCK.release()
    return guardian_snapshot()


def token_guardian_loop(config: WebConfig) -> None:
    global TOKEN_GUARDIAN_THREAD
    current_thread = threading.current_thread()
    try:
        while not TOKEN_GUARDIAN_STOP.is_set():
            with TOKEN_GUARDIAN_LOCK:
                accounts_filter = list(TOKEN_GUARDIAN_STATE.get("accounts_filter") or [])
                interval_seconds = int(TOKEN_GUARDIAN_STATE.get("interval_seconds") or 300)
                refresh_every_seconds = int(TOKEN_GUARDIAN_STATE.get("refresh_every_seconds") or 1800)
                refresh_timeout_seconds = int(TOKEN_GUARDIAN_STATE.get("refresh_timeout_seconds") or 60)
                failure_notify_threshold = int(TOKEN_GUARDIAN_STATE.get("failure_notify_threshold") or 3)
                age_warning_seconds = int(
                    TOKEN_GUARDIAN_STATE.get("age_warning_seconds") or DEFAULT_AGE_WARNING_SECONDS
                )
                notifications_enabled = bool(TOKEN_GUARDIAN_STATE.get("notifications_enabled", True))
                auto_visible_refresh = bool(TOKEN_GUARDIAN_STATE.get("auto_visible_refresh", False))
                visible_refresh_timeout_seconds = int(
                    TOKEN_GUARDIAN_STATE.get("visible_refresh_timeout_seconds") or 900
                )
            run_guardian_cycle(
                config,
                accounts_filter=accounts_filter,
                refresh_every_seconds=refresh_every_seconds,
                refresh_timeout_seconds=refresh_timeout_seconds,
                failure_notify_threshold=failure_notify_threshold,
                age_warning_seconds=age_warning_seconds,
                notifications_enabled=notifications_enabled,
                auto_visible_refresh=auto_visible_refresh,
                visible_refresh_timeout_seconds=visible_refresh_timeout_seconds,
                run_mode="loop",
            )
            if TOKEN_GUARDIAN_STOP.wait(interval_seconds):
                break
    finally:
        with TOKEN_GUARDIAN_LOCK:
            TOKEN_GUARDIAN_STATE["running"] = False
            TOKEN_GUARDIAN_STATE["last_stopped_at"] = utc_now()
            if TOKEN_GUARDIAN_THREAD is current_thread:
                TOKEN_GUARDIAN_THREAD = None
        guardian_event({"event": "guardian_stopped"})


def start_token_guardian(config: WebConfig, payload: dict) -> dict:
    global TOKEN_GUARDIAN_THREAD
    options = guardian_runtime_options(payload)
    accounts_filter = normalize_guardian_accounts(payload.get("accounts"))

    with TOKEN_GUARDIAN_LOCK:
        thread_alive = TOKEN_GUARDIAN_THREAD is not None and TOKEN_GUARDIAN_THREAD.is_alive()
        if TOKEN_GUARDIAN_STATE.get("running") and thread_alive:
            TOKEN_GUARDIAN_STATE.update(options)
            TOKEN_GUARDIAN_STATE["accounts_filter"] = accounts_filter
            guardian_event({"event": "guardian_configured", "accounts": ",".join(accounts_filter) or "all"})
            return guardian_snapshot()
        if thread_alive:
            TOKEN_GUARDIAN_STATE["error"] = "guardian is still stopping; retry start after it exits"
            return guardian_snapshot()
        TOKEN_GUARDIAN_STOP.clear()
        TOKEN_GUARDIAN_STATE.update(
            {
                "running": True,
                **options,
                "accounts_filter": accounts_filter,
                "last_started_at": utc_now(),
                "last_stopped_at": None,
                "error": None,
            }
        )
        TOKEN_GUARDIAN_THREAD = threading.Thread(
            target=token_guardian_loop,
            name="hpc-token-guardian",
            args=(config,),
            daemon=True,
        )
        TOKEN_GUARDIAN_THREAD.start()
    guardian_event({"event": "guardian_started", "accounts": ",".join(accounts_filter) or "all"})
    return guardian_snapshot()


def stop_token_guardian() -> dict:
    global TOKEN_GUARDIAN_THREAD
    TOKEN_GUARDIAN_STOP.set()
    with TOKEN_GUARDIAN_LOCK:
        TOKEN_GUARDIAN_STATE["running"] = False
        TOKEN_GUARDIAN_STATE["last_stopped_at"] = utc_now()
        thread = TOKEN_GUARDIAN_THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=5)
    with TOKEN_GUARDIAN_LOCK:
        if TOKEN_GUARDIAN_THREAD is thread and (thread is None or not thread.is_alive()):
            TOKEN_GUARDIAN_THREAD = None
    guardian_event({"event": "guardian_stop_requested"})
    return guardian_snapshot()


def run_token_guardian_once(config: WebConfig, payload: dict) -> dict:
    accounts_filter = normalize_guardian_accounts(payload.get("accounts"))
    options = guardian_runtime_options(payload)
    force_refresh = bool(payload.get("force_refresh"))

    def worker() -> None:
        run_guardian_cycle(
            config,
            accounts_filter=accounts_filter,
            refresh_every_seconds=options["refresh_every_seconds"],
            refresh_timeout_seconds=options["refresh_timeout_seconds"],
            failure_notify_threshold=options["failure_notify_threshold"],
            age_warning_seconds=options["age_warning_seconds"],
            notifications_enabled=options["notifications_enabled"],
            auto_visible_refresh=options["auto_visible_refresh"],
            visible_refresh_timeout_seconds=options["visible_refresh_timeout_seconds"],
            force_refresh=force_refresh,
            run_mode="manual",
        )

    thread = threading.Thread(target=worker, name="hpc-token-guardian-once", daemon=True)
    thread.start()
    guardian_event({"event": "manual_cycle_requested", "force_refresh": force_refresh})
    return guardian_snapshot()


def request_visible_refresh(payload: dict) -> dict:
    if "accounts" in payload:
        requested_accounts = payload.get("accounts")
    elif "account" in payload:
        # Backward compatibility for older widget hosts. Keep this singular
        # field scoped to exactly one account instead of treating it as an
        # omitted filter and opening a window for every saved account.
        requested_accounts = payload.get("account")
    else:
        raise ValueError("visible refresh requires an explicit account selection")

    accounts_filter = normalize_guardian_accounts(requested_accounts)
    explicit_all = (
        isinstance(requested_accounts, str)
        and requested_accounts.strip().lower() in {"all", "*"}
    )
    if not accounts_filter and not explicit_all:
        raise ValueError("visible refresh account selection is empty")
    timeout_seconds = clamp_seconds(
        payload.get("visible_refresh_timeout_seconds"),
        int(TOKEN_GUARDIAN_STATE.get("visible_refresh_timeout_seconds") or 900),
        60,
        3600,
    )
    rows = selected_guardian_accounts(accounts_filter)
    launched = []
    for row in rows:
        name = str(row.get("name") or "")
        if not name:
            continue
        state = launch_guardian_visible_refresh(name, "manual", timeout_seconds, force=True)
        launched.append({"account": name, "status": state.get("status"), "skipped": state.get("skipped")})
    guardian_event({"event": "visible_refresh_requested", "accounts": ",".join(item["account"] for item in launched)})
    snapshot = guardian_snapshot()
    snapshot["visible_refresh_request"] = launched
    return snapshot


def task_log(task: dict, lines: int) -> str:
    safe_lines = max(1, min(lines, 1000))
    log_file = f"{task['remote_workdir'].rstrip('/')}/{task['screen_name']}.log"
    command = f"tail -n {safe_lines} -- {shlex.quote(log_file)}"
    proc = subprocess.run(
        [
            "ssh",
            "-n",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "ConnectionAttempts=1",
            task["source_host"],
            command,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=4,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "tail failed")
    return proc.stdout


class HpcTransferHandler(BaseHTTPRequestHandler):
    server_version = "HpcTransferWeb/1.0"

    @property
    def config(self) -> WebConfig:
        return self.server.web_config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/":
                text_response(self, INDEX_HTML, content_type="text/html")
            elif path == "/api/state":
                json_response(self, {"ok": True, "state": all_state(self.config)})
            elif path == "/api/token/status":
                parsed = urlparse(self.path)
                json_response(self, {"ok": True, "token": token_status(self.config, validate="validate=1" in parsed.query)})
            elif path == "/api/credentials":
                json_response(self, {"ok": True, "credentials": credentials_status()})
            elif path == "/api/token-guardian/status":
                json_response(self, {"ok": True, "guardian": guardian_snapshot()})
            elif path.startswith("/api/tasks"):
                json_response(self, {"ok": False, "error": "task management is disabled in token-only web mode"}, status=403)
            else:
                json_response(self, {"ok": False, "error": "not found"}, status=404)
        except Exception as error:
            json_response(self, {"ok": False, "error": sanitize_guardian_text(error)}, status=500)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = read_json_body(self)
            if path.startswith("/api/tasks"):
                json_response(self, {"ok": False, "error": "task management is disabled in token-only web mode"}, status=403)
            elif path == "/api/token/refresh":
                json_response(self, {"ok": True, "launch": refresh_token(self.config, payload)})
            elif path == "/api/token/save":
                json_response(self, {"ok": True, "token": save_token(self.config, payload)})
            elif path == "/api/credentials":
                json_response(self, {"ok": True, "credentials": save_credential(payload)})
            elif path.startswith("/api/credentials/") and path.endswith("/delete"):
                name = unquote(path[len("/api/credentials/") : -len("/delete")]).strip("/")
                json_response(self, {"ok": True, "credentials": remove_credential(name)})
            elif path == "/api/token-guardian/start":
                json_response(self, {"ok": True, "guardian": start_token_guardian(self.config, payload)})
            elif path == "/api/token-guardian/stop":
                json_response(self, {"ok": True, "guardian": stop_token_guardian()})
            elif path == "/api/token-guardian/run-once":
                json_response(self, {"ok": True, "guardian": run_token_guardian_once(self.config, payload)})
            elif path == "/api/token-guardian/visible-refresh":
                json_response(self, {"ok": True, "guardian": request_visible_refresh(payload)})
            else:
                json_response(self, {"ok": False, "error": "not found"}, status=404)
        except ValueError as error:
            json_response(self, {"ok": False, "error": sanitize_guardian_text(error)}, status=400)
        except KeyError as error:
            json_response(self, {"ok": False, "error": sanitize_guardian_text(error)}, status=404)
        except Exception as error:
            json_response(self, {"ok": False, "error": sanitize_guardian_text(error)}, status=500)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BJTU HPC Auth</title>
  <style>
    :root {
      --bg: #101820;
      --panel: #17232d;
      --card: #20313d;
      --ink: #ecf4f1;
      --muted: #9db3b0;
      --line: #35505a;
      --accent: #f0b35a;
      --ok: #6ecb87;
      --bad: #ff6f61;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 15% 5%, rgba(240, 179, 90, .20), transparent 28rem),
        linear-gradient(135deg, #0b141a, var(--bg) 55%, #14242c);
      color: var(--ink);
      font-family: Avenir Next, "Trebuchet MS", sans-serif;
    }
    header {
      padding: 28px clamp(18px, 4vw, 56px) 18px;
      border-bottom: 1px solid rgba(255,255,255,.08);
    }
    h1 { margin: 0 0 6px; font-size: clamp(28px, 5vw, 52px); letter-spacing: 0; }
    .sub { color: var(--muted); }
    main { padding: 24px clamp(18px, 4vw, 56px) 60px; display: grid; gap: 18px; }
    .grid { display: grid; grid-template-columns: 1.1fr .9fr; gap: 18px; align-items: start; }
    .panel { background: rgba(23,35,45,.88); border: 1px solid rgba(255,255,255,.10); border-radius: 18px; padding: 18px; box-shadow: 0 18px 50px rgba(0,0,0,.25); }
    .panel h2 { margin: 0 0 14px; font-size: 20px; }
    form { display: grid; gap: 10px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 13px; }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #0d171d;
      color: var(--ink);
      padding: 10px 12px;
      font: inherit;
    }
    textarea { resize: vertical; min-height: 78px; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .check { display: flex; align-items: center; gap: 8px; color: var(--ink); }
    .check input { width: auto; }
    .stack { display: grid; gap: 18px; }
    button {
      border: 0;
      border-radius: 999px;
      padding: 10px 14px;
      background: var(--accent);
      color: #1a1307;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary { background: #2b4350; color: var(--ink); }
    button.danger { background: #5e2f32; color: var(--ink); }
    button:disabled { opacity: .45; cursor: not-allowed; }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .section-head { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-bottom: 14px; }
    .section-head h2 { margin: 0; }
    .jobs-head { display: flex; justify-content: space-between; gap: 10px; align-items: center; margin-bottom: 10px; }
    .pager { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 10px; }
    .task { background: rgba(32,49,61,.78); border: 1px solid rgba(255,255,255,.08); border-radius: 16px; padding: 14px; margin: 10px 0; }
    .task h3 { margin: 0 0 8px; display: flex; justify-content: space-between; gap: 8px; }
    .meta { color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; overflow-wrap: anywhere; }
    .bar { height: 12px; background: #0e171c; border-radius: 999px; overflow: hidden; margin: 10px 0; border: 1px solid var(--line); }
    .fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--ok)); width: 0%; transition: width .3s ease; }
    .speed { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 12px 0 10px; padding: 10px 0; border-top: 1px solid rgba(255,255,255,.08); border-bottom: 1px solid rgba(255,255,255,.08); }
    .metric { min-width: 0; }
    .metric span { display: block; color: var(--muted); font-size: 11px; text-transform: uppercase; }
    .metric strong { display: block; margin-top: 3px; color: var(--ink); font-size: 15px; overflow-wrap: anywhere; }
    .pill { display: inline-block; border-radius: 999px; padding: 3px 8px; background: #29424d; color: var(--muted); font-size: 12px; white-space: nowrap; }
    .pill.ok { background: rgba(110,203,135,.14); color: var(--ok); }
    .pill.warn { background: rgba(240,179,90,.14); color: var(--accent); }
    .pill.bad { background: rgba(255,111,97,.14); color: var(--bad); }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid rgba(255,255,255,.08); vertical-align: top; }
    th { color: var(--muted); font-weight: 600; }
    pre { max-height: 260px; overflow: auto; background: #0b1419; border: 1px solid var(--line); border-radius: 12px; padding: 12px; color: #d7e3df; white-space: pre-wrap; }
    .status { color: var(--muted); min-height: 20px; }
    @media (max-width: 980px) { .grid, .row, .speed { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <header>
    <h1>BJTU HPC Auth</h1>
    <div class="sub">Local token, saved CAS login, and Token Guardian dashboard.</div>
  </header>
  <main>
    <p class="status" id="status"></p>
    <section class="stack">
        <div class="panel">
          <h2>Portal Token</h2>
          <div id="tokenInfo" class="meta">Loading...</div>
          <form id="tokenForm" style="margin-top:12px">
            <label>Auth Account
              <select name="account" id="tokenAccount" required></select>
            </label>
            <div class="row">
              <label>Browser
                <select name="browser">
                  <option value="playwright">Playwright</option>
                  <option value="chrome">Chrome</option>
                  <option value="safari">Safari</option>
                </select>
              </label>
              <label>Timeout Seconds<input name="timeout" type="number" min="10" max="600" value="180"></label>
            </div>
            <div class="row">
              <label>Login Name, optional<input name="login_name" placeholder="uses this account's saved login"></label>
              <label>Password, optional<input name="login_password" type="password" autocomplete="off" placeholder="only used for this refresh"></label>
            </div>
            <label class="check"><input name="headless" type="checkbox"> Headless Playwright, requires an existing logged-in profile</label>
            <div class="actions">
              <button type="submit">Get And Save Token</button>
              <button type="button" class="secondary" id="validateToken">Validate Saved Token</button>
            </div>
          </form>
          <form id="manualTokenForm" style="margin-top:12px">
            <label>Auth Account
              <select name="account" id="manualTokenAccount" required></select>
            </label>
            <label>Manual DESKTOP_PARA_ATOKEN<textarea name="token" rows="3" placeholder="Paste token here; identity is checked before it is saved"></textarea></label>
            <div class="actions">
              <button type="submit" class="secondary">Save Pasted Token</button>
            </div>
          </form>
        </div>
        <div class="panel">
          <h2>Saved CAS Login</h2>
          <div id="credentialsInfo" class="meta">Loading...</div>
          <form id="credentialsForm" style="margin-top:12px">
            <div class="row">
              <label>Account Name<input name="name" required placeholder="main"></label>
              <label>Login Name<input name="login_name" required placeholder="portal username"></label>
            </div>
            <label>Password<input name="login_password" type="password" required autocomplete="new-password" placeholder="stored locally with chmod 600"></label>
            <div class="actions">
              <button type="submit">Save Login</button>
              <button type="button" class="secondary" id="refreshCredentials">Refresh</button>
            </div>
          </form>
        </div>
        <div class="panel">
          <h2>Token Guardian</h2>
          <div id="guardianInfo" class="meta">Loading...</div>
          <form id="guardianForm" style="margin-top:12px">
            <div class="row">
              <label>Accounts<input name="accounts" placeholder="all or main,other"></label>
              <label>Check Interval Seconds<input name="interval_seconds" type="number" min="60" value="300"></label>
            </div>
            <div class="row">
              <label>Headless Warm-up Threshold Seconds<input name="refresh_every_seconds" type="number" min="300" value="1800"></label>
              <label>Refresh Timeout Seconds<input name="refresh_timeout_seconds" type="number" min="15" value="60"></label>
            </div>
            <div class="row">
              <label>Notify After Failures<input name="failure_notify_threshold" type="number" min="1" value="3"></label>
              <label>Age Warning Seconds<input name="age_warning_seconds" type="number" min="3600" value="432000"></label>
            </div>
            <div class="row">
              <label>Visible Login Timeout Seconds<input name="visible_refresh_timeout_seconds" type="number" min="60" value="900"></label>
              <label class="check"><input name="notifications_enabled" type="checkbox" checked> macOS notifications</label>
            </div>
            <label class="check"><input name="auto_visible_refresh" type="checkbox"> Auto-open visible login windows when attention is needed</label>
            <label class="check"><input name="force_refresh" type="checkbox"> Force headless refresh when running once</label>
            <div class="actions">
              <button type="button" id="startGuardian">Start Guardian</button>
              <button type="button" class="secondary" id="runGuardianOnce">Run Once</button>
              <button type="button" class="danger" id="stopGuardian">Stop</button>
            </div>
          </form>
        </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const JOBS_PAGE_SIZE = 5;
    let jobPage = 0;
    let currentJobsRows = [];
    let reloadInFlight = false;
    const status = (text) => $("status").textContent = text || "";
    const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const bytes = (value) => {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      let n = Number(value);
      for (const unit of ["B", "KiB", "MiB", "GiB", "TiB"]) {
        if (n < 1024 || unit === "TiB") return unit === "B" ? `${Math.round(n)} B` : `${n.toFixed(1)} ${unit}`;
        n /= 1024;
      }
    };
    const rate = (value) => value === null || value === undefined || Number.isNaN(Number(value)) ? "sampling" : `${bytes(value)}/s`;
    const duration = (value) => {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      let seconds = Math.max(0, Math.round(Number(value)));
      const days = Math.floor(seconds / 86400);
      seconds %= 86400;
      const hours = Math.floor(seconds / 3600);
      seconds %= 3600;
      const minutes = Math.floor(seconds / 60);
      seconds %= 60;
      if (days) return `${days}d ${hours}h`;
      if (hours) return `${hours}h ${minutes}m`;
      if (minutes) return `${minutes}m ${seconds}s`;
      return `${seconds}s`;
    };

    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      });
      const contentType = response.headers.get("content-type") || "";
      const data = contentType.includes("application/json") ? await response.json() : await response.text();
      if (!response.ok) throw new Error(typeof data === "string" ? data : (data.error || JSON.stringify(data)));
      return data;
    }

    function renderToken(token) {
      const refresh = token.refresh || {};
      const save = token.save || {};
      const validation = token.validation;
      const lines = [
        `file: ${esc(token.path)}`,
        `saved: ${token.exists ? "yes" : "no"}`,
        token.exists ? `mtime: ${esc(token.mtime || "-")}` : "",
        token.exists ? `token: ${esc(token.masked || "")} (${token.length || 0} chars)` : "",
        validation ? `validation: ${validation.ok ? "ok" : "failed"} ${esc(validation.message || validation.error || validation.code || "")}` : "",
        refresh.status ? `refresh: ${esc(refresh.status)}${refresh.returncode !== null && refresh.returncode !== undefined ? ` rc=${refresh.returncode}` : ""}` : "",
        save.status ? `save: ${esc(save.status)} ${esc(save.stdout || save.error || "")}` : "",
        refresh.stderr ? `refresh error: ${esc(refresh.stderr)}` : "",
      ].filter(Boolean);
      $("tokenInfo").innerHTML = lines.map((line) => `<div>${line}</div>`).join("");
    }

    function renderAccountOptions(accounts) {
      const rows = accounts || [];
      for (const id of ["tokenAccount", "manualTokenAccount"]) {
        const select = $(id);
        const previous = select.value;
        select.innerHTML = rows.map((row) =>
          `<option value="${esc(row.name)}"${row.default ? " data-default=\"true\"" : ""}>${esc(row.name)}${row.default ? " (default)" : ""}</option>`
        ).join("");
        const names = rows.map((row) => String(row.name));
        const fallback = rows.find((row) => row.default)?.name || names[0] || "";
        select.value = names.includes(previous) ? previous : fallback;
      }
    }

    function renderCredentials(credentials) {
      const rows = credentials.rows || [];
      const save = credentials.save || {};
      const deleted = credentials.delete || {};
      const summary = [
        `file: ${esc(credentials.path)}`,
        `saved credentials: ${rows.length}`,
        save.status ? `save: ${esc(save.status)} ${esc(save.stdout || save.error || "")}` : "",
        deleted.status ? `delete: ${esc(deleted.status)} ${esc(deleted.stdout || deleted.error || "")}` : "",
      ].filter(Boolean).map((line) => `<div>${line}</div>`).join("");
      const table = rows.length ? `
        <table style="margin-top:10px">
          <thead><tr><th>Name</th><th>Login</th><th>Password</th><th>Updated</th><th></th></tr></thead>
          <tbody>${rows.map((row) => `
            <tr>
              <td>${esc(row.name)}</td>
              <td>${esc(row.login_name || "-")}</td>
              <td>${row.has_password ? "saved" : "-"}</td>
              <td>${esc(row.updated_at || "-")}</td>
              <td><button class="danger" onclick="deleteCredential('${encodeURIComponent(row.name)}')">Delete</button></td>
            </tr>
          `).join("")}</tbody>
        </table>
      ` : "<p class='sub'>No saved CAS login credentials.</p>";
      $("credentialsInfo").innerHTML = summary + table;
    }

    function guardianPayload() {
      const form = $("guardianForm");
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.interval_seconds = Number(payload.interval_seconds || 300);
      payload.refresh_every_seconds = Number(payload.refresh_every_seconds || 1800);
      payload.refresh_timeout_seconds = Number(payload.refresh_timeout_seconds || 60);
      payload.failure_notify_threshold = Number(payload.failure_notify_threshold || 3);
      payload.age_warning_seconds = Number(payload.age_warning_seconds || 432000);
      payload.visible_refresh_timeout_seconds = Number(payload.visible_refresh_timeout_seconds || 900);
      payload.notifications_enabled = form.elements.notifications_enabled.checked;
      payload.auto_visible_refresh = form.elements.auto_visible_refresh.checked;
      payload.force_refresh = form.elements.force_refresh.checked;
      return payload;
    }

    function renderGuardian(guardian) {
      guardian = guardian || {};
      const accounts = Object.values(guardian.accounts || {});
      const running = guardian.running ? "running" : "stopped";
      const summary = [
        `state: ${running}`,
        `accounts: ${(guardian.accounts_filter || []).length ? esc((guardian.accounts_filter || []).join(", ")) : "all"}`,
        `check interval: ${duration(guardian.interval_seconds || 0)}`,
        `warm-up threshold: ${duration(guardian.refresh_every_seconds || 0)}`,
        `notify failures: ${guardian.failure_notify_threshold || 0}`,
        `age warning: ${duration(guardian.age_warning_seconds || 0)}`,
        `notifications: ${guardian.notifications_enabled ? "on" : "off"}`,
        `auto visible login: ${guardian.auto_visible_refresh ? "on" : "off"}`,
        guardian.last_cycle_started_at ? `last cycle: ${esc(guardian.last_cycle_started_at)}` : "",
        guardian.last_cycle_finished_at ? `finished: ${esc(guardian.last_cycle_finished_at)}` : "",
        guardian.error ? `error: ${esc(guardian.error)}` : "",
        guardian.log_path ? `log: ${esc(guardian.log_path)}` : "",
      ].filter(Boolean).map((line) => `<div>${line}</div>`).join("");
      const table = accounts.length ? `
        <table style="margin-top:10px">
           <thead><tr><th>Account</th><th>Status</th><th>Age</th><th>Checked</th><th>Message</th></tr></thead>
           <tbody>${accounts.map((row) => {
            const cls = row.needs_visible_login ? "bad" : (row.attention_required ? "warn" : (row.status === "valid" || row.status === "refreshed" || row.status === "kept_alive" ? "ok" : ""));
            const visible = row.visible_refresh?.status ? `visible: ${row.visible_refresh.status}` : "";
            const failures = row.attention_reason === "headless_failures" && row.headless_failure_count ? `headless failures: ${row.headless_failure_count}` : "";
            const warmup = row.headless_unavailable ? "headless warm-up unavailable; token remains valid" : "";
            const message = failures || visible || warmup || row.validation?.message || row.refresh?.error || row.refresh?.stderr || "";
            return `<tr>
              <td>${esc(row.name)}${row.default ? " *" : ""}</td>
              <td><span class="pill ${cls}">${esc(row.status || "-")}</span></td>
              <td>${row.token_age_seconds === null || row.token_age_seconds === undefined ? "-" : duration(row.token_age_seconds)}</td>
              <td>${esc(row.checked_at || "-")}</td>
              <td>${esc(message || "-")}</td>
            </tr>`;
          }).join("")}</tbody>
        </table>
      ` : "<p class='sub'>No guardian checks yet.</p>";
      $("guardianInfo").innerHTML = summary + table;
    }

    async function reload() {
      if (reloadInFlight) return;
      reloadInFlight = true;
      try {
        const data = await api("/api/state");
        renderAccountOptions(data.state.accounts);
        renderToken(data.state.token);
        renderCredentials(data.state.credentials);
        renderGuardian(data.state.token_guardian);
        status(`Last updated: ${data.state.time}`);
      } catch (error) {
        status(error.message);
      } finally {
        reloadInFlight = false;
      }
    }

    async function reloadCredentials() {
      const data = await api("/api/credentials");
      renderCredentials(data.credentials);
    }

    async function reloadToken() {
      const data = await api("/api/token/status");
      renderToken(data.token);
    }

    async function reloadGuardian() {
      const data = await api("/api/token-guardian/status");
      renderGuardian(data.guardian);
    }

    async function refreshPanel(label) {
      status(`Refreshing ${label}...`);
      await reload();
    }

    async function deleteCredential(name) {
      if (!confirm("Delete this saved login credential?")) return;
      status("Deleting saved login...");
      const data = await api(`/api/credentials/${name}/delete`, { method: "POST", body: "{}" });
      renderCredentials(data.credentials);
      status("Saved login deleted.");
    }

    $("tokenForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.headless = form.elements.headless.checked;
      status("Getting token. Complete browser login if a window opens...");
      await api("/api/token/refresh", { method: "POST", body: JSON.stringify(payload) });
      await reload();
    });
    $("manualTokenForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      status("Saving pasted token...");
      await api("/api/token/save", { method: "POST", body: JSON.stringify({ ...payload, validate: true }) });
      form.elements.token.value = "";
      await reload();
    });
    $("credentialsForm").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      status("Saving CAS login locally...");
      const data = await api("/api/credentials", { method: "POST", body: JSON.stringify(payload) });
      form.elements.login_password.value = "";
      renderCredentials(data.credentials);
      status("CAS login saved.");
    });
    $("validateToken").addEventListener("click", async () => {
      status("Validating saved token...");
      const data = await api("/api/token/status?validate=1");
      renderToken(data.token);
      status("Token validation complete.");
    });
    $("refreshCredentials").addEventListener("click", async () => {
      status("Refreshing saved logins...");
      await reloadCredentials();
      status("Saved logins refreshed.");
    });
    $("startGuardian").addEventListener("click", async () => {
      status("Starting token guardian...");
      const data = await api("/api/token-guardian/start", { method: "POST", body: JSON.stringify(guardianPayload()) });
      renderGuardian(data.guardian);
      status("Token guardian started.");
    });
    $("stopGuardian").addEventListener("click", async () => {
      status("Stopping token guardian...");
      const data = await api("/api/token-guardian/stop", { method: "POST", body: "{}" });
      renderGuardian(data.guardian);
      status("Token guardian stop requested.");
    });
    $("runGuardianOnce").addEventListener("click", async () => {
      status("Running token guardian check...");
      const data = await api("/api/token-guardian/run-once", { method: "POST", body: JSON.stringify(guardianPayload()) });
      renderGuardian(data.guardian);
      status("Token guardian check queued.");
    });
    reload();
    setInterval(reload, 10000);
  </script>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local web dashboard for BJTU HPC transfers.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_HPC_TOKEN_FILE)
    parser.add_argument("--no-auto-refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    parser.add_argument("--token-guardian", action="store_true", help="Start the Token Guardian loop when the dashboard starts.")
    parser.add_argument(
        "--guardian-accounts",
        default="",
        help="Comma-separated account names for Token Guardian; empty/all means every saved account.",
    )
    parser.add_argument("--guardian-interval-seconds", type=int, default=300)
    parser.add_argument("--guardian-refresh-every-seconds", type=int, default=1800)
    parser.add_argument("--guardian-refresh-timeout-seconds", type=int, default=60)
    parser.add_argument("--guardian-failure-notify-threshold", type=int, default=3)
    parser.add_argument("--guardian-age-warning-seconds", type=int, default=DEFAULT_AGE_WARNING_SECONDS)
    parser.add_argument("--guardian-no-notifications", action="store_true")
    parser.add_argument("--guardian-auto-visible-refresh", action="store_true")
    parser.add_argument("--guardian-visible-refresh-timeout-seconds", type=int, default=900)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = WebConfig(
        config=args.config,
        token_file=args.token_file,
        auto_refresh_token=not args.no_auto_refresh_token,
        refresh_browser=args.refresh_browser,
        refresh_headless=args.refresh_headless,
    )
    server = ThreadingHTTPServer((args.host, args.port), HpcTransferHandler)
    server.web_config = config  # type: ignore[attr-defined]
    url = f"http://{args.host}:{args.port}/"
    print(f"[web] serving {url}")
    print(f"[web] config {config.config}")
    if args.token_guardian:
        guardian = start_token_guardian(
            config,
            {
                "accounts": args.guardian_accounts,
                "interval_seconds": args.guardian_interval_seconds,
                "refresh_every_seconds": args.guardian_refresh_every_seconds,
                "refresh_timeout_seconds": args.guardian_refresh_timeout_seconds,
                "failure_notify_threshold": args.guardian_failure_notify_threshold,
                "age_warning_seconds": args.guardian_age_warning_seconds,
                "notifications_enabled": not args.guardian_no_notifications,
                "auto_visible_refresh": args.guardian_auto_visible_refresh,
                "visible_refresh_timeout_seconds": args.guardian_visible_refresh_timeout_seconds,
            },
        )
        accounts = ",".join(guardian.get("accounts_filter") or []) or "all"
        print(
            "[web] token guardian "
            f"running={guardian.get('running')} accounts={accounts} "
            f"interval={guardian.get('interval_seconds')}s "
            f"refresh_every={guardian.get('refresh_every_seconds')}s"
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[web] stopped")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
