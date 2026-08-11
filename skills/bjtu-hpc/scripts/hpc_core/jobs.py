import contextlib
import io
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from hpc_core.auth import load_portal_token
from hpc_upload import (
    BASE_URL,
    create_session,
    join_remote_path,
    mkdir_remote,
    normalize_remote_dir,
    request_json,
    upload_path,
)


APP_CONFIG = {
    "gpu": {
        "app_id": 13,
        "name": "PyTorch-GPU",
        "default_ntasks": "1",
        "default_cpus_per_task": "8",
        "default_gres_flags": "disable-binding",
        "default_partition": "GPU",
        "partitions": {"GPU"},
    },
    "cpu": {
        "app_id": 16,
        "name": "PyTorch-CPU",
        "default_ntasks": "48",
        "default_partition": "COMPUTE",
        "partitions": {"COMPUTE", "FAT"},
    },
}

DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_ACCOUNT = os.getenv("HPC_ACCOUNT", "")
DEFAULT_PORTAL_USER = os.getenv("HPC_PORTAL_USER", "")
DEFAULT_REMOTE_DIR = os.getenv("HPC_REMOTE_DIR", "home")
DEFAULT_PYTORCH = os.getenv("HPC_PYTORCH", "pytorch1.7-python3.8")
TERMINAL_STATES = {"DONE", "FAILED", "FAIL", "CANCELLED", "CANCEL"}
COMPLETED_STATES = {"COMPLETED", "DONE"}
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_.=@:+-]{1,80}$")


@dataclass
class JobSpec:
    app: str = "gpu"
    job_name: str | None = None
    local_script_path: str | None = None
    remote_input: str | None = None
    cluster: str = DEFAULT_CLUSTER
    account: str = DEFAULT_ACCOUNT
    portal_user: str = DEFAULT_PORTAL_USER
    remote_dir: str = DEFAULT_REMOTE_DIR
    input_path_kind: str = "virtual"
    ntasks: int | str | None = None
    cpus_per_task: int | str | None = None
    gpu_count: int | str = 1
    gres_flags: str | None = None
    partition: str | None = None
    pytorch: str = DEFAULT_PYTORCH
    console: bool = False
    rewrite: bool = False
    allow_external_path: bool = False


def workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_job_name(script: str | None, app: str) -> str:
    stem = Path(script).stem if script else app
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{stem}_{app}_{stamp}"


def validate_job_name(job_name: str) -> None:
    if not JOB_NAME_RE.match(job_name):
        raise ValueError(
            "job_name must be 1-80 chars using letters, digits, '.', '_', '-', '+', ':', '=', '@'."
        )


def validate_script_path(path_text: str, allow_external_path: bool) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = workspace_root() / path
    path = path.resolve()

    if not allow_external_path:
        root = workspace_root().resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"local_script_path must be inside {root}; set allow_external_path=True to override."
            ) from exc

    if not path.is_file():
        raise FileNotFoundError(f"script not found: {path}")
    if path.suffix != ".py":
        raise ValueError("local_script_path must point to a .py file.")
    return path


def virtual_to_real_path(path: str, cluster: str, account: str) -> str:
    home_prefix = f"[PATH]/{cluster}/{account}/home"
    if path == home_prefix:
        return f"/data/home/{account}"
    if path.startswith(home_prefix + "/"):
        return f"/data/home/{account}/{path[len(home_prefix) + 1:]}"
    return path


def remote_input_for_upload(
    cluster: str,
    account: str,
    remote_dir: str,
    script_path: Path,
    input_path_kind: str,
) -> str:
    virtual_dir = normalize_remote_dir(cluster, account, remote_dir)
    virtual_path = join_remote_path(virtual_dir, script_path.name)
    if input_path_kind == "virtual":
        return virtual_path
    if input_path_kind == "real":
        return virtual_to_real_path(virtual_path, cluster, account)
    raise ValueError("input_path_kind must be 'virtual' or 'real'.")


