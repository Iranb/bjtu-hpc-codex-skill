#!/usr/bin/env python3.12
"""Fast native SLURM queue summary for saved BJTU HPC accounts."""

import argparse
import hashlib
import json
import os
import re
import shlex
import sys
import time
import warnings
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from hpc_runtime import require_native_dependencies

require_native_dependencies()

import paramiko

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*")

import hpc_winscp_info as winscp
from hpc_account_store import list_account_summaries


SQUEUE_FORMAT = "%i|%P|%j|%u|%T|%M|%l|%D|%R"
STATE_RUNNING = {"RUNNING", "R"}
STATE_PENDING = {"PENDING", "PD"}
SHARED_USER_LIMIT_REASONS = {
    "MAXJOBSPERUSER",
    "QOSMAXJOBSPERUSERLIMIT",
    "QOSMAXSUBMITJOBPERUSERLIMIT",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Summarize native SLURM queues for saved BJTU HPC auth accounts."
    )
    parser.add_argument(
        "--accounts",
        help="Comma-separated auth account names. Default: every saved account.",
    )
    parser.add_argument(
        "--partition",
        default="GPU",
        help="SLURM partition to query. Default: GPU.",
    )
    parser.add_argument(
        "--all-partitions",
        action="store_true",
        help="Do not pass -p to squeue.",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print per-job detail tables after the summary.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of tables.",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=2,
        help="Expected non-terminal job cap per auth account. Default: 2.",
    )
    parser.add_argument(
        "--run-slots",
        type=int,
        default=2,
        help="Expected running slot cap per auth account. Default: 2.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="SSH command timeout in seconds. Default: 45.",
    )
    parser.add_argument(
        "--no-cluster-resources",
        action="store_true",
        help="Skip GPU node resource and reservation summary.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=int(os.getenv("HPC_QUEUE_SUMMARY_JOBS", "4")),
        help="Maximum saved accounts to query concurrently. Default: 4.",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Query saved accounts sequentially. Useful when the portal proxy dislikes parallel SSH sessions.",
    )
    parser.add_argument(
        "--refresh-token",
        action="store_true",
        help="Attempt token refresh before querying. By default no browser is opened.",
    )
    parser.add_argument(
        "--refresh-browser",
        choices=["playwright", "chrome", "safari"],
        default="playwright",
    )
    parser.add_argument(
        "--refresh-headless",
        action="store_true",
        help="Use headless browser mode when --refresh-token is set.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=winscp.DEFAULT_TOKEN_FILE,
    )
    default_history_log = os.getenv("HPC_RESOURCE_HISTORY_LOG")
    default_history_state = os.getenv("HPC_RESOURCE_HISTORY_STATE")
    parser.add_argument(
        "--history-log",
        type=Path,
        default=Path(default_history_log).expanduser() if default_history_log else None,
        help="Append a redacted CPU/GPU request snapshot to this JSONL history file.",
    )
    parser.add_argument(
        "--history-state",
        type=Path,
        default=Path(default_history_state).expanduser() if default_history_state else None,
        help="State file used to skip unchanged history snapshots.",
    )
    parser.add_argument(
        "--history-no-dedupe",
        action="store_true",
        help="Always append history even if the queue/resource signature is unchanged.",
    )
    return parser.parse_args()


def selected_accounts(rows, accounts_arg):
    if not accounts_arg:
        return rows
    wanted = {item.strip() for item in accounts_arg.split(",") if item.strip()}
    return [row for row in rows if row.get("name") in wanted]


def build_winscp_args(row, args):
    return argparse.Namespace(
        cluster=row.get("cluster") or winscp.DEFAULT_CLUSTER,
        account=row.get("account") or winscp.DEFAULT_ACCOUNT,
        portal_user=row.get("portal_user") or winscp.DEFAULT_PORTAL_USER,
        auth_account=row.get("name"),
        token=None,
        token_file=args.token_file,
        refresh_token=args.refresh_token,
        refresh_browser=args.refresh_browser,
        refresh_headless=args.refresh_headless,
        show_secret=False,
    )


