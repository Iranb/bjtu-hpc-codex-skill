#!/usr/bin/env python3
"""Graphical macOS desktop widget for BJTU HPC account queues."""

from __future__ import annotations

import argparse
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
    NSBackingStoreBuffered,
    NSBezierPath,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSForegroundColorAttributeName,
    NSFloatingWindowLevel,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSMenu,
    NSMenuItem,
    NSCenterTextAlignment,
    NSLeftTextAlignment,
    NSParagraphStyleAttributeName,
    NSRightTextAlignment,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorStationary,
    NSWindowStyleMaskBorderless,
)
from Foundation import NSObject, NSString, NSTimer
from PyObjCTools import AppHelper

from hpc_menubar_monitor import (
    DEFAULT_DASHBOARD_URL,
    DEFAULT_PYTHON,
    DEFAULT_SLURM_DIR,
    account_auth_error,
    account_counts,
    account_status,
    adaptive_refresh_interval,
    clean_reason,
    cluster_resource_payload,
    cluster_resource_summary,
    env_int,
    overview_counts,
    own_resource_summary,
    queue_state_signature,
    render_text_summary,
    run_queue_summary,
    shorten,
    sorted_accounts,
    sorted_jobs,
)


DEFAULT_WIDTH = 320
DEFAULT_HEIGHT = 466


def color(red: float, green: float, blue: float, alpha: float = 1.0) -> NSColor:
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, alpha)


COLORS = {
    "bg": color(0.055, 0.067, 0.086, 0.86),
    "panel": color(0.118, 0.137, 0.176, 0.82),
    "panel_alt": color(0.145, 0.165, 0.208, 0.76),
    "stroke": color(0.95, 0.98, 1.0, 0.12),
    "text": color(0.94, 0.96, 0.98, 1.0),
    "muted": color(0.70, 0.75, 0.82, 1.0),
    "soft": color(0.50, 0.57, 0.66, 1.0),
    "green": color(0.16, 0.73, 0.47, 1.0),
    "cyan": color(0.23, 0.67, 0.86, 1.0),
    "amber": color(0.95, 0.64, 0.22, 1.0),
    "violet": color(0.77, 0.43, 0.96, 1.0),
    "red": color(0.92, 0.28, 0.32, 1.0),
    "blue": color(0.32, 0.56, 0.93, 1.0),
    "bar_bg": color(0.95, 0.98, 1.0, 0.11),
}


def status_color(status: str) -> NSColor:
    return {
        "FULL": COLORS["green"],
        "ROOM": COLORS["cyan"],
        "OPEN": COLORS["amber"],
        "IDLE": COLORS["soft"],
        "AUTH": COLORS["violet"],
        "ERROR": COLORS["red"],
    }.get(status, COLORS["soft"])


def status_tone(status: str) -> str:
    return {
        "FULL": "green",
        "ROOM": "cyan",
        "OPEN": "amber",
        "IDLE": "soft",
        "AUTH": "violet",
        "ERROR": "red",
    }.get(status, "soft")


def compact_reason(account: dict[str, Any]) -> str:
    if account_auth_error(account):
        return "token expired"
    if account.get("error"):
        return "query failed"
    counts = account_counts(account)
    reasons = ((account.get("summary") or {}).get("pending_reasons") or {})
    if reasons:
        labels = []
        for reason, count in sorted(reasons.items()):
            clean = clean_reason(reason)
            label = {
                "QOSMaxJobsPerUserLimit": "QOS",
                "QOSMaxSubmitJobPerUserLimit": "submit",
                "Resources": "resources",
                "Priority": "priority",
            }.get(clean, shorten(clean, 10))
            labels.append(f"{label}:{count}")
        return ", ".join(labels)
    if counts["cap_open"]:
        return f"{counts['cap_open']} slots"
    return "clear"


def account_running_resource_label(account: dict[str, Any]) -> str:
    counts = account_counts(account)
    if counts["running"] <= 0:
        return "G0 C0"
    gpus = counts.get("running_gpus", 0)
    cpus = counts.get("running_cpus", 0)
    if counts.get("running_resource_unknown", 0):
        return f"G{gpus or '?'} C{cpus or '?'}"
    return f"G{gpus} C{cpus}"


