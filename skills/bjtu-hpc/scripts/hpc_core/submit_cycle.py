"""Durable, refresh-gated BJTU HPC single/batch submission controller.

This module owns local cycle state. Native Slurm remains authoritative for job
state, and a durable submit receipt remains authoritative for backend acceptance.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "hpc_submit_batch.schema.json"
DEFAULT_CYCLE_ROOT = ROOT / "work" / "hpc_submit_cycles" / "private"
TRACE_RE = re.compile(r"^hpc_[0-9a-f]{12,16}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SBATCH_VALUE_RE = re.compile(r"^\s*#SBATCH\s+(--[A-Za-z0-9-]+)(?:=|\s+)(\S+)\s*$", re.MULTILINE)
FORBIDDEN_SCRIPT_PATTERNS = (
    (re.compile(r"\bsrun\s+--exclusive\b"), "srun --exclusive child splitting"),
    (re.compile(r"\bpytorch1\.7-python3\.8\b"), "unsupported pytorch1.7-python3.8 runtime"),
    (re.compile(r"/data/apps/anaconda/anaconda3/envs/pytorch1\.7-python3\.8/bin/python"), "unsupported Python runtime"),
    (re.compile(r"CUDA_VISIBLE_DEVICES\s*=\s*[0-9]"), "hardcoded physical CUDA device"),
    (re.compile(r"(?:bash\s+child_|run_child\b)[^\n]*&"), "background child splitting"),
)


class CycleContractError(ValueError):
    pass


class CycleLockedError(RuntimeError):
    pass


class CycleAdapter(Protocol):
    call_counts: dict[str, int]

    def snapshot(self, accounts: list[str]) -> dict[str, Any]: ...

    def plan(self, snapshot: dict[str, Any], accounts: list[str]) -> dict[str, Any]: ...

    def submit(self, candidate: dict[str, Any], receipt_path: Path) -> dict[str, Any]: ...

    def reconcile(self, candidate: dict[str, Any]) -> dict[str, Any]: ...

    def verify_existing(self, candidate: dict[str, Any], native_id: str) -> dict[str, Any]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def strict_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CycleContractError(f"JSON root must be an object: {path}")
    return value


def _sbatch_values(script_text: str) -> dict[str, str]:
    return {match.group(1): match.group(2) for match in SBATCH_VALUE_RE.finditer(script_text)}


def _parse_gres_gpus(value: str) -> int | None:
    match = re.search(r"(?:^|,)gpu(?::[^:,=]+)?(?::|=)(\d+)(?:,|$)", value)
    return int(match.group(1)) if match else None


def _parse_array(value: str) -> tuple[list[int], int | None]:
    task_text, separator, concurrency_text = value.partition("%")
    if any(character in task_text for character in "-:"):
        raise CycleContractError("array task ids must be an explicit comma-separated seed list")
    try:
        tasks = [int(part) for part in task_text.split(",") if part != ""]
        concurrency = int(concurrency_text) if separator else None
    except ValueError as error:
        raise CycleContractError(f"invalid #SBATCH --array value: {value}") from error
    return tasks, concurrency


def _intent_object(path: Path) -> dict[str, Any]:
    payload = strict_json(path)
    value = payload.get("submit_intent") if isinstance(payload.get("submit_intent"), dict) else payload
    return dict(value)


def _validate_remote_paths(entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        path = PurePosixPath(str(entry["path"]))
        root = PurePosixPath(str(entry["root"]))
        if ".." in path.parts or ".." in root.parts:
            raise CycleContractError("remote path audit entries must not contain '..'")
        try:
            path.relative_to(root)
        except ValueError as error:
            raise CycleContractError(f"remote path escapes declared root: {path} not under {root}") from error


def _validate_script(
    script_path: Path,
    *,
    trace_id: str,
    kind: str,
    seeds: list[int],
    expected: dict[str, Any],
) -> str:
    if script_path.is_symlink():
        raise CycleContractError(f"local sbatch script must not be a symlink: {script_path}")
    if not script_path.is_file():
        raise CycleContractError(f"sbatch script does not exist: {script_path}")
    text = script_path.read_text(encoding="utf-8")
    for pattern, reason in FORBIDDEN_SCRIPT_PATTERNS:
        if pattern.search(text):
            raise CycleContractError(f"script rejected: {reason}: {script_path}")
    values = _sbatch_values(text)
    if values.get("--job-name") != trace_id:
        raise CycleContractError(f"script --job-name must equal anonymous trace id {trace_id}")
    try:
        ntasks = int(values.get("--ntasks", ""))
        cpus_per_task = int(values.get("--cpus-per-task", ""))
    except ValueError as error:
        raise CycleContractError("script must declare integer --ntasks and --cpus-per-task") from error
    gpus = _parse_gres_gpus(values.get("--gres", ""))
    if ntasks != int(expected["ntasks"]):
        raise CycleContractError(f"script ntasks={ntasks} does not match manifest {expected['ntasks']}")
    if cpus_per_task != int(expected["cpus_per_task"]):
        raise CycleContractError(
            f"script cpus-per-task={cpus_per_task} does not match manifest {expected['cpus_per_task']}"
        )
    if gpus != int(expected["gpus"]):
        raise CycleContractError(f"script gpus={gpus} does not match manifest {expected['gpus']}")
    if values.get("--gres-flags") != "disable-binding":
        raise CycleContractError("script must declare #SBATCH --gres-flags=disable-binding")
    array_value = values.get("--array")
    if kind == "array":
        if not array_value:
            raise CycleContractError("array item script must declare #SBATCH --array")
        tasks, concurrency = _parse_array(array_value)
        if tasks != seeds:
            raise CycleContractError(f"array task ids {tasks} do not exactly match seeds {seeds}")
        if concurrency != len(seeds):
            raise CycleContractError("array concurrency must equal task count; queued array backlog is forbidden")
    elif array_value:
        raise CycleContractError("independent item script must not declare #SBATCH --array")
    return text


def load_and_validate_manifest(path: Path, *, schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    path = path.expanduser().resolve()
    manifest = strict_json(path)
    schema = strict_json(schema_path)
    try:
        jsonschema.Draft202012Validator(schema).validate(manifest)
    except jsonschema.ValidationError as error:
        location = "/".join(str(part) for part in error.absolute_path)
        raise CycleContractError(f"manifest schema error at {location or '<root>'}: {error.message}") from error

    normalized = deepcopy(manifest)
    normalized["max_admissions"] = int(normalized.get("max_admissions", 8))
    normalized["manifest_path"] = str(path)
    item_ids: set[str] = set()
    family_seeds: dict[str, set[int]] = {}
    for item in normalized["items"]:
        item_id = str(item["item_id"])
        if item_id in item_ids:
            raise CycleContractError(f"duplicate item_id: {item_id}")
        item_ids.add(item_id)
        seeds = [int(seed) for seed in item["seeds"]]
        if item["kind"] == "independent" and len(seeds) != 1:
            raise CycleContractError(f"independent item must contain exactly one seed: {item_id}")
        if item["kind"] == "array" and len(seeds) < 2:
            raise CycleContractError(f"array item must contain at least two seeds: {item_id}")
        family_seeds.setdefault(str(item["experiment_family_id"]), set()).update(seeds)
        candidate_accounts: set[str] = set()
        for candidate in item["candidates"]:
            account = str(candidate["auth_account"])
            if account in candidate_accounts:
                raise CycleContractError(f"duplicate account candidate for {item_id}: {account}")
            candidate_accounts.add(account)
            script_input = Path(candidate["script"])
            if not script_input.is_absolute():
                script_input = path.parent / script_input
            if script_input.is_symlink():
                raise CycleContractError(f"local sbatch script must not be a symlink: {script_input}")
            script_path = script_input.resolve(strict=False)
            intent_input = Path(candidate["submit_intent_ref"])
            if not intent_input.is_absolute():
                intent_input = path.parent / intent_input
            if intent_input.is_symlink():
                raise CycleContractError(f"durable submit intent must not be a symlink: {intent_input}")
            intent_path = intent_input.resolve(strict=False)
            if not intent_path.is_file():
                raise CycleContractError(f"durable submit intent must be a real file: {intent_path}")
            intent = _intent_object(intent_path)
            required = {
                "submit_attempt_id",
                "backend_idempotency_key",
                "anonymous_trace_id",
                "launch_identity_hash",
                "script_or_command_sha256",
                "preflight_sha256",
                "pool_id",
                "execution_route",
            }
            missing = sorted(field for field in required if not str(intent.get(field) or "").strip())
            if missing:
                raise CycleContractError(f"submit intent missing fields for {item_id}/{account}: {missing}")
            trace_id = str(intent["anonymous_trace_id"])
            if not TRACE_RE.fullmatch(trace_id):
                raise CycleContractError(f"invalid anonymous trace id for {item_id}: {trace_id}")
            if str(intent.get("execution_route")).lower() != "bjtu_hpc":
                raise CycleContractError(f"intent execution_route must be bjtu_hpc: {item_id}")
            embedding = intent.get("trace_embedding") if isinstance(intent.get("trace_embedding"), dict) else {}
            if embedding.get("anonymous_trace_id") != trace_id:
                raise CycleContractError(f"intent trace_embedding does not bind trace id: {item_id}")
            if str(embedding.get("surface") or "").lower() not in {
                "slurm_job_name",
                "slurm_comment",
                "slurm_environment",
            }:
                raise CycleContractError(f"intent trace_embedding surface is not Slurm-searchable: {item_id}")
            for digest_field in ("launch_identity_hash", "script_or_command_sha256", "preflight_sha256"):
                if not SHA256_RE.fullmatch(str(intent.get(digest_field) or "")):
                    raise CycleContractError(f"intent {digest_field} must be 64 lowercase hex: {item_id}")
            expected = candidate["expected"]
            if int(expected["gpus"]) != 1 or int(expected["ntasks"]) != 1:
                raise CycleContractError(
                    "submit-cycle v1 accepts only independent 1GPU/1-task work or one 1GPU task per array element"
                )
            if int(expected["cpus_per_task"]) not in {4, 6}:
                raise CycleContractError("submit-cycle v1 accepts only the compliant 1GPU/6CPU or 1GPU/4CPU shape")
            _validate_remote_paths(candidate["required_remote_paths"])
            _validate_script(
                script_path,
                trace_id=trace_id,
                kind=str(item["kind"]),
                seeds=seeds,
                expected=expected,
            )
            script_digest = file_sha256(script_path)
            if script_digest != str(intent["script_or_command_sha256"]):
                raise CycleContractError(f"script SHA does not match durable intent: {item_id}/{account}")
            candidate["script"] = str(script_path)
            candidate["submit_intent_ref"] = str(intent_path)
            candidate["script_sha256"] = script_digest
            candidate["intent_sha256"] = file_sha256(intent_path)
            candidate["anonymous_trace_id"] = trace_id
            candidate["launch_identity_hash"] = str(intent["launch_identity_hash"])
            candidate["backend_idempotency_key"] = str(intent["backend_idempotency_key"])
            candidate["item_id"] = item_id
            candidate["kind"] = str(item["kind"])
            candidate["seeds"] = seeds
    over_cap = {family: sorted(seeds) for family, seeds in family_seeds.items() if len(seeds) > 3}
    if over_cap:
        raise CycleContractError(f"experiment family seed cap exceeded: {over_cap}")
    normalized["manifest_sha256"] = canonical_sha256(manifest)
    return normalized


class CycleJournal:
    def __init__(self, cycle_dir: Path) -> None:
        self.cycle_dir = cycle_dir.expanduser().resolve()
        self.state_path = self.cycle_dir / "cycle.json"
        self.events_path = self.cycle_dir / "events.jsonl"
        self.result_path = self.cycle_dir / "result.json"
        self.lock_path = self.cycle_dir / "cycle.lock"
        self.snapshots_dir = self.cycle_dir / "snapshots"
        self.plans_dir = self.cycle_dir / "plans"
        self.receipts_dir = self.cycle_dir / "receipts"
        self.verifications_dir = self.cycle_dir / "verifications"

    @classmethod
    def create(cls, manifest: dict[str, Any], root: Path = DEFAULT_CYCLE_ROOT) -> "CycleJournal":
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        journal = cls(root / str(manifest["cycle_id"]))
        journal.cycle_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(journal.cycle_dir, 0o700)
        for directory in (
            journal.snapshots_dir,
            journal.plans_dir,
            journal.receipts_dir,
            journal.verifications_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        if journal.state_path.exists():
            state = journal.load()
            if state.get("manifest_sha256") != manifest["manifest_sha256"]:
                raise CycleContractError("cycle id already exists with a different manifest SHA")
            return journal
        state = {
            "schema_version": 1,
            "cycle_id": manifest["cycle_id"],
            "manifest_path": manifest["manifest_path"],
            "manifest_sha256": manifest["manifest_sha256"],
            "project_slug": manifest["project_slug"],
            "max_admissions": manifest["max_admissions"],
            "status": "initialized",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "admissions_completed": 0,
            "items": [
                {
                    "item_id": item["item_id"],
                    "priority": item["priority"],
                    "kind": item["kind"],
                    "experiment_family_id": item["experiment_family_id"],
                    "seeds": item["seeds"],
                    "status": "pending",
                    "selected_account_ref": None,
                    "anonymous_trace_id": None,
                    "native_id": None,
                    "receipt_path": None,
                    "blocker": None,
                }
                for item in manifest["items"]
            ],
        }
        journal.save(state)
        journal.event("cycle_initialized", {"manifest_sha256": manifest["manifest_sha256"]})
        return journal

    @classmethod
    def open(cls, cycle_dir: Path) -> "CycleJournal":
        journal = cls(cycle_dir)
        if not journal.state_path.is_file():
            raise CycleContractError(f"cycle journal not found: {journal.state_path}")
        return journal

    def load(self) -> dict[str, Any]:
        return strict_json(self.state_path)

    def save(self, state: dict[str, Any]) -> None:
        state["updated_at"] = now_iso()
        atomic_write_json(self.state_path, state)

    def event(self, event: str, payload: dict[str, Any] | None = None) -> None:
        record = {"at": now_iso(), "event": event, "payload": payload or {}}
        self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @contextmanager
    def lock(self):
        descriptor = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise CycleLockedError(f"cycle is already controlled by another process: {self.cycle_dir}") from error
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def snapshot_path(self, index: int, label: str) -> Path:
        return self.snapshots_dir / f"{index:03d}-{label}.json"

    def plan_path(self, index: int) -> Path:
        return self.plans_dir / f"{index:03d}.json"

    def receipt_path(self, item_id: str) -> Path:
        return self.receipts_dir / f"{item_id}.json"


def _account_ref(account: str) -> str:
    return "account:" + hashlib.sha256(str(account).encode("utf-8")).hexdigest()[:16]


def _item_map(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["item_id"]): item for item in manifest["items"]}


def _candidate_for(item: dict[str, Any], account: str) -> dict[str, Any] | None:
    for candidate in item["candidates"]:
        if candidate["auth_account"] == account:
            return candidate
    return None


def _remaining_accounts(state: dict[str, Any], items: dict[str, dict[str, Any]], blocked: set[str]) -> list[str]:
    accounts: set[str] = set()
    for row in state["items"]:
        if row["status"] not in {"pending", "planned", "blocked_preflight"}:
            continue
        item = items[row["item_id"]]
        for candidate in item["candidates"]:
            account = str(candidate["auth_account"])
            if account not in blocked:
                accounts.add(account)
    return sorted(accounts)


def _planner_allows(report: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    action = report.get("next_action")
    if not isinstance(action, dict):
        raise CycleContractError("planner returned no direct-start next_action")
    if action.get("do_not_submit") or action.get("requires_refresh_before_submit"):
        raise CycleContractError("planner next_action is not submit-eligible")
    recommendation = action.get("recommendation") or {}
    if recommendation.get("mode") != "immediate" or recommendation.get("kind") != "single":
        raise CycleContractError("planner next_action is not a compliant immediate singleton shape")
    account = str(action.get("account") or "")
    if not account:
        raise CycleContractError("planner next_action has no account")
    return account, recommendation


def _shape_matches(candidate: dict[str, Any], recommendation: dict[str, Any]) -> bool:
    expected = candidate["expected"]
    requested = recommendation.get("requested") or {}
    return (
        int(expected["gpus"]) == int(requested.get("gpus", -1))
        and int(expected["ntasks"]) == int(requested.get("tasks", -1))
        and int(expected["cpus_per_task"]) == int(requested.get("cpus_per_task", -1))
    )


def _array_fits(item: dict[str, Any], candidate: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    if item["kind"] != "array":
        return True
    task_count = len(item["seeds"])
    account_row = next(
        (row for row in snapshot.get("accounts") or [] if row.get("name") == candidate["auth_account"]),
        None,
    )
    if not account_row:
        return False
    summary = account_row.get("summary") or {}
    if int(summary.get("run_slots_open", 0)) < task_count or int(summary.get("cap_open", 0)) < task_count:
        return False
    cpus = int(candidate["expected"]["cpus_per_task"])
    remaining = task_count
    for node in deepcopy((snapshot.get("cluster_resources") or {}).get("nodes") or []):
        gpu_free = int(node.get("gpu_free", 0))
        cpu_free = int(node.get("cpu_free", 0))
        fits = min(gpu_free, cpu_free // cpus)
        remaining -= min(remaining, fits)
        if remaining <= 0:
            return True
    return False


def _post_submit_pending(snapshot: dict[str, Any], account: str, native_id: str) -> bool:
    for row in snapshot.get("accounts") or []:
        if row.get("name") != account:
            continue
        for job in row.get("jobs") or []:
            job_id = str(job.get("job_id") or "")
            if job_id == native_id or job_id.startswith(native_id + "_"):
                if str(job.get("state") or "").upper() in {"PENDING", "PD"}:
                    return True
    return False


def _terminal_status(state: dict[str, Any]) -> str:
    statuses = {str(row["status"]) for row in state["items"]}
    if "needs_reconcile" in statuses:
        return "needs_reconcile"
    if "not_attempted_cycle_cap" in statuses:
        return "cycle_cap_reached"
    if statuses.intersection({"pending", "planned", "blocked_preflight"}):
        return "partial_blocked"
    if statuses.intersection(
        {"verification_failed", "blocked_shape", "blocked_array_capacity", "blocked_exact_script"}
    ):
        return "partial_blocked"
    return "complete"


class SubmitCycleController:
    def __init__(self, manifest: dict[str, Any], journal: CycleJournal, adapter: CycleAdapter) -> None:
        self.manifest = manifest
        self.journal = journal
        self.adapter = adapter
        self.items = _item_map(manifest)

    def _write_snapshot(self, payload: dict[str, Any], index: int, label: str) -> None:
        atomic_write_json(self.journal.snapshot_path(index, label), payload)

    def _write_plan(self, payload: dict[str, Any], index: int) -> None:
        atomic_write_json(self.journal.plan_path(index), payload)

    def _result(self, state: dict[str, Any], status: str) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in state["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        blockers = [
            {
                "item_id": item["item_id"],
                "status": item["status"],
                "reason": item["blocker"],
            }
            for item in state["items"]
            if item.get("blocker")
        ]
        if state.get("blocker"):
            blockers.insert(0, {"scope": "cycle", "status": status, "reason": state["blocker"]})
        next_row = next(
            (
                item
                for item in state["items"]
                if item["status"] in {"planned", "accepted_needs_verification", "needs_reconcile"}
            ),
            None,
        )
        result = {
            "schema_version": 1,
            "cycle_id": state["cycle_id"],
            "manifest_sha256": state["manifest_sha256"],
            "status": status,
            "admissions_completed": state.get("admissions_completed", 0),
            "counts": dict(sorted(counts.items())),
            "items": state["items"],
            "next_action": (
                {
                    "item_id": next_row["item_id"],
                    "status": next_row["status"],
                    "account_ref": next_row.get("selected_account_ref"),
                }
                if next_row
                else None
            ),
            "blockers": blockers,
            "call_counts": dict(sorted(dict(getattr(self.adapter, "call_counts", {})).items())),
            "timings": getattr(self.adapter, "timings", {}),
            "cycle_dir": str(self.journal.cycle_dir),
        }
        atomic_write_json(self.journal.result_path, result)
        return result

    def run(self, *, submit_enabled: bool) -> dict[str, Any]:
        with self.journal.lock():
            state = self.journal.load()
            if state["manifest_sha256"] != self.manifest["manifest_sha256"]:
                raise CycleContractError("journal manifest SHA does not match loaded manifest")
            crashed_submitting = False
            for state_row in state["items"]:
                if state_row["status"] == "submitting":
                    state_row["status"] = "needs_reconcile"
                    state_row["blocker"] = "controller stopped after durable submitting state; trace lookup required"
                    crashed_submitting = True
            if crashed_submitting:
                state["status"] = "needs_reconcile"
                self.journal.save(state)
                self.journal.event("crash_recovery_requires_reconcile")
            if any(item["status"] == "needs_reconcile" for item in state["items"]):
                state["status"] = "needs_reconcile"
                self.journal.save(state)
                return self._result(state, "needs_reconcile")

            for recovered_row in state["items"]:
                if recovered_row["status"] != "accepted_needs_verification":
                    continue
                item = self.items[recovered_row["item_id"]]
                account_ref = recovered_row.get("selected_account_ref")
                candidate = next(
                    (
                        candidate
                        for candidate in item["candidates"]
                        if _account_ref(candidate["auth_account"]) == account_ref
                    ),
                    None,
                )
                if candidate is None or not recovered_row.get("native_id"):
                    recovered_row["status"] = "verification_failed"
                    recovered_row["blocker"] = "recovered receipt cannot be bound to a manifest candidate"
                    continue
                verification = self.adapter.verify_existing(candidate, str(recovered_row["native_id"]))
                array_tasks = verification.get("array_tasks") or []
                array_verified = item["kind"] != "array" or (
                    len(array_tasks) == len(item["seeds"])
                    and all(task.get("success") for task in array_tasks)
                )
                if verification.get("success") and array_verified:
                    atomic_write_json(
                        self.journal.verifications_dir / f"{recovered_row['item_id']}.json",
                        verification,
                    )
                    recovered_row["status"] = "verified"
                    recovered_row["blocker"] = None
                    state["admissions_completed"] = int(state.get("admissions_completed", 0)) + 1
                    self.journal.event(
                        "recovered_submit_verified",
                        {"item_id": recovered_row["item_id"], "native_id": recovered_row["native_id"]},
                    )
                else:
                    recovered_row["status"] = "verification_failed"
                    recovered_row["blocker"] = "recovered native allocation failed shape verification"
            self.journal.save(state)

            blocked_accounts: set[str] = set()
            accounts = _remaining_accounts(state, self.items, blocked_accounts)
            if not accounts:
                state["status"] = _terminal_status(state)
                self.journal.save(state)
                return self._result(state, state["status"])

            snapshot_index = int(state.get("admissions_completed", 0))
            snapshot = self.adapter.snapshot(accounts)
            self._write_snapshot(snapshot, snapshot_index, "before")
            self.journal.event("snapshot", {"index": snapshot_index, "accounts": len(accounts), "reason": "initial_or_resume"})

            if not submit_enabled:
                report = self.adapter.plan(snapshot, accounts)
                self._write_plan(report, snapshot_index)
                try:
                    account, recommendation = _planner_allows(report)
                except CycleContractError as error:
                    state["status"] = "blocked"
                    state["blocker"] = str(error)
                    self.journal.save(state)
                    return self._result(state, "blocked")
                candidates = [
                    row for row in state["items"]
                    if row["status"] == "pending" and _candidate_for(self.items[row["item_id"]], account)
                ]
                if candidates:
                    selected = sorted(candidates, key=lambda row: (-int(row["priority"]), row["item_id"]))[0]
                    candidate = _candidate_for(self.items[selected["item_id"]], account)
                    selected["status"] = "planned"
                    selected["selected_account_ref"] = _account_ref(account)
                    selected["anonymous_trace_id"] = candidate["anonymous_trace_id"] if candidate else None
                    selected["blocker"] = None if candidate and _shape_matches(candidate, recommendation) else "shape_mismatch"
                state["status"] = "dry_run_planned"
                self.journal.save(state)
                self.journal.event("dry_run_planned", {"account_ref": _account_ref(account)})
                return self._result(state, "dry_run_planned")

            while int(state.get("admissions_completed", 0)) < int(state["max_admissions"]):
                accounts = _remaining_accounts(state, self.items, blocked_accounts)
                if not accounts:
                    break
                report = self.adapter.plan(snapshot, accounts)
                plan_index = int(state.get("admissions_completed", 0))
                self._write_plan(report, plan_index)
                try:
                    account, recommendation = _planner_allows(report)
                except CycleContractError as error:
                    state["status"] = "blocked"
                    state["blocker"] = str(error)
                    self.journal.save(state)
                    break

                rows = [
                    row for row in state["items"]
                    if row["status"] in {"pending", "planned", "blocked_preflight"}
                    and _candidate_for(self.items[row["item_id"]], account)
                ]
                if not rows:
                    blocked_accounts.add(account)
                    self.journal.event("account_without_work", {"account_ref": _account_ref(account)})
                    continue
                state_row = sorted(rows, key=lambda row: (-int(row["priority"]), row["item_id"]))[0]
                item = self.items[state_row["item_id"]]
                candidate = _candidate_for(item, account)
                assert candidate is not None
                if not _shape_matches(candidate, recommendation):
                    state_row["status"] = "blocked_shape"
                    state_row["blocker"] = "candidate shape does not match fresh planner next_action"
                    blocked_accounts.add(account)
                    self.journal.save(state)
                    continue
                if not _array_fits(item, candidate, snapshot):
                    state_row["status"] = "blocked_array_capacity"
                    state_row["blocker"] = "all array tasks cannot start directly from the fresh snapshot"
                    blocked_accounts.add(account)
                    self.journal.save(state)
                    continue

                try:
                    current_script_sha256 = file_sha256(Path(candidate["script"]))
                    current_intent_sha256 = file_sha256(Path(candidate["submit_intent_ref"]))
                except OSError:
                    current_script_sha256 = None
                    current_intent_sha256 = None
                if (
                    current_script_sha256 != candidate["script_sha256"]
                    or current_intent_sha256 != candidate["intent_sha256"]
                ):
                    state_row["status"] = "blocked_exact_script"
                    state_row["blocker"] = (
                        "exact script or submit intent changed after manifest validation; "
                        "create a new validated cycle"
                    )
                    blocked_accounts.add(account)
                    self.journal.save(state)
                    self.journal.event(
                        "exact_script_binding_changed",
                        {"item_id": state_row["item_id"], "account_ref": _account_ref(account)},
                    )
                    continue

                state_row["status"] = "submitting"
                state_row["selected_account_ref"] = _account_ref(account)
                state_row["anonymous_trace_id"] = candidate["anonymous_trace_id"]
                state_row["blocker"] = None
                state["status"] = "submitting"
                self.journal.save(state)
                self.journal.event(
                    "submit_started",
                    {
                        "item_id": state_row["item_id"],
                        "account_ref": _account_ref(account),
                        "script_sha256": candidate["script_sha256"],
                    },
                )
                receipt_path = self.journal.receipt_path(state_row["item_id"])
                try:
                    result = self.adapter.submit(candidate, receipt_path)
                except Exception as error:
                    state_row["status"] = "needs_reconcile"
                    state_row["blocker"] = (
                        "submit adapter raised after durable submitting state; "
                        f"trace lookup required ({type(error).__name__})"
                    )
                    state["status"] = "needs_reconcile"
                    self.journal.save(state)
                    self.journal.event(
                        "submit_adapter_exception",
                        {"item_id": state_row["item_id"], "error_type": type(error).__name__},
                    )
                    break
                outcome = str(result.get("submit_outcome") or "")
                if outcome == "unknown" or (result.get("submitted") and not result.get("job_id")):
                    state_row["status"] = "needs_reconcile"
                    state_row["blocker"] = str(result.get("message") or "unknown submit outcome")
                    state["status"] = "needs_reconcile"
                    self.journal.save(state)
                    self.journal.event("submit_outcome_unknown", {"item_id": state_row["item_id"]})
                    break
                if result.get("submitted") or result.get("job_id"):
                    binding_matches = (
                        result.get("script_or_command_sha256") == candidate["script_sha256"]
                        and result.get("remote_script_sha256") == candidate["script_sha256"]
                        and result.get("submit_intent_sha256") == candidate["intent_sha256"]
                    )
                    if not binding_matches:
                        state_row["status"] = "verification_failed"
                        state_row["native_id"] = str(result.get("job_id") or "") or None
                        state_row["receipt_path"] = str(receipt_path) if receipt_path.exists() else None
                        state_row["blocker"] = "submitted job failed exact-script byte binding; do not retry"
                        state["status"] = "verification_failed"
                        self.journal.save(state)
                        self.journal.event("exact_script_binding_failed", {"item_id": state_row["item_id"]})
                        break
                if not result.get("success"):
                    if result.get("job_id") or receipt_path.exists():
                        state_row["status"] = "verification_failed"
                        state_row["native_id"] = str(result.get("job_id") or "") or None
                        state_row["receipt_path"] = str(receipt_path) if receipt_path.exists() else None
                        state_row["blocker"] = "native allocation verification failed; do not retry"
                    else:
                        state_row["status"] = "blocked_preflight"
                        state_row["blocker"] = str(result.get("message") or "preflight or submit failed")
                    blocked_accounts.add(account)
                    self.journal.save(state)
                    snapshot = self.adapter.snapshot(_remaining_accounts(state, self.items, blocked_accounts) or accounts)
                    self._write_snapshot(snapshot, plan_index + 1, "after-failure")
                    self.journal.event("snapshot", {"index": plan_index + 1, "reason": "failure_refresh"})
                    if state_row["status"] == "verification_failed":
                        break
                    continue

                native_id = str(result.get("job_id") or "")
                if not native_id or not receipt_path.is_file():
                    state_row["status"] = "needs_reconcile"
                    state_row["blocker"] = "backend success lacked durable native receipt"
                    state["status"] = "needs_reconcile"
                    self.journal.save(state)
                    break
                verification = result.get("verification") or {}
                array_tasks = verification.get("array_tasks") or []
                array_verified = True
                if item["kind"] == "array":
                    array_verified = (
                        len(array_tasks) == len(item["seeds"])
                        and all(task.get("success") for task in array_tasks)
                    )
                if not verification.get("success") or not array_verified:
                    state_row["status"] = "verification_failed"
                    state_row["native_id"] = native_id
                    state_row["receipt_path"] = str(receipt_path)
                    state_row["blocker"] = "scontrol shape verification failed; do not retry"
                    state["status"] = "verification_failed"
                    self.journal.save(state)
                    break
                atomic_write_json(self.journal.verifications_dir / f"{state_row['item_id']}.json", verification)
                state_row["status"] = "verified"
                state_row["native_id"] = native_id
                state_row["receipt_path"] = str(receipt_path)
                state["admissions_completed"] = int(state.get("admissions_completed", 0)) + 1
                self.journal.save(state)
                self.journal.event("submit_verified", {"item_id": state_row["item_id"], "native_id": native_id})

                snapshot_index = int(state["admissions_completed"])
                remaining = _remaining_accounts(state, self.items, blocked_accounts)
                snapshot = self.adapter.snapshot(sorted(set(remaining) | {account}))
                self._write_snapshot(snapshot, snapshot_index, "after")
                self.journal.event("snapshot", {"index": snapshot_index, "reason": "post_submit_and_next_predecision"})
                if _post_submit_pending(snapshot, account, native_id):
                    state_row["status"] = "accepted_pending"
                    state_row["blocker"] = "new job is PENDING; account blocked for this cycle"
                    blocked_accounts.add(account)
                    self.journal.save(state)

            remaining_rows = [
                row for row in state["items"]
                if row["status"] in {"pending", "planned", "blocked_preflight"}
            ]
            if int(state.get("admissions_completed", 0)) >= int(state["max_admissions"]) and remaining_rows:
                for row in remaining_rows:
                    row["status"] = "not_attempted_cycle_cap"
                state["status"] = "cycle_cap_reached"
            else:
                state["status"] = _terminal_status(state)
            self.journal.save(state)
            return self._result(state, state["status"])

    def reconcile(self) -> dict[str, Any]:
        with self.journal.lock():
            state = self.journal.load()
            changed = False
            for state_row in state["items"]:
                if state_row["status"] != "needs_reconcile":
                    continue
                item = self.items[state_row["item_id"]]
                account_ref = state_row.get("selected_account_ref")
                candidate = next(
                    (candidate for candidate in item["candidates"] if _account_ref(candidate["auth_account"]) == account_ref),
                    None,
                )
                if candidate is None:
                    continue
                outcome = self.adapter.reconcile(candidate)
                matches = outcome.get("matches") or []
                if len(matches) != 1:
                    state_row["blocker"] = f"reconciliation found {len(matches)} Slurm matches; manual audit required"
                    continue
                native_id = str(matches[0].get("job_id") or "")
                if not native_id:
                    continue
                receipt_path = self.journal.receipt_path(state_row["item_id"])
                recovered = {
                    "schema_version": 1,
                    "submit_receipt": {
                        "backend_idempotency_key": candidate["backend_idempotency_key"],
                        "anonymous_trace_id": candidate["anonymous_trace_id"],
                        "launch_identity_hash": candidate["launch_identity_hash"],
                        "script_or_command_sha256": candidate["script_sha256"],
                        "execution_route": "bjtu_hpc",
                        "native_id": native_id,
                        "accepted_at": matches[0].get("accepted_at") or now_iso(),
                        "recovered_by_trace": True,
                    },
                }
                recovered["submit_receipt_sha256"] = canonical_sha256(recovered["submit_receipt"])
                atomic_write_json(receipt_path, recovered)
                state_row["status"] = "accepted_needs_verification"
                state_row["native_id"] = native_id
                state_row["receipt_path"] = str(receipt_path)
                state_row["blocker"] = "receipt recovered; run resume to verify and refresh before further admission"
                changed = True
                self.journal.event("receipt_recovered", {"item_id": state_row["item_id"], "native_id": native_id})
            if changed:
                state["status"] = "needs_verification"
            self.journal.save(state)
            return self._result(state, state["status"])
