"""Native SLURM inspection through the BJTU portal SSH proxy."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from hpc_runtime import require_native_dependencies

require_native_dependencies()

import hpc_winscp_info as winscp


SQUEUE_FORMAT = "%.18i %.9P %.40j %.8u %.2t %.10M %.10l %.6D %R"
JOB_ID_RE = re.compile(r"^\s*(\d+)\s+")


def load_connection(
    *,
    cluster: str = winscp.DEFAULT_CLUSTER,
    account: str = winscp.DEFAULT_ACCOUNT,
    portal_user: str = winscp.DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
    token: str | None = None,
    token_file: str | Path = winscp.DEFAULT_TOKEN_FILE,
    refresh_token: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = False,
) -> dict[str, Any]:
    args = argparse.Namespace(
        cluster=cluster,
        account=account,
        portal_user=portal_user,
        auth_account=auth_account or os.getenv("HPC_AUTH_ACCOUNT"),
        token=token or os.getenv("HPC_PARA_ATOKEN"),
        token_file=Path(token_file).expanduser() if token_file else winscp.DEFAULT_TOKEN_FILE,
        refresh_token=refresh_token,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        show_secret=False,
    )
    winscp.apply_auth_account_defaults(
        args,
        default_cluster=winscp.DEFAULT_CLUSTER,
        default_account=winscp.DEFAULT_ACCOUNT,
        default_portal_user=winscp.DEFAULT_PORTAL_USER,
    )
    portal_token = winscp.load_auth(args)
    try:
        return winscp.run(args, portal_token)
    except RuntimeError as error:
        if str(error) != winscp.AUTH_ERROR_MESSAGE:
            raise
        portal_token = winscp.refresh_token(
            args.token_file.expanduser(),
            args.refresh_browser,
            args.refresh_headless,
            auth_account=args.auth_account,
        )
        return winscp.run(args, portal_token)


def connect_ssh(info: dict[str, Any]):
    import paramiko

    host, port_text = info["proxy"].rsplit(":", 1)
    username = f"{info['cluster']},{info['account']}"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(port_text),
        username=username,
        password=info["certificate"],
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run_remote(client, command: str, timeout: int = 45) -> dict[str, Any]:
    marker = "__BJTU_REMOTE_RC__"
    wrapped = f"{command}\nrc=$?\nprintf '\\n{marker}%s\\n' \"$rc\"\nexit \"$rc\""
    stdin, stdout, stderr = client.exec_command(
        f"bash -lc {shlex.quote(wrapped)}",
        timeout=timeout,
    )
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    channel_rc = stdout.channel.recv_exit_status()
    rc = channel_rc
    marker_match = re.search(rf"\n?{re.escape(marker)}(-?\d+)\s*$", out)
    if marker_match:
        rc = int(marker_match.group(1))
        out = out[: marker_match.start()].rstrip("\n")
    return {"command": command, "returncode": rc, "stdout": out, "stderr": err}


def target_squeue_command(targets: list[str], account: str, partition: str | None) -> str:
    if targets:
        numeric = [target for target in targets if str(target).isdigit()]
        names = [target for target in targets if not str(target).isdigit()]
        commands = []
        if numeric:
            commands.append(
                "squeue -j "
                + shlex.quote(",".join(numeric))
                + " -o "
                + shlex.quote(SQUEUE_FORMAT)
            )
        for name in names:
            commands.append(
                "squeue -u "
                + shlex.quote(account)
                + " --name "
                + shlex.quote(name)
                + " -o "
                + shlex.quote(SQUEUE_FORMAT)
            )
        return "; ".join(commands)
    command = "squeue -u " + shlex.quote(account)
    if partition:
        command += " -p " + shlex.quote(partition)
    command += " -o " + shlex.quote(SQUEUE_FORMAT)
    return command


def parse_job_ids(squeue_text: str) -> list[str]:
    job_ids = []
    for line in squeue_text.splitlines():
        match = JOB_ID_RE.match(line)
        if match:
            job_ids.append(match.group(1))
    return sorted(set(job_ids), key=int)


def parse_scontrol_fields(text: str) -> dict[str, str]:
    fields = {}
    for token in text.replace("\n", " ").split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key] = value
    return fields


def parse_tres(value: str | None) -> dict[str, str]:
    result = {}
    if not value:
        return result
    for item in value.split(","):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        result[key] = raw
    return result


def int_field(fields: dict[str, str], name: str) -> int | None:
    value = fields.get(name)
    if value in (None, "", "N/A"):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def gpu_count_from_fields(fields: dict[str, str]) -> int | None:
    for key in ("AllocTRES", "TRES", "ReqTRES"):
        tres = parse_tres(fields.get(key))
        value = tres.get("gres/gpu")
        if value is not None:
            try:
                return int(value)
            except ValueError:
                pass
    tres_per_node = fields.get("TresPerNode") or fields.get("TRESPerNode")
    if tres_per_node:
        match = re.search(r"gres:gpu(?::[^:=]+)?[:=](\d+)", tres_per_node)
        if match:
            return int(match.group(1))
    return None


def pending_reason(
    targets: list[str] | None = None,
    *,
    partition: str = "GPU",
    include_sinfo: bool = False,
    command_timeout: int = 45,
    **connection_kwargs: Any,
) -> dict[str, Any]:
    info = load_connection(**connection_kwargs)
    client = connect_ssh(info)
    try:
        results: dict[str, Any] = {
            "squeue": run_remote(
                client,
                target_squeue_command(targets or [], info["account"], partition),
                timeout=command_timeout,
            )
        }
        job_ids = parse_job_ids(results["squeue"]["stdout"])
        for job_id in job_ids:
            results[f"scontrol_{job_id}"] = run_remote(
                client,
                f"scontrol show job {shlex.quote(job_id)}",
                timeout=command_timeout,
            )
        if include_sinfo:
            results["sinfo_partition"] = run_remote(
                client,
                f"sinfo -p {shlex.quote(partition)} -o '%P %a %l %D %t %N' 2>&1 || true",
                timeout=command_timeout,
            )
            results["partition"] = run_remote(
                client,
                f"scontrol show partition {shlex.quote(partition)}",
                timeout=command_timeout,
            )
    finally:
        client.close()

    reasons = []
    for job_id in parse_job_ids(results["squeue"]["stdout"]):
        fields = parse_scontrol_fields(results[f"scontrol_{job_id}"]["stdout"])
        reasons.append(
            {
                "job_id": job_id,
                "state": fields.get("JobState"),
                "reason": fields.get("Reason"),
                "num_cpus": int_field(fields, "NumCPUs"),
                "num_tasks": int_field(fields, "NumTasks"),
                "cpus_per_task": int_field(fields, "CPUs/Task"),
                "gpu_count": gpu_count_from_fields(fields),
            }
        )

    return {
        "success": True,
        "cluster": info["cluster"],
        "account": info["account"],
        "home": info["home"],
        "targets": targets or [],
        "partition": partition,
        "job_ids": parse_job_ids(results["squeue"]["stdout"]),
        "reasons": reasons,
        "results": results,
    }


def verify_slurm_allocation(
    job_id: str,
    *,
    expected_total_cpus: int | None = None,
    min_cpus: int | None = None,
    expected_ntasks: int | None = None,
    expected_cpus_per_task: int | None = None,
    expected_gpus: int | None = None,
    expected_command: str | None = None,
    command_timeout: int = 45,
    connection_info: dict[str, Any] | None = None,
    client: Any | None = None,
    **connection_kwargs: Any,
) -> dict[str, Any]:
    info = connection_info or load_connection(**connection_kwargs)
    owns_client = client is None
    active_client = client or connect_ssh(info)
    try:
        command = f"scontrol show job {shlex.quote(str(job_id))}"
        result = run_remote(active_client, command, timeout=command_timeout)
    finally:
        if owns_client:
            active_client.close()

    fields = parse_scontrol_fields(result["stdout"])
    observed = {
        "job_state": fields.get("JobState"),
        "reason": fields.get("Reason"),
        "num_cpus": int_field(fields, "NumCPUs"),
        "num_tasks": int_field(fields, "NumTasks"),
        "cpus_per_task": int_field(fields, "CPUs/Task"),
        "gpu_count": gpu_count_from_fields(fields),
        "command_path": fields.get("Command"),
        "tres": fields.get("TRES"),
        "alloc_tres": fields.get("AllocTRES"),
        "req_tres": fields.get("ReqTRES"),
    }
    mismatches = []

    def add_mismatch(name: str, expected: int, actual: int | None, op: str = "==") -> None:
        if actual is None:
            mismatches.append({"field": name, "expected": expected, "actual": actual, "op": op})
            return
        if op == "==" and actual != expected:
            mismatches.append({"field": name, "expected": expected, "actual": actual, "op": op})
        if op == ">=" and actual < expected:
            mismatches.append({"field": name, "expected": expected, "actual": actual, "op": op})

    if expected_total_cpus is not None:
        add_mismatch("num_cpus", expected_total_cpus, observed["num_cpus"])
    if min_cpus is not None:
        add_mismatch("num_cpus", min_cpus, observed["num_cpus"], op=">=")
    if expected_ntasks is not None:
        add_mismatch("num_tasks", expected_ntasks, observed["num_tasks"])
    if expected_cpus_per_task is not None:
        add_mismatch("cpus_per_task", expected_cpus_per_task, observed["cpus_per_task"])
    if expected_gpus is not None:
        add_mismatch("gpu_count", expected_gpus, observed["gpu_count"])
    if expected_command is not None and observed["command_path"] != expected_command:
        mismatches.append(
            {
                "field": "command_path",
                "expected": expected_command,
                "actual": observed["command_path"],
                "op": "==",
            }
        )

    return {
        "success": result["returncode"] == 0 and not mismatches,
        "cluster": info["cluster"],
        "account": info["account"],
        "job_id": str(job_id),
        "observed": observed,
        "mismatches": mismatches,
        "command": command,
        "returncode": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
    }
