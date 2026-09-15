#!/usr/bin/env python3
"""Read-only MotionPro status. Never authenticates or reads credentials/OTP."""
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support/MotionProGuard"
VENDOR_ROOT = Path("/usr/local/motionpro")
SWIFT_EPOCH = 978307200
STATES = {0: "idle", 1: "connecting", 2: "connected", 3: "disconnecting",
          4: "reconnecting", 5: "desktop_only", 6: "both_connected", 7: "disconnected"}
GUARD_STATES = {"starting", "connected", "connecting", "disconnected", "paused",
                "locked", "setup", "permission", "offline", "held", "recovering",
                "waiting", "unknown", "error"}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def read_object(path):
    try:
        if path.stat().st_size > 65536:
            return None, "oversized"
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value, "ok") if isinstance(value, dict) else (None, "invalid")
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeError):
        return None, "unreadable_or_invalid"


def parse_status(output):
    matches = re.findall(r"^VPN status:([0-7])[ \t\r]*$", output, re.MULTILINE)
    return int(matches[0]) if len(matches) == 1 else None


def query_vendor(root=VENDOR_ROOT):
    try:
        result = subprocess.run([str(root / "vpn_cmdline"), "getstatus"],
                                cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        status = parse_status(result.stdout.decode("utf-8", errors="replace"))
        return {"status_code": status, "status": STATES.get(status, "unknown"),
                "command_returncode": result.returncode,
                "observation": "parsed" if status is not None else "unrecognized_output"}
    except subprocess.TimeoutExpired:
        reason = "timeout"
    except OSError:
        reason = "helper_unavailable"
    return {"status_code": None, "status": "unknown", "observation": reason}


def snapshot_summary(path, now):
    value, read_status = read_object(path)
    out = {"snapshot": read_status, "fresh": False}
    if value is None:
        return out
    timestamp = value.get("updatedAt")
    if number(timestamp):
        age = now - (timestamp + SWIFT_EPOCH)
        out["age_seconds"] = round(age, 1)
        out["fresh"] = -5 <= age <= 180
    state = value.get("state")
    out["state"] = state if isinstance(state, str) and state in GUARD_STATES else "unknown"
    for key in ("recoveryEnabled", "credentialReady", "otpReady", "accessibilityReady"):
        out[key] = value[key] if type(value.get(key)) is bool else None
    for key in ("attemptsThisHour", "recoveryCount"):
        if type(value.get(key)) is int and 0 <= value[key] <= 1000000000:
            out[key] = value[key]
    # Do not include title/detail, arbitrary paths, accounts, or any unknown fields.
    return out


def policy_valid(value):
    return (isinstance(value, dict)
            and type(value.get("enabled")) is bool
            and type(value.get("manualHold")) is bool
            and type(value.get("consecutiveFailures")) is int
            and value["consecutiveFailures"] >= 0
            and type(value.get("successCount")) is int and value["successCount"] >= 0
            and isinstance(value.get("submissions"), list)
            and len(value["submissions"]) <= 10000
            and all(number(t) and t >= 0 for t in value["submissions"]))


def policy_summary(path, now):
    value, read_status = read_object(path)
    if value is None or not policy_valid(value):
        return {"read_status": read_status if value is None else "invalid_schema"}
    recent = [t for t in value["submissions"] if now - SWIFT_EPOCH - t < 3600]
    return {"read_status": "ok", "enabled": value["enabled"],
            "attempts_in_rolling_hour": len(recent),
            "consecutive_failures": value["consecutiveFailures"],
            "manual_hold": value["manualHold"]}


def decision(status):
    if status in (2, 6):
        return "tunnel_connected_check_target", 0
    if status in (1, 3, 4):
        return "wait_for_transition", 3
    if status in (0, 7):
        return "recovery_needed", 2
    return "inspect_client", 4


def main():
    now = time.time()
    vendor = query_vendor()
    action, exit_code = decision(vendor["status_code"])
    result = {"observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
              "vendor": vendor, "decision": action,
              "guard": snapshot_summary(SUPPORT / "status.json", now),
              "retry_policy": policy_summary(SUPPORT / "recovery-policy.json", now),
              "target_resource_verified": False}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
