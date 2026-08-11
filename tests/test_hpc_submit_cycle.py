from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "skills" / "bjtu-hpc" / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hpc_core.submit_cycle import (
    CycleContractError,
    CycleJournal,
    CycleLockedError,
    SubmitCycleController,
    atomic_write_json,
    load_and_validate_manifest,
)
from hpc_platform import assert_private_path


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_candidate(
    root: Path,
    *,
    trace: str,
    account: str,
    seeds: list[int],
    kind: str,
    cpus: int = 6,
) -> dict[str, Any]:
    array_line = ""
    if kind == "array":
        array_line = f"#SBATCH --array={','.join(str(seed) for seed in seeds)}%{len(seeds)}\n"
    script_text = (
        "#!/usr/bin/env bash\n"
        "#SBATCH --partition=GPU\n"
        "#SBATCH --nodes=1\n"
        "#SBATCH --ntasks=1\n"
        f"#SBATCH --cpus-per-task={cpus}\n"
        "#SBATCH --gres=gpu:1\n"
        "#SBATCH --gres-flags=disable-binding\n"
        f"#SBATCH --job-name={trace}\n"
        f"{array_line}"
        "set -euo pipefail\n"
        "python train.py\n"
    )
    script = root / f"{trace}.sbatch"
    script.write_text(script_text, encoding="utf-8")
    script_digest = hashlib.sha256(script.read_bytes()).hexdigest()
    intent = {
        "schema_version": 1,
        "submit_intent": {
            "submit_attempt_id": f"attempt-{trace}",
            "backend_idempotency_key": sha("idempotency-" + trace),
            "anonymous_trace_id": trace,
            "launch_identity_hash": sha("launch-identity"),
            "script_or_command_sha256": script_digest,
            "preflight_sha256": sha("preflight-" + trace),
            "pool_id": "pool-private",
            "execution_route": "bjtu_hpc",
            "trace_embedding": {"anonymous_trace_id": trace, "surface": "slurm_job_name"},
        },
    }
    intent_path = root / f"{trace}.intent.json"
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    return {
        "auth_account": account,
        "script": str(script),
        "submit_intent_ref": str(intent_path),
        "expected": {"gpus": 1, "ntasks": 1, "cpus_per_task": cpus},
        "required_remote_paths": [
            {"path": "/data/home/private/projects/p/code/train.py", "root": "/data/home/private/projects/p"}
        ],
    }


