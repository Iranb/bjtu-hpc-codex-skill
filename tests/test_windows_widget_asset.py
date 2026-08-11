from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "bjtu-hpc"
WIDGET = SKILL / "assets" / "windows-widget"


def test_windows_widget_lock_resolves_current_generation() -> None:
    proc = subprocess.run(
        [sys.executable, str(SKILL / "scripts" / "resolve_windows_widget.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "ok"
    assert result["locked_version"] == {
        "short": "3.5",
        "build": "19",
        "package": "3.5.0.19",
    }
    assert Path(result["selected_source_root"]).resolve() == WIDGET.resolve()


def test_widget_template_has_current_size_and_refresh_contract() -> None:
    template_path = (
        WIDGET
        / "src"
        / "BjtuHpc.WidgetProvider"
        / "Templates"
        / "HpcWidgetTemplate.json"
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))
    raw = json.dumps(template)

    assert "$host.widgetSize" in raw
    assert "refreshAccount" in raw
    assert "refreshAllTokens" in raw
    assert {action["verb"] for action in template["actions"]} >= {
        "refresh",
        "refreshAllTokens",
        "openDashboard",
    }


def test_widget_manifest_matches_locked_package_version() -> None:
    manifest = (
        WIDGET
        / "src"
        / "BjtuHpc.WidgetProvider"
        / "Package.appxmanifest"
    )
    identity = ET.parse(manifest).getroot().find(
        "{http://schemas.microsoft.com/appx/manifest/foundation/windows10}Identity"
    )

    assert identity is not None
    assert identity.get("Version") == "3.5.0.19"
