#!/usr/bin/env python3
"""Verify and select the canonical BJTU HPC Windows widget source."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


SKILL_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = SKILL_ROOT / "references" / "windows_widget_component_lock.json"


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"component lock is not an object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the canonical BJTU HPC Windows widget generation."
    )
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    args = parser.parse_args()
    checks: list[dict[str, Any]] = []

    def check(name: str, actual: Any, expected: Any) -> None:
        checks.append(
            {"name": name, "ok": actual == expected, "expected": expected, "actual": actual}
        )

    try:
        lock = read_json(args.lock.expanduser())
        source_root = SKILL_ROOT / lock["canonical_source_root"]
        authority = read_json(SKILL_ROOT / lock["contract_authority"])
        check("source_root_exists", source_root.is_dir(), True)
        check("authority_short_version", lock["short_version"], authority["short_version"])
        check("authority_build_version", lock["build_version"], authority["build_version"])

        manifest = source_root / lock["package_manifest"]
        identity = ET.parse(manifest).getroot().find(
            "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}Identity"
        )
        check("package_version", identity.get("Version") if identity is not None else None,
              lock["package_version"])

        for relative in ("desktop_project", "provider_project", "template"):
            check(f"source_file:{lock[relative]}", (source_root / lock[relative]).is_file(), True)

        for relative, markers in lock.get("required_source_markers", {}).items():
            source_file = source_root / relative
            check(f"source_file:{relative}", source_file.is_file(), True)
            content = source_file.read_text(encoding="utf-8") if source_file.is_file() else ""
            for marker in markers:
                check(f"source_marker:{relative}:{marker}", marker in content, True)
    except (KeyError, OSError, ValueError, ET.ParseError) as error:
        lock = locals().get("lock", {})
        source_root = Path()
        checks.append(
            {"name": "resolver_exception", "ok": False, "expected": None, "actual": str(error)}
        )

    ok = bool(checks) and all(row["ok"] for row in checks)
    output = {
        "status": "ok" if ok else "mismatch",
        "selected_source_root": str(source_root.resolve()) if ok else None,
        "locked_version": {
            "short": lock.get("short_version"),
            "build": lock.get("build_version"),
            "package": lock.get("package_version"),
        },
        "checks": checks,
        "next_action": (
            "Build and install only from selected_source_root."
            if ok
            else "Do not install the Windows widget; reconcile the component lock and source first."
        ),
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