def load_connection(row, args):
    winscp_args = build_winscp_args(row, args)
    token = winscp.load_auth(winscp_args)
    try:
        return winscp.run(winscp_args, token)
    except RuntimeError as error:
        if str(error) != winscp.AUTH_ERROR_MESSAGE or not args.refresh_token:
            raise
        token = winscp.refresh_token(
            winscp_args.token_file.expanduser(),
            winscp_args.refresh_browser,
            winscp_args.refresh_headless,
            auth_account=winscp_args.auth_account,
        )
        return winscp.run(winscp_args, token)


def connect_ssh(info, timeout):
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


def queue_command(account, partition, all_partitions):
    command = "squeue -h -u " + shlex.quote(account)
    if not all_partitions and partition:
        command += " -p " + shlex.quote(partition)
    command += " -o " + shlex.quote(SQUEUE_FORMAT)
    return command


def run_remote(client, command, timeout):
    marker = "__HPC_QUEUE_SUMMARY_RC__"
    wrapped = (
        f"{command}; "
        "_hpc_queue_summary_rc=$?; "
        f"printf '\\n{marker}%s\\n' \"$_hpc_queue_summary_rc\""
    )
    stdin, stdout, stderr = client.exec_command(
        f"bash -lc {shlex.quote(wrapped)}",
        timeout=timeout,
    )
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    marker_index = out.rfind(marker)
    if marker_index >= 0:
        status_line = out[marker_index + len(marker) :].strip().splitlines()[0:1]
        if status_line and status_line[0].isdigit():
            rc = int(status_line[0])
            out = out[:marker_index].rstrip("\n")
    return rc, out, err


def parse_scontrol_fields(text):
    fields = {}
    for token in text.replace("\n", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def nodelist_parts(value):
    parts = []
    current = []
    depth = 0
    for char in str(value or ""):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        if char == "," and depth == 0:
            item = "".join(current).strip()
            if item:
                parts.append(item)
            current = []
            continue
        current.append(char)
    item = "".join(current).strip()
    if item:
        parts.append(item)
    return parts


def expand_nodelist(value):
    text = str(value or "").strip()
    if not text or text in {"(null)", "N/A"}:
        return []
    expanded = []
    for part in nodelist_parts(text):
        match = re.fullmatch(r"([^\[]+)\[([^\]]+)\](.*)", part)
        if not match:
            expanded.append(part)
            continue
        prefix, body, suffix = match.groups()
        for item in body.split(","):
            item = item.strip()
            if not item:
                continue
            if "-" not in item:
                expanded.append(f"{prefix}{item}{suffix}")
                continue
            start_text, end_text = item.split("-", 1)
            width = max(len(start_text), len(end_text))
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError:
                expanded.append(f"{prefix}{item}{suffix}")
                continue
            step = 1 if end >= start else -1
            for value in range(start, end + step, step):
                expanded.append(f"{prefix}{value:0{width}d}{suffix}")
    return expanded


def parse_tres(value):
    result = {}
    if not value:
        return result
    for item in str(value).split(","):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        result[key] = raw
    return result


def gpu_count_from_gres(value):
    text = str(value or "")
    total = 0
    for item in text.split(","):
        match = re.search(r"gpu(?::[^,()]+)*:(\d+)", item)
        if match:
            total += int(match.group(1))
    return total or None


def int_field(fields, name):
    value = fields.get(name)
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def gpu_count_from_fields(fields):
    for key in ("AllocTRES", "TRES", "ReqTRES"):
        value = parse_tres(fields.get(key)).get("gres/gpu")
        if value is not None:
            try:
                return int(value)
            except ValueError:
                pass
    tres_per_node = fields.get("TresPerNode") or fields.get("TRESPerNode")
    if tres_per_node:
        match = re.search(r"(?:gres[:/])?gpu(?::[^:=]+)?[:=](\d+)", tres_per_node)
        if match:
            return int(match.group(1))
    return None


def natural_key(value):
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(value))]


