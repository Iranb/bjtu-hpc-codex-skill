#!/usr/bin/env python3
"""MCP stdio server for BJTU HPC portal-backed SLURM operations."""

import argparse
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from hpc_account_store import get_account
from hpc_doctor import build_report as build_doctor_report
from hpc_core.datasets import (
    get_dataset_locations,
    plan_dataset_upload,
    start_dataset_upload,
)
from hpc_core.files import download_remote_file, read_remote_text
from hpc_core.jobs import (
    DEFAULT_ACCOUNT,
    DEFAULT_CLUSTER,
    DEFAULT_PORTAL_USER,
    DEFAULT_PYTORCH,
    DEFAULT_REMOTE_DIR,
    JobSpec,
    cancel_job,
    get_job,
    list_jobs,
    plan_job,
    submit_job,
    wait_for_job,
)
from hpc_core.native import load_connection, pending_reason, verify_slurm_allocation
from hpc_winscp_info import redact_secret, redact_sftp_url


mcp = FastMCP(
    "bjtu-hpc-slurm",
    instructions=(
        "Tools for BJTU HPC portal-backed SLURM jobs. "
        "Tokens are read from ~/.bjtu_hpc_token, HPC_PARA_ATOKEN, or HPC_AUTH_ACCOUNT and are never returned. "
        "Destructive or resource-consuming actions require confirm=True. "
        "CPU-only jobs can queue with very low priority; use a short GPU job for quick probes when acceptable. "
        "For single-process GPU jobs, prefer ntasks=1, cpus_per_task=8, and gres_flags='disable-binding'."
    ),
)


def apply_account_defaults(
    auth_account: str | None,
    cluster: str,
    account: str,
    portal_user: str,
) -> tuple[str, str, str]:
    if not auth_account:
        return cluster, account, portal_user
    _, entry = get_account(auth_account)
    if cluster == DEFAULT_CLUSTER:
        cluster = entry.get("cluster") or cluster
    if account == DEFAULT_ACCOUNT:
        account = entry.get("account") or account
    if portal_user == DEFAULT_PORTAL_USER:
        portal_user = entry.get("portal_user") or portal_user
    return cluster, account, portal_user


def connection_kwargs(
    *,
    auth_account: str | None,
    token_file: str | None,
    refresh_token: bool,
    cluster: str,
    account: str,
    portal_user: str,
) -> dict[str, Any]:
    return {
        "cluster": cluster,
        "account": account,
        "portal_user": portal_user,
        "auth_account": auth_account,
        "token_file": token_file or None,
        "refresh_token": refresh_token,
    }


def split_targets(targets: str) -> list[str]:
    return [item for item in targets.replace(",", " ").split() if item]


def expected_total_cpus_for(app: str, ntasks: int | None, cpus_per_task: int | None) -> int | None:
    effective_ntasks = ntasks if ntasks is not None else (1 if app == "gpu" else 48)
    effective_cpus_per_task = cpus_per_task if cpus_per_task is not None else (8 if app == "gpu" else None)
    if effective_ntasks is None or effective_cpus_per_task is None:
        return None
    return int(effective_ntasks) * int(effective_cpus_per_task)


def build_spec(
    *,
    app: Literal["gpu", "cpu"],
    job_name: str | None,
    local_script_path: str | None,
    remote_input: str | None,
    cluster: str,
    account: str,
    portal_user: str,
    remote_dir: str,
    input_path_kind: Literal["virtual", "real"],
    ntasks: int | None,
    cpus_per_task: int | None,
    gpu_count: int,
    gres_flags: str | None,
    partition: str | None,
    pytorch: str,
    console: bool,
    rewrite: bool,
    allow_external_path: bool,
) -> JobSpec:
    return JobSpec(
        app=app,
        job_name=job_name,
        local_script_path=local_script_path,
        remote_input=remote_input,
        cluster=cluster,
        account=account,
        portal_user=portal_user,
        remote_dir=remote_dir,
        input_path_kind=input_path_kind,
        ntasks=ntasks,
        cpus_per_task=cpus_per_task,
        gpu_count=gpu_count,
        gres_flags=gres_flags,
        partition=partition,
        pytorch=pytorch,
        console=console,
        rewrite=rewrite,
        allow_external_path=allow_external_path,
    )


