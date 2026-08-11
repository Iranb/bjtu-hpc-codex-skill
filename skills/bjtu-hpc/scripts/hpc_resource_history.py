#!/usr/bin/env python3
"""Record redacted BJTU HPC CPU/GPU request history for later scheduling analysis."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SCHEMA = "bjtu-hpc-resource-history/v1"
ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY_PATH = ROOT / "work" / "hpc_resource_history.jsonl"
DEFAULT_STATE_PATH = ROOT / "work" / "hpc_resource_history.state.json"
DEFAULT_PENDING_DIR = ROOT / "hpc_stdout"


def now_local() -> str:
    return datetime.now().isoformat(timespec="seconds")


def parse_local_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text in {"Unknown", "N/A", "None"}:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_scontrol_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for token in str(text or "").replace("\n", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def int_value(value: Any) -> int | None:
    if value in (None, "", "N/A", "Unknown", "(null)"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_tres(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not value:
        return result
    for item in str(value).split(","):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        result[key] = raw
    return result


def gpu_count_from_fields(fields: dict[str, Any]) -> int | None:
    for key in ("AllocTRES", "TRES", "ReqTRES"):
        value = parse_tres(fields.get(key)).get("gres/gpu")
        parsed = int_value(value)
        if parsed is not None:
            return parsed
    tres_per_node = fields.get("TresPerNode") or fields.get("TRESPerNode")
    if tres_per_node:
        match = re.search(r"(?:gres[:/])?gpu(?::[^:=]+)?[:=](\d+)", str(tres_per_node))
        if match:
            return int(match.group(1))
    return None


def request_shape(cpus: int | None, gpus: int | None, tasks: int | None, cpus_per_task: int | None) -> dict[str, Any]:
    cpus_per_gpu = None
    if cpus is not None and gpus:
        cpus_per_gpu = cpus / gpus
    return {
        "cpus": cpus,
        "gpus": gpus,
        "num_tasks": tasks,
        "cpus_per_task": cpus_per_task,
        "cpus_per_gpu": cpus_per_gpu,
    }


def resources_from_fields(fields: dict[str, Any]) -> dict[str, Any]:
    cpus = int_value(fields.get("NumCPUs"))
    tasks = int_value(fields.get("NumTasks"))
    cpus_per_task = int_value(fields.get("CPUs/Task"))
    gpus = gpu_count_from_fields(fields)
    return {
        **request_shape(cpus, gpus, tasks, cpus_per_task),
        "tres": fields.get("TRES"),
        "alloc_tres": fields.get("AllocTRES"),
        "req_tres": fields.get("ReqTRES"),
        "tres_per_node": fields.get("TresPerNode") or fields.get("TRESPerNode"),
    }


def resources_from_job(job: dict[str, Any]) -> dict[str, Any]:
    resources = job.get("resources") or {}
    cpus = int_value(resources.get("num_cpus"))
    gpus = int_value(resources.get("gpu_count"))
    tasks = int_value(resources.get("num_tasks"))
    cpus_per_task = int_value(resources.get("cpus_per_task"))
    return {
        **request_shape(cpus, gpus, tasks, cpus_per_task),
        "tres": resources.get("tres"),
        "alloc_tres": resources.get("alloc_tres"),
        "req_tres": resources.get("req_tres"),
        "tres_per_node": resources.get("tres_per_node"),
    }


def clean_reason(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text or "-"


def saved_account_aliases() -> dict[str, str]:
    try:
        from hpc_account_store import list_account_summaries

        aliases = {}
        for row in list_account_summaries():
            account = row.get("account")
            name = row.get("name")
            if account and name:
                aliases[str(account)] = str(name)
        return aliases
    except Exception:
        return {}


def snapshot_signature(payload: dict[str, Any]) -> str:
    resources = payload.get("cluster_resources") or {}
    compact_accounts = []
    for account in payload.get("accounts") or []:
        compact_jobs = []
        for job in account.get("jobs") or []:
            compact_jobs.append(
                {
                    "job_id": str(job.get("job_id") or ""),
                    "name": job.get("name") or "",
                    "state": str(job.get("state") or "").upper(),
                    "reason": clean_reason(job.get("reason")),
                    "nodes": job.get("nodes") or "",
                    "resources": resources_from_job(job),
                    "timing": job.get("timing") or {},
                }
            )
        compact_accounts.append(
            {
                "name": account.get("name") or "",
                "account": account.get("account") or "",
                "error": account.get("error") or "",
                "summary": account.get("summary") or {},
                "jobs": sorted(compact_jobs, key=lambda item: item["job_id"]),
            }
        )
    stable = {
        "partition": payload.get("partition"),
        "cluster_resources": {
            "error": resources.get("error"),
            "summary": resources.get("summary") or {},
            "excluded_reserved_nodes": sorted(resources.get("excluded_reserved_nodes") or []),
            "nodes": sorted(
                resources.get("nodes") or [],
                key=lambda item: str(item.get("name") or ""),
            ),
        },
        "accounts": sorted(compact_accounts, key=lambda item: item["name"]),
    }
    data = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def history_key(record: dict[str, Any]) -> str:
    return str(record.get("history_key") or "")


def load_existing_keys(path: Path) -> set[str]:
    keys: set[str] = set()
    if not path.exists():
        return keys
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    key = history_key(json.loads(line))
                except Exception:
                    continue
                if key:
                    keys.add(key)
    except FileNotFoundError:
        return keys
    return keys


def append_records(records: list[dict[str, Any]], path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    existing = load_existing_keys(path)
    new_records = [record for record in records if history_key(record) not in existing]
    if dry_run:
        return {"written": 0, "would_write": len(new_records), "skipped_existing": len(records) - len(new_records)}
    if not new_records:
        return {"written": 0, "skipped_existing": len(records)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for record in new_records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {"written": len(new_records), "skipped_existing": len(records) - len(new_records)}


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cluster_record(payload: dict[str, Any], signature: str, source: str) -> dict[str, Any] | None:
    resources = payload.get("cluster_resources")
    if not resources:
        return None
    collected_at = payload.get("checked_at_local") or now_local()
    return {
        "schema": SCHEMA,
        "type": "cluster_resource_sample",
        "history_key": f"cluster|{collected_at}|{signature}",
        "source": source,
        "snapshot_signature": signature,
        "collected_at_local": collected_at,
        "partition": payload.get("partition"),
        "error": resources.get("error"),
        "summary": resources.get("summary") or {},
        "nodes": resources.get("nodes") or [],
        "excluded_reserved_nodes": resources.get("excluded_reserved_nodes") or [],
    }


def job_record_from_queue(
    payload: dict[str, Any],
    account: dict[str, Any],
    job: dict[str, Any],
    signature: str,
    source: str,
) -> dict[str, Any]:
    collected_at = payload.get("checked_at_local") or now_local()
    requested = resources_from_job(job)
    native = job.get("native") or {}
    timing = job.get("timing") or {}
    job_id = str(job.get("job_id") or "")
    native_reason = native.get("reason")
    reason = native_reason if native_reason not in (None, "", "(null)") else job.get("reason")
    native_nodes = native.get("node_list")
    nodes = native_nodes if native_nodes not in (None, "", "(null)") else job.get("nodes")
    return {
        "schema": SCHEMA,
        "type": "job_resource_sample",
        "history_key": f"queue|{collected_at}|{account.get('name') or ''}|{job_id}|{signature}",
        "source": source,
        "snapshot_signature": signature,
        "collected_at_local": collected_at,
        "account_alias": account.get("name"),
        "cluster_account": account.get("account"),
        "partition": job.get("partition") or payload.get("partition"),
        "job_id": job_id,
        "job_name": job.get("name"),
        "state": str(job.get("state") or native.get("job_state") or "").upper(),
        "reason": clean_reason(reason),
        "elapsed": job.get("elapsed"),
        "time_limit": job.get("time_limit"),
        "nodes": nodes,
        "sched_node_list": native.get("sched_node_list"),
        "qos": native.get("qos"),
        "priority": native.get("priority"),
        "requested": requested,
        "timing": timing,
    }


def queue_records(payload: dict[str, Any], source: str = "hpc_queue_summary") -> tuple[str, list[dict[str, Any]]]:
    signature = snapshot_signature(payload)
    records: list[dict[str, Any]] = []
    cluster = cluster_record(payload, signature, source)
    if cluster:
        records.append(cluster)
    for account in payload.get("accounts") or []:
        for job in account.get("jobs") or []:
            records.append(job_record_from_queue(payload, account, job, signature, source))
    return signature, records


def record_queue_summary(
    payload: dict[str, Any],
    *,
    history_path: Path | str = DEFAULT_HISTORY_PATH,
    state_path: Path | str | None = None,
    source: str = "hpc_queue_summary",
    dedupe: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    history_path = Path(history_path).expanduser()
    state_path = Path(state_path).expanduser() if state_path else DEFAULT_STATE_PATH
    signature, records = queue_records(payload, source)
    state = load_state(state_path)
    same_signature = dedupe and state.get("last_queue_signature") == signature
    if same_signature:
        return {
            "written": 0,
            "skipped_same_signature": True,
            "signature": signature,
            "history_path": str(history_path),
        }
    result = append_records(records, history_path, dry_run=dry_run)
    if not dry_run:
        state.update(
            {
                "last_queue_signature": signature,
                "last_queue_recorded_at_local": now_local(),
                "history_path": str(history_path),
            }
        )
        save_state(state_path, state)
    return {**result, "signature": signature, "history_path": str(history_path)}


def job_record_from_fields(
    fields: dict[str, str],
    *,
    collected_at: str,
    source_file: Path,
    cluster_account: str | None,
    account_alias: str | None,
    partition: str | None,
) -> dict[str, Any] | None:
    job_id = fields.get("JobId")
    if not job_id:
        return None
    requested = resources_from_fields(fields)
    return {
        "schema": SCHEMA,
        "type": "job_resource_sample",
        "history_key": f"pending|{source_file.name}|{job_id}",
        "source": "hpc_pending_reason_snapshot",
        "source_file": source_file.name,
        "collected_at_local": collected_at,
        "account_alias": account_alias,
        "cluster_account": cluster_account,
        "partition": fields.get("Partition") or partition,
        "job_id": job_id,
        "job_name": fields.get("JobName"),
        "state": str(fields.get("JobState") or "").upper(),
        "reason": clean_reason(fields.get("Reason")),
        "elapsed": fields.get("RunTime"),
        "time_limit": fields.get("TimeLimit"),
        "nodes": fields.get("NodeList"),
        "sched_node_list": fields.get("SchedNodeList"),
        "qos": fields.get("QOS"),
        "priority": int_value(fields.get("Priority")),
        "requested": requested,
        "timing": {
            "submit_time": fields.get("SubmitTime"),
            "eligible_time": fields.get("EligibleTime"),
            "start_time": fields.get("StartTime"),
            "end_time": fields.get("EndTime"),
            "last_sched_eval": fields.get("LastSchedEval"),
        },
    }


def pending_snapshot_records(path: Path, account_aliases: dict[str, str] | None = None) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    collected_at = payload.get("checked_at_local") or datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    cluster_account = payload.get("account")
    account_alias = (account_aliases or {}).get(str(cluster_account or ""))
    partition = payload.get("partition")
    records: list[dict[str, Any]] = []
    for key, item in (payload.get("results") or {}).items():
        if not str(key).startswith("scontrol_"):
            continue
        stdout = (item or {}).get("stdout") or ""
        fields = parse_scontrol_fields(stdout)
        record = job_record_from_fields(
            fields,
            collected_at=collected_at,
            source_file=path,
            cluster_account=cluster_account,
            account_alias=account_alias,
            partition=partition,
        )
        if record:
            records.append(record)
    return records


def backfill_pending_snapshots(
    *,
    pending_dir: Path = DEFAULT_PENDING_DIR,
    history_path: Path = DEFAULT_HISTORY_PATH,
    since_days: int = 14,
    limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    cutoff = datetime.now() - timedelta(days=since_days)
    files = sorted(pending_dir.glob("bjtu_pending_reason_*.json"), key=lambda item: item.name)
    selected: list[Path] = []
    for path in files:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            continue
        if mtime >= cutoff:
            selected.append(path)
    if limit:
        selected = selected[-limit:]
    records: list[dict[str, Any]] = []
    account_aliases = saved_account_aliases()
    for path in selected:
        records.extend(pending_snapshot_records(path, account_aliases))
    result = append_records(records, history_path, dry_run=dry_run)
    return {**result, "files_scanned": len(selected), "records_seen": len(records), "history_path": str(history_path)}


def summarize_history(path: Path, *, days: int = 14) -> dict[str, Any]:
    cutoff = datetime.now() - timedelta(days=days)
    latest_by_job: dict[str, dict[str, Any]] = {}
    samples = 0
    cluster_samples = 0
    if not path.exists():
        return {"history_path": str(path), "exists": False}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except Exception:
                continue
            collected = parse_local_time(record.get("collected_at_local"))
            if collected and collected < cutoff:
                continue
            if record.get("type") == "cluster_resource_sample":
                cluster_samples += 1
                continue
            if record.get("type") != "job_resource_sample":
                continue
            samples += 1
            job_id = str(record.get("job_id") or "")
            if not job_id:
                continue
            prev = latest_by_job.get(job_id)
            if not prev or str(record.get("collected_at_local") or "") >= str(prev.get("collected_at_local") or ""):
                latest_by_job[job_id] = record
    shapes: dict[str, int] = {}
    states: dict[str, int] = {}
    accounts: dict[str, int] = {}
    for record in latest_by_job.values():
        requested = record.get("requested") or {}
        gpus = requested.get("gpus")
        cpus = requested.get("cpus")
        tasks = requested.get("num_tasks")
        cpt = requested.get("cpus_per_task")
        shape = f"{gpus if gpus is not None else '?'}G/{cpus if cpus is not None else '?'}C/{tasks if tasks is not None else '?'}T/{cpt if cpt is not None else '?'}CPT"
        shapes[shape] = shapes.get(shape, 0) + 1
        state = str(record.get("state") or "?").upper()
        states[state] = states.get(state, 0) + 1
        account = str(record.get("account_alias") or record.get("cluster_account") or "?")
        accounts[account] = accounts.get(account, 0) + 1
    return {
        "history_path": str(path),
        "exists": True,
        "days": days,
        "job_samples": samples,
        "cluster_samples": cluster_samples,
        "unique_jobs": len(latest_by_job),
        "latest_shapes": dict(sorted(shapes.items())),
        "latest_states": dict(sorted(states.items())),
        "latest_accounts": dict(sorted(accounts.items())),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record and summarize BJTU HPC CPU/GPU request history.")
    parser.add_argument("--history-log", type=Path, default=DEFAULT_HISTORY_PATH)
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--queue-json", type=Path, help="Record one hpc_queue_summary.py --json payload.")
    parser.add_argument("--stdin", action="store_true", help="Read one queue summary JSON payload from stdin.")
    parser.add_argument("--backfill-days", type=int, help="Backfill recent hpc_pending_reason snapshots from hpc_stdout.")
    parser.add_argument("--pending-dir", type=Path, default=DEFAULT_PENDING_DIR)
    parser.add_argument("--limit", type=int, help="Limit backfill to the newest N snapshot files.")
    parser.add_argument("--no-dedupe", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--summary", action="store_true", help="Print a compact JSON summary after recording.")
    parser.add_argument("--summary-days", type=int, default=14)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reports: list[dict[str, Any]] = []
    if args.queue_json or args.stdin:
        if args.stdin:
            payload = json.load(sys.stdin)
        else:
            payload = json.loads(args.queue_json.read_text(encoding="utf-8"))
        reports.append(
            {
                "action": "record_queue_summary",
                **record_queue_summary(
                    payload,
                    history_path=args.history_log,
                    state_path=args.state_file,
                    dedupe=not args.no_dedupe,
                    dry_run=args.dry_run,
                ),
            }
        )
    if args.backfill_days is not None:
        reports.append(
            {
                "action": "backfill_pending_snapshots",
                **backfill_pending_snapshots(
                    pending_dir=args.pending_dir,
                    history_path=args.history_log,
                    since_days=args.backfill_days,
                    limit=args.limit,
                    dry_run=args.dry_run,
                ),
            }
        )
    if args.summary or not reports:
        reports.append({"action": "summary", **summarize_history(args.history_log, days=args.summary_days)})
    print(json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