def parse_cluster_resources(node_stdout, reservation_stdout, partition):
    reserved_nodes = set()
    for line in reservation_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = parse_scontrol_fields(line)
        if fields.get("State") != "ACTIVE":
            continue
        reservation_partition = fields.get("PartitionName")
        if reservation_partition not in (None, "", "(null)") and partition and reservation_partition != partition:
            continue
        reserved_nodes.update(expand_nodelist(fields.get("Nodes")))

    nodes = []
    for line in node_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = parse_scontrol_fields(line)
        name = fields.get("NodeName")
        if not name or name in reserved_nodes:
            continue
        partitions = {item.strip() for item in str(fields.get("Partitions") or "").split(",")}
        gres = fields.get("Gres") or ""
        cfg_tres = parse_tres(fields.get("CfgTRES"))
        if partition and partition not in partitions:
            continue
        if "gpu" not in gres and "gres/gpu" not in cfg_tres:
            continue
        alloc_tres = parse_tres(fields.get("AllocTRES"))
        gpu_total = int_field({"gpu": cfg_tres.get("gres/gpu")}, "gpu") or gpu_count_from_gres(gres) or 0
        gpu_alloc = int_field({"gpu": alloc_tres.get("gres/gpu")}, "gpu") or 0
        cpu_total = int_field(fields, "CPUTot") or int_field({"cpu": cfg_tres.get("cpu")}, "cpu") or 0
        cpu_alloc = int_field(fields, "CPUAlloc") or int_field({"cpu": alloc_tres.get("cpu")}, "cpu") or 0
        nodes.append(
            {
                "name": name,
                "state": fields.get("State") or "-",
                "cpu_alloc": cpu_alloc,
                "cpu_total": cpu_total,
                "cpu_free": max(0, cpu_total - cpu_alloc),
                "gpu_alloc": gpu_alloc,
                "gpu_total": gpu_total,
                "gpu_free": max(0, gpu_total - gpu_alloc),
                "gres": gres,
            }
        )

    nodes.sort(key=lambda item: natural_key(item.get("name")))
    summary = {
        "nodes": len(nodes),
        "cpu_alloc": sum(node["cpu_alloc"] for node in nodes),
        "cpu_total": sum(node["cpu_total"] for node in nodes),
        "cpu_free": sum(node["cpu_free"] for node in nodes),
        "gpu_alloc": sum(node["gpu_alloc"] for node in nodes),
        "gpu_total": sum(node["gpu_total"] for node in nodes),
        "gpu_free": sum(node["gpu_free"] for node in nodes),
        "reserved_nodes": len(reserved_nodes),
    }
    return {
        "partition": partition,
        "error": None,
        "excluded_reserved_nodes": sorted(reserved_nodes, key=natural_key),
        "summary": summary,
        "nodes": nodes,
    }


def cluster_resource_command():
    return (
        "printf '__HPC_NODES__\\n'; "
        "scontrol show node -o; "
        "printf '\\n__HPC_RESERVATIONS__\\n'; "
        "scontrol show reservation -o"
    )


def split_cluster_resource_output(stdout):
    node_marker = "__HPC_NODES__"
    reservation_marker = "__HPC_RESERVATIONS__"
    if node_marker not in stdout or reservation_marker not in stdout:
        return stdout, ""
    after_node = stdout.split(node_marker, 1)[1]
    node_text, reservation_text = after_node.split(reservation_marker, 1)
    return node_text.strip(), reservation_text.strip()


