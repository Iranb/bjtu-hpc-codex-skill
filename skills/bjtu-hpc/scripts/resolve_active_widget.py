#!/usr/bin/env python3
"""Resolve and verify the currently deployed BJTU HPC WidgetKit source."""

from __future__ import annotations

import argparse
import json
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_LOCK = (
    Path(__file__).resolve().parent.parent
    / "references"
    / "apple_native_widget_component_lock.json"
)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"component lock is not an object: {path}")
    return value


def read_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        value = plistlib.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"plist is not a dictionary: {path}")
    return value


def plugin_entries(bundle_id: str) -> tuple[list[dict[str, Any]], str | None]:
    try:
        proc = subprocess.run(
            ["pluginkit", "-m", "-v", "-i", bundle_id],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [], str(error)

    entries: list[dict[str, Any]] = []
    pattern = re.compile(rf"{re.escape(bundle_id)}\(([^)]*)\)")
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        match = pattern.search(stripped)
        if not match:
            continue
        fields = [field.strip() for field in line.split("\t")]
        entries.append(
            {
                "enabled": stripped.startswith("+"),
                "version": match.group(1),
                "path": fields[-1] if len(fields) >= 2 else None,
            }
        )
    error = proc.stderr.strip() or None
    if proc.returncode != 0 and error is None:
        error = f"pluginkit exited with {proc.returncode}"
    return entries, error


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the live BJTU HPC widget and select its canonical UI source."
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()

    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any) -> None:
        checks.append(
            {
                "name": name,
                "ok": actual == expected,
                "expected": expected,
                "actual": actual,
            }
        )

    try:
        lock = read_json(args.lock.expanduser())
        app_path = Path(lock["deployed_app_path"]).expanduser()
        extension_path = app_path / lock["deployed_extension_relative_path"]
        source_root = Path(lock["canonical_source_root"]).expanduser()

        entries, plugin_error = plugin_entries(lock["widget_extension_bundle_id"])
        check("pluginkit_error", plugin_error, None)
        check("registered_entry_count", len(entries), 1)
        if len(entries) == 1:
            entry = entries[0]
            check("registered_entry_enabled", entry["enabled"], True)
            check(
                "registered_extension_path",
                str(Path(entry["path"]).resolve()) if entry["path"] else None,
                str(extension_path.resolve()),
            )
            check("registered_extension_version", entry["version"], lock["short_version"])

        app_info = read_plist(app_path / "Contents" / "Info.plist")
        extension_info = read_plist(extension_path / "Contents" / "Info.plist")
        source_app_info = read_plist(source_root / lock["source_host_info_plist"])
        source_extension_info = read_plist(
            source_root / lock["source_extension_info_plist"]
        )

        for prefix, info, bundle_id in (
            ("installed_host", app_info, lock["host_bundle_id"]),
            ("installed_extension", extension_info, lock["widget_extension_bundle_id"]),
            ("source_host", source_app_info, lock["host_bundle_id"]),
            ("source_extension", source_extension_info, lock["widget_extension_bundle_id"]),
        ):
            check(f"{prefix}_bundle_id", info.get("CFBundleIdentifier"), bundle_id)
            check(
                f"{prefix}_short_version",
                str(info.get("CFBundleShortVersionString")),
                lock["short_version"],
            )
            check(
                f"{prefix}_build_version",
                str(info.get("CFBundleVersion")),
                lock["build_version"],
            )

        for relative in lock.get("ui_source_files") or []:
            source_file = source_root / relative
            check(f"source_file:{relative}", source_file.is_file(), True)

        for relative, markers in (lock.get("required_source_markers") or {}).items():
            source_file = source_root / relative
            text = source_file.read_text(encoding="utf-8")
            for marker in markers:
                check(f"source_marker:{relative}:{marker}", marker in text, True)

    except (KeyError, OSError, ValueError, plistlib.InvalidFileException) as error:
        checks.append(
            {
                "name": "resolver_exception",
                "ok": False,
                "expected": None,
                "actual": str(error),
            }
        )
        lock = locals().get("lock", {})
        source_root = Path(lock.get("canonical_source_root", "")) if lock else Path()
        entries = locals().get("entries", [])

    ok = bool(checks) and all(row["ok"] for row in checks)
    output = {
        "status": "ok" if ok else "mismatch",
        "selected_source_root": str(source_root) if ok else None,
        "ui_source_files": lock.get("ui_source_files") if ok else None,
        "runtime_entries": entries,
        "locked_version": {
            "short": lock.get("short_version"),
            "build": lock.get("build_version"),
        },
        "legacy_ui_source_roots": lock.get("legacy_ui_source_roots") or [],
        "checks": checks,
        "next_action": (
            "Edit only the selected source files."
            if ok
            else "Do not edit or build widget UI; reconcile live registration and the component lock first."
        ),
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