def build_spec_from_dict(job_spec: dict[str, Any]) -> JobSpec:
    allowed = set(JobSpec.__dataclass_fields__)
    unknown = sorted(set(job_spec) - allowed)
    if unknown:
        raise ValueError(f"unknown job_spec keys: {', '.join(unknown)}")
    return JobSpec(**job_spec)


@mcp.tool(structured_output=True)
def hpc_plan_job(
    local_script_path: str | None = None,
    remote_input: str | None = None,
    app: Literal["gpu", "cpu"] = "gpu",
    job_name: str | None = None,
    gpu_count: int = 1,
    ntasks: int | None = None,
    cpus_per_task: int | None = None,
    gres_flags: str | None = None,
    partition: str | None = None,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    input_path_kind: Literal["virtual", "real"] = "virtual",
    pytorch: str = DEFAULT_PYTORCH,
    console: bool = False,
    allow_external_path: bool = False,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Validate and render the portal payload for a job without uploading or submitting."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    spec = build_spec(
        app=app,
        job_name=job_name,
        local_script_path=local_script_path,
        remote_input=remote_input,
        cluster=cluster,
        account=account,
        portal_user=portal_user,
        remote_dir=remote_dir,
        input_path_kind=input_path_kind,
        ntasks=ntasks,
        cpus_per_task=cpus_per_task,
        gpu_count=gpu_count,
        gres_flags=gres_flags,
        partition=partition,
        pytorch=pytorch,
        console=console,
        rewrite=False,
        allow_external_path=allow_external_path,
    )
    return plan_job(spec)