def query_cluster_resources(rows, args, broker=None):
    if args.no_cluster_resources:
        return None
    errors = []
    for row in rows:
        if not row.get("has_token") or not row.get("account") or not row.get("cluster"):
            continue
        try:
            key = str(row.get("name") or row.get("account"))
            info = broker.connection_for(key) if broker else load_connection(row, args)
            client = broker.client_for(key) if broker else connect_ssh(info, args.timeout)
            try:
                rc, out, err = run_remote(client, cluster_resource_command(), args.timeout)
            finally:
                if broker is None:
                    client.close()
            if rc != 0:
                errors.append((err or out or f"scontrol returned {rc}").strip())
                continue
            node_text, reservation_text = split_cluster_resource_output(out)
            return parse_cluster_resources(node_text, reservation_text, args.partition)
        except Exception as error:
            errors.append(short_error(error))
    return {
        "partition": args.partition,
        "error": "; ".join(error for error in errors if error) or "no usable auth account",
        "excluded_reserved_nodes": [],
        "summary": {
            "nodes": 0,
            "cpu_alloc": 0,
            "cpu_total": 0,
            "cpu_free": 0,
            "gpu_alloc": 0,
            "gpu_total": 0,
            "gpu_free": 0,
            "reserved_nodes": 0,
        },
        "nodes": [],
    }


def scontrol_command(job_ids):
    parts = []
    for job_id in job_ids:
        safe_id = shlex.quote(str(job_id))
        parts.append(f"printf '\\n__HPC_JOB__{safe_id}\\n'; scontrol show job -o {safe_id}")
    return "; ".join(parts)


def parse_scontrol_blocks(stdout):
    blocks = {}
    current = None
    lines = []
    for line in stdout.splitlines():
        if line.startswith("__HPC_JOB__"):
            if current is not None:
                blocks[current] = "\n".join(lines).strip()
            current = line.replace("__HPC_JOB__", "", 1).strip()
            lines = []
            continue
        if current is not None:
            lines.append(line)
    if current is not None:
        blocks[current] = "\n".join(lines).strip()
    return blocks


def resource_summary(fields):
    return {
        "num_cpus": int_field(fields, "NumCPUs"),
        "num_tasks": int_field(fields, "NumTasks"),
        "cpus_per_task": int_field(fields, "CPUs/Task"),
        "gpu_count": gpu_count_from_fields(fields),
        "tres": fields.get("TRES"),
        "alloc_tres": fields.get("AllocTRES"),
        "req_tres": fields.get("ReqTRES"),
        "tres_per_node": fields.get("TresPerNode") or fields.get("TRESPerNode"),
    }


def timing_summary(fields):
    return {
        "submit_time": fields.get("SubmitTime"),
        "eligible_time": fields.get("EligibleTime"),
        "start_time": fields.get("StartTime"),
        "end_time": fields.get("EndTime"),
        "last_sched_eval": fields.get("LastSchedEval"),
    }


def native_summary(fields):
    return {
        "job_state": fields.get("JobState"),
        "reason": fields.get("Reason"),
        "qos": fields.get("QOS"),
        "priority": int_field(fields, "Priority"),
        "node_list": fields.get("NodeList"),
        "sched_node_list": fields.get("SchedNodeList"),
        "batch_host": fields.get("BatchHost"),
        "num_nodes": fields.get("NumNodes"),
        "min_cpus_node": int_field(fields, "MinCPUsNode"),
        "gres_enforce_bind": fields.get("GresEnforceBind"),
    }


def enrich_job_resources(client, jobs, timeout):
    job_ids = [
        job.get("job_id")
        for job in jobs
        if job.get("job_id")
    ]
    if not job_ids:
        return None
    rc, out, err = run_remote(client, scontrol_command(job_ids), timeout)
    if rc != 0:
        return (err or out or f"scontrol returned {rc}").strip()
    blocks = parse_scontrol_blocks(out)
    for job in jobs:
        job_id = str(job.get("job_id") or "")
        text = blocks.get(job_id)
        if not text:
            continue
        fields = parse_scontrol_fields(text)
        job["resources"] = resource_summary(fields)
        job["timing"] = timing_summary(fields)
        job["native"] = native_summary(fields)
    return None