def normalize_spec(spec: JobSpec) -> tuple[JobSpec, Path | None, str]:
    if spec.app not in APP_CONFIG:
        raise ValueError(f"app must be one of: {', '.join(sorted(APP_CONFIG))}.")
    if bool(spec.local_script_path) == bool(spec.remote_input):
        raise ValueError("provide exactly one of local_script_path or remote_input.")
    if spec.remote_input and not str(spec.remote_input).endswith(".py"):
        raise ValueError("remote_input must point to a .py file.")

    local_script = (
        validate_script_path(spec.local_script_path, spec.allow_external_path)
        if spec.local_script_path
        else None
    )
    job_name = spec.job_name or default_job_name(spec.local_script_path or spec.remote_input, spec.app)
    validate_job_name(job_name)

    normalized = JobSpec(**{**asdict(spec), "job_name": job_name})
    if spec.remote_input:
        input_path = spec.remote_input
    else:
        input_path = remote_input_for_upload(
            spec.cluster,
            spec.account,
            spec.remote_dir,
            local_script,
            spec.input_path_kind,
        )
    return normalized, local_script, input_path


def build_payload(spec: JobSpec, input_path: str) -> dict[str, Any]:
    app = APP_CONFIG[spec.app]
    partition = spec.partition or app["default_partition"]
    if partition not in app["partitions"]:
        allowed = ", ".join(sorted(app["partitions"]))
        raise ValueError(f"{app['name']} partition must be one of: {allowed}.")

    ntasks = spec.ntasks or app["default_ntasks"]
    cpus_per_task = spec.cpus_per_task or app.get("default_cpus_per_task")
    cmd: list[dict[str, str]] = [
        {"--job-name": str(spec.job_name)},
        {"--ntasks": str(ntasks)},
    ]
    if cpus_per_task:
        cmd.append({"--cpus-per-task": str(cpus_per_task)})
    if spec.app == "gpu":
        cmd.append({"--gpu": str(spec.gpu_count)})
        gres_flags = spec.gres_flags if spec.gres_flags is not None else app.get("default_gres_flags")
        if gres_flags:
            cmd.append({"--gres-flags": str(gres_flags)})
    cmd.append({"--partition": partition})

    params: list[dict[str, Any]] = [
        {"--input": [input_path]},
        {"--pytorch": spec.pytorch},
    ]
    if spec.console:
        params.append({"--console": "true"})

    return {
        "appId": app["app_id"],
        "cluster_id": spec.cluster,
        "userName": spec.portal_user,
        "osUser": spec.account,
        "cmd": cmd,
        "params": params,
    }


def plan_job(spec: JobSpec) -> dict[str, Any]:
    normalized, local_script, input_path = normalize_spec(spec)
    payload = build_payload(normalized, input_path)
    warnings = []
    if normalized.app == "cpu":
        warnings.append(
            "CPU-only jobs can have very low queue priority on this cluster; prefer a short GPU job for quick probes when acceptable."
        )
    return {
        "success": True,
        "action": "plan",
        "job_spec": asdict(normalized),
        "local_script_path": str(local_script) if local_script else None,
        "input_path": input_path,
        "payload": payload,
        "warnings": warnings,
    }


def ensure_remote_dir(session, token: str, cluster: str, account: str, remote_dir: str) -> None:
    virtual_dir = normalize_remote_dir(cluster, account, remote_dir)
    if virtual_dir.endswith(f"/{account}/home") or virtual_dir.endswith(f"/{account}"):
        return

    parts = virtual_dir.split("/")
    for index in range(4, len(parts) + 1):
        mkdir_remote(session, cluster, account, "/".join(parts[:index]), token, verbose=False)