@mcp.tool(structured_output=True)
def hpc_submit_job(
    local_script_path: str | None = None,
    remote_input: str | None = None,
    app: Literal["gpu", "cpu"] = "gpu",
    job_name: str | None = None,
    confirm: bool = False,
    wait: bool = False,
    wait_timeout: int = 0,
    wait_interval: int = 10,
    gpu_count: int = 1,
    ntasks: int | None = None,
    cpus_per_task: int | None = None,
    gres_flags: str | None = None,
    partition: str | None = None,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    input_path_kind: Literal["virtual", "real"] = "virtual",
    pytorch: str = DEFAULT_PYTORCH,
    console: bool = False,
    rewrite: bool = False,
    allow_external_path: bool = False,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Upload a local .py script or use a remote .py input, then submit a portal SLURM job."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    spec = build_spec(
        app=app,
        job_name=job_name,
        local_script_path=local_script_path,
        remote_input=remote_input,
        cluster=cluster,
        account=account,
        portal_user=portal_user,
        remote_dir=remote_dir,
        input_path_kind=input_path_kind,
        ntasks=ntasks,
        cpus_per_task=cpus_per_task,
        gpu_count=gpu_count,
        gres_flags=gres_flags,
        partition=partition,
        pytorch=pytorch,
        console=console,
        rewrite=rewrite,
        allow_external_path=allow_external_path,
    )
    return submit_job(
        spec,
        confirm=confirm,
        wait=wait,
        wait_interval=wait_interval,
        wait_timeout=wait_timeout,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_submit_job_spec(
    job_spec: dict[str, Any],
    confirm: bool = False,
    wait: bool = False,
    wait_timeout: int = 0,
    wait_interval: int = 10,
    refresh_token: bool = False,
    token_file: str | None = None,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Submit using a single MCP JSON object matching the JobSpec fields."""
    spec = build_spec_from_dict(job_spec)
    spec.cluster, spec.account, spec.portal_user = apply_account_defaults(
        auth_account,
        spec.cluster,
        spec.account,
        spec.portal_user,
    )
    return submit_job(
        spec,
        confirm=confirm,
        wait=wait,
        wait_interval=wait_interval,
        wait_timeout=wait_timeout,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_list_jobs(
    keyword: str = "",
    scope: Literal["current", "history", "both"] = "current",
    page: int = 0,
    size: int = 20,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """List portal jobs. scope is current, history, or both."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return list_jobs(
        keyword=keyword,
        scope=scope,
        page=page,
        size=size,
        cluster=cluster,
        portal_user=portal_user,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_get_job(
    target: str,
    scope: Literal["current", "history", "both"] = "current",
    size: int = 20,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """Get one job by job name, SLURM id, or portal platform id."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return get_job(
        target,
        scope=scope,
        size=size,
        cluster=cluster,
        portal_user=portal_user,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_wait_job(
    target: str,
    interval: int = 10,
    timeout: int = 0,
    size: int = 20,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """Poll a current job until it reaches a terminal state or timeout seconds elapse."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return wait_for_job(
        target,
        interval=interval,
        timeout=timeout,
        size=size,
        cluster=cluster,
        portal_user=portal_user,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_cancel_job(
    target: str,
    confirm: bool = False,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
    account: str = DEFAULT_ACCOUNT,
) -> dict[str, Any]:
    """Cancel a current portal job by job name, SLURM id, or platform id."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return cancel_job(
        target,
        confirm=confirm,
        cluster=cluster,
        portal_user=portal_user,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_download_file(
    remote_path: str,
    output: str = ".",
    remote_dir: str = DEFAULT_REMOTE_DIR,
    allow_external_path: bool = False,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Download a remote portal file to the local workspace."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return download_remote_file(
        remote_path,
        output=output,
        cluster=cluster,
        account=account,
        remote_dir=remote_dir,
        allow_external_path=allow_external_path,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_read_remote_text(
    remote_path: str,
    max_bytes: int = 12000,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Read the tail of a remote text file, useful for SLURM stdout paths."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return read_remote_text(
        remote_path,
        max_bytes=max_bytes,
        cluster=cluster,
        account=account,
        remote_dir=remote_dir,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )


@mcp.tool(structured_output=True)
def hpc_auth_status(
    auth_account: str | None = None,
    token_file: str | None = None,
    validate: bool = True,
    deep: bool = False,
    source_host: str | None = None,
    timeout: int = 10,
) -> dict[str, Any]:
    """Return structured local dependency and auth readiness without printing secrets."""
    args = argparse.Namespace(
        json=True,
        auth_account=auth_account,
        token_file=Path(token_file).expanduser() if token_file else Path("~/.bjtu_hpc_token").expanduser(),
        no_validate=not validate,
        deep=deep,
        source_host=source_host,
        timeout=timeout,
    )
    return build_doctor_report(args)


@mcp.tool(structured_output=True)
def hpc_get_sftp_info(
    include_secret: bool = False,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Fetch portal SSH/SFTP proxy details. Secrets are redacted unless include_secret=true."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    info = load_connection(
        **connection_kwargs(
            auth_account=auth_account,
            token_file=token_file,
            refresh_token=refresh_token,
            cluster=cluster,
            account=account,
            portal_user=portal_user,
        )
    )
    certificate = info["certificate"] if include_secret else redact_secret(info["certificate"])
    sftp_url = info["sftp_url"] if include_secret else redact_sftp_url(info["sftp_url"])
    return {
        "success": True,
        "portal_user": info["portal_user"],
        "cluster": info["cluster"],
        "account": info["account"],
        "proxy": info["proxy"],
        "home": info["home"],
        "certificate": certificate,
        "sftp_url": sftp_url,
        "secret_redacted": not include_secret,
    }


@mcp.tool(structured_output=True)
def hpc_pending_reason(
    targets: str = "",
    partition: str = "GPU",
    include_sinfo: bool = False,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Inspect native squeue/scontrol state and pending reasons via the portal SSH proxy."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return pending_reason(
        split_targets(targets),
        partition=partition,
        include_sinfo=include_sinfo,
        **connection_kwargs(
            auth_account=auth_account,
            token_file=token_file,
            refresh_token=refresh_token,
            cluster=cluster,
            account=account,
            portal_user=portal_user,
        ),
    )


@mcp.tool(structured_output=True)
def hpc_verify_slurm_allocation(
    job_id: str,
    expected_total_cpus: int | None = None,
    min_cpus: int | None = None,
    expected_ntasks: int | None = None,
    expected_cpus_per_task: int | None = None,
    expected_gpus: int | None = None,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Verify native Slurm NumCPUs/NumTasks/CPUsPerTask/GPU TRES for a job."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    return verify_slurm_allocation(
        job_id,
        expected_total_cpus=expected_total_cpus,
        min_cpus=min_cpus,
        expected_ntasks=expected_ntasks,
        expected_cpus_per_task=expected_cpus_per_task,
        expected_gpus=expected_gpus,
        **connection_kwargs(
            auth_account=auth_account,
            token_file=token_file,
            refresh_token=refresh_token,
            cluster=cluster,
            account=account,
            portal_user=portal_user,
        ),
    )


@mcp.tool(structured_output=True)
def hpc_tail_stdout(
    target: str | None = None,
    remote_path: str | None = None,
    max_bytes: int = 12000,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Read the tail of a job stdout path, resolving target through the portal job list if needed."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    job = None
    if not remote_path:
        if not target:
            raise ValueError("provide either remote_path or target")
        job_data = get_job(
            target,
            scope="both",
            cluster=cluster,
            portal_user=portal_user,
            token_file=token_file,
            refresh_token=refresh_token,
            auth_account=auth_account,
        )
        job = job_data.get("job")
        remote_path = job.get("stdout") if job else None
    if not remote_path:
        return {"success": False, "message": "no stdout path found", "job": job}
    text = read_remote_text(
        remote_path,
        max_bytes=max_bytes,
        cluster=cluster,
        account=account,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )
    return {**text, "job": job}


@mcp.tool(structured_output=True)
def hpc_submit_and_verify(
    local_script_path: str | None = None,
    remote_input: str | None = None,
    app: Literal["gpu", "cpu"] = "gpu",
    job_name: str | None = None,
    confirm: bool = False,
    wait: bool = False,
    wait_timeout: int = 0,
    wait_interval: int = 10,
    gpu_count: int = 1,
    ntasks: int | None = None,
    cpus_per_task: int | None = None,
    gres_flags: str | None = None,
    partition: str | None = None,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    input_path_kind: Literal["virtual", "real"] = "virtual",
    pytorch: str = DEFAULT_PYTORCH,
    console: bool = False,
    rewrite: bool = False,
    allow_external_path: bool = False,
    native_check: bool = True,
    expected_total_cpus: int | None = None,
    expected_gpus: int | None = None,
    refresh_token: bool = False,
    token_file: str | None = None,
    cluster: str = DEFAULT_CLUSTER,
    account: str = DEFAULT_ACCOUNT,
    portal_user: str = DEFAULT_PORTAL_USER,
    auth_account: str | None = None,
) -> dict[str, Any]:
    """Submit a portal job, inspect its portal row, and verify native Slurm allocation when possible."""
    cluster, account, portal_user = apply_account_defaults(auth_account, cluster, account, portal_user)
    spec = build_spec(
        app=app,
        job_name=job_name,
        local_script_path=local_script_path,
        remote_input=remote_input,
        cluster=cluster,
        account=account,
        portal_user=portal_user,
        remote_dir=remote_dir,
        input_path_kind=input_path_kind,
        ntasks=ntasks,
        cpus_per_task=cpus_per_task,
        gpu_count=gpu_count,
        gres_flags=gres_flags,
        partition=partition,
        pytorch=pytorch,
        console=console,
        rewrite=rewrite,
        allow_external_path=allow_external_path,
    )
    result = submit_job(
        spec,
        confirm=confirm,
        wait=wait,
        wait_interval=wait_interval,
        wait_timeout=wait_timeout,
        token_file=token_file,
        refresh_token=refresh_token,
        auth_account=auth_account,
    )
    if not confirm or not result.get("submit_result", {}).get("success"):
        return result

    if not result.get("job") and (result.get("wait") or {}).get("job"):
        result["job"] = result["wait"]["job"]
    job = result.get("job") or (result.get("wait") or {}).get("job") or {}
    slurm_id = str(job.get("slurm_id")) if job and job.get("slurm_id") else ""
    state = str(job.get("state") or "").upper() if job else ""
    verification: dict[str, Any] = {"portal_job": job}
    conn = connection_kwargs(
        auth_account=auth_account,
        token_file=token_file,
        refresh_token=refresh_token,
        cluster=cluster,
        account=account,
        portal_user=portal_user,
    )
    native_partition = partition or ("GPU" if app == "gpu" else "COMPUTE")
    if native_check and not slurm_id:
        verification["success"] = False
        verification["message"] = "portal row has no Slurm job id yet"
    if slurm_id and state == "PENDING":
        verification["pending_reason"] = pending_reason([slurm_id], partition=native_partition, **conn)
    if slurm_id and native_check:
        verification["allocation"] = verify_slurm_allocation(
            slurm_id,
            expected_total_cpus=expected_total_cpus or expected_total_cpus_for(app, ntasks, cpus_per_task),
            expected_ntasks=ntasks if ntasks is not None else (1 if app == "gpu" else 48),
            expected_cpus_per_task=cpus_per_task if cpus_per_task is not None else (8 if app == "gpu" else None),
            expected_gpus=expected_gpus if expected_gpus is not None else (gpu_count if app == "gpu" else None),
            **conn,
        )
        verification["success"] = bool(verification["allocation"].get("success"))
    result["verification"] = verification
    allocation = verification.get("allocation")
    result["success"] = (
        bool(result.get("success"))
        and verification.get("success") is not False
        and (not allocation or allocation.get("success"))
    )
    return result


@mcp.tool(structured_output=True)
def hpc_dataset_locations(
    include_tasks: bool = True,
    include_task_state: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Return dataset source/destination roots, manifest items, and saved upload tasks read-only."""
    return get_dataset_locations(
        include_tasks=include_tasks,
        include_task_state=include_task_state,
        config_path=config_path,
    )


@mcp.tool(structured_output=True)
def hpc_plan_dataset_upload(
    task_name: str | None = None,
    source_host: str | None = None,
    source_path: str | None = None,
    dest_path: str | None = None,
    pack: bool = True,
    screen_name: str | None = None,
    remote_workdir: str | None = None,
    archive_name: str | None = None,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Plan a dataset or code upload without writing task config, packing, or starting transfer."""
    return plan_dataset_upload(
        task_name=task_name,
        source_host=source_host,
        source_path=source_path,
        dest_path=dest_path,
        pack=pack,
        screen_name=screen_name,
        remote_workdir=remote_workdir,
        archive_name=archive_name,
        config_path=config_path,
    )


@mcp.tool(structured_output=True)
def hpc_start_dataset_upload(
    task_name: str | None = None,
    source_host: str | None = None,
    source_path: str | None = None,
    dest_path: str | None = None,
    pack: bool = True,
    screen_name: str | None = None,
    remote_workdir: str | None = None,
    archive_name: str | None = None,
    confirm: bool = False,
    refresh_browser: Literal["playwright", "chrome", "safari"] = "playwright",
    refresh_headless: bool = False,
    no_auto_refresh_token: bool = False,
    pack_timeout: int = 0,
    upload_method: Literal["rsync", "paramiko", "parallel-chunk"] = "parallel-chunk",
    parallel: int = 4,
    chunk_mib: int = 8,
    buffer_mib: int = 4,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Pack a source path when requested and start the resumable source-host to cluster upload."""
    return start_dataset_upload(
        task_name=task_name,
        source_host=source_host,
        source_path=source_path,
        dest_path=dest_path,
        pack=pack,
        screen_name=screen_name,
        remote_workdir=remote_workdir,
        archive_name=archive_name,
        confirm=confirm,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        no_auto_refresh_token=no_auto_refresh_token,
        pack_timeout=pack_timeout,
        upload_method=upload_method,
        parallel=parallel,
        chunk_mib=chunk_mib,
        buffer_mib=buffer_mib,
        config_path=config_path,
    )


if __name__ == "__main__":
    mcp.run()
