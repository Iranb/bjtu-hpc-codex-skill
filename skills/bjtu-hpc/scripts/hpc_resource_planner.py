#!/usr/bin/env python3.12
"""Plan BJTU HPC CPU/GPU request shapes from live node and queue state.

The planner is read-only. It does not submit, cancel, or edit Slurm jobs.
It consumes the JSON payload produced by hpc_queue_summary.py. Legacy queued
mode plans toward a non-terminal cap; direct-start mode exposes independently
eligible account admissions while keeping every physical submit refresh-gated.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from hpc_runtime import require_native_dependencies

require_native_dependencies()

import paramiko

import hpc_winscp_info as winscp


ROOT = Path(__file__).resolve().parent
DEFAULT_HISTORY_LOG = ROOT / "work" / "hpc_resource_history.jsonl"
RUNNING_STATES = {"R", "RUNNING", "COMPLETING", "CG"}
PENDING_STATES = {"PD", "PENDING"}
TEST_ONLY_START_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})\b")
BAD_NODE_STATE_TOKENS = {
    "DOWN",
    "DRAIN",
    "DRAINED",
    "FAIL",
    "FAILING",
    "MAINT",
    "POWER_DOWN",
    "RESERVED",
}


@dataclass(frozen=True)
class Candidate:
    name: str
    gpus: int
    cpus: int
    tasks: int
    cpus_per_task: int
    kind: str
    emergency: bool = False
    note: str = ""

    @property
    def shape_key(self) -> str:
        return f"{self.gpus}G/{self.cpus}C/{self.tasks}T/{self.cpus_per_task}CPT"

    @property
    def sbatch_flags(self) -> list[str]:
        return [
            "--nodes=1",
            f"--ntasks={self.tasks}",
            f"--cpus-per-task={self.cpus_per_task}",
            f"--gres=gpu:{self.gpus}",
            "--gres-flags=disable-binding",
        ]


@dataclass
class ShapeStats:
    jobs: int = 0
    ever_running: int = 0
    resource_pending: int = 0
    waits_minutes: list[float] = field(default_factory=list)

    @property
    def start_rate(self) -> float:
        return self.ever_running / self.jobs if self.jobs else 0.5

    @property
    def resource_pending_rate(self) -> float:
        return self.resource_pending / self.jobs if self.jobs else 0.0

    @property
    def median_wait_minutes(self) -> float:
        if not self.waits_minutes:
            return 180.0
        return float(statistics.median(self.waits_minutes))

    def public(self) -> dict[str, Any]:
        return {
            "jobs": self.jobs,
            "ever_running": self.ever_running,
            "start_rate": round(self.start_rate, 3),
            "resource_pending_rate": round(self.resource_pending_rate, 3),
            "median_wait_minutes": round(self.median_wait_minutes, 1),
        }


PRIOR_STATS: dict[str, ShapeStats] = {
    # Policy priors for the 1GPU/6CPU ordinary target until the local ledger has
    # enough observed starts to dominate shape scoring.
    "1G/6C/1T/6CPT": ShapeStats(jobs=1, ever_running=1, resource_pending=0, waits_minutes=[60.0]),
    "1G/16C/1T/16CPT": ShapeStats(jobs=9, ever_running=8, resource_pending=0, waits_minutes=[19.6]),
    "1G/8C/1T/8CPT": ShapeStats(jobs=17, ever_running=1, resource_pending=2, waits_minutes=[271.8]),
    "1G/4C/1T/4CPT": ShapeStats(jobs=10, ever_running=6, resource_pending=0, waits_minutes=[73.9]),
    "2G/12C/2T/6CPT": ShapeStats(jobs=1, ever_running=1, resource_pending=0, waits_minutes=[60.0]),
    "2G/32C/2T/16CPT": ShapeStats(jobs=48, ever_running=19, resource_pending=4, waits_minutes=[287.6]),
    "2G/24C/2T/12CPT": ShapeStats(jobs=0, ever_running=0, resource_pending=0, waits_minutes=[180.0]),
    "2G/16C/2T/8CPT": ShapeStats(jobs=113, ever_running=63, resource_pending=6, waits_minutes=[112.5]),
    "2G/8C/2T/4CPT": ShapeStats(jobs=2, ever_running=1, resource_pending=0, waits_minutes=[17.5]),
}

CPU_POLICIES = ("balanced", "gpu-dense", "cpu-fill")
TESTED_TOTAL_CPUS = {1, 2, 4, 6, 8, 12, 16, 18, 20, 24, 30, 32, 36, 40, 42, 48}
QUEUE_PROBE_BACKLOG_REASON = (
    "queue_probe is backlog-only: no same-node candidate fits current resources; "
    "refresh resources or run exact sbatch --test-only before submitting"
)


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
        description=(
            "Recommend CPU/GPU request shapes for new BJTU HPC jobs, using "
            "live node resources, current account queues, and recent history."
        )
    )
    parser.add_argument("--queue-json", type=Path, help="Use an existing hpc_queue_summary.py --json payload.")
    parser.add_argument(
        "--accounts",
        help="Comma-separated auth account names to plan for. Default: every account in the queue payload.",
    )
    parser.add_argument("--partition", default="GPU")
    parser.add_argument("--timeout", type=int, default=30, help="Timeout for hpc_queue_summary.py when live querying.")
    parser.add_argument(
        "--cap",
        type=int,
        default=4,
        help="Target non-terminal jobs per account. Default: 4 (2 running + 2 queued).",
    )
    parser.add_argument("--run-slots", type=int, default=2, help="Expected running slots per account. Default: 2.")
    parser.add_argument(
        "--admission-mode",
        choices=["queued", "direct-start"],
        default="queued",
        help=(
            "queued preserves legacy non-terminal-cap planning; direct-start blocks only the affected "
            "account/shared-limit group on pending work, rejects queue probes, and exposes a refresh-gated admission frontier."
        ),
    )
    parser.add_argument(
        "--max-admissions-per-cycle",
        type=positive_int_arg,
        default=8,
        help="Maximum direct-start account candidates exposed for one controller cycle. Default: 8.",
    )
    parser.add_argument(
        "--workload",
        choices=["packed", "single", "mixed", "gpu-fill"],
        default="mixed",
        help="Candidate family. mixed allows packed jobs plus 1GPU compatibility fallbacks.",
    )
    parser.add_argument(
        "--submit-mode",
        choices=["sequential", "batch"],
        default="sequential",
        help=(
            "sequential recommends the next one-by-one submission and expects a refresh after submit; "
            "batch virtually packs all open account slots in one pass."
        ),
    )
    parser.add_argument(
        "--gpu-first",
        action="store_true",
        help="Allow GPU-fill fragment candidates down to 2 CPUs per single-GPU child.",
    )
    parser.add_argument(
        "--cpu-policy",
        choices=CPU_POLICIES,
        default="balanced",
        help=(
            "CPU request policy. balanced maximizes GPUs first and targets 1GPU/6CPU unless a higher "
            "CPU shape has explicit immediate-start evidence; gpu-dense preserves lower-CPU GPU-fill bias; "
            "cpu-fill strongly prefers the largest fitting CPU shape."
        ),
    )
    parser.add_argument(
        "--wide-gpu-policy",
        choices=["auto", "always", "off"],
        default="auto",
        help=(
            "Whether to generate wide single-allocation candidates. "
            "auto enables them when a node has >=3 free GPUs, GPU-first is active, or test-only probing is active."
        ),
    )
    parser.add_argument(
        "--max-gpus-per-job",
        type=int,
        default=8,
        help="Maximum GPUs in one generated wide/GPU-fill allocation. Default: 8.",
    )
    parser.add_argument(
        "--wide-cpus-per-gpu",
        default="6,4,2",
        help="Comma-separated CPU-per-GPU choices for wide/GPU-fill candidates. Default: 6,4,2.",
    )
    parser.add_argument(
        "--available-children",
        type=int,
        default=None,
        help=(
            "Number of independent one-GPU child experiments available for the next allocation. "
            "Wide/GPU-fill candidates are capped by this value; when omitted, they are capped at 2."
        ),
    )
    parser.add_argument(
        "--child-manifest",
        type=Path,
        help=(
            "Optional JSON manifest of unlaunched child experiments. If --available-children is omitted, "
            "the planner counts manifest entries from children/experiments/jobs or a top-level list."
        ),
    )
    parser.add_argument(
        "--test-only-probe",
        action="store_true",
        help="Run read-only remote sbatch --test-only probes for the sequential next_action alternatives.",
    )
    parser.add_argument(
        "--probe-script",
        type=Path,
        help=(
            "Local sbatch template to rewrite for each candidate during --test-only-probe. "
            "When omitted, the probe is resource-shape-only and the final exact script still must be tested."
        ),
    )
    parser.add_argument(
        "--write-selected-script",
        type=Path,
        help=(
            "With --test-only-probe --probe-script, write the selected rewritten local sbatch script here. "
            "This is read-only with respect to Slurm; submit it separately with hpc_native_submit.py."
        ),
    )
    parser.add_argument(
        "--test-only-candidates",
        type=int,
        default=12,
        help="Maximum candidate shapes to probe with sbatch --test-only. Default: 12.",
    )
    parser.add_argument(
        "--immediate-window-seconds",
        type=int,
        default=180,
        help="Treat a test-only start estimate within this window as immediate. Default: 180.",
    )
    parser.add_argument("--token-file", type=Path, default=winscp.DEFAULT_TOKEN_FILE)
    parser.add_argument(
        "--prefer-cpu",
        action="store_true",
        help="Compatibility alias for --cpu-policy cpu-fill.",
    )
    parser.add_argument(
        "--no-queued",
        action="store_true",
        help="Only recommend jobs that fit the current same-node free resources.",
    )
    parser.add_argument(
        "--allow-queued-submit",
        action="store_true",
        help=(
            "Allow a queue_probe recommendation to count as a submit-now backlog action. "
            "By default queue_probe is advisory until an exact sbatch --test-only probe accepts a candidate."
        ),
    )
    parser.add_argument(
        "--history-log",
        type=Path,
        default=DEFAULT_HISTORY_LOG,
        help="Local resource history JSONL used for empirical shape stats.",
    )
    parser.add_argument("--history-days", type=int, default=14)
    parser.add_argument("--json", action="store_true", help="Emit planner JSON only.")
    return parser.parse_args()


def parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text in {"Unknown", "N/A", "None", "(null)"}:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", text):
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_int_list(value: Any, *, default: list[int]) -> list[int]:
    result: list[int] = []
    for item in str(value or "").split(","):
        parsed = int_value(item.strip(), -1)
        if parsed > 0 and parsed not in result:
            result.append(parsed)
    return result or default


def effective_cpu_policy(args: argparse.Namespace) -> str:
    if getattr(args, "prefer_cpu", False):
        return "cpu-fill"
    policy = str(getattr(args, "cpu_policy", "balanced") or "balanced")
    return policy if policy in CPU_POLICIES else "balanced"


def count_manifest_children(path: Path | None) -> int | None:
    if not path:
        return None
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("children", "experiments", "jobs", "tasks", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return len(value)
        for key in ("available_children", "unlaunched_children", "count"):
            value = payload.get(key)
            if value is not None:
                return max(0, int_value(value))
    raise ValueError(f"Cannot count child experiments from manifest: {path}")


def available_children(args: argparse.Namespace) -> int:
    configured = getattr(args, "available_children", None)
    if configured is not None:
        return max(0, int_value(configured))
    manifest_count = count_manifest_children(getattr(args, "child_manifest", None))
    if manifest_count is not None:
        return max(0, manifest_count)
    # Safe default: do not surface allocations wider than the normal two-child
    # packed job unless the caller proves enough independent one-GPU children.
    return 2


def clean_reason(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text or "-"


def resources_shape_key(resources: dict[str, Any]) -> str | None:
    gpus = resources.get("gpus")
    cpus = resources.get("cpus")
    tasks = resources.get("num_tasks")
    cpt = resources.get("cpus_per_task")
    if gpus is None or cpus is None or tasks is None or cpt is None:
        return None
    return f"{gpus}G/{cpus}C/{tasks}T/{cpt}CPT"


def job_resource_shape_key(job: dict[str, Any]) -> str | None:
    resources = job.get("resources") or {}
    gpus = resources.get("gpu_count")
    cpus = resources.get("num_cpus")
    tasks = resources.get("num_tasks")
    cpt = resources.get("cpus_per_task")
    if gpus is None or cpus is None or tasks is None or cpt is None:
        return None
    return f"{gpus}G/{cpus}C/{tasks}T/{cpt}CPT"


def load_queue_payload(args: argparse.Namespace) -> dict[str, Any]:
    if args.queue_json:
        return json.loads(args.queue_json.expanduser().read_text(encoding="utf-8"))

    command = [
        sys.executable,
        str(ROOT / "hpc_queue_summary.py"),
        "--json",
        "--partition",
        args.partition,
        "--timeout",
        str(args.timeout),
        "--cap",
        str(args.cap),
        "--run-slots",
        str(args.run_slots),
    ]
    if args.accounts:
        command.extend(["--accounts", args.accounts])
    if args.history_log:
        command.extend(["--history-log", str(args.history_log.expanduser())])
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"hpc_queue_summary.py failed with rc={completed.returncode}: {detail}")
    return json.loads(completed.stdout)


def candidate_family(workload: str, gpu_first: bool, cpu_policy: str) -> list[Candidate]:
    include_cpu_rich = cpu_policy == "cpu-fill"
    packed = [
        Candidate("packed-2g12c", 2, 12, 2, 6, "packed", note="ordinary 1GPU/6CPU packed shape"),
        Candidate("packed-2g8c", 2, 8, 2, 4, "packed", emergency=True, note="resource-wait fallback packed shape"),
    ]
    single = [
        Candidate("single-1g6c", 1, 6, 1, 6, "single", note="ordinary 1GPU/6CPU singleton"),
        Candidate("single-1g4c", 1, 4, 1, 4, "single", emergency=True, note="resource-wait fallback singleton"),
    ]
    if include_cpu_rich:
        packed = [
            Candidate("packed-2g32c", 2, 32, 2, 16, "packed", note="CPU-rich packed shape"),
            Candidate("packed-2g24c", 2, 24, 2, 12, "packed", note="CPU-rich packed shape"),
            Candidate("packed-2g16c", 2, 16, 2, 8, "packed", note="CPU-rich packed shape"),
        ] + packed
        single = [
            Candidate("single-1g16c", 1, 16, 1, 16, "single", note="CPU-rich singleton"),
            Candidate("single-1g8c", 1, 8, 1, 8, "single", note="CPU-rich singleton"),
        ] + single
    if workload == "packed":
        base = packed
    elif workload == "single":
        base = single
    else:
        base = packed + single
    if workload == "gpu-fill" or gpu_first:
        # The concrete GPU count is generated after the live nodes are known.
        base = base.copy()
    return base


def wide_gpu_enabled(nodes: list[dict[str, Any]], args: argparse.Namespace) -> bool:
    if args.wide_gpu_policy == "off":
        return False
    if args.wide_gpu_policy == "always" or args.gpu_first or args.test_only_probe:
        return True
    for node in nodes:
        free_gpu = int_value(node.get("gpu_free"))
        free_cpu = int_value(node.get("cpu_free"))
        if free_gpu >= 3 and free_cpu > 0:
            return True
    return False


def wide_gpu_candidates(nodes: list[dict[str, Any]], args: argparse.Namespace) -> list[Candidate]:
    if not wide_gpu_enabled(nodes, args):
        return []
    policy = effective_cpu_policy(args)
    child_limit = available_children(args)
    allow_two_gpu_fill = bool(args.gpu_first or getattr(args, "workload", "") == "gpu-fill")
    min_child_count = 2 if allow_two_gpu_fill else 3
    if child_limit < min_child_count:
        return []
    max_gpus = max(2, min(8, int_value(args.max_gpus_per_job, 8), child_limit))
    cpu_choices = sorted(parse_int_list(args.wide_cpus_per_gpu, default=[6, 4, 2]), reverse=True)
    max_node_gpus = max([int_value(node.get("gpu_total")) for node in nodes] or [max_gpus])
    max_node_cpus = max([int_value(node.get("cpu_total")) for node in nodes] or [48])
    current_free_gpu = max([int_value(node.get("gpu_free")) for node in nodes] or [0])
    current_free_cpu = max([int_value(node.get("cpu_free")) for node in nodes] or [0])
    candidates: dict[tuple[int, int], Candidate] = {}

    def add_candidate(tasks: int, cpus_per_gpu: int, note: str) -> None:
        if tasks <= 0 or cpus_per_gpu <= 0:
            return
        if tasks > max_gpus or tasks > child_limit:
            return
        start_tasks = 2 if allow_two_gpu_fill and cpus_per_gpu == 2 else 3
        if tasks < start_tasks:
            return
        key = (tasks, cpus_per_gpu)
        kind = "gpu-fill" if cpus_per_gpu == 2 else "wide"
        name = f"{kind}-{tasks}g{tasks * cpus_per_gpu}c"
        candidates[key] = Candidate(
            name,
            tasks,
            tasks * cpus_per_gpu,
            tasks,
            cpus_per_gpu,
            kind,
            emergency=cpus_per_gpu <= 4,
            note=note,
        )

    for node in nodes:
        free_gpu = int_value(node.get("gpu_free"))
        free_cpu = int_value(node.get("cpu_free"))
        max_tasks_by_gpu = min(max_gpus, free_gpu, child_limit)
        for tasks in range(2 if allow_two_gpu_fill else 3, max_tasks_by_gpu + 1):
            per_task_choices = {value for value in cpu_choices if free_cpu >= tasks * value}
            if policy in {"balanced", "cpu-fill"} and free_cpu >= tasks * 2:
                fill_cap = 16 if policy == "cpu-fill" else 6
                fill_cpt = min(fill_cap, free_cpu // tasks)
                total_cpu = tasks * fill_cpt
                if policy == "cpu-fill" or total_cpu in TESTED_TOTAL_CPUS:
                    per_task_choices.add(fill_cpt)
            for cpus_per_gpu in per_task_choices:
                note = (
                    "single allocation using the largest fitting same-node CPU shape"
                    if cpus_per_gpu not in cpu_choices
                    else (
                        "single allocation for multiple one-GPU children"
                        if cpus_per_gpu > 2
                        else "explicit GPU-first fragment"
                    )
                )
                add_candidate(tasks, cpus_per_gpu, note)

    # For --test-only-probe, also include theoretical per-node shapes that fit
    # a full node even if the current snapshot cannot run them immediately.
    if args.test_only_probe:
        max_tasks_by_gpu = min(max_gpus, max_node_gpus, child_limit)
        for tasks in range(2 if allow_two_gpu_fill else 3, max_tasks_by_gpu + 1):
            per_task_choices = {value for value in cpu_choices if max_node_cpus >= tasks * value}
            if policy in {"balanced", "cpu-fill"} and max_node_cpus >= tasks * 2:
                fill_cap = 16 if policy == "cpu-fill" else 6
                fill_cpt = min(fill_cap, max_node_cpus // tasks)
                total_cpu = tasks * fill_cpt
                if policy == "cpu-fill" or total_cpu in TESTED_TOTAL_CPUS:
                    per_task_choices.add(fill_cpt)
            for cpus_per_gpu in per_task_choices:
                if (tasks, cpus_per_gpu) in candidates:
                    continue
                add_candidate(tasks, cpus_per_gpu, "test-only future-start candidate")

    # Avoid surfacing low-CPU wide candidates when the current cluster has no
    # GPU fragment pressure and the caller did not ask for test-only evidence.
    if not args.test_only_probe and not args.gpu_first and current_free_gpu < 3:
        candidates = {
            key: value
            for key, value in candidates.items()
            if value.kind == "wide" and value.cpus_per_task >= 4 and current_free_cpu >= value.cpus
        }

    return sorted(
        candidates.values(),
        key=lambda item: (item.gpus, item.cpus_per_task),
        reverse=True,
    )


def gpu_fill_candidates(nodes: list[dict[str, Any]], enabled: bool) -> list[Candidate]:
    if not enabled:
        return []
    args = argparse.Namespace(
        wide_gpu_policy="always",
        gpu_first=True,
        cpu_policy="gpu-dense",
        prefer_cpu=False,
        test_only_probe=False,
        max_gpus_per_job=8,
        wide_cpus_per_gpu="2",
        available_children=8,
        child_manifest=None,
    )
    return [
        candidate
        for candidate in wide_gpu_candidates(nodes, args)
        if candidate.kind == "gpu-fill"
    ]


def load_history_stats(path: Path, days: int) -> dict[str, ShapeStats]:
    path = path.expanduser()
    stats: dict[str, ShapeStats] = {}
    if not path.exists():
        return dict(PRIOR_STATS)

    cutoff = datetime.now() - timedelta(days=days)
    grouped: dict[str, list[dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "job_resource_sample":
                continue
            collected = parse_time(record.get("collected_at_local"))
            if collected and collected < cutoff:
                continue
            job_id = str(record.get("job_id") or "")
            if not job_id:
                continue
            grouped.setdefault(job_id, []).append(record)

    for records in grouped.values():
        records.sort(key=lambda item: str(item.get("collected_at_local") or ""))
        latest = records[-1]
        key = resources_shape_key(latest.get("requested") or {})
        if not key:
            continue
        item = stats.setdefault(key, ShapeStats())
        item.jobs += 1
        ever_running = False
        first_submit: datetime | None = None
        first_start: datetime | None = None
        for record in records:
            state = str(record.get("state") or "").upper()
            if state in RUNNING_STATES or state in {"COMPLETED", "COMPLETE", "CD"}:
                ever_running = True
            timing = record.get("timing") or {}
            submit = parse_time(timing.get("submit_time"))
            start = parse_time(timing.get("start_time"))
            if submit and (first_submit is None or submit < first_submit):
                first_submit = submit
            if start and (first_start is None or start < first_start):
                first_start = start
        if ever_running:
            item.ever_running += 1
        latest_state = str(latest.get("state") or "").upper()
        latest_reason = clean_reason(latest.get("reason")).upper()
        if latest_state in PENDING_STATES and ("RESOURCE" in latest_reason or "RESERVATION" in latest_reason):
            item.resource_pending += 1
        if first_submit and first_start and first_start >= first_submit:
            item.waits_minutes.append((first_start - first_submit).total_seconds() / 60.0)

    merged = dict(PRIOR_STATS)
    merged.update(stats)
    return merged


def node_is_usable(node: dict[str, Any]) -> bool:
    state = str(node.get("state") or "").upper().replace("*", "")
    tokens = {part for part in state.replace("+", " ").replace("~", " ").split() if part}
    if tokens & BAD_NODE_STATE_TOKENS:
        return False
    return int_value(node.get("gpu_total")) > 0 and int_value(node.get("cpu_total")) > 0


def live_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cluster = payload.get("cluster_resources") or {}
    nodes = []
    for node in cluster.get("nodes") or []:
        copy = dict(node)
        copy["gpu_free"] = int_value(copy.get("gpu_free"))
        copy["cpu_free"] = int_value(copy.get("cpu_free"))
        copy["gpu_total"] = int_value(copy.get("gpu_total"))
        copy["cpu_total"] = int_value(copy.get("cpu_total"))
        copy["usable"] = node_is_usable(copy)
        nodes.append(copy)
    return nodes


def fits(node: dict[str, Any], candidate: Candidate) -> bool:
    return (
        bool(node.get("usable"))
        and int_value(node.get("gpu_free")) >= candidate.gpus
        and int_value(node.get("cpu_free")) >= candidate.cpus
    )


def has_resource_pressure(account: dict[str, Any]) -> bool:
    reasons = (account.get("summary") or {}).get("pending_reasons") or {}
    for reason in reasons:
        upper = str(reason).upper()
        if "RESOURCE" in upper or "RESERVATION" in upper:
            return True
    return False


def stranded_gpu_penalty(node: dict[str, Any], candidate: Candidate, args: argparse.Namespace) -> float:
    after_gpu = int_value(node.get("gpu_free")) - candidate.gpus
    after_cpu = int_value(node.get("cpu_free")) - candidate.cpus
    if after_gpu <= 0:
        return 0.0
    policy = effective_cpu_policy(args)
    min_cpu_per_gpu = 4
    if args.gpu_first or policy in {"balanced", "cpu-fill"}:
        min_cpu_per_gpu = 2
    penalty = 0.0
    if after_cpu <= 0:
        penalty += 240.0 * after_gpu
    if after_cpu < after_gpu * min_cpu_per_gpu:
        penalty += 120.0 * after_gpu
    if policy == "balanced":
        penalty *= 0.45
    elif policy == "cpu-fill":
        penalty *= 0.15
    return penalty


def cpu_soft_cap(candidate: Candidate, node: dict[str, Any] | None, args: argparse.Namespace) -> int:
    policy = effective_cpu_policy(args)
    if policy == "gpu-dense":
        return candidate.gpus * 4
    node_cpu_total = int_value((node or {}).get("cpu_total"), 48)
    per_child_cap = 16 if policy == "cpu-fill" else 6
    return max(1, min(node_cpu_total, candidate.gpus * per_child_cap))


def cpu_score(candidate: Candidate, node: dict[str, Any] | None, args: argparse.Namespace) -> float:
    policy = effective_cpu_policy(args)
    cap = cpu_soft_cap(candidate, node, args)
    if policy == "gpu-dense":
        return 12.0 * min(candidate.cpus, cap) + 2.0 * max(0, candidate.cpus - cap)
    if policy == "cpu-fill":
        return 24.0 * min(candidate.cpus, cap) + 12.0 * max(0, candidate.cpus - cap)
    return 18.0 * min(candidate.cpus, cap) + 6.0 * max(0, candidate.cpus - cap)


def stats_for(candidate: Candidate, stats: dict[str, ShapeStats]) -> ShapeStats:
    return stats.get(candidate.shape_key) or PRIOR_STATS.get(candidate.shape_key) or ShapeStats()


def candidate_score(
    candidate: Candidate,
    node: dict[str, Any],
    account: dict[str, Any],
    stats: dict[str, ShapeStats],
    args: argparse.Namespace,
) -> float:
    shape_stats = stats_for(candidate, stats)
    policy = effective_cpu_policy(args)
    score = 1000.0 * candidate.gpus
    score += cpu_score(candidate, node, args)
    score += 260.0 * shape_stats.start_rate
    score -= 0.25 * shape_stats.median_wait_minutes
    score -= 260.0 * shape_stats.resource_pending_rate
    score -= stranded_gpu_penalty(node, candidate, args)

    if candidate.kind == "packed":
        score += 80.0
    if candidate.name == "packed-2g12c":
        score += 180.0 if policy != "cpu-fill" else 80.0
    elif candidate.name == "packed-2g8c":
        score += 100.0 if has_resource_pressure(account) or args.gpu_first else -40.0
    elif candidate.name in {"packed-2g16c", "packed-2g24c", "packed-2g32c"}:
        score += 80.0 if policy == "cpu-fill" else -180.0
    elif candidate.kind == "single":
        score -= 40.0
    elif candidate.kind == "wide":
        if policy == "gpu-dense":
            score += 120.0 if args.gpu_first or candidate.cpus_per_task <= 4 else -40.0
        elif policy == "cpu-fill":
            score += 180.0 + 12.0 * candidate.cpus_per_task
        else:
            score += 150.0 + 8.0 * candidate.cpus_per_task
    elif candidate.kind == "gpu-fill":
        if policy == "gpu-dense":
            score += 220.0 if args.gpu_first else -200.0
        elif policy == "cpu-fill":
            score -= 80.0
        else:
            score += 40.0 if args.gpu_first else -160.0

    if candidate.emergency:
        score -= 80.0
    if args.gpu_first:
        score += 180.0 * candidate.gpus
        if policy == "gpu-dense":
            score -= 1.5 * candidate.cpus
    if has_resource_pressure(account):
        pressure_limit = 12 if policy != "cpu-fill" else 24
        score -= 14.0 * max(0, candidate.cpus - pressure_limit)
    return score


def select_immediate(
    nodes: list[dict[str, Any]],
    candidates: list[Candidate],
    account: dict[str, Any],
    stats: dict[str, ShapeStats],
    args: argparse.Namespace,
) -> tuple[Candidate, dict[str, Any], float] | None:
    best: tuple[Candidate, dict[str, Any], float] | None = None
    for candidate in candidates:
        for node in nodes:
            if not fits(node, candidate):
                continue
            score = candidate_score(candidate, node, account, stats, args)
            if best is None or score > best[2]:
                best = (candidate, node, score)
    return best


def queue_score(candidate: Candidate, account: dict[str, Any], stats: dict[str, ShapeStats], args: argparse.Namespace) -> float:
    shape_stats = stats_for(candidate, stats)
    policy = effective_cpu_policy(args)
    score = 1000.0 * candidate.gpus
    score += cpu_score(candidate, None, args)
    score += 220.0 * shape_stats.start_rate
    score -= 0.30 * shape_stats.median_wait_minutes
    score -= 280.0 * shape_stats.resource_pending_rate
    if candidate.name == "packed-2g12c":
        score += 180.0 if policy != "cpu-fill" else 80.0
    elif candidate.name == "packed-2g8c":
        score += 80.0 if has_resource_pressure(account) or args.gpu_first else -60.0
    elif candidate.name in {"packed-2g16c", "packed-2g24c", "packed-2g32c"}:
        score += 80.0 if policy == "cpu-fill" else -220.0
    elif candidate.kind == "single":
        score -= 120.0
    elif candidate.kind == "wide":
        if policy == "gpu-dense":
            score += 120.0 if args.gpu_first or candidate.cpus_per_task <= 4 else -40.0
        elif policy == "cpu-fill":
            score += 160.0 + 10.0 * candidate.cpus_per_task
        else:
            score += 130.0 + 7.0 * candidate.cpus_per_task
    elif candidate.kind == "gpu-fill":
        if policy == "gpu-dense":
            score += 80.0 if args.gpu_first else -160.0
        elif policy == "cpu-fill":
            score -= 120.0
        else:
            score += 20.0 if args.gpu_first else -160.0
    if has_resource_pressure(account):
        pressure_limit = 12 if policy != "cpu-fill" else 24
        score -= 12.0 * max(0, candidate.cpus - pressure_limit)
    return score


def low_cpu_backlog_candidate(candidate: Candidate, args: argparse.Namespace) -> bool:
    """Keep no-fit backlog guidance from drifting toward CPU-heavy shapes."""
    if effective_cpu_policy(args) == "cpu-fill":
        return True
    if candidate.kind == "packed" and candidate.gpus == 2:
        return candidate.cpus <= 12
    if candidate.kind == "single" and candidate.gpus == 1:
        return candidate.cpus <= 6
    if candidate.kind in {"wide", "gpu-fill"}:
        return candidate.cpus_per_task <= 6
    return True


def select_queue_probe(
    candidates: list[Candidate],
    account: dict[str, Any],
    stats: dict[str, ShapeStats],
    args: argparse.Namespace,
) -> tuple[Candidate, float] | None:
    if args.gpu_first or args.test_only_probe:
        allowed = candidates
    else:
        allowed = [candidate for candidate in candidates if candidate.kind != "gpu-fill"]
    if not allowed:
        return None
    best = max(allowed, key=lambda candidate: queue_score(candidate, account, stats, args))
    return best, queue_score(best, account, stats, args)


def candidate_options(
    nodes: list[dict[str, Any]],
    candidates: list[Candidate],
    account: dict[str, Any],
    stats: dict[str, ShapeStats],
    args: argparse.Namespace,
) -> list[tuple[Candidate, str, float, dict[str, Any] | None]]:
    options: list[tuple[Candidate, str, float, dict[str, Any] | None]] = []
    seen: set[str] = set()
    immediate: list[tuple[Candidate, str, float, dict[str, Any] | None]] = []
    for candidate in candidates:
        for node in nodes:
            if not fits(node, candidate):
                continue
            score = candidate_score(candidate, node, account, stats, args)
            immediate.append((candidate, "immediate", score, node))
    for item in sorted(immediate, key=lambda entry: entry[2], reverse=True):
        candidate = item[0]
        if candidate.shape_key in seen:
            continue
        seen.add(candidate.shape_key)
        options.append(item)

    if args.gpu_first or args.test_only_probe:
        queue_allowed = candidates
    else:
        queue_allowed = [candidate for candidate in candidates if candidate.kind != "gpu-fill"]
    if not immediate:
        queue_allowed = [candidate for candidate in queue_allowed if low_cpu_backlog_candidate(candidate, args)]
    queue_items = [
        (candidate, "queue_probe", queue_score(candidate, account, stats, args), None)
        for candidate in queue_allowed
        if candidate.shape_key not in seen
    ]
    options.extend(sorted(queue_items, key=lambda entry: entry[2], reverse=True))
    return options


def account_rows(payload: dict[str, Any], accounts_arg: str | None) -> list[dict[str, Any]]:
    rows = payload.get("accounts") or []
    if not accounts_arg:
        return rows
    wanted = {item.strip() for item in accounts_arg.split(",") if item.strip()}
    return [row for row in rows if row.get("name") in wanted]


def pending_shapes(account: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in account.get("jobs") or []:
        if str(job.get("state") or "").upper() not in PENDING_STATES:
            continue
        key = job_resource_shape_key(job) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def running_shapes(account: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in account.get("jobs") or []:
        if str(job.get("state") or "").upper() not in RUNNING_STATES:
            continue
        key = job_resource_shape_key(job) or "unknown"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def running_resource_totals(account: dict[str, Any]) -> tuple[int, int]:
    summary = account.get("summary") or {}
    summary_gpus = int_value(summary.get("running_gpus"), -1)
    summary_cpus = int_value(summary.get("running_cpus"), -1)
    if summary_gpus >= 0 and summary_cpus >= 0:
        return summary_gpus, summary_cpus

    total_gpus = 0
    total_cpus = 0
    for job in account.get("jobs") or []:
        if str(job.get("state") or "").upper() not in RUNNING_STATES:
            continue
        resources = job.get("resources") or {}
        total_gpus += int_value(resources.get("gpu_count"))
        total_cpus += int_value(resources.get("num_cpus"))
    return total_gpus, total_cpus


def one_gpu_running_jobs(account: dict[str, Any]) -> list[str]:
    result: list[str] = []
    for job in account.get("jobs") or []:
        if str(job.get("state") or "").upper() not in RUNNING_STATES:
            continue
        resources = job.get("resources") or {}
        if int_value(resources.get("gpu_count")) == 1:
            result.append(str(job.get("job_id") or job.get("name") or "unknown"))
    return result


def dependency_pending_count(account: dict[str, Any]) -> int:
    total = 0
    for job in account.get("jobs") or []:
        if str(job.get("state") or "").upper() not in PENDING_STATES:
            continue
        reason = clean_reason(job.get("reason")).upper()
        native_reason = clean_reason((job.get("native") or {}).get("reason")).upper()
        if "DEPEND" in reason or "DEPEND" in native_reason:
            total += 1
    return total


def account_diagnostics(
    account: dict[str, Any],
    *,
    running: int,
    total: int,
    cap_open: int,
    args: argparse.Namespace,
    cluster_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    running_gpus, running_cpus = running_resource_totals(account)
    expected_gpus = args.run_slots * 2
    singleton_jobs = one_gpu_running_jobs(account)
    run_slots_open = max(0, args.run_slots - running)
    cluster_gpu_free = int_value(cluster_summary.get("gpu_free"))

    if running >= args.run_slots and running_gpus < expected_gpus:
        diagnostics.append(
            {
                "type": "slot_fragmentation",
                "severity": "warning",
                "running_gpus": running_gpus,
                "running_cpus": running_cpus,
                "expected_running_gpus": expected_gpus,
                "one_gpu_running_jobs": singleton_jobs,
                "message": (
                    "running job slots are full, but at least one slot is a 1GPU singleton; "
                    "future refills should prefer packed/wide jobs so each running slot carries more GPUs"
                ),
            }
        )

    dependency_count = dependency_pending_count(account)
    if dependency_count:
        diagnostics.append(
            {
                "type": "dependency_held_followups",
                "severity": "info",
                "pending_dependency_jobs": dependency_count,
                "message": (
                    "dependency-held follow-ups count toward the account cap but cannot exploit an early "
                    "free run slot unless the dependency is satisfied; use dependencies only for strict ordering"
                ),
            }
        )

    if cap_open <= 0 and cluster_gpu_free > 0:
        diagnostics.append(
            {
                "type": "full_account_with_cluster_free_gpu",
                "severity": "info",
                "cluster_gpu_free": cluster_gpu_free,
                "run_slots_open": run_slots_open,
                "message": (
                    "the account has reached the configured non-terminal job cap; do not submit another job. "
                    "If utilization is low, fix future job shape/packing rather than adding more jobs"
                ),
            }
        )

    if total >= args.cap and running < args.run_slots:
        diagnostics.append(
            {
                "type": "cap_full_but_run_slots_open",
                "severity": "warning",
                "run_slots_open": run_slots_open,
                "message": (
                    "the account is at job cap but has fewer running jobs than expected; inspect pending reasons "
                    "before replacing jobs or changing CPU/GPU shape"
                ),
            }
        )

    return diagnostics


def cluster_diagnostics(nodes: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for node in nodes:
        if not node.get("usable"):
            continue
        free_gpus = int_value(node.get("gpu_free"))
        free_cpus = int_value(node.get("cpu_free"))
        if free_gpus <= 0:
            continue
        max_gpu_fill_2cpt = min(free_gpus, free_cpus // 2, args.max_gpus_per_job)
        diagnostics.append(
            {
                "node": node.get("name"),
                "free_gpus": free_gpus,
                "free_cpus": free_cpus,
                "fits_2g12c": free_gpus >= 2 and free_cpus >= 12,
                "fits_2g16c": free_gpus >= 2 and free_cpus >= 16,
                "fits_2g8c": free_gpus >= 2 and free_cpus >= 8,
                "gpu_fill_2cpt_max_gpus": max_gpu_fill_2cpt,
                "message": (
                    "same-node fragment can run packed or GPU-fill work"
                    if max_gpu_fill_2cpt >= 2 or (free_gpus >= 2 and free_cpus >= 8)
                    else "GPU is stranded by insufficient same-node CPU for allowed fallback shapes"
                ),
            }
        )
    return diagnostics


def recommendation_record(
    slot_index: int,
    candidate: Candidate,
    mode: str,
    reason: str,
    score: float,
    node: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record = {
        "slot_index": slot_index,
        "mode": mode,
        "shape": candidate.name,
        "kind": candidate.kind,
        "requested": {
            "gpus": candidate.gpus,
            "cpus": candidate.cpus,
            "tasks": candidate.tasks,
            "cpus_per_task": candidate.cpus_per_task,
            "shape_key": candidate.shape_key,
        },
        "sbatch_flags": candidate.sbatch_flags,
        "requires_sbatch_test_only": True,
        "score": round(score, 2),
        "reason": reason,
        "note": candidate.note,
    }
    if node is not None:
        record["selected_node"] = {
            "name": node.get("name"),
            "gpu_free_before": int_value(node.get("gpu_free")),
            "cpu_free_before": int_value(node.get("cpu_free")),
            "gpu_free_after": int_value(node.get("gpu_free")) - candidate.gpus,
            "cpu_free_after": int_value(node.get("cpu_free")) - candidate.cpus,
        }
    return record


def consume_node(node: dict[str, Any], candidate: Candidate) -> None:
    node["gpu_free"] = int_value(node.get("gpu_free")) - candidate.gpus
    node["cpu_free"] = int_value(node.get("cpu_free")) - candidate.cpus


def ranked_actions(account_plans: list[dict[str, Any]], *, immediate_only: bool = False) -> list[dict[str, Any]]:
    ranked: list[tuple[float, str, str, dict[str, Any]]] = []
    mode_rank = {"immediate": 10_000.0, "queue_probe": 1_000.0}
    for account in account_plans:
        current = account.get("current") or {}
        pending_reasons = current.get("pending_reasons") or {}
        qos_blocked = any("QOS" in str(reason).upper() for reason in pending_reasons)
        for rec in account.get("recommendations") or []:
            mode = rec.get("mode")
            if mode not in mode_rank:
                continue
            if immediate_only and mode != "immediate":
                continue
            requested = rec.get("requested") or {}
            account_bonus = 100.0 * int_value(current.get("run_slots_open"))
            account_bonus += 20.0 * int_value(current.get("cap_open"))
            if qos_blocked and int_value(current.get("run_slots_open")) <= 0:
                account_bonus -= 80.0
            rank = mode_rank[mode] + float(rec.get("score") or 0) + account_bonus
            action = {
                "account": account.get("name"),
                "cluster_account": account.get("cluster_account"),
                "recommendation": rec,
                "current": current,
                "rank": round(rank, 2),
                "submit_then_refresh": True,
            }
            ranked.append(
                (
                    rank,
                    str(account.get("name") or ""),
                    str(rec.get("shape") or ""),
                    action,
                )
            )
    return [item[3] for item in sorted(ranked, key=lambda item: (-item[0], item[1], item[2]))]


def select_next_action(account_plans: list[dict[str, Any]]) -> dict[str, Any] | None:
    ranked = ranked_actions(account_plans)
    return ranked[0] if ranked else None


def shared_limit_ref(account: dict[str, Any]) -> str:
    summary = account.get("summary") or {}
    return str(summary.get("shared_limit_ref") or account.get("shared_limit_ref") or "").strip()


def shared_limit_is_blocked(account: dict[str, Any]) -> bool:
    summary = account.get("summary") or {}
    return bool(summary.get("shared_limit_blocked") is True or account.get("shared_limit_blocked") is True)


def load_probe_connection(account: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    winscp_args = argparse.Namespace(
        cluster=account.get("cluster") or winscp.DEFAULT_CLUSTER,
        account=account.get("account") or winscp.DEFAULT_ACCOUNT,
        portal_user=account.get("portal_user") or winscp.DEFAULT_PORTAL_USER,
        auth_account=account.get("name"),
        token=None,
        token_file=args.token_file,
        refresh_token=False,
        refresh_browser="playwright",
        refresh_headless=True,
        show_secret=False,
    )
    token = winscp.load_auth(winscp_args)
    return winscp.run(winscp_args, token)


def connect_probe_ssh(info: dict[str, Any], timeout: int) -> paramiko.SSHClient:
    host, port_text = info["proxy"].rsplit(":", 1)
    username = f"{info['cluster']},{info['account']}"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(port_text),
        username=username,
        password=info["certificate"],
        timeout=timeout,
        banner_timeout=timeout,
        auth_timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run_remote(client: paramiko.SSHClient, command: str, timeout: int) -> tuple[int, str, str]:
    stdin, stdout, stderr = client.exec_command(f"bash -lc {shlex.quote(command)}", timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


SBATCH_MANAGED_KEYS = {
    "--partition",
    "--nodes",
    "--ntasks",
    "--cpus-per-task",
    "--gres",
    "--gres-flags",
}
THREAD_ENV_NAMES = {"OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"}


def sbatch_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("#SBATCH"):
        return None
    parts = stripped.split(maxsplit=1)
    if len(parts) < 2:
        return None
    flag = parts[1].strip()
    for key in SBATCH_MANAGED_KEYS:
        if flag == key or flag.startswith(key + "=") or flag.startswith(key + " "):
            return key
    return None


def managed_sbatch_lines(candidate: Candidate, partition: str) -> list[str]:
    return [
        f"#SBATCH --partition={partition}",
        "#SBATCH --nodes=1",
        f"#SBATCH --ntasks={candidate.tasks}",
        f"#SBATCH --cpus-per-task={candidate.cpus_per_task}",
        f"#SBATCH --gres=gpu:{candidate.gpus}",
        "#SBATCH --gres-flags=disable-binding",
    ]


def rewrite_thread_env(line: str, candidate: Candidate) -> str | None:
    match = re.match(r"^(\s*)(?:export\s+)?([A-Z0-9_]+)=", line)
    if not match or match.group(2) not in THREAD_ENV_NAMES:
        return None
    return f'{match.group(1)}export {match.group(2)}="${{SLURM_CPUS_PER_TASK:-{candidate.cpus_per_task}}}"'


def insert_after_shebang(lines: list[str], insert: list[str]) -> list[str]:
    if lines and lines[0].startswith("#!"):
        return [lines[0], *insert, *lines[1:]]
    return [*insert, *lines]


def rewrite_sbatch_template(template_text: str, candidate: Candidate, partition: str) -> str:
    kept: list[str] = []
    inserted = False
    managed = managed_sbatch_lines(candidate, partition)
    for raw_line in template_text.splitlines():
        thread_line = rewrite_thread_env(raw_line, candidate)
        if thread_line is not None:
            kept.append(thread_line)
            continue
        if sbatch_key(raw_line) in SBATCH_MANAGED_KEYS:
            if not inserted:
                kept.extend(managed)
                inserted = True
            continue
        kept.append(raw_line)
    if not inserted:
        kept = insert_after_shebang(kept, managed)
    return "\n".join(kept).rstrip() + "\n"


def load_probe_template(args: argparse.Namespace) -> str | None:
    probe_script_path = getattr(args, "probe_script", None)
    if not probe_script_path:
        return None
    return probe_script_path.expanduser().read_text(encoding="utf-8")


def probe_script(candidate: Candidate, partition: str, template_text: str | None = None) -> str:
    if template_text is not None:
        return rewrite_sbatch_template(template_text, candidate, partition)
    return "\n".join(
        [
            "#!/bin/bash",
            f"#SBATCH --partition={partition}",
            "#SBATCH --nodes=1",
            f"#SBATCH --ntasks={candidate.tasks}",
            f"#SBATCH --cpus-per-task={candidate.cpus_per_task}",
            f"#SBATCH --gres=gpu:{candidate.gpus}",
            "#SBATCH --gres-flags=disable-binding",
            "#SBATCH --time=00:05:00",
            "#SBATCH --job-name=hpc-plan-probe",
            "exit 0",
            "",
        ]
    )


def probe_command(candidate: Candidate, partition: str, template_text: str | None = None) -> str:
    remote_path = f"/tmp/bjtu_hpc_plan_{os.getpid()}_{candidate.name}.sbatch"
    script = probe_script(candidate, partition, template_text)
    return (
        f"cat > {shlex.quote(remote_path)} <<'__BJTU_HPC_PLAN__'\n"
        f"{script}"
        "__BJTU_HPC_PLAN__\n"
        f"bash -n {shlex.quote(remote_path)} && sbatch --test-only {shlex.quote(remote_path)}; "
        "_rc=$?; "
        f"rm -f {shlex.quote(remote_path)}; "
        "exit $_rc"
    )


def parse_test_only_result(
    *,
    candidate: Candidate,
    mode: str,
    rc: int,
    stdout: str,
    stderr: str,
    checked_at: datetime | None,
    immediate_window_seconds: int,
    probe_script_kind: str,
    probe_script_source: str | None,
) -> dict[str, Any]:
    text = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part)
    start_time = None
    match = TEST_ONLY_START_RE.search(text)
    if match:
        start_time = match.group(1)
    start_dt = parse_time(start_time)
    immediate = False
    wait_seconds = None
    if rc == 0 and checked_at and start_dt:
        wait_seconds = max(0.0, (start_dt - checked_at).total_seconds())
        immediate = wait_seconds <= immediate_window_seconds
    elif rc == 0 and not start_dt:
        immediate = False
    return {
        "shape": candidate.name,
        "mode_before_probe": mode,
        "requested": {
            "gpus": candidate.gpus,
            "cpus": candidate.cpus,
            "tasks": candidate.tasks,
            "cpus_per_task": candidate.cpus_per_task,
            "shape_key": candidate.shape_key,
        },
        "sbatch_flags": candidate.sbatch_flags,
        "returncode": rc,
        "accepted": rc == 0,
        "immediate": immediate,
        "estimated_start_time": start_time,
        "estimated_wait_seconds": wait_seconds,
        "probe_script_kind": probe_script_kind,
        "probe_script_source": probe_script_source,
        "requires_exact_script_test_only": probe_script_kind != "rewritten_template",
        "output": text[-1000:],
    }


def candidate_from_requested(name: str, kind: str, requested: dict[str, Any], note: str = "") -> Candidate:
    return Candidate(
        name=name,
        gpus=int_value(requested.get("gpus")),
        cpus=int_value(requested.get("cpus")),
        tasks=int_value(requested.get("tasks")),
        cpus_per_task=int_value(requested.get("cpus_per_task")),
        kind=kind,
        emergency=int_value(requested.get("cpus_per_task")) <= 4,
        note=note,
    )


def kind_from_shape(shape: Any, requested: dict[str, Any], fallback: Any) -> str:
    text = str(shape or "")
    if text.startswith("gpu-fill"):
        return "gpu-fill"
    if text.startswith("wide"):
        return "wide"
    if int_value(requested.get("gpus")) > 2:
        return "wide"
    return str(fallback or "candidate")


def mark_queue_probe_backlog_only(
    report: dict[str, Any],
    reason: str = QUEUE_PROBE_BACKLOG_REASON,
) -> None:
    next_action = report.get("next_action")
    if not next_action:
        return
    recommendation = next_action.get("recommendation") or {}
    recommendation["do_not_submit"] = True
    recommendation["reason"] = reason
    next_action["do_not_submit"] = True
    next_action["submit_then_refresh"] = False
    next_action["queue_probe_backlog_only"] = True
    totals = report.get("totals") or {}
    totals["submissions_to_do_now"] = 0
    totals["immediate_jobs"] = 0
    totals["requested_gpus"] = 0
    totals["requested_cpus"] = 0


def apply_test_only_probe(report: dict[str, Any], payload: dict[str, Any], args: argparse.Namespace) -> None:
    next_action = report.get("next_action")
    if not next_action:
        return
    account_name = next_action.get("account")
    account = next((item for item in payload.get("accounts") or [] if item.get("name") == account_name), None)
    if not account:
        next_action["test_only_probe"] = {"error": f"account {account_name!r} not found in payload"}
        if (next_action.get("recommendation") or {}).get("mode") == "queue_probe":
            mark_queue_probe_backlog_only(report, "queue_probe test-only probe failed: account not found in payload")
        return
    alternatives = (next_action.get("recommendation") or {}).get("alternatives") or [next_action.get("recommendation")]
    checked_at = parse_time(report.get("checked_at_local"))
    probed: list[dict[str, Any]] = []
    try:
        template_text = load_probe_template(args)
        template_source = str(args.probe_script.expanduser()) if args.probe_script else None
        probe_script_kind = "rewritten_template" if template_text is not None else "resource_only"
        info = load_probe_connection(account, args)
        client = connect_probe_ssh(info, args.timeout)
        try:
            for alt in alternatives[: args.test_only_candidates]:
                if not alt:
                    continue
                requested = alt.get("requested") or {}
                candidate = candidate_from_requested(
                    str(alt.get("shape") or requested.get("shape_key") or "candidate"),
                        str(alt.get("kind") or "candidate"),
                        requested,
                        str(alt.get("note") or ""),
                    )
                rc, out, err = run_remote(client, probe_command(candidate, args.partition, template_text), args.timeout)
                probed.append(
                    parse_test_only_result(
                        candidate=candidate,
                        mode=str(alt.get("mode") or "probe"),
                        rc=rc,
                        stdout=out,
                        stderr=err,
                        checked_at=checked_at,
                        immediate_window_seconds=args.immediate_window_seconds,
                        probe_script_kind=probe_script_kind,
                        probe_script_source=template_source,
                    )
                )
        finally:
            client.close()
    except Exception as error:
        next_action["test_only_probe"] = {"error": str(error)}
        if (next_action.get("recommendation") or {}).get("mode") == "queue_probe":
            mark_queue_probe_backlog_only(report, f"queue_probe test-only probe failed: {error}")
        return

    accepted = [item for item in probed if item.get("accepted")]
    immediate = [item for item in accepted if item.get("immediate")]
    if immediate:
        selected_probe = max(immediate, key=lambda item: (int_value(item["requested"].get("gpus")), int_value(item["requested"].get("cpus"))))
        selection_reason = "largest immediate sbatch --test-only candidate"
    elif accepted:
        selected_probe = min(
            accepted,
            key=lambda item: (
                item.get("estimated_wait_seconds") is None,
                item.get("estimated_wait_seconds") if item.get("estimated_wait_seconds") is not None else float("inf"),
                -int_value(item["requested"].get("gpus")),
                int_value(item["requested"].get("cpus")),
            ),
        )
        selection_reason = "earliest accepted sbatch --test-only start estimate"
    else:
        selected_probe = None
        selection_reason = "no candidate accepted by sbatch --test-only"

    next_action["test_only_probe"] = {
        "probed": probed,
        "selection_reason": selection_reason,
        "selected": selected_probe,
    }
    if selected_probe:
        selected_requested = selected_probe.get("requested") or {}
        selected_script_path = None
        if args.write_selected_script and template_text is not None:
            selected_candidate = candidate_from_requested(
                str(selected_probe.get("shape") or selected_requested.get("shape_key") or "candidate"),
                kind_from_shape(selected_probe.get("shape"), selected_requested, "candidate"),
                selected_requested,
            )
            selected_script_path = str(args.write_selected_script.expanduser())
            args.write_selected_script.expanduser().write_text(
                rewrite_sbatch_template(template_text, selected_candidate, args.partition),
                encoding="utf-8",
            )
        recommendation = next_action.get("recommendation") or {}
        recommendation.update(
            {
                "mode": "test_only_immediate" if selected_probe.get("immediate") else "test_only_earliest",
                "shape": selected_probe.get("shape"),
                "kind": kind_from_shape(selected_probe.get("shape"), selected_requested, recommendation.get("kind")),
                "requested": selected_requested,
                "sbatch_flags": selected_probe.get("sbatch_flags"),
                "reason": selection_reason,
                "probe_script_kind": selected_probe.get("probe_script_kind"),
                "probe_script_source": selected_probe.get("probe_script_source"),
                "requires_exact_script_test_only": selected_probe.get("requires_exact_script_test_only"),
                "selected_probe_script": selected_script_path,
            }
        )
        if selected_script_path:
            selected_probe["selected_probe_script"] = selected_script_path
        totals = report.get("totals") or {}
        totals["requested_gpus"] = int_value(selected_requested.get("gpus"))
        totals["requested_cpus"] = int_value(selected_requested.get("cpus"))
    else:
        recommendation = next_action.get("recommendation") or {}
        recommendation.update(
            {
                "mode": "test_only_rejected",
                "reason": selection_reason,
                "do_not_submit": True,
            }
        )
        next_action["do_not_submit"] = True
        totals = report.get("totals") or {}
        totals["submissions_to_do_now"] = 0
        totals["immediate_jobs"] = 0
        totals["queued_probe_jobs"] = 0
        totals["requested_gpus"] = 0
        totals["requested_cpus"] = 0


def plan(payload: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    nodes = live_nodes(payload)
    stats = load_history_stats(args.history_log, args.history_days)
    base_candidates = candidate_family(args.workload, args.gpu_first, effective_cpu_policy(args))
    candidates = base_candidates + wide_gpu_candidates(nodes, args)
    cluster_summary = ((payload.get("cluster_resources") or {}).get("summary") or {})
    accounts = account_rows(payload, args.accounts)
    # Fill emptier accounts first. In sequential mode this is display order; the
    # next_action selector still chooses one concrete submit target.
    accounts = sorted(
        accounts,
        key=lambda item: (
            int_value((item.get("summary") or {}).get("total")),
            int_value((item.get("summary") or {}).get("running")),
            str(item.get("name") or ""),
        ),
    )
    direct_start = args.admission_mode == "direct-start"
    effective_running_cap = max(0, min(args.cap, args.run_slots))
    shared_blocked_refs = {
        shared_limit_ref(account)
        for account in accounts
        if shared_limit_ref(account) and shared_limit_is_blocked(account)
    }

    account_plans: list[dict[str, Any]] = []
    totals = {
        "new_jobs": 0,
        "immediate_jobs": 0,
        "queued_probe_jobs": 0,
        "hold_jobs": 0,
        "submissions_to_do_now": 0,
        "requested_gpus": 0,
        "requested_cpus": 0,
        "admission_candidates": 0,
    }
    sequential = args.submit_mode == "sequential"

    for account in accounts:
        summary = account.get("summary") or {}
        total = int_value(summary.get("total"), len(account.get("jobs") or []))
        running = int_value(summary.get("running"))
        pending = int_value(summary.get("pending"))
        cap_open = max(0, args.cap - total)
        run_slots_open = max(0, effective_running_cap - running) if direct_start else max(0, args.run_slots - running)
        account_shared_ref = shared_limit_ref(account)
        account_plan = {
            "name": account.get("name"),
            "cluster_account": account.get("account"),
            "status": "ok",
            "current": {
                "running": running,
                "pending": pending,
                "total": total,
                "cap": args.cap,
                "cap_open": cap_open,
                "run_slots": args.run_slots,
                "run_slots_open": run_slots_open,
                "admission_mode": args.admission_mode,
                "shared_limit_ref": account_shared_ref or None,
                "pending_reasons": summary.get("pending_reasons") or {},
                "running_shapes": running_shapes(account),
                "pending_shapes": pending_shapes(account),
            },
            "recommendations": [],
        }
        account_plan["diagnostics"] = account_diagnostics(
            account,
            running=running,
            total=total,
            cap_open=cap_open,
            args=args,
            cluster_summary=cluster_summary,
        )

        if account.get("error"):
            account_plan["status"] = "error"
            account_plan["reason"] = account.get("error")
            account_plans.append(account_plan)
            continue
        if direct_start and account_shared_ref and account_shared_ref in shared_blocked_refs:
            account_plan["status"] = "blocked_shared_limit"
            account_plan["reason"] = f"live evidence marks shared limit {account_shared_ref} blocked"
            account_plans.append(account_plan)
            continue
        if direct_start and pending > 0:
            account_plan["status"] = "blocked_pending"
            account_plan["reason"] = "this account has pending work; direct-start admission is blocked only for this account"
            account_plans.append(account_plan)
            continue
        if direct_start and total > running:
            account_plan["status"] = "blocked_nonrunning_nonterminal"
            account_plan["reason"] = "this account has non-running non-terminal work not represented as pending"
            account_plans.append(account_plan)
            continue
        if direct_start and run_slots_open <= 0:
            account_plan["status"] = "full"
            account_plan["reason"] = "account has reached the configured direct-running cap"
            account_plans.append(account_plan)
            continue
        if cap_open <= 0:
            account_plan["status"] = "full"
            account_plan["reason"] = "account already has target non-terminal job count"
            account_plans.append(account_plan)
            continue

        if direct_start:
            slots_to_plan = min(cap_open, run_slots_open, 1)
        else:
            slots_to_plan = min(cap_open, 1) if sequential else cap_open
        for slot_offset in range(slots_to_plan):
            slot_index = total + slot_offset + 1
            options = candidate_options(nodes, candidates, account, stats, args)
            if args.no_queued or direct_start:
                options = [option for option in options if option[1] == "immediate"]
            if not options:
                account_plan["recommendations"].append(
                    {
                        "slot_index": slot_index,
                        "mode": "hold",
                        "reason": "no non-reserved node currently has same-node free GPU/CPU for any candidate",
                    }
                )
                totals["hold_jobs"] += 1
                continue

            candidate, mode, score, node = options[0]
            reason = (
                "same-node free GPU/CPU fits now; still run sbatch --test-only before submit"
                if mode == "immediate"
                else (
                    "account is below cap, but no candidate fits current same-node resources; "
                    "use this as the next sbatch --test-only shape, not as proof of immediate start"
                )
            )
            record = recommendation_record(slot_index, candidate, mode, reason, score, node)
            queue_probe_backlog_only = (
                mode == "queue_probe"
                and not args.allow_queued_submit
                and not args.test_only_probe
            )
            if queue_probe_backlog_only:
                record["do_not_submit"] = True
                record["reason"] = QUEUE_PROBE_BACKLOG_REASON
            alternatives = []
            for alt_candidate, alt_mode, alt_score, alt_node in options[: args.test_only_candidates]:
                alt_record = recommendation_record(slot_index, alt_candidate, alt_mode, reason, alt_score, alt_node)
                if (
                    alt_mode == "queue_probe"
                    and not args.allow_queued_submit
                    and not args.test_only_probe
                ):
                    alt_record["do_not_submit"] = True
                    alt_record["reason"] = QUEUE_PROBE_BACKLOG_REASON
                alternatives.append(alt_record)
            record["alternatives"] = alternatives
            account_plan["recommendations"].append(record)
            if mode == "immediate" and not sequential and not direct_start and node is not None:
                consume_node(node, candidate)
            totals["new_jobs"] += 1
            if mode == "immediate":
                totals["immediate_jobs"] += 1
            else:
                totals["queued_probe_jobs"] += 1
            totals["requested_gpus"] += candidate.gpus
            totals["requested_cpus"] += candidate.cpus

        account_plans.append(account_plan)

    admission_frontier: list[dict[str, Any]] = []
    if direct_start:
        admission_frontier = ranked_actions(account_plans, immediate_only=True)[: args.max_admissions_per_cycle]
        for index, action in enumerate(admission_frontier):
            action["admission_index"] = index + 1
            action["requires_refresh_before_submit"] = index > 0
            action["requires_exact_script_preflight"] = True
            action["do_not_batch_submit"] = True
            action["snapshot_checked_at_local"] = payload.get("checked_at_local")
        next_action = admission_frontier[0] if admission_frontier else None
        totals["admission_candidates"] = len(admission_frontier)
    else:
        next_action = select_next_action(account_plans) if sequential else None
    if (sequential or direct_start) and next_action:
        rec = next_action.get("recommendation") or {}
        requested = rec.get("requested") or {}
        queue_probe_backlog_only = (
            rec.get("mode") == "queue_probe"
            and not args.allow_queued_submit
            and not args.test_only_probe
        )
        totals["submissions_to_do_now"] = 0 if queue_probe_backlog_only else 1
        totals["requested_gpus"] = int_value(requested.get("gpus"))
        totals["requested_cpus"] = int_value(requested.get("cpus"))
        totals["immediate_jobs"] = 1 if rec.get("mode") == "immediate" else 0
        totals["queued_probe_jobs"] = 1 if rec.get("mode") == "queue_probe" else 0
        if queue_probe_backlog_only:
            rec["do_not_submit"] = True
            rec["reason"] = QUEUE_PROBE_BACKLOG_REASON
            next_action["do_not_submit"] = True
            next_action["submit_then_refresh"] = False
            next_action["queue_probe_backlog_only"] = True
            totals["requested_gpus"] = 0
            totals["requested_cpus"] = 0

    return {
        "schema": "bjtu-hpc-resource-plan/v1",
        "checked_at_local": payload.get("checked_at_local"),
        "planner_options": {
            "cap": args.cap,
            "run_slots": args.run_slots,
            "admission_mode": args.admission_mode,
            "max_admissions_per_cycle": args.max_admissions_per_cycle,
            "workload": args.workload,
            "submit_mode": args.submit_mode,
            "gpu_first": args.gpu_first,
            "cpu_policy": effective_cpu_policy(args),
            "wide_gpu_policy": args.wide_gpu_policy,
            "max_gpus_per_job": args.max_gpus_per_job,
            "wide_cpus_per_gpu": args.wide_cpus_per_gpu,
            "available_children": available_children(args),
            "child_manifest": str(args.child_manifest.expanduser()) if args.child_manifest else None,
            "test_only_probe": args.test_only_probe,
            "probe_script": str(args.probe_script.expanduser()) if args.probe_script else None,
            "write_selected_script": str(args.write_selected_script.expanduser()) if args.write_selected_script else None,
            "prefer_cpu": args.prefer_cpu,
            "allow_queued_probe": not args.no_queued,
            "allow_queued_submit": args.allow_queued_submit,
            "history_log": str(args.history_log.expanduser()),
            "history_days": args.history_days,
        },
        "cluster_resources": payload.get("cluster_resources") or {},
        "cluster_diagnostics": cluster_diagnostics(nodes, args),
        "virtual_nodes_after_immediate_recommendations": nodes,
        "accounts": account_plans,
        "next_action": next_action,
        "admission_frontier": admission_frontier,
        "shared_blocked_limit_refs": sorted(shared_blocked_refs),
        "totals": totals,
        "shape_stats_used": {key: value.public() for key, value in sorted(stats.items())},
    }


def compact(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def print_text(report: dict[str, Any]) -> None:
    cluster = report.get("cluster_resources") or {}
    summary = cluster.get("summary") or {}
    options = report.get("planner_options") or {}
    print(f"checked_at_local: {report.get('checked_at_local')}")
    print(
        "planner_mode: "
        f"admission={options.get('admission_mode', 'queued')} "
        f"submit={options.get('submit_mode', 'sequential')} "
        f"cpu_policy={options.get('cpu_policy', 'balanced')}"
    )
    if cluster.get("error"):
        print(f"cluster_resources: error: {cluster.get('error')}")
    else:
        excluded = ",".join(cluster.get("excluded_reserved_nodes") or []) or "-"
        print(
            "cluster_resources: "
            f"GPU {summary.get('gpu_alloc', 0)}/{summary.get('gpu_total', 0)} "
            f"CPU {summary.get('cpu_alloc', 0)}/{summary.get('cpu_total', 0)} "
            f"reserved_excluded {excluded}"
        )
        for node in cluster.get("nodes") or []:
            print(
                "  "
                f"{compact(node.get('name'), 8):<8} "
                f"{compact(node.get('state'), 10):<10} "
                f"free G{node.get('gpu_free', 0):>2} C{node.get('cpu_free', 0):>2} "
                f"alloc G{node.get('gpu_alloc', 0):>2}/{node.get('gpu_total', 0):<2} "
                f"C{node.get('cpu_alloc', 0):>2}/{node.get('cpu_total', 0):<2}"
            )
        cluster_diag = report.get("cluster_diagnostics") or []
        if cluster_diag:
            print("cluster_fragments:")
            for item in cluster_diag:
                print(
                    "  "
                    f"{compact(item.get('node'), 8):<8} "
                    f"free G{item.get('free_gpus', 0):>2} C{item.get('free_cpus', 0):>2} "
                    f"2g12={str(item.get('fits_2g12c')).lower()} "
                    f"2g8={str(item.get('fits_2g8c')).lower()} "
                    f"fill2cpt={item.get('gpu_fill_2cpt_max_gpus', 0)} "
                    f"{compact(item.get('message'), 70)}"
                )

    next_action = report.get("next_action")
    if next_action:
        rec = next_action.get("recommendation") or {}
        requested = rec.get("requested") or {}
        node = rec.get("selected_node") or {}
        print()
        print(
            "next_action: "
            f"account={next_action.get('account')} "
            f"mode={rec.get('mode')} "
            f"shape={rec.get('shape')} "
            f"gpu={requested.get('gpus')} "
            f"cpu={requested.get('cpus')} "
            f"tasks={requested.get('tasks')} "
            f"cpus_per_task={requested.get('cpus_per_task')} "
            f"node={node.get('name') or '-'}"
        )
        print("next_action_flags: " + " ".join(rec.get("sbatch_flags") or []))
        probe = next_action.get("test_only_probe") or {}
        if probe.get("selected"):
            selected = probe["selected"]
            probe_kind = selected.get("probe_script_kind") or "-"
            print(
                "next_action_probe: "
                f"{probe.get('selection_reason')} "
                f"start={selected.get('estimated_start_time') or 'unknown'} "
                f"immediate={selected.get('immediate')} "
                f"script={probe_kind}"
            )
            if selected.get("requires_exact_script_test_only"):
                print("next_action_probe_note: resource-only probe; test the exact sbatch script before submit.")
            if selected.get("selected_probe_script"):
                print(f"next_action_selected_script: {selected.get('selected_probe_script')}")
        elif probe.get("probed"):
            print(f"next_action_probe: {probe.get('selection_reason')}; do_not_submit=true")
        elif probe.get("error"):
            print(f"next_action_probe_error: {compact(probe.get('error'), 120)}")
        if next_action.get("do_not_submit"):
            print("next_action_note: do not submit; refresh queue/resources or change constraints before retrying.")
        else:
            print("next_action_note: submit this one job, refresh queue/resources, then run planner again.")

    frontier = report.get("admission_frontier") or []
    if options.get("admission_mode") == "direct-start":
        print()
        print(
            "admission_frontier: "
            f"candidates={len(frontier)} "
            f"cycle_cap={options.get('max_admissions_per_cycle')} "
            "submit_authorized_now=1"
        )
        if len(frontier) > 1:
            print(
                "admission_frontier_note: entries after the first are snapshot-only candidates; "
                "refresh, replan, and preflight before each physical submit."
            )

    print()
    headers = ["account", "RUN", "PD", "TOT", "open", "mode", "shape", "G", "C", "node", "reason"]
    widths = [10, 3, 3, 3, 4, 11, 16, 2, 3, 8, 60]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for account in report.get("accounts") or []:
        current = account.get("current") or {}
        recs = account.get("recommendations") or []
        if not recs:
            diag_types = ",".join(item.get("type", "-") for item in account.get("diagnostics") or [])
            reason = account.get("reason") or "-"
            if diag_types:
                reason = f"{reason}; diag={diag_types}"
            values = [
                account.get("name"),
                current.get("running"),
                current.get("pending"),
                current.get("total"),
                current.get("cap_open"),
                account.get("status"),
                "-",
                "-",
                "-",
                "-",
                reason,
            ]
            print("  ".join(compact(value, width).ljust(width) for value, width in zip(values, widths)))
            continue
        for rec in recs:
            requested = rec.get("requested") or {}
            node = rec.get("selected_node") or {}
            values = [
                account.get("name"),
                current.get("running"),
                current.get("pending"),
                current.get("total"),
                current.get("cap_open"),
                rec.get("mode"),
                rec.get("shape"),
                requested.get("gpus"),
                requested.get("cpus"),
                node.get("name") or "-",
                rec.get("reason"),
            ]
            print("  ".join(compact(value, width).ljust(width) for value, width in zip(values, widths)))

    diagnostics = [
        (account.get("name"), item)
        for account in report.get("accounts") or []
        for item in account.get("diagnostics") or []
    ]
    if diagnostics:
        print()
        print("diagnostics:")
        for account_name, item in diagnostics:
            print(f"  {account_name}: {item.get('type')}: {compact(item.get('message'), 120)}")

    totals = report.get("totals") or {}
    print()
    if options.get("admission_mode") == "direct-start" or options.get("submit_mode") == "sequential":
        print(
            "totals: "
            f"candidate_accounts={totals.get('new_jobs', 0)} "
            f"admission_candidates={totals.get('admission_candidates', 0)} "
            f"submit_now={totals.get('submissions_to_do_now', 0)} "
            f"immediate={totals.get('immediate_jobs', 0)} "
            f"queue_probe={totals.get('queued_probe_jobs', 0)} "
            f"requested_gpu={totals.get('requested_gpus', 0)} "
            f"requested_cpu={totals.get('requested_cpus', 0)}"
        )
    else:
        print(
            "totals: "
            f"new_jobs={totals.get('new_jobs', 0)} "
            f"immediate={totals.get('immediate_jobs', 0)} "
            f"queue_probe={totals.get('queued_probe_jobs', 0)} "
            f"hold={totals.get('hold_jobs', 0)} "
            f"requested_gpu={totals.get('requested_gpus', 0)} "
            f"requested_cpu={totals.get('requested_cpus', 0)}"
        )
    if totals.get("queued_probe_jobs"):
        print("note: queue_probe entries still require sbatch --test-only on the exact sbatch script.")
    if totals.get("new_jobs", 0) == 0:
        if options.get("admission_mode") == "direct-start":
            print("note: no account has a verified direct-start candidate in this snapshot.")
        else:
            print("note: no new submissions are needed to reach the configured per-account cap.")


def main() -> int:
    args = parse_args()
    payload = load_queue_payload(args)
    report = plan(payload, args)
    if args.test_only_probe:
        apply_test_only_probe(report, payload, args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
