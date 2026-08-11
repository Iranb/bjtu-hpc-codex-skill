#!/usr/bin/env python3
"""Produce the redacted snapshot consumed by the Windows widget."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FORBIDDEN_FIELDS = {
    "token", "password", "login_password", "cookie", "cookies", "secret",
    "private_key", "certificate_token", "temporary_certificate",
}


def reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in FORBIDDEN_FIELDS:
                raise ValueError(f"refusing secret-bearing field: {key}")
            reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child)


def demo_payload() -> dict[str, Any]:
    return {
        "checked_at_local": datetime.now().astimezone().isoformat(),
        "accounts": [
            {"name": "acct-a", "has_token": True, "summary": {"running": 1, "pending": 0, "running_gpus": 1, "running_cpus": 6}},
            {"name": "acct-b", "has_token": True, "summary": {"running": 1, "pending": 1, "running_gpus": 1, "running_cpus": 6}},
            {"name": "acct-c", "has_token": False, "summary": {"running": 0, "pending": 0, "running_gpus": 0, "running_cpus": 0}},
        ],
        "cluster_resources": {
            "summary": {"gpu_free": 12, "gpu_total": 32, "cpu_free": 108, "cpu_total": 192},
            "nodes": [
                {"name": "gpu01", "state": "MIXED", "gpu_free": 0, "gpu_total": 8, "cpu_free": 32, "cpu_total": 48},
                {"name": "gpu03", "state": "IDLE", "gpu_free": 8, "gpu_total": 8, "cpu_free": 48, "cpu_total": 48},
                {"name": "gpu04", "state": "MIXED", "gpu_free": 4, "gpu_total": 8, "cpu_free": 23, "cpu_total": 48},
            ],
        },
    }


def run_helper(slurm_dir: Path, python: str, timeout: int) -> dict[str, Any]:
    helper = slurm_dir / "hpc_queue_summary.py"
    if not helper.is_file():
        raise FileNotFoundError(f"helper not found: {helper}")
    completed = subprocess.run(
        [python, str(helper), "--json"], cwd=slurm_dir, check=False,
        capture_output=True, text=True, timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError("hpc_queue_summary.py failed; inspect its private local log")
    return json.loads(completed.stdout)


def atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_args() -> argparse.Namespace:
    default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "BJTUHPCWidget" / "snapshot.json"
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="write privacy-safe synthetic data")
    source.add_argument("--input", type=Path, help="read an existing redacted queue-summary JSON file")
    source.add_argument("--slurm-dir", type=Path, help="run hpc_queue_summary.py --json in this directory")
    parser.add_argument("--python", default=os.environ.get("HPC_PYTHON", sys.executable))
    parser.add_argument("--output", type=Path, default=default)
    parser.add_argument("--timeout", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.demo:
        payload = demo_payload()
    elif args.input:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    else:
        payload = run_helper(args.slurm_dir.resolve(strict=True), args.python, args.timeout)
    reject_secrets(payload)
    snapshot = {
        "version": 1,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
        "returncode": 0,
    }
    atomic_write(args.output, snapshot)
    print(json.dumps({"written": str(args.output), "accounts": len(payload.get("accounts", []))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
