import shlex
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hpc_transfer_app import (
    DEFAULT_CONFIG,
    DEFAULT_REMOTE_WORKDIR,
    DEFAULT_SOURCE_HOST,
    UploadTask,
    derive_archive_name,
    get_remote_task_state,
    load_tasks,
)
from transfer_dataset_to_cluster import (
    DEFAULT_DEST_ROOT,
    DEFAULT_DIR_ITEMS,
    DEFAULT_FILE_ITEMS,
    SOURCE_HOST,
    SOURCE_ROOT,
)


def workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent


def normalize_config_path(path_text: str | None) -> Path:
    if not path_text:
        return DEFAULT_CONFIG
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = workspace_root() / path
    return path


def safe_task_name(value: str) -> str:
    clean = value.strip()
    if not clean or "/" in clean:
        raise ValueError("task name/screen name cannot be empty or contain '/'.")
    return clean


def task_from_inputs(
    *,
    task_name: str | None = None,
    source_host: str | None = None,
    source_path: str | None = None,
    dest_path: str | None = None,
    pack: bool = True,
    screen_name: str | None = None,
    remote_workdir: str | None = None,
    config_path: str | None = None,
) -> tuple[UploadTask, bool]:
    if task_name:
        config = normalize_config_path(config_path)
        tasks = {item["name"]: item for item in load_tasks(config)}
        task_data = tasks.get(task_name)
        if not task_data:
            raise ValueError(f"upload task not found: {task_name}")
        return UploadTask(**task_data), True

    if not source_path:
        raise ValueError("source_path is required when task_name is not provided.")
    if not dest_path:
        raise ValueError("dest_path is required when task_name is not provided.")

    name = safe_task_name(screen_name or "bjtu-mcp-upload")
    return (
        UploadTask(
            name=name,
            source_host=source_host or DEFAULT_SOURCE_HOST,
            source_path=source_path,
            dest_path=dest_path,
            pack=pack,
            screen_name=name,
            remote_workdir=remote_workdir or DEFAULT_REMOTE_WORKDIR,
        ),
        False,
    )


def get_dataset_locations(
    *,
    include_tasks: bool = True,
    include_task_state: bool = False,
    config_path: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "success": True,
        "manifest": {
            "source_host": SOURCE_HOST,
            "source_root": SOURCE_ROOT,
            "dest_root": DEFAULT_DEST_ROOT,
            "file_items": list(DEFAULT_FILE_ITEMS),
            "dir_items": list(DEFAULT_DIR_ITEMS),
        },
        "upload_defaults": {
            "source_host": DEFAULT_SOURCE_HOST,
            "remote_workdir": DEFAULT_REMOTE_WORKDIR,
            "config_path": str(normalize_config_path(config_path)),
        },
    }

    if include_tasks:
        config = normalize_config_path(config_path)
        tasks = load_tasks(config) if config.exists() else []
        normalized_tasks = []
        for task in tasks:
            item = dict(task)
            if include_task_state:
                item["state"] = get_remote_task_state(task)
            normalized_tasks.append(item)
        result["saved_upload_tasks"] = normalized_tasks
    return result


