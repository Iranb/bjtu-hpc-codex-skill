#!/usr/bin/env python3
"""Graphical macOS desktop widget for BJTU HPC account queues."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
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
    NSImage,
    NSImageSymbolConfiguration,
    NSMakeRect,
    NSMutableParagraphStyle,
    NSMenu,
    NSMenuItem,
    NSCenterTextAlignment,
    NSLeftTextAlignment,
    NSParagraphStyleAttributeName,
    NSRightTextAlignment,
    NSNormalWindowLevel,
    NSRoundLineCapStyle,
    NSRoundLineJoinStyle,
    NSScreen,
    NSView,
    NSWindow,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
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
DEFAULT_HEIGHT = 370
CONFIG_PATH = Path.home() / "Library" / "Application Support" / "BJTUHPCWidget" / "config.json"
ACCOUNT_START_Y = 184
ACCOUNT_FOOTER_BOTTOM_MARGIN = 18
ACCOUNT_VISIBLE_ROWS = 3
ACCOUNT_DEFAULT_CARD_H = 45
ACCOUNT_MIN_CARD_H = 28
ACCOUNT_DEFAULT_GAP = 4


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def load_widget_config(default_always_on_top: bool, default_all_spaces: bool) -> dict[str, Any]:
    config = {
        "all_spaces": default_all_spaces,
        "always_on_top": default_always_on_top,
        "auto_visible_refresh": False,
    }
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return config
    if isinstance(payload, dict):
        if "always_on_top" in payload:
            config["always_on_top"] = bool(payload["always_on_top"])
        if "all_spaces" in payload:
            config["all_spaces"] = bool(payload["all_spaces"])
        if "auto_visible_refresh" in payload:
            config["auto_visible_refresh"] = bool(payload["auto_visible_refresh"])
            config["_auto_visible_config_present"] = True
    return config


def save_widget_config(config: dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: value for key, value in config.items() if not key.startswith("_")}
        CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


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
        return f"open {counts['cap_open']} slots"
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


def dashboard_api_url(dashboard_url: str, path: str) -> str:
    return dashboard_url.rstrip("/") + path


def guardian_account_state(guardian: dict[str, Any] | None, name: str) -> dict[str, Any]:
    if not guardian:
        return {}
    row = (guardian.get("accounts") or {}).get(str(name))
    return row if isinstance(row, dict) else {}


def guardian_account_attention_reason(guardian: dict[str, Any] | None, name: str) -> str:
    row = guardian_account_state(guardian, name)
    if not row:
        return ""
    threshold = int((guardian or {}).get("failure_notify_threshold") or 3)
    failures = int(row.get("headless_failure_count") or 0)
    status = str(row.get("status") or "")
    reason = clean_reason(row.get("attention_reason"))
    if row.get("needs_visible_login") or status == "needs_visible_login":
        return "visible login"
    if row.get("age_warning") or reason == "token_age":
        return "token age"
    if reason == "headless_failures" or (row.get("attention_required") and failures >= threshold):
        return "headless failed"
    if row.get("attention_required"):
        return shorten(reason, 18)
    return ""


def guardian_account_visible_status(guardian: dict[str, Any] | None, name: str) -> str:
    if not guardian:
        return ""
    visible_refreshes = guardian.get("visible_refreshes") or {}
    top_row = visible_refreshes.get(str(name))
    if isinstance(top_row, dict) and top_row.get("status"):
        return str(top_row.get("status") or "")
    row = guardian_account_state(guardian, name)
    visible_row = row.get("visible_refresh") if isinstance(row, dict) else None
    if isinstance(visible_row, dict):
        return str(visible_row.get("status") or "")
    return ""


def guardian_visible_refresh_running(guardian: dict[str, Any] | None) -> bool:
    if not guardian:
        return False
    visible_refreshes = guardian.get("visible_refreshes") or {}
    for row in visible_refreshes.values():
        if isinstance(row, dict) and row.get("status") == "running":
            return True
    for name in (guardian.get("accounts") or {}):
        if guardian_account_visible_status(guardian, str(name)) == "running":
            return True
    return False


def guardian_attention_accounts(guardian: dict[str, Any] | None) -> list[str]:
    if not guardian:
        return []
    names = []
    for name, row in sorted((guardian.get("accounts") or {}).items()):
        if guardian_account_attention_reason(guardian, str(name)):
            names.append(str(name))
    return names


def guardian_error_count(guardian: dict[str, Any] | None) -> int:
    if not guardian:
        return 0
    return sum(
        1
        for row in (guardian.get("accounts") or {}).values()
        if row.get("needs_visible_login") or row.get("status") == "needs_visible_login"
    )


def widget_sorted_accounts(
    accounts: list[dict[str, Any]],
    guardian: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    def key(account: dict[str, Any]) -> tuple[Any, ...]:
        name = str(account.get("name") or "")
        counts = account_counts(account)
        token_attention = guardian_account_attention_reason(guardian, name)
        visible_running = guardian_account_visible_status(guardian, name) == "running"
        hard_attention = (
            bool(token_attention)
            or visible_running
            or account_auth_error(account)
            or bool(account.get("error"))
        )
        open_slots = counts["run_open"] > 0 or counts["cap_open"] > 0
        reasons = {
            clean_reason(reason)
            for reason in ((account.get("summary") or {}).get("pending_reasons") or {})
        }
        normal_cap_reasons = {"QOSMaxJobsPerUserLimit", "QOSMaxSubmitJobPerUserLimit"}
        unusual_pending = bool(reasons and not reasons.issubset(normal_cap_reasons))
        priority = 0 if hard_attention else 1 if open_slots else 2 if unusual_pending else 3
        return (
            priority,
            -counts["run_open"],
            -counts["cap_open"],
            -counts["running"],
            -counts["pending"],
            name,
        )

    return sorted(accounts, key=key)


def widget_footer_y(view_height: float) -> float:
    return max(ACCOUNT_START_Y + 150, view_height - ACCOUNT_FOOTER_BOTTOM_MARGIN)


def account_row_layout(row_count: int, view_height: float = DEFAULT_HEIGHT) -> list[tuple[float, float]]:
    if row_count <= 0:
        return []
    available = max(ACCOUNT_MIN_CARD_H, widget_footer_y(view_height) - ACCOUNT_START_Y - 24)
    gap = ACCOUNT_DEFAULT_GAP
    card_h = min(
        ACCOUNT_DEFAULT_CARD_H,
        max(ACCOUNT_MIN_CARD_H, int((available - gap * (row_count - 1)) / row_count)),
    )
    return [
        (ACCOUNT_START_Y + index * (card_h + gap), float(card_h))
        for index in range(row_count)
    ]


def max_account_scroll_offset(account_count: int) -> int:
    return max(0, account_count - ACCOUNT_VISIBLE_ROWS)


def clamp_account_scroll_offset(offset: int, account_count: int) -> int:
    return max(0, min(max_account_scroll_offset(account_count), int(offset)))


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


def post_guardian_json(
    dashboard_url: str,
    path: str,
    payload: dict[str, Any],
    timeout: int = 4,
) -> tuple[dict[str, Any] | None, str | None]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        dashboard_api_url(dashboard_url, path),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
        return None, str(error)
    return data.get("guardian") or {}, None


def guardian_state_signature(guardian: dict[str, Any] | None) -> str:
    if not guardian:
        return ""
    accounts = []
    for name, row in sorted((guardian.get("accounts") or {}).items()):
        accounts.append(
            {
                "name": name,
                "status": row.get("status"),
                "attention": row.get("attention_required"),
                "reason": row.get("attention_reason"),
                "failures": row.get("headless_failure_count"),
                "age_warning": row.get("age_warning"),
                "visible": (row.get("visible_refresh") or {}).get("status"),
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
            "auto_visible_refresh": guardian.get("auto_visible_refresh"),
            "notifications_enabled": guardian.get("notifications_enabled"),
            "visible_refreshes": visible_refreshes,
            "accounts": accounts,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def top_y(view_height: float, y: float, height: float) -> float:
    return view_height - y - height


class WidgetView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(WidgetView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.payload = None
        self.guardian = None
        self.guardian_error = None
        self.error = None
        self.refreshing = False
        self.last_refresh_started_at = None
        self.updated_at = None
        self.dashboard_url = DEFAULT_DASHBOARD_URL
        self.always_on_top = True
        self.pin_button_rect = (0.0, 0.0, 0.0, 0.0)
        self.token_account_rects = {}
        self.token_requesting_accounts = set()
        self.account_scroll_offset = 0
        return self

    def acceptsFirstMouse_(self, event):
        return True

    def event_hits_top_rect(self, event, rect) -> bool:
        local = self.convertPoint_fromView_(event.locationInWindow(), None)
        x, y, width, height = rect
        top_point_y = self.bounds().size.height - local.y
        return x <= local.x <= x + width and y <= top_point_y <= y + height

    def mouseDown_(self, event):
        if self.event_hits_top_rect(event, self.pin_button_rect):
            delegate = NSApp.delegate()
            if delegate is not None:
                delegate.toggleAlwaysOnTop_(self)
            return
        for account_name, rect in list(self.token_account_rects.items()):
            if self.event_hits_top_rect(event, rect):
                delegate = NSApp.delegate()
                if delegate is not None:
                    delegate.openTokenLoginForAccount_(account_name)
                return
        objc.super(WidgetView, self).mouseDown_(event)

    def mouseDragged_(self, event):
        window = self.window()
        if window is None:
            return
        frame = window.frame()
        delta = event.deltaX(), event.deltaY()
        frame.origin.x += delta[0]
        frame.origin.y -= delta[1]
        window.setFrame_display_(frame, True)

    def scrollWheel_(self, event):
        accounts = widget_sorted_accounts((self.payload or {}).get("accounts") or [], self.guardian)
        max_offset = max_account_scroll_offset(len(accounts))
        if max_offset <= 0:
            objc.super(WidgetView, self).scrollWheel_(event)
            return
        delta_y = float(event.scrollingDeltaY())
        if delta_y == 0:
            objc.super(WidgetView, self).scrollWheel_(event)
            return
        step = -1 if delta_y > 0 else 1
        next_offset = clamp_account_scroll_offset(self.account_scroll_offset + step, len(accounts))
        if next_offset != self.account_scroll_offset:
            self.account_scroll_offset = next_offset
            self.setNeedsDisplay_(True)

    def menuForEvent_(self, event):
        menu = NSMenu.alloc().initWithTitle_("BJTU HPC Widget")
        dashboard = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Open Dashboard", "openDashboard:", "")
        dashboard.setTarget_(NSApp.delegate())
        menu.addItem_(dashboard)
        auto_title = (
            "Auto Token Login: On"
            if bool((self.guardian or {}).get("auto_visible_refresh"))
            else "Auto Token Login: Off"
        )
        auto_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(auto_title, "toggleAutoVisibleRefresh:", "")
        auto_item.setTarget_(NSApp.delegate())
        menu.addItem_(auto_item)
        delegate = NSApp.delegate()
        all_spaces = bool(getattr(delegate, "all_spaces", False)) if delegate is not None else False
        spaces_title = "All Desktops: On" if all_spaces else "All Desktops: Off"
        spaces_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(spaces_title, "toggleAllSpaces:", "")
        spaces_item.setTarget_(delegate)
        menu.addItem_(spaces_item)
        attention = guardian_attention_accounts(self.guardian)
        login_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Open Token Login ({len(attention)})" if attention else "Open Token Login",
            "openTokenLogin:",
            "",
        )
        login_item.setTarget_(NSApp.delegate())
        login_item.setEnabled_(bool(attention))
        menu.addItem_(login_item)
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
            accounts = widget_sorted_accounts(payload.get("accounts") or [], self.guardian)
            self.account_scroll_offset = clamp_account_scroll_offset(
                self.account_scroll_offset,
                len(accounts),
            )
        if payload:
            self.updated_at = payload.get("checked_at_local") or datetime.now().isoformat(timespec="seconds")
        self.setNeedsDisplay_(True)

    def setGuardian_error_(self, guardian, error):
        self.guardian = guardian
        self.guardian_error = error
        if self.payload:
            accounts = widget_sorted_accounts(self.payload.get("accounts") or [], guardian)
            self.account_scroll_offset = clamp_account_scroll_offset(
                self.account_scroll_offset,
                len(accounts),
            )
        if guardian:
            for name in list(self.token_requesting_accounts):
                visible_status = guardian_account_visible_status(guardian, name)
                if visible_status and visible_status != "running":
                    self.token_requesting_accounts.discard(name)
        self.setNeedsDisplay_(True)

    def mark_token_login_request(self, accounts):
        if isinstance(accounts, str):
            names = [accounts]
        else:
            names = list(accounts or [])
        self.token_requesting_accounts.update(str(name) for name in names if str(name).strip())
        self.setNeedsDisplay_(True)

    def finish_token_login_request(self, accounts):
        if isinstance(accounts, str):
            names = [accounts]
        else:
            names = list(accounts or [])
        for name in names:
            self.token_requesting_accounts.discard(str(name))
        self.setNeedsDisplay_(True)

    def setDashboardURL_(self, url):
        self.dashboard_url = url

    def setAlwaysOnTop_(self, value):
        self.always_on_top = bool(value)
        self.setNeedsDisplay_(True)

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

    def draw_pin_button(self, x, y, size):
        view_height = self.bounds().size.height
        hit_padding = 4
        self.pin_button_rect = (x - hit_padding, y - hit_padding, size + hit_padding * 2, size + hit_padding * 2)
        rect = NSMakeRect(x - 2, top_y(view_height, y - 2, size + 4), size + 4, size + 4)
        bg = color(0.95, 0.98, 1.0, 0.10 if self.always_on_top else 0.04)
        ring = color(0.23, 0.67, 0.86, 0.30) if self.always_on_top else COLORS["stroke"]
        self.rounded_rect(rect, (size + 4) / 2, bg, ring)

        stroke = COLORS["cyan"] if self.always_on_top else COLORS["muted"]

        try:
            symbol_name = "pin.fill" if self.always_on_top else "pin.slash"
            symbol = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol_name, None)
            if symbol is not None:
                base_config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
                    size - 1,
                    NSFontWeightSemibold,
                )
                color_config = NSImageSymbolConfiguration.configurationWithHierarchicalColor_(stroke)
                symbol = symbol.imageWithSymbolConfiguration_(
                    base_config.configurationByApplyingConfiguration_(color_config)
                ) or symbol
                glyph_rect = NSMakeRect(x + 1, top_y(view_height, y + 1, size - 2), size - 2, size - 2)
                symbol.drawInRect_(glyph_rect)
                return
        except Exception:
            pass

        def point(px, py):
            return (x + px, view_height - (y + py))

        def pin_line(start_x, start_y, end_x, end_y, line_width):
            path = NSBezierPath.bezierPath()
            path.setLineWidth_(line_width)
            path.setLineCapStyle_(NSRoundLineCapStyle)
            path.setLineJoinStyle_(NSRoundLineJoinStyle)
            stroke.setStroke()
            path.moveToPoint_(point(size * start_x, size * start_y))
            path.lineToPoint_(point(size * end_x, size * end_y))
            path.stroke()

        pin_line(0.42, 0.24, 0.73, 0.40, 2.0)
        pin_line(0.43, 0.35, 0.34, 0.60, 1.8)
        pin_line(0.66, 0.49, 0.54, 0.73, 1.8)
        pin_line(0.30, 0.62, 0.55, 0.77, 2.0)
        pin_line(0.40, 0.75, 0.24, 0.92, 1.6)

        if not self.always_on_top:
            slash = NSBezierPath.bezierPath()
            slash.setLineWidth_(1.7)
            slash.setLineCapStyle_(NSRoundLineCapStyle)
            COLORS["soft"].setStroke()
            slash.moveToPoint_(point(size * 0.22, size * 0.84))
            slash.lineToPoint_(point(size * 0.78, size * 0.28))
            slash.stroke()

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
        self.draw_text("updated", width - 166, 18, 58, 18, 10.5, "muted", align="right")
        self.draw_text(stamp[-8:], width - 100, 18, 58, 18, 10.5, "muted", align="right")
        self.draw_pin_button(width - 28, 18, 16)

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

    def draw_account(self, account, index, y=None, card_h=None):
        y = 184 + index * 49 if y is None else float(y)
        card_h = 45 if card_h is None else float(card_h)
        view_height = self.bounds().size.height
        width = self.bounds().size.width
        card_x = 10
        card_w = width - 20
        rect = NSMakeRect(card_x, top_y(view_height, y, card_h), card_w, card_h)
        fill = COLORS["panel_alt"] if index % 2 else COLORS["panel"]
        self.rounded_rect(rect, 12, fill, COLORS["stroke"])

        name = str(account.get("name") or "unknown")
        status = account_status(account)
        counts = account_counts(account)
        token_attention_reason = guardian_account_attention_reason(self.guardian, name)
        visible_status = guardian_account_visible_status(self.guardian, name)
        token_risky = bool(token_attention_reason) or account_auth_error(account)
        visible_running = visible_status == "running"
        dot = COLORS["cyan"] if visible_running else COLORS["violet"] if token_risky else status_color(status)
        inner_left = card_x + 10
        inner_right = card_x + card_w - 10
        identity_w = 64
        metric_gap = 6
        metric_x = inner_left + identity_w + metric_gap
        metric_w = inner_right - metric_x
        col_w = metric_w / 4
        dot_y = y + max(0, (card_h - 8) / 2)
        name_y = y + max(0, (card_h - 18) / 2)
        metric_y = y + (4 if card_h < 43 else 6)
        rule_y = y + 8
        rule_h = max(18, card_h - 20)
        detail_y = y + max(26, card_h - 12)

        self.draw_status_dot(inner_left, dot_y, 8, dot)
        if token_risky:
            self.token_account_rects[name] = (card_x, y, card_w, card_h)
        name_tone = "cyan" if visible_running else "violet" if token_risky else status_tone(status)
        self.draw_text(shorten(name, 7), inner_left + 15, name_y, identity_w - 15, 18, 13.0, name_tone, "bold")

        if name in self.token_requesting_accounts and not visible_status:
            detail = "opening login"
            tone = "cyan"
        elif visible_running:
            detail = "login running"
            tone = "cyan"
        elif token_risky:
            detail = token_attention_reason or compact_reason(account)
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
            self.draw_vrule(metric_x + col * col_w, rule_y, rule_h)

        wait_tone = "amber" if counts["pending"] else "soft"
        self.draw_metric("RUN", str(counts["running"]), metric_x, metric_y, col_w, "text")
        self.draw_metric("G / C", account_running_resource_label(account), metric_x + col_w, metric_y, col_w, "text")
        self.draw_metric("JOBS", f"{counts['total']}/{counts['cap']}", metric_x + col_w * 2, metric_y, col_w, "text")
        self.draw_metric("WAIT", str(counts["pending"]), metric_x + col_w * 3, metric_y, col_w, wait_tone)
        self.draw_text(
            shorten(detail, 34),
            metric_x,
            detail_y,
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

    def draw_account_scroll_indicator(self, account_count, visible_count):
        if account_count <= visible_count:
            return
        width = self.bounds().size.width
        view_height = self.bounds().size.height
        layouts = account_row_layout(visible_count, view_height)
        if not layouts:
            return
        first_y, _ = layouts[0]
        last_y, last_h = layouts[-1]
        track_y = first_y + 2
        track_h = max(28, last_y + last_h - first_y - 4)
        track_x = width - 15
        track_rect = NSMakeRect(track_x, top_y(view_height, track_y, track_h), 3, track_h)
        self.rounded_rect(track_rect, 1.5, COLORS["bar_bg"])
        max_offset = max_account_scroll_offset(account_count)
        thumb_h = max(18, track_h * visible_count / max(account_count, 1))
        thumb_y = track_y
        if max_offset:
            thumb_y += (track_h - thumb_h) * self.account_scroll_offset / max_offset
        thumb_rect = NSMakeRect(track_x, top_y(view_height, thumb_y, thumb_h), 3, thumb_h)
        self.rounded_rect(thumb_rect, 1.5, COLORS["soft"])

    def draw_footer(self, account_count, visible_count=None):
        visible_count = account_count if visible_count is None else int(visible_count)
        view_height = self.bounds().size.height
        footer_y = widget_footer_y(view_height)
        start = self.account_scroll_offset + 1 if account_count else 0
        end = min(account_count, self.account_scroll_offset + visible_count)
        count_label = f"{start}-{end}/{account_count}" if account_count > visible_count else str(account_count)
        self.draw_text(count_label, 20, footer_y, 48, 16, 10.5, "text", "bold")
        self.draw_text("accounts", 70, footer_y, 48, 16, 10.5, "muted")
        attention_count = len(guardian_attention_accounts(self.guardian))
        invalid_count = guardian_error_count(self.guardian)
        if self.refreshing:
            self.draw_text("refreshing", 120, footer_y, 76, 16, 10.5, "cyan", "bold")
        elif self.error:
            self.draw_text("network error", 120, footer_y, 96, 16, 10.5, "red", "bold")
        elif guardian_visible_refresh_running(self.guardian):
            self.draw_text("token login", 120, footer_y, 84, 16, 10.5, "cyan", "bold")
        elif attention_count:
            tone = "red" if invalid_count else "violet"
            self.draw_text(f"token {attention_count}", 120, footer_y, 76, 16, 10.5, tone, "bold")
        elif self.guardian and self.guardian.get("auto_visible_refresh"):
            self.draw_text("auto token", 120, footer_y, 78, 16, 10.5, "cyan", "bold")

    def draw_status_legend(self, compact=False):
        width = self.bounds().size.width
        footer_y = widget_footer_y(self.bounds().size.height)
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
        y = footer_y
        for label, fill in items:
            self.draw_status_dot(x, y + 4, 5, fill)
            label_w = 8 if compact else 32 if label == "token" else 18 if label == "err" else 25
            self.draw_text(label, x + 8, y, label_w, 12, 8.2, "muted")
            x += label_w + (9 if compact else 15)

    def drawRect_(self, dirty_rect):
        bounds = self.bounds()
        self.token_account_rects = {}
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
            accounts = widget_sorted_accounts(self.payload.get("accounts") or [], self.guardian)
            self.account_scroll_offset = clamp_account_scroll_offset(
                self.account_scroll_offset,
                len(accounts),
            )
            visible_accounts = accounts[
                self.account_scroll_offset : self.account_scroll_offset + ACCOUNT_VISIBLE_ROWS
            ]
            layouts = account_row_layout(len(visible_accounts), bounds.size.height)
            for index, (account, layout) in enumerate(zip(visible_accounts, layouts)):
                row_y, card_h = layout
                self.draw_account(account, index, row_y, card_h)
            self.draw_account_scroll_indicator(len(accounts), len(visible_accounts))
            self.draw_status_legend(
                compact=bool(self.refreshing or self.error or guardian_attention_accounts(self.guardian))
            )
            if len(accounts) >= ACCOUNT_VISIBLE_ROWS:
                self.draw_footer(len(accounts), len(visible_accounts))
            else:
                self.draw_jobs_strip(self.payload)
            return

        width = bounds.size.width
        self.draw_text("BJTU HPC", 22, 20, 160, 28, 20, "text", "bold")
        self.draw_pin_button(width - 28, 24, 16)
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
        self.active_interval = env_int("HPC_MONITOR_ACTIVE_INTERVAL", 5, minimum=3)
        self.guardian_poll_interval = env_int("HPC_MONITOR_GUARDIAN_INTERVAL", 3, minimum=1)
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
        self.config = load_widget_config(
            env_bool("HPC_WIDGET_ALWAYS_ON_TOP", True),
            env_bool("HPC_WIDGET_ALL_SPACES", False),
        )
        self.always_on_top = bool(self.config.get("always_on_top", True))
        self.all_spaces = bool(self.config.get("all_spaces", False))
        self.auto_visible_refresh = bool(self.config.get("auto_visible_refresh", False))
        self.window = None
        self.view = None
        self.timer = None
        self.guardian_timer = None
        self.refreshing = False
        self.guardian_polling = False
        self.pending_queue_refresh_after_token_login = False
        self.last_state_signature = None
        self.last_guardian_signature = None
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
        self.apply_window_level()
        self.apply_window_collection_behavior()
        self.view = WidgetView.alloc().initWithFrame_(NSMakeRect(0, 0, self.width, self.height))
        self.view.setDashboardURL_(self.dashboard_url)
        self.view.setAlwaysOnTop_(self.always_on_top)
        self.window.setContentView_(self.view)
        self.window.makeKeyAndOrderFront_(None)
        if self.config.get("_auto_visible_config_present"):
            self.sync_guardian_config()
        self.refresh_(None)

    def apply_window_level(self):
        if self.window is None:
            return
        self.window.setLevel_(NSFloatingWindowLevel if self.always_on_top else NSNormalWindowLevel)
        if self.always_on_top:
            self.window.makeKeyAndOrderFront_(None)

    def apply_window_collection_behavior(self):
        if self.window is None:
            return
        behavior = NSWindowCollectionBehaviorFullScreenAuxiliary
        if self.all_spaces:
            behavior |= NSWindowCollectionBehaviorCanJoinAllSpaces
        self.window.setCollectionBehavior_(behavior)

    def toggleAlwaysOnTop_(self, sender):
        self.always_on_top = not self.always_on_top
        self.config["always_on_top"] = self.always_on_top
        save_widget_config(self.config)
        self.apply_window_level()
        if self.view:
            self.view.setAlwaysOnTop_(self.always_on_top)

    def toggleAllSpaces_(self, sender):
        self.all_spaces = not self.all_spaces
        self.config["all_spaces"] = self.all_spaces
        save_widget_config(self.config)
        self.apply_window_collection_behavior()

    def timerFired_(self, timer):
        self.timer = None
        self.refresh_(None)

    def guardianTimerFired_(self, timer):
        self.guardian_timer = None
        self.poll_guardian_status()

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
        guardian, guardian_error = fetch_guardian_status(self.dashboard_url)
        AppHelper.callAfter(self.apply_refresh_result, payload, error, returncode, guardian, guardian_error)

    def update_refresh_cadence(self, payload, returncode, guardian):
        active_token_login = guardian_visible_refresh_running(guardian)
        if payload is None:
            self.stable_refreshes = 0
            self.interval = self.active_interval if active_token_login else self.base_interval
            return
        signature = queue_state_signature(payload)
        guardian_signature = guardian_state_signature(guardian)
        changed = (
            self.last_state_signature is None
            or self.last_state_signature != signature
            or self.last_guardian_signature != guardian_signature
        )
        self.last_state_signature = signature
        self.last_guardian_signature = guardian_signature
        if active_token_login:
            self.stable_refreshes = 0
            self.interval = self.active_interval
        elif returncode != 0 or changed:
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

    def apply_refresh_result(self, payload, error, returncode, guardian, guardian_error):
        self.update_refresh_cadence(payload, returncode, guardian)
        self.refreshing = False
        if self.view:
            visible_payload = payload if payload is not None else self.view.payload
            visible_error = error if (returncode != 0 or payload is None) else None
            self.view.updateState_error_refreshing_(visible_payload, visible_error, False)
            self.view.setGuardian_error_(guardian, guardian_error)
        self.schedule_next_refresh()

    def schedule_guardian_poll(self, delay=None):
        if self.guardian_timer is not None:
            self.guardian_timer.invalidate()
        interval = self.guardian_poll_interval if delay is None else max(0.5, float(delay))
        self.guardian_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            interval, self, "guardianTimerFired:", None, False
        )

    def poll_guardian_status(self):
        if self.guardian_polling:
            return
        self.guardian_polling = True

        def worker() -> None:
            guardian, error = fetch_guardian_status(self.dashboard_url)
            AppHelper.callAfter(self.apply_guardian_poll_result, guardian, error)

        threading.Thread(target=worker, daemon=True).start()

    def apply_guardian_poll_result(self, guardian, error):
        self.guardian_polling = False
        if self.view:
            self.view.setGuardian_error_(guardian if guardian is not None else self.view.guardian, error)
        if guardian is not None:
            self.last_guardian_signature = guardian_state_signature(guardian)
        if guardian_visible_refresh_running(guardian):
            self.schedule_guardian_poll()
            return
        if self.pending_queue_refresh_after_token_login:
            self.pending_queue_refresh_after_token_login = False
            self.refresh_(None)

    def openDashboard_(self, sender):
        subprocess.Popen(["/usr/bin/open", self.dashboard_url])

    def guardian_payload(self) -> dict[str, Any]:
        return {
            "accounts": "all",
            "auto_visible_refresh": self.auto_visible_refresh,
            "notifications_enabled": True,
        }

    def sync_guardian_config(self):
        def worker() -> None:
            post_guardian_json(self.dashboard_url, "/api/token-guardian/start", self.guardian_payload())

        threading.Thread(target=worker, daemon=True).start()

    def toggleAutoVisibleRefresh_(self, sender):
        self.auto_visible_refresh = not self.auto_visible_refresh
        self.config["auto_visible_refresh"] = self.auto_visible_refresh
        self.config["_auto_visible_config_present"] = True
        save_widget_config(self.config)
        self.sync_guardian_config()
        self.refresh_(None)

    def request_token_login(self, accounts: list[str]) -> None:
        accounts = [str(account).strip() for account in accounts if str(account).strip()]
        if not accounts:
            return
        self.pending_queue_refresh_after_token_login = True
        if self.view:
            self.view.mark_token_login_request(accounts)

        def worker() -> None:
            guardian, error = post_guardian_json(
                self.dashboard_url,
                "/api/token-guardian/visible-refresh",
                {"accounts": ",".join(accounts)},
                timeout=5,
            )
            AppHelper.callAfter(self.apply_token_login_result, accounts, guardian, error)

        threading.Thread(target=worker, daemon=True).start()

    def apply_token_login_result(self, accounts, guardian, error):
        if self.view:
            self.view.finish_token_login_request(accounts)
            if guardian is not None:
                self.view.setGuardian_error_(guardian, error)
            elif error:
                self.view.setGuardian_error_(self.view.guardian, error)
        if guardian_visible_refresh_running(guardian):
            self.schedule_guardian_poll(delay=1)
        else:
            self.pending_queue_refresh_after_token_login = False
            self.refresh_(None)

    def openTokenLoginForAccount_(self, account_name):
        name = str(account_name or "").strip()
        if not name:
            return
        self.request_token_login([name])

    def openTokenLogin_(self, sender):
        guardian = self.view.guardian if self.view else None
        accounts = guardian_attention_accounts(guardian)
        if not accounts:
            return
        self.request_token_login(accounts)

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
    draw_fit_text("updated", (width - 166, 19, 58, 18), small, (178, 190, 208), "right")
    draw_fit_text(stamp[-8:], (width - 100, 19, 58, 18), small, (178, 190, 208), "right")
    pin_x, pin_y, pin_size = width - 28, 20, 16
    draw.ellipse((pin_x - 2, pin_y - 2, pin_x + pin_size + 2, pin_y + pin_size + 2), fill=(255, 255, 255, 24), outline=(255, 255, 255, 30))
    pin_color = (58, 171, 218)

    icon_scale = 4
    icon = Image.new("RGBA", (pin_size * icon_scale, pin_size * icon_scale), (0, 0, 0, 0))
    icon_draw = ImageDraw.Draw(icon)

    def pin_point(px: float, py: float) -> tuple[int, int]:
        return int(pin_size * icon_scale * px), int(pin_size * icon_scale * py)

    icon_draw.polygon(
        [
            pin_point(0.42, 0.23),
            pin_point(0.77, 0.39),
            pin_point(0.71, 0.50),
            pin_point(0.36, 0.34),
        ],
        fill=pin_color,
    )
    icon_draw.polygon(
        [
            pin_point(0.42, 0.35),
            pin_point(0.67, 0.49),
            pin_point(0.54, 0.75),
            pin_point(0.30, 0.61),
        ],
        fill=pin_color,
    )
    icon_draw.polygon(
        [
            pin_point(0.28, 0.60),
            pin_point(0.58, 0.77),
            pin_point(0.51, 0.89),
            pin_point(0.21, 0.72),
        ],
        fill=pin_color,
    )
    icon_draw.polygon(
        [pin_point(0.41, 0.78), pin_point(0.18, 0.95), pin_point(0.30, 0.68)],
        fill=pin_color,
    )
    icon = icon.resize((pin_size, pin_size), Image.Resampling.LANCZOS)
    image.alpha_composite(icon, (pin_x, pin_y))

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
    accounts = sorted_accounts(payload.get("accounts") or [])
    visible_accounts = accounts[:ACCOUNT_VISIBLE_ROWS]
    for index, (account, layout) in enumerate(
        zip(visible_accounts, account_row_layout(len(visible_accounts), height))
    ):
        y, card_h = layout
        card_x = 10
        card_w = width - 20
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
        dot_y = y + max(0, (card_h - 8) / 2)
        name_y = y + max(0, (card_h - 18) / 2)
        metric_y = y + (4 if card_h < 43 else 6)
        rule_y = y + 8
        rule_h = max(18, card_h - 20)
        detail_y = y + max(26, card_h - 12)
        draw.ellipse((inner_left, dot_y, inner_left + 8, dot_y + 8), fill=rgb)
        draw_fit_text(name, (inner_left + 15, name_y, identity_w - 15, 18), font, name_rgb)

        for col in range(1, 4):
            rule_x = int(metric_x + col * col_w)
            draw.line((rule_x, rule_y, rule_x, rule_y + rule_h), fill=(255, 255, 255, 24), width=1)
        wait_fill = (242, 164, 56) if c["pending"] else (128, 145, 166)
        draw_metric("RUN", str(c["running"]), metric_x, metric_y, col_w, (240, 245, 250))
        draw_metric("G / C", account_running_resource_label(account), metric_x + col_w, metric_y, col_w, (240, 245, 250))
        draw_metric("JOBS", f"{c['total']}/{c['cap']}", metric_x + col_w * 2, metric_y, col_w, (240, 245, 250))
        draw_metric("WAIT", str(c["pending"]), metric_x + col_w * 3, metric_y, col_w, wait_fill)
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
        draw_fit_text(detail, (metric_x, detail_y, metric_w, 11), small, detail_fill, "center")
    legend = [
        ("full", (48, 186, 120), 25),
        ("room", (58, 171, 218), 25),
        ("open", (242, 164, 56), 25),
        ("token", (196, 110, 245), 32),
        ("err", (234, 72, 82), 18),
    ]
    legend_x = width - 198
    footer_y = widget_footer_y(height)
    for label, fill, label_w in legend:
        draw.ellipse((legend_x, footer_y + 4, legend_x + 5, footer_y + 9), fill=fill)
        draw_fit_text(label, (legend_x + 8, footer_y, label_w, 12), small, (178, 190, 208))
        legend_x += label_w + 15
    if len(accounts) >= ACCOUNT_VISIBLE_ROWS:
        count_label = f"1-{len(visible_accounts)}/{len(accounts)}"
        draw_fit_text(count_label, (20, footer_y, 48, 16), small, (240, 245, 250))
        draw_fit_text("accounts", (70, footer_y, 48, 16), small, (178, 190, 208))
        if len(accounts) > len(visible_accounts):
            layouts = account_row_layout(len(visible_accounts), height)
            if layouts:
                first_y, _ = layouts[0]
                last_y, last_h = layouts[-1]
                track_y = first_y + 2
                track_h = max(28, last_y + last_h - first_y - 4)
                track_x = width - 15
                thumb_h = max(18, track_h * len(visible_accounts) / max(len(accounts), 1))
                draw.rounded_rectangle(
                    (track_x, track_y, track_x + 3, track_y + track_h),
                    radius=2,
                    fill=(255, 255, 255, 28),
                )
                draw.rounded_rectangle(
                    (track_x, track_y, track_x + 3, track_y + thumb_h),
                    radius=2,
                    fill=(128, 145, 166),
                )
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
