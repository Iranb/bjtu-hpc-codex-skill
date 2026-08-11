#!/usr/bin/env python3.12
"""Build a BJTU HPC resource plan from one queue snapshot.

This helper is intentionally read-only. It either consumes an existing
``hpc_queue_summary.py --json`` payload or creates one, then runs
``hpc_resource_planner.py --queue-json`` so the planner does not perform a
second live SSH/proxy sweep.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from hpc_runtime import require_controller_python

require_controller_python()


ROOT = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = ROOT / "work" / "bjtu_hpc_queue_summary_current.json"


def positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a positive integer") from error
    if str(parsed) != value.strip() and value.strip() != f"+{parsed}":
        raise argparse.ArgumentTypeError("must be a positive integer")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run hpc_queue_summary.py once, then plan from that saved snapshot."
    )
    parser.add_argument("--accounts", help="Comma-separated saved auth accounts.")
    parser.add_argument("--queue-json", type=Path, help="Existing hpc_queue_summary.py --json payload.")
    parser.add_argument(
        "--snapshot-out",
        type=Path,
        default=DEFAULT_SNAPSHOT,
        help=f"Where to write a fresh snapshot when --queue-json is omitted. Default: {DEFAULT_SNAPSHOT}",
    )
    parser.add_argument("--summary-timeout", type=int, default=45)
    parser.add_argument("--summary-jobs", type=int, default=None)
    parser.add_argument("--summary-serial", action="store_true")
    parser.add_argument(
        "--no-cluster-resources",
        action="store_true",
        help="Pass --no-cluster-resources to the summary command.",
    )
    parser.add_argument("--partition", default="GPU")
    parser.add_argument(
        "--cap",
        type=int,
        default=4,
        help="Target non-terminal job cap per auth account. Default: 4 (2 running + 2 queued).",
    )
    parser.add_argument("--run-slots", type=int, default=2)
    parser.add_argument(
        "--admission-mode",
        choices=["queued", "direct-start"],
        default="queued",
        help="Planner admission policy. AutoResearch callers should pass direct-start explicitly.",
    )
    parser.add_argument(
        "--max-admissions-per-cycle",
        type=positive_int_arg,
        default=8,
        help="Maximum direct-start candidates exposed for a refresh-gated controller cycle.",
    )

    parser.add_argument("--planner-json", action="store_true", help="Emit planner JSON.")
    parser.add_argument("--planner-timeout", type=int, default=30)
    parser.add_argument("--submit-mode", choices=["sequential", "batch"], default="sequential")
    parser.add_argument("--workload", choices=["packed", "single", "mixed", "gpu-fill"], default="mixed")
    parser.add_argument("--gpu-first", action="store_true")
    parser.add_argument("--cpu-policy", choices=["balanced", "gpu-dense", "cpu-fill"], default="balanced")
    parser.add_argument("--wide-gpu-policy", choices=["auto", "always", "off"], default="auto")
    parser.add_argument("--max-gpus-per-job", type=int, default=8)
    parser.add_argument("--wide-cpus-per-gpu", default="6,4,2")
    parser.add_argument("--available-children", type=int, default=None)
    parser.add_argument("--child-manifest", type=Path)
    parser.add_argument("--test-only-probe", action="store_true")
    parser.add_argument("--probe-script", type=Path)
    parser.add_argument("--write-selected-script", type=Path)
    parser.add_argument("--test-only-candidates", type=int, default=12)
    parser.add_argument("--immediate-window-seconds", type=int, default=180)
    parser.add_argument("--history-log", type=Path)
    parser.add_argument("--history-days", type=int, default=14)
    parser.add_argument("--no-queued", action="store_true")
    parser.add_argument("--allow-queued-submit", action="store_true")
    parser.add_argument("--prefer-cpu", action="store_true")
    parser.add_argument(
        "--print-commands",
        action="store_true",
        help="Print the summary and planner commands to stderr.",
    )
    return parser.parse_args()


def run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def checked_json_payload(text: str, source: str) -> dict:
    try:
        payload = json.loads(text)
    except Exception as error:
        raise RuntimeError(f"{source} did not produce valid JSON: {error}") from error
    if not isinstance(payload, dict) or "accounts" not in payload:
        raise RuntimeError(f"{source} JSON is not an hpc_queue_summary payload")
    return payload


def ensure_snapshot(args: argparse.Namespace) -> Path:
    if args.queue_json:
        snapshot = args.queue_json.expanduser()
        checked_json_payload(snapshot.read_text(encoding="utf-8"), str(snapshot))
        return snapshot

    snapshot = args.snapshot_out.expanduser()
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "hpc_queue_summary.py"),
        "--json",
        "--partition",
        args.partition,
        "--timeout",
        str(args.summary_timeout),
        "--cap",
        str(args.cap),
        "--run-slots",
        str(args.run_slots),
    ]
    if args.accounts:
        command.extend(["--accounts", args.accounts])
    if args.summary_jobs is not None:
        command.extend(["--jobs", str(args.summary_jobs)])
    if args.summary_serial:
        command.append("--serial")
    if args.no_cluster_resources:
        command.append("--no-cluster-resources")

    if args.print_commands:
        print("summary_command: " + " ".join(command), file=sys.stderr)
    completed = run_command(command, cwd=ROOT)
    try:
        payload = checked_json_payload(completed.stdout, "hpc_queue_summary.py")
    except Exception:
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        raise
    payload["_snapshot_written_at_local"] = datetime.now().isoformat(timespec="seconds")
    snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if completed.returncode != 0:
        print(
            f"warning: hpc_queue_summary.py exited rc={completed.returncode}; "
            "using valid JSON payload with per-account errors if present",
            file=sys.stderr,
        )
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
    return snapshot


def planner_command(args: argparse.Namespace, snapshot: Path) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "hpc_resource_planner.py"),
        "--queue-json",
        str(snapshot),
        "--partition",
        args.partition,
        "--timeout",
        str(args.planner_timeout),
        "--cap",
        str(args.cap),
        "--run-slots",
        str(args.run_slots),
        "--admission-mode",
        args.admission_mode,
        "--max-admissions-per-cycle",
        str(args.max_admissions_per_cycle),
        "--submit-mode",
        args.submit_mode,
        "--workload",
        args.workload,
        "--cpu-policy",
        args.cpu_policy,
        "--wide-gpu-policy",
        args.wide_gpu_policy,
        "--max-gpus-per-job",
        str(args.max_gpus_per_job),
        "--wide-cpus-per-gpu",
        args.wide_cpus_per_gpu,
        "--test-only-candidates",
        str(args.test_only_candidates),
        "--immediate-window-seconds",
        str(args.immediate_window_seconds),
        "--history-days",
        str(args.history_days),
    ]
    if args.accounts:
        command.extend(["--accounts", args.accounts])
    if args.gpu_first:
        command.append("--gpu-first")
    if args.available_children is not None:
        command.extend(["--available-children", str(args.available_children)])
    if args.child_manifest:
        command.extend(["--child-manifest", str(args.child_manifest.expanduser())])
    if args.test_only_probe:
        command.append("--test-only-probe")
    if args.probe_script:
        command.extend(["--probe-script", str(args.probe_script.expanduser())])
    if args.write_selected_script:
        command.extend(["--write-selected-script", str(args.write_selected_script.expanduser())])
    if args.history_log:
        command.extend(["--history-log", str(args.history_log.expanduser())])
    if args.no_queued:
        command.append("--no-queued")
    if args.allow_queued_submit:
        command.append("--allow-queued-submit")
    if args.prefer_cpu:
        command.append("--prefer-cpu")
    if args.planner_json:
        command.append("--json")
    return command


def main() -> int:
    args = parse_args()
    try:
        snapshot = ensure_snapshot(args)
        command = planner_command(args, snapshot)
        if args.print_commands:
            print("planner_command: " + " ".join(command), file=sys.stderr)
        completed = run_command(command, cwd=ROOT)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
