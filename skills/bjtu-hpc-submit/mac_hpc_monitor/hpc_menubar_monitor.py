#!/usr/bin/env python3
"""macOS menu bar monitor for BJTU HPC account queues."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import objc
from AppKit import (
    NSApp,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSAttributedString, NSTimer
from PyObjCTools import AppHelper


DEFAULT_PYTHON = os.getenv("HPC_MONITOR_PYTHON", "python3")
DEFAULT_SLURM_DIR = os.getenv("HPC_MONITOR_SLURM_DIR", str(Path(__file__).resolve().parents[1]))
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8765/"


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


def clean_reason(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text or "-"


def shorten(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def account_auth_error(account: dict[str, Any]) -> bool:
    if account.get("has_token") is False:
        return True
    text = str(account.get("error") or "").lower()
    if not text:
        return False
    markers = (
        "11009",
        "11011",
        "11012",
        "401",
        "no saved token",
        "missing profile token",
        "expired",
        "invalid token",
        "unauthorized",
        "need visible login",
        "cas login",
    )
    return any(marker in text for marker in markers)


def account_counts(account: dict[str, Any]) -> dict[str, int]:
    summary = account.get("summary") or {}
    running = as_int(summary.get("running"))
    pending = as_int(summary.get("pending"))
    other = as_int(summary.get("other"))
    total = as_int(summary.get("total"))
    run_open = as_int(summary.get("run_slots_open"))
    cap_open = as_int(summary.get("cap_open"))
    running_cpus = as_int(summary.get("running_cpus"))
    running_gpus = as_int(summary.get("running_gpus"))
    resource_unknown = as_int(summary.get("running_resource_unknown"))
    return {
        "running": running,
        "pending": pending,
        "other": other,
        "total": total,
        "run_open": run_open,
        "cap_open": cap_open,
        "run_slots": running + run_open,
        "cap": total + cap_open,
        "running_cpus": running_cpus,
        "running_gpus": running_gpus,
        "running_resource_unknown": resource_unknown,
    }


def account_status(account: dict[str, Any]) -> str:
    if account_auth_error(account):
        return "AUTH"
    if account.get("error"):
        return "ERROR"
    counts = account_counts(account)
    if counts["total"] == 0:
        return "IDLE"
    if counts["cap_open"] == 0:
        return "FULL"
    if counts["run_open"] > 0:
        return "OPEN"
    return "ROOM"


def account_summary_line(account: dict[str, Any]) -> str:
    name = account.get("name") or "unknown"
    counts = account_counts(account)
    status = account_status(account)
    if status == "AUTH":
        return f"{name:<7} {status:<5}   token refresh needed"
    if account.get("error"):
        return f"{name:<7} {status:<5}   query failed"
    other = f" O{counts['other']}" if counts["other"] else ""
    return (
        f"{name:<7} {status:<5}   "
        f"R{counts['running']}/{counts['run_slots']}  "
        f"J{counts['total']}/{counts['cap']}  "
        f"Q{counts['pending']}{other}"
    )


def account_detail_line(account: dict[str, Any]) -> str:
    counts = account_counts(account)
    if account_auth_error(account):
        return "auth/token refresh needed"
    if account.get("error"):
        return shorten(account.get("error"), 78)
    reasons = ((account.get("summary") or {}).get("pending_reasons") or {})
    if reasons:
        return "wait: " + shorten(format_reasons(reasons), 72)
    if counts["cap_open"]:
        return f"available: {counts['cap_open']} job slots, {counts['run_open']} run slots"
    return "no pending blockers"


def overview_counts(payload: dict[str, Any]) -> dict[str, int]:
    accounts = payload.get("accounts") or []
    counts = {
        "running": 0,
        "pending": 0,
        "total": 0,
        "run_slots": 0,
        "cap": 0,
        "errors": 0,
    }
    for account in accounts:
        item = account_counts(account)
        counts["running"] += item["running"]
        counts["pending"] += item["pending"]
        counts["total"] += item["total"]
        counts["run_slots"] += item["run_slots"]
        counts["cap"] += item["cap"]
        counts["errors"] += 1 if account.get("error") else 0
    return counts


def own_resource_summary(payload: dict[str, Any]) -> dict[str, int]:
    summary = {
        "running_gpus": 0,
        "running_cpus": 0,
        "unknown": 0,
    }
    for account in payload.get("accounts") or []:
        item = account_counts(account)
        summary["running_gpus"] += item["running_gpus"]
        summary["running_cpus"] += item["running_cpus"]
        summary["unknown"] += item["running_resource_unknown"]
    return summary


def overview_line(payload: dict[str, Any]) -> str:
    counts = overview_counts(payload)
    parts = [
        f"RUN {counts['running']}/{counts['run_slots']}",
        f"JOBS {counts['total']}/{counts['cap']}",
        f"WAIT {counts['pending']}",
    ]
    if counts["errors"]:
        parts.append(f"ERR {counts['errors']}")
    return "   ".join(parts)


def cluster_resource_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("cluster_resources") or {}


def cluster_resource_summary(payload: dict[str, Any]) -> dict[str, int]:
    resources = cluster_resource_payload(payload)
    summary = resources.get("summary") or {}
    return {
        "nodes": as_int(summary.get("nodes")),
        "gpu_alloc": as_int(summary.get("gpu_alloc")),
        "gpu_total": as_int(summary.get("gpu_total")),
        "gpu_free": as_int(summary.get("gpu_free")),
        "cpu_alloc": as_int(summary.get("cpu_alloc")),
        "cpu_total": as_int(summary.get("cpu_total")),
        "cpu_free": as_int(summary.get("cpu_free")),
        "reserved_nodes": as_int(summary.get("reserved_nodes")),
    }


def cluster_resource_line(payload: dict[str, Any]) -> str:
    resources = cluster_resource_payload(payload)
    error = resources.get("error")
    if error:
        return "nodes: query failed"
    summary = cluster_resource_summary(payload)
    own = own_resource_summary(payload)
    excluded = ",".join(resources.get("excluded_reserved_nodes") or [])
    suffix = f" excl {excluded}" if excluded else ""
    return (
        f"NODES {summary['nodes']}  "
        f"GPU {summary['gpu_alloc']}/{summary['gpu_total']}  "
        f"CPU {summary['cpu_alloc']}/{summary['cpu_total']}  "
        f"MINE G{own['running_gpus']} C{own['running_cpus']}"
        f"{suffix}"
    )


def cluster_node_line(node: dict[str, Any]) -> str:
    name = node.get("name") or "-"
    state = shorten(node.get("state") or "-", 8)
    return (
        f"{name:<6} {state:<8} "
        f"G{as_int(node.get('gpu_alloc'))}/{as_int(node.get('gpu_total'))} "
        f"C{as_int(node.get('cpu_alloc'))}/{as_int(node.get('cpu_total'))}"
    )


def format_reasons(reasons: dict[str, Any]) -> str:
    if not reasons:
        return "-"
    return ", ".join(f"{key} x{value}" for key, value in sorted(reasons.items()))


def sorted_accounts(accounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        accounts,
        key=lambda account: (
            0 if account.get("error") else 1,
            0 if account_counts(account)["total"] else 1,
            -account_counts(account)["running"],
            -account_counts(account)["pending"],
            str(account.get("name") or ""),
        ),
    )


def sorted_jobs(account: dict[str, Any]) -> list[dict[str, Any]]:
    order = {"RUNNING": 0, "R": 0, "PENDING": 1, "PD": 1}
    return sorted(
        account.get("jobs") or [],
        key=lambda job: (
            order.get(str(job.get("state") or "").upper(), 2),
            job.get("job_id") or "",
        ),
    )


def job_state(job: dict[str, Any]) -> str:
    state = str(job.get("state") or "?").upper()
    if state in {"RUNNING", "R"}:
        return "RUN"
    if state in {"PENDING", "PD"}:
        return "WAIT"
    return state[:5]


def job_row(job: dict[str, Any], name_limit: int = 32) -> str:
    state_label = job_state(job)
    job_id = job.get("job_id") or "-"
    elapsed = job.get("elapsed") or "-"
    limit = job.get("time_limit") or "-"
    reason = shorten(clean_reason(job.get("reason")), 20)
    name = shorten(job.get("name") or "", name_limit)
    return f"[{state_label}] {job_id}  {elapsed}/{limit}  {reason}  {name}"


def job_metric_line(job: dict[str, Any]) -> str:
    job_id = job.get("job_id") or "-"
    elapsed = job.get("elapsed") or "-"
    limit = job.get("time_limit") or "-"
    reason = shorten(clean_reason(job.get("reason")), 18)
    resources = job.get("resources") or {}
    cpus = resources.get("num_cpus")
    gpus = resources.get("gpu_count")
    resource_label = ""
    if cpus is not None or gpus is not None:
        resource_label = f" {gpus if gpus is not None else '?'}G/{cpus if cpus is not None else '?'}C"
    return f"{job_id:<8} {elapsed:>8}/{limit:<8} {reason}{resource_label}"


def job_name_line(job: dict[str, Any], limit: int = 54) -> str:
    return "    " + shorten(job.get("name") or "-", limit)


def render_text_summary(payload: dict[str, Any]) -> str:
    checked_at = payload.get("checked_at_local") or "-"
    lines = [f"checked_at_local: {checked_at}", overview_line(payload), "accounts:"]
    for account in sorted_accounts(payload.get("accounts") or []):
        lines.append(account_summary_line(account))
        detail = account_detail_line(account)
        if detail:
            lines.append(f"  {detail}")
        error = account.get("error")
        if error:
            continue
        for job in sorted_jobs(account):
            lines.append(f"  {job_metric_line(job)}")
            lines.append(f"  {job_name_line(job, 58)}")
    resources = cluster_resource_payload(payload)
    if resources:
        lines.append("cluster resources:")
        lines.append(f"  {cluster_resource_line(payload)}")
        for node in resources.get("nodes") or []:
            lines.append(f"  {cluster_node_line(node)}")
    return "\n".join(lines)


def queue_state_signature(payload: dict[str, Any]) -> str:
    resources = cluster_resource_payload(payload)
    cluster_state = {
        "error": resources.get("error"),
        "summary": cluster_resource_summary(payload),
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
                    "gres": node.get("gres"),
                }
                for node in resources.get("nodes") or []
            ),
            key=lambda item: str(item.get("name") or ""),
        ),
    }
    accounts_state = []
    for account in payload.get("accounts") or []:
        jobs = []
        for job in account.get("jobs") or []:
            resources = job.get("resources") or {}
            jobs.append(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "name": job.get("name") or "",
                    "state": str(job.get("state") or "").upper(),
                    "reason": clean_reason(job.get("reason")),
                    "node": job.get("node") or job.get("nodelist") or "",
                    "nodes": job.get("nodes") or "",
                    "partition": job.get("partition") or "",
                    "cpus": job.get("cpus") or job.get("ncpus") or resources.get("num_cpus"),
                    "gpus": job.get("gpus") or job.get("ngpus") or resources.get("gpu_count"),
                    "gres": resources.get("gres") or resources.get("tres_per_node") or "",
                }
            )
        accounts_state.append(
            {
                "name": account.get("name") or "",
                "account": account.get("account") or "",
                "error": account.get("error") or "",
                "summary": account.get("summary") or {},
                "jobs": sorted(jobs, key=lambda item: item["job_id"]),
            }
        )
    snapshot = {
        "cluster": cluster_state,
        "accounts": sorted(accounts_state, key=lambda item: item["name"]),
    }
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def adaptive_refresh_interval(base_interval: int, max_interval: int, stable_refreshes: int) -> int:
    return min(max_interval, base_interval * max(1, stable_refreshes + 1))


def run_queue_summary(
    python_path: str,
    slurm_dir: str,
    accounts: str | None,
    timeout: int,
    all_partitions: bool,
) -> tuple[dict[str, Any] | None, str | None, int]:
    script = str(Path(slurm_dir) / "hpc_queue_summary.py")
    command = [python_path, script, "--json", "--timeout", str(timeout)]
    if accounts:
        command.extend(["--accounts", accounts])
    if all_partitions:
        command.append("--all-partitions")

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
    except Exception as error:
        return None, str(error), -1

    payload = None
    parse_error = None
    try:
        payload = json.loads(proc.stdout)
    except Exception as error:
        parse_error = f"could not parse hpc_queue_summary JSON: {error}"

    if payload is not None:
        return payload, proc.stderr.strip() or None, proc.returncode
    return None, (proc.stderr.strip() or proc.stdout.strip() or parse_error), proc.returncode


class HPCMonitorDelegate(NSObject):
    def init(self):
        self = objc.super(HPCMonitorDelegate, self).init()
        if self is None:
            return None
        self.python_path = os.getenv("HPC_MONITOR_PYTHON", DEFAULT_PYTHON)
        self.slurm_dir = os.getenv("HPC_MONITOR_SLURM_DIR", DEFAULT_SLURM_DIR)
        self.accounts = os.getenv("HPC_MONITOR_ACCOUNTS") or None
        self.base_interval = env_int("HPC_MONITOR_INTERVAL", 60, minimum=15)
        self.max_interval = max(
            self.base_interval,
            env_int("HPC_MONITOR_MAX_INTERVAL", 600, minimum=self.base_interval),
        )
        self.interval = self.base_interval
        self.timeout = env_int("HPC_MONITOR_TIMEOUT", 45, minimum=10)
        self.all_partitions = os.getenv("HPC_MONITOR_ALL_PARTITIONS") in {"1", "true", "yes"}
        self.dashboard_url = os.getenv("HPC_MONITOR_DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
        self.status_item = None
        self.menu = None
        self.timer = None
        self.payload = None
        self.last_error = None
        self.last_returncode = 0
        self.refreshing = False
        self.last_refresh_started_at = None
        self.last_state_signature = None
        self.stable_refreshes = 0
        self.last_state_changed = False
        return self

    def applicationDidFinishLaunching_(self, notification):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.status_item.button().setTitle_("HPC ...")
        self.menu = NSMenu.alloc().init()
        self.status_item.setMenu_(self.menu)
        self.rebuild_menu()
        self.refresh_(None)

    def timerFired_(self, timer):
        self.timer = None
        self.refresh_(None)

    def refresh_(self, sender):
        if self.refreshing:
            return
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        self.refreshing = True
        self.last_refresh_started_at = datetime.now()
        if self.status_item is not None:
            self.status_item.button().setTitle_("HPC ...")
        self.rebuild_menu()
        worker = threading.Thread(target=self._refresh_worker, daemon=True)
        worker.start()

    def _refresh_worker(self):
        payload, error, returncode = run_queue_summary(
            self.python_path,
            self.slurm_dir,
            self.accounts,
            self.timeout,
            self.all_partitions,
        )
        AppHelper.callAfter(self.apply_refresh_result, payload, error, returncode)

    def update_refresh_cadence(self, payload, returncode):
        if payload is None:
            self.stable_refreshes = 0
            self.interval = self.base_interval
            self.last_state_changed = True
            return
        signature = queue_state_signature(payload)
        changed = self.last_state_signature is None or self.last_state_signature != signature
        self.last_state_signature = signature
        self.last_state_changed = changed
        if returncode != 0 or changed:
            self.stable_refreshes = 0
            self.interval = self.base_interval
        else:
            self.stable_refreshes += 1
            self.interval = adaptive_refresh_interval(
                self.base_interval,
                self.max_interval,
                self.stable_refreshes,
            )

    def schedule_next_refresh(self):
        if self.timer is not None:
            self.timer.invalidate()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            self.interval, self, "timerFired:", None, False
        )

    def apply_refresh_result(self, payload, error, returncode):
        self.update_refresh_cadence(payload, returncode)
        self.payload = payload
        self.last_error = error
        self.last_returncode = returncode
        self.refreshing = False
        self.update_status_title()
        self.rebuild_menu()
        self.schedule_next_refresh()

    def update_status_title(self):
        title = "HPC !"
        if self.payload:
            counts = overview_counts(self.payload)
            title = f"HPC R{counts['running']}/{counts['run_slots']} Q{counts['pending']}"
            if counts["errors"]:
                title += f" E{counts['errors']}"
        if self.status_item is not None:
            self.status_item.button().setTitle_(title)

    def style_item(
        self,
        item: NSMenuItem,
        title: str,
        monospace: bool = False,
        tone: str = "default",
    ):
        try:
            if tone == "header":
                font = NSFont.boldSystemFontOfSize_(13.0)
            elif monospace:
                font = NSFont.monospacedSystemFontOfSize_weight_(13.0, 0.0)
            else:
                font = NSFont.systemFontOfSize_(13.0)

            if tone == "danger":
                color = NSColor.systemRedColor()
            elif tone in {"muted", "section"}:
                color = NSColor.secondaryLabelColor()
            else:
                color = NSColor.labelColor()

            attributed = NSAttributedString.alloc().initWithString_attributes_(
                title,
                {
                    NSFontAttributeName: font,
                    NSForegroundColorAttributeName: color,
                },
            )
            item.setAttributedTitle_(attributed)
        except Exception:
            pass

    def add_item(
        self,
        title: str,
        action: str | None = None,
        enabled: bool = True,
        monospace: bool = False,
        tone: str = "default",
    ):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, action, ""
        )
        item.setEnabled_(enabled)
        if action:
            item.setTarget_(self)
        self.style_item(item, title, monospace=monospace, tone=tone)
        self.menu.addItem_(item)
        return item

    def add_separator(self):
        self.menu.addItem_(NSMenuItem.separatorItem())

    def add_section(self, title: str):
        return self.add_item(title.upper(), enabled=False, tone="section")

    def add_menu_item(
        self,
        menu: NSMenu,
        title: str,
        action: str | None = None,
        enabled: bool = True,
        monospace: bool = False,
        tone: str = "default",
    ):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        item.setEnabled_(enabled)
        if action:
            item.setTarget_(self)
        self.style_item(item, title, monospace=monospace, tone=tone)
        menu.addItem_(item)
        return item

    def add_menu_section(self, menu: NSMenu, title: str):
        return self.add_menu_item(menu, title.upper(), enabled=False, tone="section")

    def add_job_rows(self, menu: NSMenu, jobs: list[dict[str, Any]]):
        for job in jobs:
            self.add_menu_item(menu, job_metric_line(job), enabled=False, monospace=True)
            self.add_menu_item(menu, job_name_line(job, 52), enabled=False, tone="muted")

    def account_menu(self, account: dict[str, Any]) -> NSMenu:
        name = account.get("name") or "unknown"
        menu = NSMenu.alloc().initWithTitle_(str(name))
        cluster_user = account.get("account") or "-"
        token_time = account.get("token_updated_at") or "-"
        self.add_menu_item(
            menu,
            f"{name} / {cluster_user}",
            enabled=False,
            tone="header",
        )
        self.add_menu_item(menu, account_summary_line(account), enabled=False, monospace=True)
        self.add_menu_item(menu, f"token: {shorten(token_time, 32)}", enabled=False, tone="muted")

        error = account.get("error")
        if error:
            menu.addItem_(NSMenuItem.separatorItem())
            self.add_menu_section(menu, "status")
            self.add_menu_item(menu, "Query failed", enabled=False, tone="danger")
            self.add_menu_item(menu, shorten(error, 86), enabled=False, tone="muted")
            return menu

        detail = account_detail_line(account)
        if detail:
            self.add_menu_item(menu, detail, enabled=False, tone="muted")

        jobs = sorted_jobs(account)
        menu.addItem_(NSMenuItem.separatorItem())
        if not jobs:
            self.add_menu_section(menu, "jobs")
            self.add_menu_item(menu, "No current GPU jobs", enabled=False, tone="muted")
            return menu

        run_jobs = [job for job in jobs if job_state(job) == "RUN"]
        pending_jobs = [job for job in jobs if job_state(job) == "WAIT"]
        other_jobs = [job for job in jobs if job not in run_jobs and job not in pending_jobs]
        if run_jobs:
            self.add_menu_section(menu, f"running ({len(run_jobs)})")
            self.add_job_rows(menu, run_jobs)
        if pending_jobs:
            if run_jobs:
                menu.addItem_(NSMenuItem.separatorItem())
            self.add_menu_section(menu, f"waiting ({len(pending_jobs)})")
            self.add_job_rows(menu, pending_jobs)
        if other_jobs:
            menu.addItem_(NSMenuItem.separatorItem())
            self.add_menu_section(menu, f"other ({len(other_jobs)})")
            self.add_job_rows(menu, other_jobs)
        return menu

    def add_account_submenu(self, account: dict[str, Any]):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            account_summary_line(account), None, ""
        )
        item.setEnabled_(True)
        self.style_item(
            item,
            account_summary_line(account),
            monospace=True,
            tone="danger" if account.get("error") else "default",
        )
        item.setSubmenu_(self.account_menu(account))
        self.menu.addItem_(item)

    def rebuild_menu(self):
        if self.menu is None:
            return
        self.menu.removeAllItems()
        self.add_item("BJTU HPC Monitor", enabled=False, tone="header")
        if self.refreshing and self.last_refresh_started_at:
            started = self.last_refresh_started_at.strftime("%H:%M:%S")
            self.add_item(f"Refreshing since {started}", enabled=False, tone="muted")
        elif self.payload:
            self.add_item(f"Updated {self.payload.get('checked_at_local', '-')}", enabled=False, tone="muted")
        else:
            self.add_item("No queue snapshot yet", enabled=False, tone="muted")
        if self.last_error and not self.payload:
            self.add_item(f"Error: {shorten(self.last_error, 64)}", enabled=False, tone="danger")
        self.add_separator()

        if self.payload:
            accounts = sorted_accounts(self.payload.get("accounts") or [])
            self.add_section("dashboard")
            self.add_item(overview_line(self.payload), enabled=False, monospace=True)
            resources = cluster_resource_payload(self.payload)
            if resources:
                self.add_item(cluster_resource_line(self.payload), enabled=False, monospace=True)
                for node in resources.get("nodes") or []:
                    self.add_item("  " + cluster_node_line(node), enabled=False, monospace=True)
            self.add_separator()
            self.add_section("accounts")
            for account in accounts:
                self.add_account_submenu(account)
            self.add_separator()

        self.add_section("actions")
        self.add_item("Refresh Now", action="refresh:")
        self.add_item("Open Dashboard", action="openDashboard:")
        self.add_item("Open Helper Folder", action="openHelperFolder:")
        self.add_item("Copy Text Summary", action="copySummary:")
        self.add_separator()
        interval_note = f"Next refresh {self.interval}s"
        if self.stable_refreshes:
            interval_note += f" | stable x{self.stable_refreshes}"
        elif self.last_state_changed:
            interval_note += " | state changed"
        if self.accounts:
            interval_note += f" | accounts: {self.accounts}"
        self.add_item(interval_note, enabled=False, tone="muted")
        self.add_item("Quit", action="quit:")

    def openDashboard_(self, sender):
        subprocess.Popen(["/usr/bin/open", self.dashboard_url])

    def openHelperFolder_(self, sender):
        subprocess.Popen(["/usr/bin/open", self.slurm_dir])

    def copySummary_(self, sender):
        from AppKit import NSPasteboard, NSPasteboardTypeString

        if self.payload:
            text = render_text_summary(self.payload)
        else:
            text = self.last_error or "No BJTU HPC queue snapshot yet."
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        pasteboard.setString_forType_(text, NSPasteboardTypeString)

    def quit_(self, sender):
        NSApp.terminate_(self)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BJTU HPC macOS menu bar monitor")
    parser.add_argument("--once", action="store_true", help="Print one queue snapshot and exit.")
    parser.add_argument("--accounts", help="Comma-separated account aliases to query.")
    parser.add_argument("--timeout", type=int, default=env_int("HPC_MONITOR_TIMEOUT", 45, minimum=10))
    parser.add_argument("--all-partitions", action="store_true")
    parser.add_argument("--python", default=os.getenv("HPC_MONITOR_PYTHON", DEFAULT_PYTHON))
    parser.add_argument("--slurm-dir", default=os.getenv("HPC_MONITOR_SLURM_DIR", DEFAULT_SLURM_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.once:
        payload, error, returncode = run_queue_summary(
            args.python,
            args.slurm_dir,
            args.accounts or os.getenv("HPC_MONITOR_ACCOUNTS") or None,
            args.timeout,
            args.all_partitions or os.getenv("HPC_MONITOR_ALL_PARTITIONS") in {"1", "true", "yes"},
        )
        if payload is None:
            print(error or "queue summary failed", file=sys.stderr)
            return returncode or 1
        print(render_text_summary(payload))
        if error:
            print(f"warning: {error}", file=sys.stderr)
        return 0 if returncode == 0 else returncode

    app = NSApplication.sharedApplication()
    delegate = HPCMonitorDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