def submit_payload(session, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{BASE_URL}/job/submit"
    return request_json(session, "POST", url, headers={"PARA_ATOKEN": token}, json=payload)


def query_jobs(
    session,
    token: str,
    *,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    keyword: str = "",
    page: int = 0,
    size: int = 20,
    history: bool = False,
) -> list[dict[str, Any]]:
    endpoint = "job/hist-jobs" if history else "job/list"
    params = {
        "pno": str(page),
        "psize": str(size),
        "cluster_id": cluster,
        "keyword": keyword or "",
        "order": "time",
        "reverse": "0",
        "user": portal_user,
    }
    url = f"{BASE_URL}/{endpoint}?{urlencode(params)}"
    data = request_json(session, "GET", url, headers={"PARA_ATOKEN": token})
    return data.get("data") or []


def normalize_job_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    state = str(row.get("state") or "")
    origin_state = str(row.get("originState") or "")
    done = row.get("done") == 1 or state.upper() in TERMINAL_STATES
    completed = origin_state.upper() in COMPLETED_STATES or state.upper() in COMPLETED_STATES
    return {
        "platform_id": row.get("id"),
        "slurm_id": row.get("jobId"),
        "name": row.get("name"),
        "state": row.get("state"),
        "origin_state": row.get("originState"),
        "done": done,
        "completed": completed,
        "nodes": row.get("nodes"),
        "partition": row.get("part"),
        "stdout": row.get("stdOutput"),
        "work_dir": row.get("workDir"),
        "submit_time_ms": row.get("submit"),
        "raw": row,
    }


def row_matches(row: dict[str, Any], target: str) -> bool:
    target = str(target)
    return target in {
        str(row.get("id") or ""),
        str(row.get("jobId") or ""),
        str(row.get("name") or ""),
    }


def select_target_row(rows: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    exact = [row for row in rows if row_matches(row, target)]
    if exact:
        return exact[0]
    return rows[0] if rows else None


def list_jobs(
    *,
    keyword: str = "",
    scope: str = "current",
    page: int = 0,
    size: int = 20,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = True,
    auth_account: str | None = None,
) -> dict[str, Any]:
    if scope not in {"current", "history", "both"}:
        raise ValueError("scope must be current, history, or both.")
    token = load_portal_token(
        token_file=token_file,
        refresh=refresh_token,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        auth_account=auth_account,
    )
    session = create_session()
    rows: list[dict[str, Any]] = []
    if scope in {"current", "both"}:
        rows.extend(
            query_jobs(
                session,
                token,
                cluster=cluster,
                portal_user=portal_user,
                keyword=keyword,
                page=page,
                size=size,
                history=False,
            )
        )
    if scope in {"history", "both"}:
        rows.extend(
            query_jobs(
                session,
                token,
                cluster=cluster,
                portal_user=portal_user,
                keyword=keyword,
                page=page,
                size=size,
                history=True,
            )
        )
    return {"success": True, "jobs": [normalize_job_row(row) for row in rows], "count": len(rows)}


def get_job(
    target: str,
    *,
    scope: str = "current",
    size: int = 20,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = True,
    auth_account: str | None = None,
) -> dict[str, Any]:
    data = list_jobs(
        keyword=target,
        scope=scope,
        page=0,
        size=size,
        cluster=cluster,
        portal_user=portal_user,
        token_file=token_file,
        refresh_token=refresh_token,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        auth_account=auth_account,
    )
    raw_rows = [job["raw"] for job in data["jobs"]]
    row = select_target_row(raw_rows, target)
    return {"success": bool(row), "job": normalize_job_row(row)}


def wait_for_job(
    target: str,
    *,
    interval: int = 10,
    timeout: int = 0,
    size: int = 20,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = True,
    auth_account: str | None = None,
) -> dict[str, Any]:
    token = load_portal_token(
        token_file=token_file,
        refresh=refresh_token,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        auth_account=auth_account,
    )
    session = create_session()
    deadline = time.monotonic() + timeout if timeout else None
    snapshots = []

    while True:
        rows = query_jobs(
            session,
            token,
            cluster=cluster,
            portal_user=portal_user,
            keyword=target,
            page=0,
            size=size,
            history=False,
        )
        row = select_target_row(rows, target)
        normalized = normalize_job_row(row)
        if normalized:
            snapshots.append(
                {
                    "state": normalized["state"],
                    "origin_state": normalized["origin_state"],
                    "done": normalized["done"],
                    "nodes": normalized["nodes"],
                }
            )
            if normalized["done"]:
                return {"success": normalized["completed"], "job": normalized, "snapshots": snapshots}

        if deadline and time.monotonic() >= deadline:
            return {"success": False, "timeout": True, "job": normalized, "snapshots": snapshots}
        time.sleep(max(interval, 1))


def submit_job(
    spec: JobSpec,
    *,
    confirm: bool = False,
    wait: bool = False,
    wait_interval: int = 10,
    wait_timeout: int = 0,
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = True,
    auth_account: str | None = None,
) -> dict[str, Any]:
    planned = plan_job(spec)
    if not confirm:
        return {
            **planned,
            "success": False,
            "requires_confirmation": True,
            "message": "Set confirm=True to upload and submit this job.",
        }

    token = load_portal_token(
        token_file=token_file,
        refresh=refresh_token,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        auth_account=auth_account,
    )
    session = create_session()
    session.headers.update({"Accept": "application/json, text/plain, */*"})
    upload_log = ""

    local_script = planned["local_script_path"]
    if local_script:
        ensure_remote_dir(session, token, spec.cluster, spec.account, spec.remote_dir)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            upload_path(
                Path(local_script),
                spec.cluster,
                spec.account,
                spec.remote_dir,
                token,
                rewrite=spec.rewrite,
                verbose=False,
                show_progress=False,
                include_parent_dir=False,
            )
        upload_log = buffer.getvalue()

    submit_result = submit_payload(session, token, planned["payload"])
    jobs = query_jobs(
        session,
        token,
        cluster=spec.cluster,
        portal_user=spec.portal_user,
        keyword=planned["job_spec"]["job_name"],
        page=0,
        size=5,
        history=False,
    )
    row = normalize_job_row(select_target_row(jobs, planned["job_spec"]["job_name"]))
    result = {
        **planned,
        "success": bool(submit_result.get("success")),
        "action": "submit",
        "submit_result": submit_result,
        "job": row,
        "upload_log": upload_log.strip(),
    }
    if wait and submit_result.get("success"):
        result["wait"] = wait_for_job(
            planned["job_spec"]["job_name"],
            interval=wait_interval,
            timeout=wait_timeout,
            cluster=spec.cluster,
            portal_user=spec.portal_user,
            token_file=token_file,
            refresh_token=False,
            refresh_browser=refresh_browser,
            refresh_headless=refresh_headless,
            auth_account=auth_account,
        )
    return result


def cancel_job(
    target: str,
    *,
    confirm: bool = False,
    cluster: str = DEFAULT_CLUSTER,
    portal_user: str = DEFAULT_PORTAL_USER,
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = True,
    auth_account: str | None = None,
) -> dict[str, Any]:
    token = load_portal_token(
        token_file=token_file,
        refresh=refresh_token,
        refresh_browser=refresh_browser,
        refresh_headless=refresh_headless,
        auth_account=auth_account,
    )
    session = create_session()
    rows = query_jobs(
        session,
        token,
        cluster=cluster,
        portal_user=portal_user,
        keyword=target,
        page=0,
        size=20,
        history=False,
    )
    row = select_target_row(rows, target)
    job = normalize_job_row(row)
    if not row:
        return {"success": False, "message": f"no matching current job: {target}", "job": None}
    if not confirm:
        return {
            "success": False,
            "requires_confirmation": True,
            "message": "Set confirm=True to cancel this job.",
            "job": job,
        }
    platform_id = row.get("id")
    if not platform_id:
        raise RuntimeError(f"job has no platform id: {row}")

    result = request_json(
        session,
        "POST",
        f"{BASE_URL}/job/{platform_id}/cancel",
        headers={"PARA_ATOKEN": token},
        data={"cluster_id": cluster},
    )
    return {"success": bool(result.get("success")), "cancel_result": result, "job": job}