def write_manifest(root: Path, specs: list[dict[str, Any]], *, cycle_hex: str = "1" * 12) -> Path:
    items = []
    for index, spec in enumerate(specs):
        trace = f"hpc_{index + 1:012x}"
        kind = spec.get("kind", "independent")
        seeds = spec.get("seeds", [index])
        accounts = spec.get("accounts", ["a"])
        candidates = [
            write_candidate(root, trace=trace, account=account, seeds=seeds, kind=kind)
            for account in accounts
        ]
        items.append(
            {
                "item_id": f"item_{index + 1:012x}",
                "priority": spec.get("priority", 100 - index),
                "kind": kind,
                "experiment_family_id": spec.get("family", f"family_{index + 1:012x}"),
                "seeds": seeds,
                "candidates": candidates,
            }
        )
    manifest = {
        "schema_version": 1,
        "cycle_id": f"cycle_{cycle_hex}",
        "project_slug": "test_project",
        "max_admissions": 8,
        "items": items,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


class FakeAdapter:
    def __init__(
        self,
        manifest: dict[str, Any],
        *,
        unknown_on: int | None = None,
        pending_accounts=None,
        reconcile_matches=None,
        verification_success: bool = True,
    ) -> None:
        self.manifest = manifest
        self.counts: Counter[str] = Counter()
        self.running: dict[str, list[dict[str, Any]]] = {}
        self.unknown_on = unknown_on
        self.pending_accounts = set(pending_accounts or [])
        self.reconcile_matches = list(reconcile_matches or [])
        self.verification_success = verification_success

    @property
    def call_counts(self):
        return dict(self.counts)

    def snapshot(self, accounts: list[str]) -> dict[str, Any]:
        self.counts["snapshot"] += 1
        rows = []
        for account in accounts:
            jobs = list(self.running.get(account, []))
            pending = sum(job["state"] == "PENDING" for job in jobs)
            running = sum(job["state"] == "RUNNING" for job in jobs)
            rows.append(
                {
                    "name": account,
                    "account": "os-" + account,
                    "cluster": "cluster2",
                    "jobs": jobs,
                    "summary": {
                        "running": running,
                        "pending": pending,
                        "total": len(jobs),
                        "run_slots_open": max(0, 2 - running - pending),
                        "cap_open": max(0, 2 - len(jobs)),
                        "pending_reasons": {"Priority": pending} if pending else {},
                        "shared_limit_blocked": False,
                    },
                }
            )
        return {
            "checked_at_local": f"snapshot-{self.counts['snapshot']}",
            "accounts": rows,
            "cluster_resources": {
                "summary": {"gpu_free": 8, "cpu_free": 48},
                "nodes": [{"name": "gpu01", "gpu_free": 8, "cpu_free": 48}],
            },
        }

    def plan(self, snapshot: dict[str, Any], accounts: list[str]) -> dict[str, Any]:
        self.counts["planner"] += 1
        rows = {row["name"]: row for row in snapshot["accounts"]}
        eligible = [
            account for account in accounts
            if account in rows
            and rows[account]["summary"]["run_slots_open"] > 0
            and rows[account]["summary"]["pending"] == 0
        ]
        if not eligible:
            return {"next_action": None, "admission_frontier": []}
        account = sorted(eligible)[0]
        return {
            "next_action": {
                "account": account,
                "requires_refresh_before_submit": False,
                "submit_then_refresh": True,
                "recommendation": {
                    "mode": "immediate",
                    "kind": "single",
                    "requested": {"gpus": 1, "tasks": 1, "cpus_per_task": 6},
                },
            },
            "admission_frontier": [
                {"account": account, "requires_refresh_before_submit": False},
                *[
                    {"account": other, "requires_refresh_before_submit": True}
                    for other in sorted(eligible)[1:]
                ],
            ],
        }

    def submit(self, candidate: dict[str, Any], receipt_path: Path) -> dict[str, Any]:
        self.counts["upload"] += 1
        self.counts["preflight"] += 1
        self.counts["sbatch"] += 1
        if self.unknown_on == self.counts["sbatch"]:
            return {
                "success": False,
                "submitted": True,
                "submit_outcome": "unknown",
                "message": "injected response loss",
            }
        job_id = str(1000 + self.counts["sbatch"])
        receipt = {
            "schema_version": 1,
            "submit_receipt": {
                "native_id": job_id,
                "anonymous_trace_id": candidate["anonymous_trace_id"],
                "script_or_command_sha256": candidate["script_sha256"],
            },
        }
        atomic_write_json(receipt_path, receipt)
        state = "PENDING" if candidate["auth_account"] in self.pending_accounts else "RUNNING"
        self.running.setdefault(candidate["auth_account"], []).append(
            {"job_id": job_id, "state": state, "resources": {"gpu_count": 1, "num_cpus": 6}}
        )
        task_results = []
        if candidate["kind"] == "array":
            task_results = [
                {"success": True, "job_id": f"{job_id}_{seed}", "observed": {"gpu_count": 1}}
                for seed in candidate["seeds"]
            ]
        verification = {
            "success": self.verification_success,
            "observed": {"gpu_count": 1},
            "array_tasks": task_results,
        }
        self.counts["verification"] += 1 + len(task_results)
        return {
            "success": self.verification_success,
            "submitted": True,
            "job_id": job_id,
            "script_or_command_sha256": candidate["script_sha256"],
            "remote_script_sha256": candidate["script_sha256"],
            "submit_intent_sha256": candidate["intent_sha256"],
            "receipt_out": str(receipt_path),
            "verification": verification,
        }

    def reconcile(self, candidate: dict[str, Any]) -> dict[str, Any]:
        self.counts["reconcile"] += 1
        return {"success": True, "matches": self.reconcile_matches}

    def verify_existing(self, candidate: dict[str, Any], native_id: str) -> dict[str, Any]:
        self.counts["verification"] += 1
        return {
            "success": self.verification_success,
            "job_id": native_id,
            "array_tasks": [],
        }


def test_manifest_validation_and_permissions(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, [{"accounts": ["a"]}])
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    assert manifest["items"][0]["candidates"][0]["script_sha256"]
    assert_private_path(journal.cycle_dir, 0o700)
    assert_private_path(journal.state_path, 0o600)
    assert_private_path(journal.events_path, 0o600)

    with journal.lock():
        try:
            with journal.lock():
                pass
        except CycleLockedError:
            pass
        else:
            raise AssertionError("a second controller must not acquire the same cycle lock")


def test_manifest_mutation_and_local_symlink_fail_closed(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, [{"accounts": ["a"]}])
    manifest = load_and_validate_manifest(manifest_path)
    CycleJournal.create(manifest, tmp_path / "cycles")

    payload = json.loads(manifest_path.read_text())
    payload["project_slug"] = "changed_after_cycle_init"
    manifest_path.write_text(json.dumps(payload))
    changed = load_and_validate_manifest(manifest_path)
    try:
        CycleJournal.create(changed, tmp_path / "cycles")
    except CycleContractError as error:
        assert "different manifest SHA" in str(error)
    else:
        raise AssertionError("an existing cycle must reject a changed manifest")

    symlink_root = tmp_path / "symlink"
    symlink_root.mkdir()
    symlink_manifest = write_manifest(
        symlink_root,
        [{"accounts": ["a"]}],
        cycle_hex="7" * 12,
    )
    symlink_payload = json.loads(symlink_manifest.read_text())
    original = Path(symlink_payload["items"][0]["candidates"][0]["script"])
    linked = symlink_root / "linked.sbatch"
    linked.symlink_to(original)
    symlink_payload["items"][0]["candidates"][0]["script"] = str(linked)
    symlink_manifest.write_text(json.dumps(symlink_payload))
    try:
        load_and_validate_manifest(symlink_manifest)
    except CycleContractError as error:
        assert "must not be a symlink" in str(error)
    else:
        raise AssertionError("a symlinked exact script must be rejected")


def test_manifest_rejects_four_family_seeds_and_queued_array(tmp_path: Path) -> None:
    manifest_path = write_manifest(
        tmp_path,
        [
            {"seeds": [0], "family": "family_aaaaaaaaaaaa"},
            {"seeds": [1], "family": "family_aaaaaaaaaaaa"},
            {"seeds": [2], "family": "family_aaaaaaaaaaaa"},
            {"seeds": [3], "family": "family_aaaaaaaaaaaa"},
        ],
    )
    try:
        load_and_validate_manifest(manifest_path)
    except CycleContractError as error:
        assert "seed cap" in str(error)
    else:
        raise AssertionError("four family seeds must be rejected")

    array_root = tmp_path / "array"
    array_root.mkdir()
    array_path = write_manifest(array_root, [{"kind": "array", "seeds": [0, 1]}], cycle_hex="2" * 12)
    payload = json.loads(array_path.read_text())
    script = Path(payload["items"][0]["candidates"][0]["script"])
    script.write_text(script.read_text().replace("--array=0,1%2", "--array=0,1%1"))
    intent_path = Path(payload["items"][0]["candidates"][0]["submit_intent_ref"])
    intent = json.loads(intent_path.read_text())
    intent["submit_intent"]["script_or_command_sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    intent_path.write_text(json.dumps(intent))
    try:
        load_and_validate_manifest(array_path)
    except CycleContractError as error:
        assert "array concurrency" in str(error)
    else:
        raise AssertionError("queued array concurrency must be rejected")


def test_dry_run_then_three_item_submit_uses_n_plus_one_snapshots(tmp_path: Path) -> None:
    manifest_path = write_manifest(
        tmp_path,
        [
            {"accounts": ["a"]},
            {"accounts": ["a"]},
            {"accounts": ["b"]},
        ],
    )
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")

    dry_adapter = FakeAdapter(manifest)
    dry = SubmitCycleController(manifest, journal, dry_adapter).run(submit_enabled=False)
    assert dry["status"] == "dry_run_planned"
    assert dry["call_counts"].get("sbatch", 0) == 0
    assert dry["next_action"]["status"] == "planned"
    assert dry["blockers"] == []

    adapter = FakeAdapter(manifest)
    result = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert result["status"] == "complete"
    assert result["admissions_completed"] == 3
    assert result["call_counts"]["snapshot"] == 4
    assert result["call_counts"]["planner"] == 3
    assert result["call_counts"]["upload"] == 3
    assert result["call_counts"]["preflight"] == 3
    assert result["call_counts"]["sbatch"] == 3
    assert result["call_counts"]["verification"] == 3
    assert len(list(journal.receipts_dir.glob("*.json"))) == 3
    for receipt_path in journal.receipts_dir.glob("*.json"):
        assert_private_path(receipt_path, 0o600)


def test_cycle_cap_leaves_remaining_items_unattempted(tmp_path: Path) -> None:
    manifest_path = write_manifest(
        tmp_path,
        [
            {"accounts": ["a"]},
            {"accounts": ["b"]},
            {"accounts": ["c"]},
        ],
        cycle_hex="8" * 12,
    )
    payload = json.loads(manifest_path.read_text())
    payload["max_admissions"] = 2
    manifest_path.write_text(json.dumps(payload))
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    adapter = FakeAdapter(manifest)
    result = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert result["status"] == "cycle_cap_reached"
    assert result["admissions_completed"] == 2
    assert result["call_counts"]["sbatch"] == 2
    assert result["call_counts"]["snapshot"] == 3
    assert [row["status"] for row in result["items"]].count("not_attempted_cycle_cap") == 1


def test_unknown_outcome_never_resubmits(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, [{"accounts": ["a"]}], cycle_hex="3" * 12)
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    adapter = FakeAdapter(manifest, unknown_on=1)
    first = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert first["status"] == "needs_reconcile"
    assert adapter.counts["sbatch"] == 1
    second = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert second["status"] == "needs_reconcile"
    assert adapter.counts["sbatch"] == 1


def test_exact_script_mutation_blocks_before_submit(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, [{"accounts": ["a"]}], cycle_hex="c" * 12)
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    candidate = manifest["items"][0]["candidates"][0]
    script = Path(candidate["script"])
    script.write_bytes(script.read_bytes() + b"echo changed-after-validation\n")

    adapter = FakeAdapter(manifest)
    result = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert result["status"] == "partial_blocked"
    assert result["items"][0]["status"] == "blocked_exact_script"
    assert adapter.counts["upload"] == 0
    assert adapter.counts["preflight"] == 0
    assert adapter.counts["sbatch"] == 0


def test_crashed_submitting_state_requires_reconcile(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, [{"accounts": ["a"]}], cycle_hex="6" * 12)
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    state = journal.load()
    state["items"][0]["status"] = "submitting"
    journal.save(state)
    adapter = FakeAdapter(manifest)
    result = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert result["status"] == "needs_reconcile"
    assert adapter.counts["sbatch"] == 0
    second = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert second["status"] == "needs_reconcile"
    assert adapter.counts["sbatch"] == 0


def test_unique_reconcile_repairs_receipt_without_duplicate_submit(tmp_path: Path) -> None:
    manifest_path = write_manifest(tmp_path, [{"accounts": ["a"]}], cycle_hex="9" * 12)
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    unknown = FakeAdapter(manifest, unknown_on=1)
    first = SubmitCycleController(manifest, journal, unknown).run(submit_enabled=True)
    assert first["status"] == "needs_reconcile"
    assert unknown.counts["sbatch"] == 1

    recovery = FakeAdapter(manifest, reconcile_matches=[{"job_id": "4242"}])
    reconciled = SubmitCycleController(manifest, journal, recovery).reconcile()
    assert reconciled["status"] == "needs_verification"
    assert recovery.counts["sbatch"] == 0
    assert journal.receipt_path("item_000000000001").is_file()
    resumed = SubmitCycleController(manifest, journal, recovery).run(submit_enabled=True)
    assert resumed["status"] == "complete"
    assert resumed["items"][0]["native_id"] == "4242"
    assert recovery.counts["sbatch"] == 0


def test_ambiguous_reconcile_and_verification_failure_stay_blocked(tmp_path: Path) -> None:
    ambiguous_root = tmp_path / "ambiguous"
    ambiguous_root.mkdir()
    manifest_path = write_manifest(ambiguous_root, [{"accounts": ["a"]}], cycle_hex="a" * 12)
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, ambiguous_root / "cycles")
    unknown = FakeAdapter(manifest, unknown_on=1)
    SubmitCycleController(manifest, journal, unknown).run(submit_enabled=True)
    ambiguous = FakeAdapter(manifest, reconcile_matches=[{"job_id": "1"}, {"job_id": "2"}])
    result = SubmitCycleController(manifest, journal, ambiguous).reconcile()
    assert result["status"] == "needs_reconcile"
    assert "2 Slurm matches" in result["items"][0]["blocker"]
    assert not journal.receipt_path("item_000000000001").exists()

    mismatch_root = tmp_path / "mismatch"
    mismatch_root.mkdir()
    mismatch_path = write_manifest(mismatch_root, [{"accounts": ["a"]}], cycle_hex="b" * 12)
    mismatch_manifest = load_and_validate_manifest(mismatch_path)
    mismatch_journal = CycleJournal.create(mismatch_manifest, mismatch_root / "cycles")
    mismatch = FakeAdapter(mismatch_manifest, verification_success=False)
    failed = SubmitCycleController(mismatch_manifest, mismatch_journal, mismatch).run(submit_enabled=True)
    assert failed["status"] == "partial_blocked"
    assert failed["items"][0]["status"] == "verification_failed"
    assert mismatch.counts["sbatch"] == 1
    repeated = SubmitCycleController(mismatch_manifest, mismatch_journal, mismatch).run(submit_enabled=True)
    assert repeated["status"] == "partial_blocked"
    assert mismatch.counts["sbatch"] == 1


def test_pending_blocks_same_account_but_allows_independent_pool(tmp_path: Path) -> None:
    manifest_path = write_manifest(
        tmp_path,
        [
            {"accounts": ["a"]},
            {"accounts": ["a"]},
            {"accounts": ["b"]},
        ],
        cycle_hex="4" * 12,
    )
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    adapter = FakeAdapter(manifest, pending_accounts={"a"})
    result = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert result["admissions_completed"] == 2
    statuses = [item["status"] for item in result["items"]]
    assert "accepted_pending" in statuses
    assert "verified" in statuses
    assert statuses.count("pending") + statuses.count("planned") + statuses.count("blocked_preflight") >= 1


def test_two_seed_array_is_one_submit_with_task_level_verification(tmp_path: Path) -> None:
    manifest_path = write_manifest(
        tmp_path,
        [{"kind": "array", "seeds": [0, 1], "accounts": ["a"]}],
        cycle_hex="5" * 12,
    )
    manifest = load_and_validate_manifest(manifest_path)
    journal = CycleJournal.create(manifest, tmp_path / "cycles")
    adapter = FakeAdapter(manifest)
    result = SubmitCycleController(manifest, journal, adapter).run(submit_enabled=True)
    assert result["status"] == "complete"
    assert result["admissions_completed"] == 1
    assert result["call_counts"]["sbatch"] == 1
    assert result["call_counts"]["snapshot"] == 2
    assert result["call_counts"]["verification"] == 3


def main() -> None:
    tests = [
        test_manifest_validation_and_permissions,
        test_manifest_mutation_and_local_symlink_fail_closed,
        test_manifest_rejects_four_family_seeds_and_queued_array,
        test_dry_run_then_three_item_submit_uses_n_plus_one_snapshots,
        test_cycle_cap_leaves_remaining_items_unattempted,
        test_unknown_outcome_never_resubmits,
        test_exact_script_mutation_blocks_before_submit,
        test_crashed_submitting_state_requires_reconcile,
        test_unique_reconcile_repairs_receipt_without_duplicate_submit,
        test_ambiguous_reconcile_and_verification_failure_stay_blocked,
        test_pending_blocks_same_account_but_allows_independent_pool,
        test_two_seed_array_is_one_submit_with_task_level_verification,
    ]
    for test in tests:
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
    print("PASS submit cycle fixtures")


if __name__ == "__main__":
    main()