def plan_dataset_upload(
    *,
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
    task, loaded_from_config = task_from_inputs(
        task_name=task_name,
        source_host=source_host,
        source_path=source_path,
        dest_path=dest_path,
        pack=pack,
        screen_name=screen_name,
        remote_workdir=remote_workdir,
        config_path=config_path,
    )
    archive = archive_name or derive_archive_name(task)
    source_file = f"{task.remote_workdir.rstrip('/')}/{archive}" if task.pack else task.source_path
    state_file = f"{task.remote_workdir.rstrip('/')}/{task.screen_name}.state.json"
    log_file = f"{task.remote_workdir.rstrip('/')}/{task.screen_name}.log"

    return {
        "success": True,
        "action": "plan",
        "loaded_from_config": loaded_from_config,
        "task": asdict(task),
        "archive_name": archive if task.pack else None,
        "source_file_for_upload": source_file,
        "dest_file": task.dest_path,
        "state_file": f"{task.source_host}:{state_file}",
        "log_file": f"{task.source_host}:{log_file}",
        "notes": [
            "This plan does not write hpc_transfer_tasks.json.",
            "If pack=true, the source is tarred on source_host before the resumable upload starts.",
            "Set confirm=True on hpc_start_dataset_upload to perform the action.",
        ],
    }


def plan_external_hdf5_artifact(
    *,
    contract_path: str,
    inventory_path: str,
) -> dict[str, Any]:
    """Plan the external-HDF5 path without invoking legacy tar/upload code."""

    from external_hdf5_artifact import plan_artifact

    plan = plan_artifact(contract_path, inventory_path)
    return {
        **plan,
        "mode": "external_hdf5",
        "raw_data_on_hpc": False,
        "archive_on_hpc": False,
        "next_action": "build_and_validate_on_external_factory",
        "notes": [
            "This entry point is read-only and never calls pack_remote_source().",
            "Use hpc_data_supply.py for quota-bound placement after an attestation exists.",
            "Production remote mutation remains disabled until its native adapter is explicitly enabled.",
        ],
    }


def run_captured(command: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=workspace_root(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def pack_remote_source(
    task: UploadTask,
    archive_name: str,
    *,
    timeout: int | None = None,
) -> tuple[str, str, str]:
    remote_archive = f"{task.remote_workdir.rstrip('/')}/{archive_name}"
    q = shlex.quote
    source_q = q(task.source_path)
    archive_q = q(remote_archive)
    mkdir_cmd = f"mkdir -p {q(task.remote_workdir)}"
    tar_cmd = (
        f"{mkdir_cmd} && "
        f"SRC={source_q}; SRC=${{SRC/#\\~/$HOME}}; "
        f"DIR=$(dirname \"$SRC\"); BASE=$(basename \"$SRC\"); cd \"$DIR\" && "
        f"if command -v pigz >/dev/null 2>&1; then "
        f"tar -I 'pigz -1 -p 8' -cf {archive_q} \"$BASE\"; "
        f"else tar -czf {archive_q} \"$BASE\"; fi"
    )
    result = run_captured(["ssh", task.source_host, tar_cmd], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "remote packing failed")
    return remote_archive, result.stdout, result.stderr


def start_dataset_upload(
    *,
    task_name: str | None = None,
    source_host: str | None = None,
    source_path: str | None = None,
    dest_path: str | None = None,
    pack: bool = True,
    screen_name: str | None = None,
    remote_workdir: str | None = None,
    archive_name: str | None = None,
    confirm: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = False,
    no_auto_refresh_token: bool = False,
    pack_timeout: int = 0,
    upload_method: str = "parallel-chunk",
    parallel: int = 4,
    chunk_mib: int = 8,
    buffer_mib: int = 4,
    config_path: str | None = None,
) -> dict[str, Any]:
    plan = plan_dataset_upload(
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
    if not confirm:
        return {
            **plan,
            "success": False,
            "requires_confirmation": True,
            "message": "Set confirm=True to pack and/or start the resumable upload.",
        }

    task = UploadTask(**plan["task"])
    source_file = plan["source_file_for_upload"]
    pack_stdout = ""
    pack_stderr = ""
    if task.pack:
        packed, pack_stdout, pack_stderr = pack_remote_source(
            task,
            plan["archive_name"],
            timeout=pack_timeout or None,
        )
        source_file = packed

    command = [
        sys.executable,
        str(workspace_root() / "start_resumable_upload.py"),
        "--source-file",
        source_file,
        "--dest-file",
        task.dest_path,
        "--source-host",
        task.source_host,
        "--screen-name",
        task.screen_name,
        "--remote-workdir",
        task.remote_workdir,
        "--method",
        upload_method,
        "--refresh-browser",
        refresh_browser,
    ]
    if refresh_headless:
        command.append("--refresh-headless")
    if no_auto_refresh_token:
        command.append("--no-auto-refresh-token")
    if upload_method == "parallel-chunk":
        command.extend(["--parallel", str(parallel), "--chunk-mib", str(chunk_mib), "--buffer-mib", str(buffer_mib)])

    result = run_captured(command)
    return {
        **plan,
        "success": result.returncode == 0,
        "action": "start_upload",
        "source_file_for_upload": source_file,
        "command": command[:1] + ["start_resumable_upload.py", *command[2:]],
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "pack_stdout": pack_stdout,
        "pack_stderr": pack_stderr,
    }
