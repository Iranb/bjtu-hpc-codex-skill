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


def test_desktop_installer_wires_live_redacted_snapshot_path() -> None:
    installer = (WIDGET / "scripts" / "install-desktop.ps1").read_text(encoding="utf-8")
    background = (WIDGET / "scripts" / "install-background.ps1").read_text(encoding="utf-8")

    assert "install-background.ps1" in installer
    assert "hpc_native_widget_snapshot.py" in background
    assert "hpc_dashboard_service.py" in background
    assert "BJTU HPC Widget Snapshot" in background
    assert "Initial widget snapshot contains no accounts" in background
    assert "--snapshot-path" in background


def test_wpf_design_system_has_accessible_operations_contract() -> None:
    main_window = (
        WIDGET / "src" / "BjtuHpc.Desktop" / "MainWindow.xaml"
    ).read_text(encoding="utf-8")
    design_system = (
        WIDGET / "src" / "BjtuHpc.Desktop" / "Themes" / "DesignSystem.xaml"
    ).read_text(encoding="utf-8")
    app_manifest = (
        WIDGET / "src" / "BjtuHpc.Desktop" / "app.manifest"
    ).read_text(encoding="utf-8")

    assert "AutomationProperties.Name" in main_window
    assert "WidgetCommands.Reload" in main_window
    assert 'Value="{Binding GpuFree, Mode=OneWay}"' in main_window
    assert "PrimaryButtonStyle" in design_system
    assert "SurfaceCardStyle" in design_system
    assert "PerMonitorV2" in app_manifest


def test_wpf_widget_uses_notification_area_without_taskbar_button() -> None:
    desktop = WIDGET / "src" / "BjtuHpc.Desktop"
    app_xaml = (desktop / "App.xaml").read_text(encoding="utf-8")
    project = (desktop / "BjtuHpc.Desktop.csproj").read_text(encoding="utf-8")
    main_window = (desktop / "MainWindow.xaml").read_text(encoding="utf-8")
    main_window_code = (desktop / "MainWindow.xaml.cs").read_text(encoding="utf-8")
    tray_host = (desktop / "TrayIconHost.cs").read_text(encoding="utf-8")

    assert 'ShutdownMode="OnExplicitShutdown"' in app_xaml
    assert "<UseWindowsForms>true</UseWindowsForms>" in project
    assert "Assets\\TrayLogo.png" in project
    assert 'ShowInTaskbar="False"' in main_window
    assert "Hide widget to notification area" in main_window
    assert "HideToTray" in main_window_code
    assert "CloseForExit" in main_window_code
    assert "NotifyIcon" in tray_host
    assert 'Visible = true' in tray_host
    assert all(
        label in tray_host
        for label in ("Show widget", "Reload snapshot", "Open dashboard", "Exit")
    )
