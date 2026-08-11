#!/usr/bin/env python3
"""Offline regression checks for BJTU direct-start admission planning."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1] / "skills" / "bjtu-hpc" / "scripts"
PLANNER = ROOT / "hpc_resource_planner.py"
SNAPSHOT_WRAPPER = ROOT / "hpc_plan_from_snapshot.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def load_queue_summary_module() -> Any:
    spec = importlib.util.spec_from_file_location("direct_start_queue_summary", ROOT / "hpc_queue_summary.py")
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load hpc_queue_summary.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def account(
    name: str,
    *,
    running: int = 0,
    pending: int = 0,
    shared_limit_ref: str | None = None,
    shared_limit_blocked: bool = False,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "running": running,
        "pending": pending,
        "total": running + pending,
        "pending_reasons": {"Priority": pending} if pending else {},
    }
    if shared_limit_ref:
        summary["shared_limit_ref"] = shared_limit_ref
        summary["shared_limit_blocked"] = shared_limit_blocked
    jobs = [
        {
            "job_id": f"{name}-running-{index}",
            "state": "RUNNING",
            "resources": {"gpu_count": 1, "num_cpus": 6, "num_tasks": 1, "cpus_per_task": 6},
        }
        for index in range(running)
    ]
    jobs.extend(
        {
            "job_id": f"{name}-pending-{index}",
            "state": "PENDING",
            "reason": "Priority",
            "resources": {"gpu_count": 1, "num_cpus": 6, "num_tasks": 1, "cpus_per_task": 6},
        }
        for index in range(pending)
    )
    return {
        "name": name,
        "account": f"os-{name}",
        "cluster": "cluster2",
        "summary": summary,
        "jobs": jobs,
    }


def snapshot(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "checked_at_local": "2026-07-10T17:00:00+08:00",
        "cluster_resources": {
            "summary": {
                "gpu_total": 8,
                "gpu_alloc": 0,
                "gpu_free": 8,
                "cpu_total": 48,
                "cpu_alloc": 0,
                "cpu_free": 48,
            },
            "nodes": [
                {
                    "name": "gpu01",
                    "state": "IDLE",
                    "gpu_total": 8,
                    "gpu_alloc": 0,
                    "gpu_free": 8,
                    "cpu_total": 48,
                    "cpu_alloc": 0,
                    "cpu_free": 48,
                }
            ],
        },
        "accounts": accounts,
    }


def run_json(command: list[str], expected_code: int = 0) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != expected_code:
        raise AssertionError(
            f"unexpected exit {completed.returncode}, expected {expected_code}: {' '.join(command)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return json.loads(completed.stdout)


def planner_command(queue_path: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        str(PLANNER),
        "--queue-json",
        str(queue_path),
        "--admission-mode",
        "direct-start",
        "--max-admissions-per-cycle",
        "8",
        "--cap",
        "2",
        "--run-slots",
        "2",
        "--workload",
        "single",
        "--no-queued",
        "--json",
        *extra,
    ]


def test_pending_isolation_and_refresh_frontier(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending.json"
    queue_path.write_text(
        json.dumps(snapshot([account("a", pending=1), account("b"), account("c")]), indent=2) + "\n",
        encoding="utf-8",
    )
    first = run_json(planner_command(queue_path))
    second = run_json(planner_command(queue_path))
    require(first["admission_frontier"] == second["admission_frontier"], "same snapshot must plan deterministically")
    require(
        [item["account"] for item in first["admission_frontier"]] == ["b", "c"],
        f"pending account a must not block independent accounts: {first}",
    )
    require(first["totals"]["submissions_to_do_now"] == 1, f"only one stale-free submit is authorized: {first}")
    require(first["admission_frontier"][0]["requires_refresh_before_submit"] is False, f"first action uses current snapshot: {first}")
    require(first["admission_frontier"][1]["requires_refresh_before_submit"] is True, f"later actions require refresh: {first}")
    require(
        all(item["recommendation"]["mode"] == "immediate" for item in first["admission_frontier"]),
        f"direct-start must never expose queue probes: {first}",
    )
    status = {item["name"]: item["status"] for item in first["accounts"]}
    require(status["a"] == "blocked_pending", f"pending should be account-local: {first}")


def test_shared_limit_running_cap_and_cycle_cap(tmp_path: Path) -> None:
    queue_path = tmp_path / "shared.json"
    queue_path.write_text(
        json.dumps(
            snapshot(
                [
                    account("a", shared_limit_ref="qos-shared", shared_limit_blocked=True),
                    account("b", shared_limit_ref="qos-shared"),
                    account("c"),
                    account("d", running=2),
                ]
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report = run_json(planner_command(queue_path, "--max-admissions-per-cycle", "1"))
    require([item["account"] for item in report["admission_frontier"]] == ["c"], f"cycle cap should retain only c: {report}")
    status = {item["name"]: item["status"] for item in report["accounts"]}
    require(status["a"] == "blocked_shared_limit" and status["b"] == "blocked_shared_limit", f"shared evidence must propagate: {report}")
    require(status["d"] == "full", f"per-account running cap must block d: {report}")


def test_wrapper_and_legacy_mode(tmp_path: Path) -> None:
    queue_path = tmp_path / "wrapper.json"
    queue_path.write_text(json.dumps(snapshot([account("a"), account("b")]), indent=2) + "\n", encoding="utf-8")
    wrapped = run_json(
        [
            sys.executable,
            str(SNAPSHOT_WRAPPER),
            "--queue-json",
            str(queue_path),
            "--admission-mode",
            "direct-start",
            "--max-admissions-per-cycle",
            "2",
            "--cap",
            "2",
            "--run-slots",
            "2",
            "--workload",
            "single",
            "--no-queued",
            "--planner-json",
        ]
    )
    require(wrapped["planner_options"]["admission_mode"] == "direct-start", f"wrapper must pass mode: {wrapped}")
    require(len(wrapped["admission_frontier"]) == 2, f"wrapper must pass cycle cap: {wrapped}")

    legacy = run_json(
        [
            sys.executable,
            str(PLANNER),
            "--queue-json",
            str(queue_path),
            "--admission-mode",
            "queued",
            "--cap",
            "4",
            "--run-slots",
            "2",
            "--workload",
            "single",
            "--json",
        ]
    )
    require(legacy["planner_options"]["admission_mode"] == "queued", f"legacy mode must remain available: {legacy}")
    require(legacy["next_action"] is not None, f"legacy sequential planning should still return an action: {legacy}")


def test_queue_summary_shared_limit_annotation() -> None:
    module = load_queue_summary_module()
    rows = [
        {
            "name": "alias-a",
            "cluster": "cluster2",
            "account": "same-os-user",
            "summary": {"pending_reasons": {"QOSMaxJobsPerUserLimit": 1}},
        },
        {
            "name": "alias-b",
            "cluster": "cluster2",
            "account": "same-os-user",
            "summary": {"pending_reasons": {}},
        },
        {
            "name": "independent",
            "cluster": "cluster2",
            "account": "other-os-user",
            "summary": {"pending_reasons": {"Priority": 1}},
        },
    ]
    module.annotate_shared_limits(rows)
    first = rows[0]["summary"]
    second = rows[1]["summary"]
    third = rows[2]["summary"]
    require(first["shared_limit_blocked"] is True, f"explicit per-user QOS reason must block: {rows}")
    require(first["shared_limit_ref"] == second["shared_limit_ref"], f"same OS user aliases must share a ref: {rows}")
    require(third["shared_limit_ref"] != first["shared_limit_ref"], f"different OS users must stay independent: {rows}")
    require(third["shared_limit_blocked"] is False, f"Priority must not infer a shared block: {rows}")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        test_pending_isolation_and_refresh_frontier(tmp_path)
        test_shared_limit_running_cap_and_cycle_cap(tmp_path)
        test_wrapper_and_legacy_mode(tmp_path)
        test_queue_summary_shared_limit_annotation()
    print("PASS direct-start admission fixtures")


if __name__ == "__main__":
    main()