def node_short_name(name: Any) -> str:
    text = str(name or "-")
    return text[3:] if text.startswith("gpu") else text


def top_y(view_height: float, y: float, height: float) -> float:
    return view_height - y - height


class WidgetView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(WidgetView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.payload = None
        self.error = None
        self.refreshing = False
        self.last_refresh_started_at = None
        self.updated_at = None
        self.dashboard_url = DEFAULT_DASHBOARD_URL
        return self

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDragged_(self, event):
        window = self.window()
        if window is None:
            return
        frame = window.frame()
        delta = event.deltaX(), event.deltaY()
        frame.origin.x += delta[0]
        frame.origin.y -= delta[1]
        window.setFrame_display_(frame, True)

    def menuForEvent_(self, event):
        menu = NSMenu.alloc().initWithTitle_("BJTU HPC Widget")
        dashboard = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open Dashboard", "openDashboard:", "")
        dashboard.setTarget_(NSApp.delegate())
        menu.addItem_(dashboard)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit Widget", "quit:", "")
        quit_item.setTarget_(NSApp.delegate())
        menu.addItem_(quit_item)
        return menu

    def updateState_error_refreshing_(self, payload, error, refreshing):
        self.payload = payload
        self.error = error
        self.refreshing = refreshing
        if payload:
            self.updated_at = payload.get("checked_at_local") or datetime.now().isoformat(timespec="seconds")
        self.setNeedsDisplay_(True)

    def setDashboardURL_(self, url):
        self.dashboard_url = url

    def rounded_rect(self, rect, radius, fill, stroke=None, line_width=1.0):
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(rect, radius, radius)
        fill.setFill()
        path.fill()
        if stroke is not None:
            stroke.setStroke()
            path.setLineWidth_(line_width)
            path.stroke()

    def draw_text(
        self,
        text,
        x,
        y,
        width,
        height,
        size=12.0,
        tone="text",
        weight="regular",
        align="left",
    ):
        view_height = self.bounds().size.height
        if weight == "bold":
            font = NSFont.systemFontOfSize_weight_(size, NSFontWeightSemibold)
        else:
            font = NSFont.systemFontOfSize_weight_(size, NSFontWeightRegular)
            font = NSFont.labelFontOfSize_(size) or font
        alignment = {
            "center": NSCenterTextAlignment,
            "right": NSRightTextAlignment,
        }.get(align, NSLeftTextAlignment)
        style = NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(alignment)
        attrs = {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: COLORS.get(tone, COLORS["text"]),
            NSParagraphStyleAttributeName: style,
        }
        rect = NSMakeRect(x, top_y(view_height, y, height), width, height)
        NSString.stringWithString_(str(text)).drawInRect_withAttributes_(rect, attrs)

    def draw_pill(self, text, x, y, width, height, fill, text_tone="text"):
        view_height = self.bounds().size.height
        rect = NSMakeRect(x, top_y(view_height, y, height), width, height)
        self.rounded_rect(rect, height / 2, fill)
        self.draw_text(text, x + 6, y + 2, width - 12, height - 3, 10.0, text_tone, "bold", "center")

    def draw_bar(self, x, y, width, height, current, total, fill):
        view_height = self.bounds().size.height
        bg_rect = NSMakeRect(x, top_y(view_height, y, height), width, height)
        self.rounded_rect(bg_rect, height / 2, COLORS["bar_bg"])
        if total:
            filled = max(0.0, min(1.0, current / total)) * width
            fg_rect = NSMakeRect(x, top_y(view_height, y, height), filled, height)
            self.rounded_rect(fg_rect, height / 2, fill)

    def draw_status_dot(self, x, y, size, fill):
        view_height = self.bounds().size.height
        rect = NSMakeRect(x, top_y(view_height, y, size), size, size)
        path = NSBezierPath.bezierPathWithOvalInRect_(rect)
        fill.setFill()
        path.fill()

    def draw_vrule(self, x, y, height):
        view_height = self.bounds().size.height
        rect = NSMakeRect(x, top_y(view_height, y, height), 1, height)
        path = NSBezierPath.bezierPathWithRect_(rect)
        COLORS["stroke"].setFill()
        path.fill()

    def draw_metric(self, label, value, x, y, width, value_tone="text"):
        self.draw_text(label, x, y, width, 10, 8.5, "soft", "bold", "center")
        self.draw_text(value, x, y + 13, width, 16, 12.0, value_tone, "bold", "center")

    def draw_overview(self, payload):
        counts = overview_counts(payload)
        total_run = max(counts["run_slots"], 1)
        total_cap = max(counts["cap"], 1)
        width = self.bounds().size.width
        overview_x = 20
        overview_w = width - 40
        section_gap = 10
        section_w = (overview_w - section_gap * 2) / 3
        self.draw_text("BJTU HPC", 20, 14, 150, 26, 19, "text", "bold")
        stamp = payload.get("checked_at_local") or "-"
        self.draw_text("updated", width - 148, 18, 58, 18, 10.5, "muted", align="right")
        self.draw_text(stamp[-8:], width - 82, 18, 62, 18, 10.5, "muted", align="right")

        metrics = [
            ("Running", f"{counts['running']}/{counts['run_slots']}", counts["running"], total_run, COLORS["green"]),
            ("Waiting", str(counts["pending"]), counts["pending"], max(counts["total"], 1), COLORS["amber"]),
            ("Jobs", f"{counts['total']}/{counts['cap']}", counts["total"], total_cap, COLORS["cyan"]),
        ]
        for index, (label, value, current, total, fill) in enumerate(metrics):
            x = overview_x + index * (section_w + section_gap)
            bar_x = x + 7
            bar_w = section_w - 14
            self.draw_text(label, x, 48, section_w, 15, 10.5, "muted", align="center")
            self.draw_text(value, x, 64, section_w, 25, 22, "text", "bold", "center")
            self.draw_bar(bar_x, 91, bar_w, 6, current, total, fill)

    def draw_cluster_resources(self, payload):
        resources = cluster_resource_payload(payload)
        if not resources:
            return
        width = self.bounds().size.width
        view_height = self.bounds().size.height
        card_x = 10
        card_y = 108
        card_w = width - 20
        card_h = 66
        rect = NSMakeRect(card_x, top_y(view_height, card_y, card_h), card_w, card_h)
        self.rounded_rect(rect, 12, COLORS["panel"], COLORS["stroke"])

        if resources.get("error"):
            self.draw_text("GPU nodes", card_x + 10, card_y + 7, 80, 14, 10, "soft", "bold")
            self.draw_text("query failed", card_x + 94, card_y + 7, card_w - 106, 14, 10, "red", "bold", "right")
            self.draw_text(shorten(resources.get("error"), 46), card_x + 10, card_y + 26, card_w - 20, 14, 9.5, "muted")
            return

        summary = cluster_resource_summary(payload)
        own = own_resource_summary(payload)
        excluded = ",".join(resources.get("excluded_reserved_nodes") or [])
        self.draw_text("GPU nodes", card_x + 10, card_y + 6, 72, 14, 10, "soft", "bold")
        self.draw_text("Cluster", card_x + 10, card_y + 22, 48, 12, 9.5, "soft", "bold")
        self.draw_text(f"G{summary['gpu_alloc']}/{summary['gpu_total']} C{summary['cpu_alloc']}/{summary['cpu_total']}", card_x + 64, card_y + 22, 112, 12, 9.5, "text", "bold")
        if excluded:
            self.draw_text(f"ex {shorten(excluded, 8)}", card_x + 230, card_y + 6, card_w - 240, 14, 9.5, "muted", align="right")
        self.draw_text("Mine", card_x + 182, card_y + 22, 34, 12, 9.5, "soft", "bold")
        self.draw_text(f"G{own['running_gpus']} C{own['running_cpus']}", card_x + 218, card_y + 22, card_w - 228, 12, 9.5, "cyan", "bold", "right")
        if own.get("unknown"):
            self.draw_text(f"{own['unknown']} unknown", card_x + 164, card_y + 35, card_w - 174, 12, 9.5, "amber", align="right")

        nodes = (resources.get("nodes") or [])[:4]
        chip_gap = 8
        chip_w = (card_w - 28 - chip_gap) / 2
        for index, node in enumerate(nodes):
            row = index // 2
            col = index % 2
            x = card_x + 10 + col * (chip_w + chip_gap)
            y = card_y + 42 + row * 11
            gpu_total = int(node.get("gpu_total") or 0)
            gpu_alloc = int(node.get("gpu_alloc") or 0)
            gpu_free = max(0, gpu_total - gpu_alloc)
            tone = "green" if gpu_free else "amber"
            self.draw_status_dot(x, y + 3, 6, COLORS[tone])
            label = (
                f"{node_short_name(node.get('name'))} "
                f"G{gpu_alloc}/{gpu_total} "
                f"C{int(node.get('cpu_alloc') or 0)}/{int(node.get('cpu_total') or 0)}"
            )
            self.draw_text(label, x + 9, y, chip_w - 9, 12, 9.5, "muted", "bold")

    def draw_account(self, account, index):
        y = 184 + index * 49
        view_height = self.bounds().size.height
        width = self.bounds().size.width
        card_x = 10
        card_w = width - 20
        card_h = 45
        rect = NSMakeRect(card_x, top_y(view_height, y, card_h), card_w, card_h)
        fill = COLORS["panel_alt"] if index % 2 else COLORS["panel"]
        self.rounded_rect(rect, 12, fill, COLORS["stroke"])

        name = str(account.get("name") or "unknown")
        status = account_status(account)
        counts = account_counts(account)
        dot = status_color(status)
        inner_left = card_x + 10
        inner_right = card_x + card_w - 10
        identity_w = 64
        metric_gap = 6
        metric_x = inner_left + identity_w + metric_gap
        metric_w = inner_right - metric_x
        col_w = metric_w / 4

        self.draw_status_dot(inner_left, y + 19, 8, dot)
        self.draw_text(shorten(name, 7), inner_left + 15, y + 14, identity_w - 15, 18, 13.0, status_tone(status), "bold")

        if account_auth_error(account):
            detail = compact_reason(account)
            tone = "violet"
        elif account.get("error"):
            detail = compact_reason(account)
            tone = "red"
        else:
            reasons = ((account.get("summary") or {}).get("pending_reasons") or {})
            if reasons:
                detail = compact_reason(account)
                tone = "amber"
            else:
                detail = compact_reason(account)
                tone = "soft"
        for col in range(1, 4):
            self.draw_vrule(metric_x + col * col_w, y + 9, 24)

        wait_tone = "amber" if counts["pending"] else "soft"
        self.draw_metric("RUN", str(counts["running"]), metric_x, y + 6, col_w, "text")
        self.draw_metric("G / C", account_running_resource_label(account), metric_x + col_w, y + 6, col_w, "text")
        self.draw_metric("JOBS", f"{counts['total']}/{counts['cap']}", metric_x + col_w * 2, y + 6, col_w, "text")
        self.draw_metric("WAIT", str(counts["pending"]), metric_x + col_w * 3, y + 6, col_w, wait_tone)
        self.draw_text(
            shorten(detail, 34),
            metric_x,
            y + 33,
            metric_w,
            11,
            9.5,
            tone,
            "bold" if tone != "soft" else "regular",
            "center",
        )

    def draw_jobs_strip(self, payload):
        accounts = payload.get("accounts") or []
        jobs = []
        for account in accounts:
            for job in sorted_jobs(account):
                jobs.append((account.get("name") or "?", job))
        if not jobs:
            self.draw_text("No active GPU jobs", 20, 448, 170, 16, 10.5, "muted")
            return
        recent = jobs[:3]
        x = 24
        y = 446
        for account_name, job in recent:
            state = str(job.get("state") or "").upper()
            fill = COLORS["green"] if state in {"RUNNING", "R"} else COLORS["amber"]
            self.draw_status_dot(x, y + 5, 7, fill)
            text = f"{account_name}:{job.get('job_id')} {shorten(clean_reason(job.get('reason')), 10)}"
            self.draw_text(text, x + 11, y, 124, 17, 10.5, "muted")
            x += 120

    def draw_footer(self, account_count):
        self.draw_text(str(account_count), 20, 448, 16, 16, 10.5, "text", "bold")
        self.draw_text("accounts", 40, 448, 78, 16, 10.5, "muted")
        if self.refreshing:
            self.draw_text("refreshing", 112, 448, 92, 16, 10.5, "cyan", "bold")
        elif self.error:
            self.draw_text("network error", 112, 448, 118, 16, 10.5, "red", "bold")

    def draw_status_legend(self, compact=False):
        width = self.bounds().size.width
        items = (
            [
                ("F", COLORS["green"]),
                ("R", COLORS["cyan"]),
                ("O", COLORS["amber"]),
                ("T", COLORS["violet"]),
                ("E", COLORS["red"]),
            ]
            if compact
            else [
                ("full", COLORS["green"]),
                ("room", COLORS["cyan"]),
                ("open", COLORS["amber"]),
                ("token", COLORS["violet"]),
                ("err", COLORS["red"]),
            ]
        )
        x = width - (85 if compact else 198)
        y = 448
        for label, fill in items:
            self.draw_status_dot(x, y + 4, 5, fill)
            label_w = 8 if compact else 32 if label == "token" else 18 if label == "err" else 25
            self.draw_text(label, x + 8, y, label_w, 12, 8.2, "muted")
            x += label_w + (9 if compact else 15)

    def drawRect_(self, dirty_rect):
        bounds = self.bounds()
        self.rounded_rect(bounds, 18, COLORS["bg"], COLORS["stroke"])
        self.rounded_rect(
            NSMakeRect(10, bounds.size.height - 102, bounds.size.width - 20, 90),
            14,
            color(0.08, 0.10, 0.135, 0.82),
            COLORS["stroke"],
        )

        if self.payload:
            self.draw_overview(self.payload)
            self.draw_cluster_resources(self.payload)
            accounts = sorted_accounts(self.payload.get("accounts") or [])
            for index, account in enumerate(accounts[:5]):
                self.draw_account(account, index)
            self.draw_status_legend(compact=bool(self.refreshing or self.error))
            if len(accounts) >= 5:
                self.draw_footer(len(accounts))
            else:
                self.draw_jobs_strip(self.payload)
            return

        width = bounds.size.width
        self.draw_text("BJTU HPC", 22, 20, 160, 28, 20, "text", "bold")
        if self.error:
            self.draw_pill("ERROR", width - 88, 24, 66, 22, COLORS["red"])
            self.draw_text(shorten(self.error, 52), 24, 82, width - 48, 46, 12, "muted")
        elif self.refreshing:
            self.draw_pill("REFRESH", width - 104, 24, 80, 22, COLORS["blue"])
            self.draw_text("Loading queue snapshot...", 24, 82, 220, 24, 13, "muted")
        else:
            self.draw_text("Waiting for queue snapshot...", 24, 82, 240, 24, 13, "muted")


class WidgetDelegate(NSObject):
    def init(self):
        self = objc.super(WidgetDelegate, self).init()
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
        self.width = env_int("HPC_WIDGET_WIDTH", DEFAULT_WIDTH, minimum=300)
        self.height = env_int("HPC_WIDGET_HEIGHT", DEFAULT_HEIGHT, minimum=340)
        self.window = None
        self.view = None
        self.timer = None
        self.refreshing = False
        self.last_state_signature = None
        self.stable_refreshes = 0
        return self

    def applicationDidFinishLaunching_(self, notification):
        NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        screen = NSScreen.mainScreen()
        visible = screen.visibleFrame() if screen else NSMakeRect(0, 0, 1440, 900)
        x = visible.origin.x + visible.size.width - self.width - 24
        y = visible.origin.y + visible.size.height - self.height - 38
        frame = NSMakeRect(x, y, self.width, self.height)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setHasShadow_(True)
        self.window.setMovableByWindowBackground_(True)
        self.window.setLevel_(NSFloatingWindowLevel)
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        self.view = WidgetView.alloc().initWithFrame_(NSMakeRect(0, 0, self.width, self.height))
        self.view.setDashboardURL_(self.dashboard_url)
        self.window.setContentView_(self.view)
        self.window.makeKeyAndOrderFront_(None)
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
        if self.view:
            self.view.updateState_error_refreshing_(self.view.payload, self.view.error, True)
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
            return
        signature = queue_state_signature(payload)
        changed = self.last_state_signature is None or self.last_state_signature != signature
        self.last_state_signature = signature
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
        self.refreshing = False
        if self.view:
            visible_payload = payload if payload is not None else self.view.payload
            visible_error = error if (returncode != 0 or payload is None) else None
            self.view.updateState_error_refreshing_(visible_payload, visible_error, False)
        self.schedule_next_refresh()

    def openDashboard_(self, sender):
        subprocess.Popen(["/usr/bin/open", self.dashboard_url])

    def quit_(self, sender):
        NSApp.terminate_(self)


def render_preview(payload: dict[str, Any], path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = DEFAULT_WIDTH, DEFAULT_HEIGHT
    image = Image.new("RGBA", (width, height), (15, 18, 24, 235))
    draw = ImageDraw.Draw(image)
    system_font = "/System/Library/Fonts/SFNS.ttf"
    try:
        font = ImageFont.truetype(system_font, 13)
        small = ImageFont.truetype(system_font, 11)
        medium = ImageFont.truetype(system_font, 12)
        bold = ImageFont.truetype(system_font, 20)
    except Exception:
        font = small = medium = bold = ImageFont.load_default()

    def fit_text(text: str, draw_font, max_width: int) -> str:
        text = str(text)
        if draw.textbbox((0, 0), text, font=draw_font)[2] <= max_width:
            return text
        suffix = "..."
        for end in range(len(text), 0, -1):
            candidate = text[:end] + suffix
            if draw.textbbox((0, 0), candidate, font=draw_font)[2] <= max_width:
                return candidate
        return suffix

    def draw_fit_text(text: str, box, draw_font, fill, align: str = "left") -> None:
        x, y, box_w, _box_h = box
        fitted = fit_text(text, draw_font, int(box_w))
        bounds = draw.textbbox((0, 0), fitted, font=draw_font)
        text_w = bounds[2] - bounds[0]
        if align == "center":
            tx = x + max((box_w - text_w) / 2, 0)
        elif align == "right":
            tx = x + max(box_w - text_w, 0)
        else:
            tx = x
        draw.text((int(tx), int(y)), fitted, fill=fill, font=draw_font)

    def draw_metric(label: str, value: str, x: float, y: float, box_w: float, value_fill) -> None:
        draw_fit_text(label, (x, y, box_w, 10), small, (128, 145, 166), "center")
        draw_fit_text(value, (x, y + 13, box_w, 16), font, value_fill, "center")

    draw.rounded_rectangle((10, 12, width - 10, 102), radius=14, fill=(22, 27, 36, 230), outline=(255, 255, 255, 32))
    draw_fit_text("BJTU HPC", (20, 15, 150, 26), bold, (240, 245, 250))
    stamp = payload.get("checked_at_local") or "-"
    draw_fit_text("updated", (width - 148, 19, 58, 18), small, (178, 190, 208), "right")
    draw_fit_text(stamp[-8:], (width - 82, 19, 62, 18), small, (178, 190, 208), "right")

    counts = overview_counts(payload)
    cards = [("Running", f"{counts['running']}/{counts['run_slots']}", (48, 186, 120)), ("Waiting", str(counts["pending"]), (242, 164, 56)), ("Jobs", f"{counts['total']}/{counts['cap']}", (58, 171, 218))]
    overview_x = 20
    overview_w = width - 40
    section_gap = 10
    section_w = (overview_w - section_gap * 2) / 3
    for index, (label, value, fill) in enumerate(cards):
        x = overview_x + index * (section_w + section_gap)
        bar_x = int(x + 7)
        bar_w = int(section_w - 14)
        draw_fit_text(label, (x, 48, section_w, 15), small, (178, 190, 208), "center")
        draw_fit_text(value, (x, 64, section_w, 25), bold, (240, 245, 250), "center")
        draw.rounded_rectangle((bar_x, 92, bar_x + bar_w, 98), radius=3, fill=(255, 255, 255, 28))
        total = max(counts["run_slots" if label == "Running" else "total" if label == "Waiting" else "cap"], 1)
        current = counts["running" if label == "Running" else "pending" if label == "Waiting" else "total"]
        draw.rounded_rectangle((bar_x, 92, bar_x + int(bar_w * min(current / total, 1)), 98), radius=3, fill=fill)

    resources = cluster_resource_payload(payload)
    if resources:
        card_x = 10
        card_y = 108
        card_w = width - 20
        card_h = 66
        draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h), radius=12, fill=(32, 37, 49, 210), outline=(255, 255, 255, 28))
        if resources.get("error"):
            draw_fit_text("GPU nodes", (card_x + 10, card_y + 7, 80, 14), small, (128, 145, 166))
            draw_fit_text("query failed", (card_x + 94, card_y + 7, card_w - 106, 14), small, (234, 72, 82), "right")
            draw_fit_text(resources.get("error"), (card_x + 10, card_y + 26, card_w - 20, 14), small, (178, 190, 208))
        else:
            summary = cluster_resource_summary(payload)
            own = own_resource_summary(payload)
            excluded = ",".join(resources.get("excluded_reserved_nodes") or [])
            draw_fit_text("GPU nodes", (card_x + 10, card_y + 6, 72, 14), small, (128, 145, 166))
            draw_fit_text("Cluster", (card_x + 10, card_y + 22, 48, 12), small, (128, 145, 166))
            draw_fit_text(f"G{summary['gpu_alloc']}/{summary['gpu_total']} C{summary['cpu_alloc']}/{summary['cpu_total']}", (card_x + 64, card_y + 22, 112, 12), small, (240, 245, 250))
            if excluded:
                draw_fit_text(f"ex {excluded}", (card_x + 230, card_y + 6, card_w - 240, 14), small, (178, 190, 208), "right")
            draw_fit_text("Mine", (card_x + 182, card_y + 22, 34, 12), small, (128, 145, 166))
            draw_fit_text(f"G{own['running_gpus']} C{own['running_cpus']}", (card_x + 218, card_y + 22, card_w - 228, 12), small, (58, 171, 218), "right")
            if own.get("unknown"):
                draw_fit_text(f"{own['unknown']} unknown", (card_x + 164, card_y + 35, card_w - 174, 12), small, (242, 164, 56), "right")
            nodes = (resources.get("nodes") or [])[:4]
            chip_gap = 8
            chip_w = (card_w - 28 - chip_gap) / 2
            for node_index, node in enumerate(nodes):
                row = node_index // 2
                col = node_index % 2
                x = card_x + 10 + col * (chip_w + chip_gap)
                y = card_y + 42 + row * 11
                gpu_total = int(node.get("gpu_total") or 0)
                gpu_alloc = int(node.get("gpu_alloc") or 0)
                dot = (48, 186, 120) if max(0, gpu_total - gpu_alloc) else (242, 164, 56)
                draw.ellipse((x, y + 3, x + 6, y + 9), fill=dot)
                label = (
                    f"{node_short_name(node.get('name'))} "
                    f"G{gpu_alloc}/{gpu_total} "
                    f"C{int(node.get('cpu_alloc') or 0)}/{int(node.get('cpu_total') or 0)}"
                )
                draw_fit_text(label, (x + 9, y, chip_w - 9, 12), small, (178, 190, 208))

    status_rgb = {
        "FULL": (48, 186, 120),
        "ROOM": (58, 171, 218),
        "OPEN": (242, 164, 56),
        "IDLE": (128, 145, 166),
        "AUTH": (196, 110, 245),
        "ERROR": (234, 72, 82),
    }
    status_name_rgb = status_rgb
    for index, account in enumerate(sorted_accounts(payload.get("accounts") or [])[:5]):
        y = 184 + index * 49
        card_x = 10
        card_w = width - 20
        card_h = 45
        fill = (32, 37, 49, 210) if index % 2 == 0 else (38, 44, 58, 200)
        draw.rounded_rectangle((card_x, y, card_x + card_w, y + card_h), radius=12, fill=fill, outline=(255, 255, 255, 28))
        name = str(account.get("name") or "unknown")
        status = account_status(account)
        rgb = status_rgb.get(status, (128, 145, 166))
        name_rgb = status_name_rgb.get(status, (128, 145, 166))
        c = account_counts(account)
        inner_left = card_x + 10
        inner_right = card_x + card_w - 10
        identity_w = 64
        metric_gap = 6
        metric_x = inner_left + identity_w + metric_gap
        metric_w = inner_right - metric_x
        col_w = metric_w / 4
        draw.ellipse((inner_left, y + 19, inner_left + 8, y + 27), fill=rgb)
        draw_fit_text(name, (inner_left + 15, y + 14, identity_w - 15, 18), font, name_rgb)

        for col in range(1, 4):
            rule_x = int(metric_x + col * col_w)
            draw.line((rule_x, y + 9, rule_x, y + 33), fill=(255, 255, 255, 24), width=1)
        wait_fill = (242, 164, 56) if c["pending"] else (128, 145, 166)
        draw_metric("RUN", str(c["running"]), metric_x, y + 6, col_w, (240, 245, 250))
        draw_metric("G / C", account_running_resource_label(account), metric_x + col_w, y + 6, col_w, (240, 245, 250))
        draw_metric("JOBS", f"{c['total']}/{c['cap']}", metric_x + col_w * 2, y + 6, col_w, (240, 245, 250))
        draw_metric("WAIT", str(c["pending"]), metric_x + col_w * 3, y + 6, col_w, wait_fill)
        detail = compact_reason(account)
        reasons = ((account.get("summary") or {}).get("pending_reasons") or {})
        if account_auth_error(account):
            detail_fill = (196, 110, 245)
        elif account.get("error"):
            detail_fill = (234, 72, 82)
        elif reasons:
            detail_fill = (242, 164, 56)
        else:
            detail_fill = (128, 145, 166)
        draw_fit_text(detail, (metric_x, y + 33, metric_w, 11), small, detail_fill, "center")
    legend = [
        ("full", (48, 186, 120), 25),
        ("room", (58, 171, 218), 25),
        ("open", (242, 164, 56), 25),
        ("token", (196, 110, 245), 32),
        ("err", (234, 72, 82), 18),
    ]
    legend_x = width - 198
    for label, fill, label_w in legend:
        draw.ellipse((legend_x, 452, legend_x + 5, 457), fill=fill)
        draw_fit_text(label, (legend_x + 8, 448, label_w, 12), small, (178, 190, 208))
        legend_x += label_w + 15
    if len(payload.get("accounts") or []) >= 5:
        draw_fit_text(str(len(payload.get("accounts") or [])), (20, 448, 16, 16), small, (240, 245, 250))
        draw_fit_text("accounts", (40, 448, 78, 16), small, (178, 190, 208))
    image.save(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BJTU HPC graphical desktop widget")
    parser.add_argument("--once", action="store_true", help="Print one queue snapshot and exit.")
    parser.add_argument("--preview", type=Path, help="Write a PNG preview and exit.")
    parser.add_argument("--accounts", help="Comma-separated account aliases to query.")
    parser.add_argument("--timeout", type=int, default=env_int("HPC_MONITOR_TIMEOUT", 45, minimum=10))
    parser.add_argument("--all-partitions", action="store_true")
    parser.add_argument("--python", default=os.getenv("HPC_MONITOR_PYTHON", DEFAULT_PYTHON))
    parser.add_argument("--slurm-dir", default=os.getenv("HPC_MONITOR_SLURM_DIR", DEFAULT_SLURM_DIR))
    return parser.parse_args()


def one_snapshot(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None, int]:
    return run_queue_summary(
        args.python,
        args.slurm_dir,
        args.accounts or os.getenv("HPC_MONITOR_ACCOUNTS") or None,
        args.timeout,
        args.all_partitions or os.getenv("HPC_MONITOR_ALL_PARTITIONS") in {"1", "true", "yes"},
    )


def main() -> int:
    args = parse_args()
    if args.once or args.preview:
        payload, error, returncode = one_snapshot(args)
        if payload is None:
            print(error or "queue summary failed", file=sys.stderr)
            return returncode or 1
        if args.preview:
            render_preview(payload, args.preview)
            print(args.preview)
        else:
            print(render_text_summary(payload))
        if error:
            print(f"warning: {error}", file=sys.stderr)
        return 0 if returncode == 0 else returncode

    app = NSApplication.sharedApplication()
    delegate = WidgetDelegate.alloc().init()
    app.setDelegate_(delegate)
    AppHelper.runEventLoop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
