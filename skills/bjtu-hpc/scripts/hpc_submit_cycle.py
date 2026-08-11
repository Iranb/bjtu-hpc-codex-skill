#!/usr/bin/env python3.12
"""Run one or more BJTU HPC submissions through refresh-gated admissions."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from hpc_runtime import require_native_dependencies

require_native_dependencies()

import hpc_native_submit
import hpc_queue_summary
import hpc_winscp_info as winscp
from hpc_account_store import list_account_summaries
from hpc_core.connection_broker import ConnectionBroker
from hpc_core.native import run_remote, verify_slurm_allocation
from hpc_core.submit_cycle import (
    DEFAULT_CYCLE_ROOT,
    CycleContractError,
    CycleJournal,
    CycleLockedError,
    SubmitCycleController,
    load_and_validate_manifest,
)


ROOT = Path(__file__).resolve().parent


def _queue_args(accounts: list[str], timeout: int, jobs: int) -> argparse.Namespace:
    return argparse.Namespace(
        accounts=",".join(accounts),
        partition="GPU",
        all_partitions=False,
        details=False,
        json=True,
        cap=2,
        run_slots=2,
        timeout=timeout,
        jobs=jobs,
        serial=jobs <= 1,
        no_cluster_resources=False,
        token_file=winscp.DEFAULT_TOKEN_FILE,
        refresh_token=False,
        refresh_browser="playwright",
        refresh_headless=False,
        history_log=None,
        history_state=None,
        history_no_dedupe=False,
    )


def _remote_path_audit_command(entries: list[dict[str, str]]) -> str:
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    code = """
import json, os, stat, sys
entries = json.loads(sys.argv[1])
for entry in entries:
    path = entry['path']
    root = entry['root']
    if not os.path.exists(root) or not os.path.exists(path):
        raise SystemExit('missing declared root/path')
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    if path_real != root_real and not path_real.startswith(root_real.rstrip('/') + '/'):
        raise SystemExit('declared path escapes registered root')
    current = root_real
    if stat.S_ISLNK(os.lstat(root).st_mode):
        raise SystemExit('registered root is a persistent symlink')
    relative = os.path.relpath(path_real, root_real)
    if relative != '.':
        for part in relative.split(os.sep):
            current = os.path.join(current, part)
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise SystemExit('declared path traverses a persistent symlink')
print('path_audit_ok')
""".strip()
    return f"python3 -c {shlex.quote(code)} {shlex.quote(payload)}"


class LiveCycleAdapter:
    def __init__(self, manifest: dict[str, Any], *, timeout: int = 45, jobs: int = 4) -> None:
        self.manifest = manifest
        self.timeout = timeout
        self.jobs = max(1, jobs)
        summaries = list_account_summaries()
        self.rows = {str(row.get("name")): row for row in summaries}
        required = {
            str(candidate["auth_account"])
            for item in manifest["items"]
            for candidate in item["candidates"]
        }
        missing = sorted(required - set(self.rows))
        if missing:
            raise CycleContractError(f"manifest references unknown saved auth accounts: {missing}")
        self._queue_args_all = _queue_args(sorted(required), timeout, self.jobs)

        def loader(key: str) -> dict[str, Any]:
            return hpc_queue_summary.load_connection(self.rows[key], self._queue_args_all)

        def connector(info: dict[str, Any]):
            return hpc_queue_summary.connect_ssh(info, self.timeout)

        self.broker = ConnectionBroker(loader, connector)
        self._counts: Counter[str] = Counter()
        self._timing_samples: dict[str, list[float]] = {}

    @property
    def call_counts(self) -> dict[str, int]:
        counts = Counter(self._counts)
        counts.update(self.broker.redacted_counts())
        return dict(counts)

    @property
    def timings(self) -> dict[str, dict[str, float | int]]:
        result: dict[str, dict[str, float | int]] = {}
        for name, samples in sorted(self._timing_samples.items()):
            total = sum(samples)
            result[name] = {
                "count": len(samples),
                "total_seconds": round(total, 6),
                "average_seconds": round(total / len(samples), 6),
            }
        return result

    def _record_timing(self, name: str, started: float) -> None:
        self._timing_samples.setdefault(name, []).append(time.perf_counter() - started)

    def snapshot(self, accounts: list[str]) -> dict[str, Any]:
        started = time.perf_counter()
        self._counts["snapshot"] += 1
        rows = [self.rows[account] for account in accounts]
        args = _queue_args(accounts, self.timeout, min(self.jobs, max(1, len(accounts))))
        try:
            return hpc_queue_summary.collect_snapshot(args, rows=rows, broker=self.broker)
        finally:
            self._record_timing("snapshot", started)

    def plan(self, snapshot: dict[str, Any], accounts: list[str]) -> dict[str, Any]:
        started = time.perf_counter()
        self._counts["planner"] += 1
        descriptor, temporary = tempfile.mkstemp(prefix="bjtu_cycle_snapshot_", suffix=".json")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            command = [
                sys.executable,
                str(ROOT / "hpc_resource_planner.py"),
                "--queue-json",
                temporary,
                "--accounts",
                ",".join(accounts),
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
            ]
            completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        if completed.returncode != 0:
            raise CycleContractError(
                f"resource planner failed rc={completed.returncode}: {completed.stderr.strip()[:300]}"
            )
        value = json.loads(completed.stdout)
        if not isinstance(value, dict):
            raise CycleContractError("resource planner returned a non-object JSON payload")
        self._record_timing("planner", started)
        return value

    def submit(self, candidate: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
        started = time.perf_counter()
        account = str(candidate["auth_account"])
        info = self.broker.connection_for(account)
        client = self.broker.client_for(account)
        self._counts["path_audit"] += 1
        audit = run_remote(
            client,
            _remote_path_audit_command(candidate["required_remote_paths"]),
            timeout=self.timeout,
        )
        if audit["returncode"] != 0:
            result = {
                "success": False,
                "submitted": False,
                "message": "remote persistent-symlink/root audit failed",
                "path_audit": audit,
            }
            self._record_timing("submit_admission", started)
            return result
        expected = candidate["expected"]
        args = argparse.Namespace(
            script=Path(candidate["script"]),
            remote_script=None,
            remote_path=None,
            submit=True,
            submit_intent=Path(candidate["submit_intent_ref"]),
            receipt_out=receipt_path,
            script_sha256=None,
            frozen_script_sha256=candidate["script_sha256"],
            frozen_intent_sha256=candidate["intent_sha256"],
            json=True,
            timeout=self.timeout,
            expected_total_cpus=int(expected.get("total_cpus") or expected["ntasks"] * expected["cpus_per_task"]),
            expected_ntasks=int(expected["ntasks"]),
            expected_cpus_per_task=int(expected["cpus_per_task"]),
            expected_gpus=int(expected["gpus"]),
            no_verify=False,
            cluster=info["cluster"],
            account=info["account"],
            portal_user=info.get("portal_user") or winscp.DEFAULT_PORTAL_USER,
            auth_account=account,
            token_file=winscp.DEFAULT_TOKEN_FILE,
            refresh_token=False,
            refresh_browser="playwright",
            refresh_headless=False,
        )
        self._counts["upload"] += 1
        self._counts["preflight"] += 1
        try:
            result = hpc_native_submit.run(args, connection_info=info, client=client)
        finally:
            self._record_timing("submit_admission", started)
        if (
            result.get("submitted")
            or result.get("submit") is not None
            or result.get("submit_outcome") in {"unknown", "definitive_failure"}
        ):
            self._counts["sbatch"] += 1
        verification = result.get("verification")
        if verification is not None:
            self._counts["verification"] += 1 + len(verification.get("array_tasks") or [])
        return result

    def reconcile(self, candidate: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        account = str(candidate["auth_account"])
        trace = str(candidate["anonymous_trace_id"])
        client = self.broker.client_for(account)
        self._counts["reconcile"] += 1
        command = (
            f"squeue -h --name {shlex.quote(trace)} -o '%i|%j|%T' 2>/dev/null; "
            f"sacct -n -X --name {shlex.quote(trace)} --format=JobIDRaw,JobName,State,Submit -P 2>/dev/null || true"
        )
        result = run_remote(client, command, timeout=self.timeout)
        matches: dict[str, dict[str, Any]] = {}
        for line in result.get("stdout", "").splitlines():
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 2 or parts[1] != trace:
                continue
            job_id = parts[0].split("_", 1)[0].split(".", 1)[0]
            if not job_id.isdigit():
                continue
            matches.setdefault(
                job_id,
                {
                    "job_id": job_id,
                    "state": parts[2] if len(parts) > 2 else None,
                    "accepted_at": parts[3] if len(parts) > 3 else None,
                },
            )
        self._record_timing("reconcile", started)
        return {"success": result["returncode"] == 0, "matches": list(matches.values())}

    def verify_existing(self, candidate: dict[str, Any], native_id: str) -> dict[str, Any]:
        account = str(candidate["auth_account"])
        info = self.broker.connection_for(account)
        client = self.broker.client_for(account)
        expected = candidate["expected"]
        kwargs = {
            "expected_total_cpus": int(
                expected.get("total_cpus") or expected["ntasks"] * expected["cpus_per_task"]
            ),
            "expected_ntasks": int(expected["ntasks"]),
            "expected_cpus_per_task": int(expected["cpus_per_task"]),
            "expected_gpus": int(expected["gpus"]),
            "connection_info": info,
            "client": client,
        }
        parent = verify_slurm_allocation(native_id, **kwargs)
        task_ids = hpc_native_submit.parse_array_task_ids(Path(candidate["script"]).read_text(encoding="utf-8"))
        if not task_ids:
            self._counts["verification"] += 1
            return parent
        tasks = [verify_slurm_allocation(f"{native_id}_{task_id}", **kwargs) for task_id in task_ids]
        self._counts["verification"] += 1 + len(tasks)
        result = dict(parent)
        result["parent"] = parent
        result["array_tasks"] = tasks
        result["success"] = bool(parent.get("success") and all(task.get("success") for task in tasks))
        return result

    def close(self) -> None:
        self.broker.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate, plan, run, resume, or reconcile a refresh-gated BJTU HPC submit cycle."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="Validate a manifest locally; no portal or Slurm calls.")
    validate.add_argument("--manifest", type=Path, required=True)

    run = subparsers.add_parser("run", help="Plan or execute a new cycle. Default: no submit.")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--cycle-root", type=Path, default=DEFAULT_CYCLE_ROOT)
    run.add_argument("--submit", action="store_true", help="Perform authorized physical sbatch admissions.")
    run.add_argument("--timeout", type=int, default=45)
    run.add_argument("--jobs", type=int, default=4)

    status = subparsers.add_parser("status", help="Read a local cycle journal; no portal or Slurm calls.")
    status.add_argument("--cycle-dir", type=Path, required=True)

    resume = subparsers.add_parser("resume", help="Resume a cycle. Default: no submit.")
    resume.add_argument("--cycle-dir", type=Path, required=True)
    resume.add_argument("--submit", action="store_true")
    resume.add_argument("--timeout", type=int, default=45)
    resume.add_argument("--jobs", type=int, default=4)

    reconcile = subparsers.add_parser("reconcile", help="Trace-search unknown outcomes without resubmitting.")
    reconcile.add_argument("--cycle-dir", type=Path, required=True)
    reconcile.add_argument("--timeout", type=int, default=45)
    reconcile.add_argument("--jobs", type=int, default=4)
    return parser.parse_args()


def _manifest_for_journal(journal: CycleJournal) -> dict[str, Any]:
    state = journal.load()
    manifest_path = Path(str(state.get("manifest_path") or ""))
    manifest = load_and_validate_manifest(manifest_path)
    if manifest["manifest_sha256"] != state.get("manifest_sha256"):
        raise CycleContractError("manifest changed after cycle initialization")
    return manifest


def main() -> int:
    args = parse_args()
    exit_code = 0
    try:
        if args.command == "validate":
            manifest = load_and_validate_manifest(args.manifest)
            result = {
                "success": True,
                "cycle_id": manifest["cycle_id"],
                "manifest_sha256": manifest["manifest_sha256"],
                "items": len(manifest["items"]),
                "physical_admission_cap": manifest["max_admissions"],
            }
        elif args.command == "status":
            journal = CycleJournal.open(args.cycle_dir)
            result = {"success": True, "state": journal.load(), "cycle_dir": str(journal.cycle_dir)}
        else:
            if args.command == "run":
                manifest = load_and_validate_manifest(args.manifest)
                journal = CycleJournal.create(manifest, args.cycle_root)
            else:
                journal = CycleJournal.open(args.cycle_dir)
                manifest = _manifest_for_journal(journal)
            adapter = LiveCycleAdapter(manifest, timeout=args.timeout, jobs=args.jobs)
            try:
                controller = SubmitCycleController(manifest, journal, adapter)
                if args.command == "reconcile":
                    result = controller.reconcile()
                else:
                    result = controller.run(submit_enabled=bool(args.submit))
            finally:
                adapter.close()
            result["success"] = result.get("status") in {
                "complete",
                "dry_run_planned",
                "needs_verification",
            }
    except (CycleContractError, CycleLockedError, ValueError, json.JSONDecodeError) as error:
        exit_code = 2
        result = {"success": False, "error": f"{type(error).__name__}: {error}"}
    except Exception as error:
        exit_code = 1
        result = {"success": False, "error": f"{type(error).__name__}: {error}"}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("success"):
        return 0
    return exit_code or 1


if __name__ == "__main__":
    raise SystemExit(main())