def parse_squeue(stdout):
    jobs = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.rstrip("\n").split("|", 8)
        if len(parts) != 9:
            jobs.append({"parse_error": line})
            continue
        job_id, partition, name, user, state, elapsed, time_limit, nodes, reason = parts
        jobs.append(
            {
                "job_id": job_id.strip(),
                "partition": partition.strip(),
                "name": name.strip(),
                "user": user.strip(),
                "state": state.strip(),
                "elapsed": elapsed.strip(),
                "time_limit": time_limit.strip(),
                "nodes": nodes.strip(),
                "reason": reason.strip(),
            }
        )
    return jobs


def normalize_reason(value):
    text = str(value or "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text or "-"


def summarize_jobs(jobs, run_slots, cap):
    running = 0
    pending = 0
    other = 0
    running_cpus = 0
    running_gpus = 0
    running_resource_unknown = 0
    reasons = Counter()
    for job in jobs:
        state = str(job.get("state") or "").upper()
        if state in STATE_RUNNING:
            running += 1
            resources = job.get("resources") or {}
            num_cpus = resources.get("num_cpus")
            gpu_count = resources.get("gpu_count")
            if num_cpus is None or gpu_count is None:
                running_resource_unknown += 1
            if num_cpus is not None:
                running_cpus += num_cpus
            if gpu_count is not None:
                running_gpus += gpu_count
        elif state in STATE_PENDING:
            pending += 1
            reasons[normalize_reason(job.get("reason"))] += 1
        else:
            other += 1
    total = len(jobs)
    return {
        "running": running,
        "pending": pending,
        "other": other,
        "total": total,
        "run_slots_open": max(0, run_slots - running),
        "cap_open": max(0, cap - total),
        "pending_reasons": dict(reasons),
        "running_cpus": running_cpus,
        "running_gpus": running_gpus,
        "running_resource_unknown": running_resource_unknown,
    }


def shared_user_limit_ref(result):
    cluster = str(result.get("cluster") or "").strip()
    account = str(result.get("account") or "").strip()
    if not cluster or not account:
        return None
    digest = hashlib.sha256(f"{cluster}\0{account}".encode("utf-8")).hexdigest()[:16]
    return f"slurm-user:{digest}"


def annotate_shared_limits(results):
    """Add auditable per-user shared-limit evidence without guessing QOS scope."""
    for result in results:
        if not isinstance(result, dict):
            continue
        summary = result.get("summary")
        if not isinstance(summary, dict):
            continue
        shared_ref = shared_user_limit_ref(result)
        if shared_ref:
            summary["shared_limit_ref"] = shared_ref
        reasons = summary.get("pending_reasons") or {}
        shared_reasons = sorted(
            str(reason)
            for reason in reasons
            if re.sub(r"[^A-Z0-9]", "", str(reason).upper()) in SHARED_USER_LIMIT_REASONS
        )
        summary["shared_limit_blocked"] = bool(shared_ref and shared_reasons)
        summary["shared_limit_evidence"] = shared_reasons
    return results


def short_error(error):
    text = str(error).replace("\n", " ").strip()
    return text[:220] if text else "unknown error"


def query_account(row, args, broker=None):
    result = {
        "name": row.get("name"),
        "portal_user": row.get("portal_user"),
        "cluster": row.get("cluster"),
        "account": row.get("account"),
        "has_token": bool(row.get("has_token")),
        "token_updated_at": row.get("token_updated_at"),
        "error": None,
        "warning": None,
        "jobs": [],
        "summary": summarize_jobs([], args.run_slots, args.cap),
    }
    if not row.get("has_token"):
        result["error"] = "no saved token"
        return result
    if not row.get("account") or not row.get("cluster"):
        result["error"] = "missing cluster/account metadata"
        return result

    try:
        key = str(row.get("name") or row.get("account"))
        info = broker.connection_for(key) if broker else load_connection(row, args)
        result["cluster"] = info.get("cluster") or result["cluster"]
        result["account"] = info.get("account") or result["account"]
        command = queue_command(info["account"], args.partition, args.all_partitions)
        rc = 1
        out = ""
        err = ""
        attempts = 2
        for attempt in range(attempts):
            client = broker.client_for(key) if broker else connect_ssh(info, args.timeout)
            try:
                rc, out, err = run_remote(client, command, args.timeout)
            finally:
                if broker is None:
                    client.close()
            # The portal SSH proxy can occasionally close without sending an
            # exit status. Empty output in that case is a transport hiccup, not
            # reliable evidence that the account has no queue.
            if rc == -1 and not out.strip() and not err.strip() and attempt + 1 < attempts:
                if broker is not None:
                    broker.invalidate_client(key)
                time.sleep(1)
                continue
            break
        jobs = parse_squeue(out)
        resource_warning = None
        if jobs and any(job.get("job_id") for job in jobs):
            client = broker.client_for(key) if broker else connect_ssh(info, args.timeout)
            try:
                resource_warning = enrich_job_resources(client, jobs, args.timeout)
            finally:
                if broker is None:
                    client.close()
        if rc != 0:
            if any(job.get("job_id") for job in jobs):
                result["jobs"] = jobs
                result["summary"] = summarize_jobs(result["jobs"], args.run_slots, args.cap)
                result["warning"] = err.strip() or resource_warning or None
                return result
            result["error"] = (err or out or f"squeue returned {rc}").strip()
            return result
        result["jobs"] = jobs
        result["summary"] = summarize_jobs(result["jobs"], args.run_slots, args.cap)
        result["warning"] = resource_warning
        return result
    except Exception as error:
        result["error"] = short_error(error)
        return result


def account_error_result(row, args, error):
    return {
        "name": row.get("name"),
        "portal_user": row.get("portal_user"),
        "cluster": row.get("cluster"),
        "account": row.get("account"),
        "has_token": bool(row.get("has_token")),
        "token_updated_at": row.get("token_updated_at"),
        "error": short_error(error),
        "warning": None,
        "jobs": [],
        "summary": summarize_jobs([], args.run_slots, args.cap),
    }


def query_accounts(rows, args, broker=None):
    if args.serial or args.jobs <= 1 or len(rows) <= 1:
        return [query_account(row, args, broker=broker) for row in rows]

    max_workers = max(1, min(args.jobs, len(rows)))
    results = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_index = {
            executor.submit(query_account, row, args, broker): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as error:
                results[index] = account_error_result(rows[index], args, error)
    return results


def fmt_reasons(reasons):
    if not reasons:
        return "-"
    return ",".join(f"{key}:{value}" for key, value in sorted(reasons.items()))


def compact(value, width):
    text = "" if value is None else str(value)
    if len(text) <= width:
        return text
    return text[: width - 3] + "..."


def print_summary(results, checked_at, partition_label):
    print(f"checked_at_local: {checked_at}")
    print(f"partition: {partition_label}")
    headers = [
        "account",
        "cluster_user",
        "RUN",
        "PD",
        "OTHER",
        "TOTAL",
        "run_open",
        "cap_open",
        "pending_reasons",
        "status",
    ]
    widths = [12, 14, 3, 3, 5, 5, 8, 8, 34, 24]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
    print("  ".join("-" * width for width in widths))
    for result in results:
        summary = result["summary"]
        status = result["error"] or (f"warn: {result['warning']}" if result.get("warning") else "ok")
        values = [
            result.get("name"),
            result.get("account") or "-",
            summary["running"],
            summary["pending"],
            summary["other"],
            summary["total"],
            summary["run_slots_open"],
            summary["cap_open"],
            fmt_reasons(summary["pending_reasons"]),
            status,
        ]
        print("  ".join(compact(value, width).ljust(width) for value, width in zip(values, widths)))


def print_cluster_resources(cluster_resources):
    if not cluster_resources:
        return
    summary = cluster_resources.get("summary") or {}
    error = cluster_resources.get("error")
    print()
    if error:
        print(f"cluster_resources: error: {compact(error, 90)}")
        return
    excluded = ",".join(cluster_resources.get("excluded_reserved_nodes") or []) or "-"
    print(
        "cluster_resources: "
        f"nodes {summary.get('nodes', 0)} "
        f"GPU {summary.get('gpu_alloc', 0)}/{summary.get('gpu_total', 0)} "
        f"CPU {summary.get('cpu_alloc', 0)}/{summary.get('cpu_total', 0)} "
        f"reserved_excluded {excluded}"
    )
    for node in cluster_resources.get("nodes") or []:
        print(
            "  "
            f"{compact(node.get('name'), 8):<8} "
            f"{compact(node.get('state'), 10):<10} "
            f"G{node.get('gpu_alloc', 0)}/{node.get('gpu_total', 0)} "
            f"C{node.get('cpu_alloc', 0)}/{node.get('cpu_total', 0)}"
        )


def print_details(results):
    for result in results:
        jobs = result.get("jobs") or []
        if not jobs:
            continue
        print()
        print(f"[{result.get('name')} / {result.get('account')}]")
        headers = ["job_id", "state", "part", "gpu", "cpu", "elapsed", "limit", "nodes", "reason/nodelist", "name"]
        widths = [8, 10, 8, 3, 3, 8, 8, 5, 24, 42]
        print("  ".join(header.ljust(width) for header, width in zip(headers, widths)))
        print("  ".join("-" * width for width in widths))
        for job in sorted(jobs, key=lambda item: (item.get("state") != "RUNNING", item.get("job_id") or "")):
            resources = job.get("resources") or {}
            values = [
                job.get("job_id"),
                job.get("state"),
                job.get("partition"),
                resources.get("gpu_count") if resources else "-",
                resources.get("num_cpus") if resources else "-",
                job.get("elapsed"),
                job.get("time_limit"),
                job.get("nodes"),
                normalize_reason(job.get("reason")),
                job.get("name"),
            ]
            print("  ".join(compact(value, width).ljust(width) for value, width in zip(values, widths)))


def write_history(payload, args):
    if not args.history_log:
        return
    try:
        from hpc_resource_history import record_queue_summary

        result = record_queue_summary(
            payload,
            history_path=args.history_log,
            state_path=args.history_state,
            source="hpc_queue_summary",
            dedupe=not args.history_no_dedupe,
        )
        if result.get("error"):
            print(f"history warning: {result['error']}", file=sys.stderr)
    except Exception as error:
        print(f"history warning: {short_error(error)}", file=sys.stderr)


def collect_snapshot(args, *, rows=None, broker=None):
    checked_at = datetime.now().isoformat(timespec="seconds")
    rows = rows if rows is not None else selected_accounts(list_account_summaries(), args.accounts)
    if not rows:
        raise ValueError("no matching saved auth accounts")

    results = annotate_shared_limits(query_accounts(rows, args, broker=broker))
    cluster_resources = query_cluster_resources(rows, args, broker=broker)
    payload = {
        "checked_at_local": checked_at,
        "partition": "all" if args.all_partitions else args.partition,
        "run_slots": args.run_slots,
        "cap": args.cap,
        "cluster_resources": cluster_resources,
        "accounts": results,
    }
    write_history(payload, args)
    return payload


def main():
    args = parse_args()
    try:
        payload = collect_snapshot(args)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    results = payload["accounts"]
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_summary(results, payload["checked_at_local"], payload["partition"])
        print_cluster_resources(payload.get("cluster_resources"))
        if args.details:
            print_details(results)

    return 1 if any(result.get("error") for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
