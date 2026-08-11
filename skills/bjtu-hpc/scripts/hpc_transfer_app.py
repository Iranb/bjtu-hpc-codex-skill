#!/usr/bin/env python3
"""Small task manager for BJTU HPC uploads and job progress."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


DEFAULT_CONFIG = Path("hpc_transfer_tasks.json")
DEFAULT_REMOTE_WORKDIR = os.getenv("BJTU_SOURCE_WORKDIR", "~/.cache/bjtu-hpc-transfer")
DEFAULT_SOURCE_HOST = os.getenv("DATASET_SOURCE_HOST", "")
DEFAULT_CLUSTER_STATE_ROOT = os.getenv("BJTU_CLUSTER_STATE_ROOT", "")


@dataclass
class UploadTask:
    name: str
    source_host: str
    source_path: str
    dest_path: str
    pack: bool = True
    screen_name: str = "bjtu-resume-upload"
    remote_workdir: str = DEFAULT_REMOTE_WORKDIR
    total_bytes: int | None = None
    auth_account: str | None = None


def run(cmd: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def load_tasks(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [normalize_task(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def normalize_task(item: dict) -> dict:
    source_path = item.get("source_path") or item.get("source")
    dest_path = item.get("dest_path") or item.get("dest")
    if not item.get("name") or not source_path or not dest_path:
        raise ValueError(f"invalid upload task: {item}")
    source_host = item.get("source_host") or item.get("source_server") or DEFAULT_SOURCE_HOST
    if not source_host:
        raise ValueError("upload task requires source_host or DATASET_SOURCE_HOST")
    normalized = {
        "name": item["name"],
        "source_host": source_host,
        "source_path": source_path,
        "dest_path": dest_path,
        "pack": bool(item.get("pack", True)),
        "screen_name": item.get("screen_name") or f"bjtu-{item['name']}",
        "remote_workdir": item.get("remote_workdir") or DEFAULT_REMOTE_WORKDIR,
    }
    if item.get("total_bytes") is not None:
        normalized["total_bytes"] = int(item["total_bytes"])
    if item.get("auth_account"):
        normalized["auth_account"] = str(item["auth_account"]).strip()
    return normalized


def save_tasks(path: Path, tasks: list[dict]) -> None:
    path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def upsert_task(path: Path, task: UploadTask) -> None:
    tasks = load_tasks(path)
    filtered = [item for item in tasks if item.get("name") != task.name]
    filtered.append(asdict(task))
    save_tasks(path, filtered)


def derive_archive_name(task: UploadTask) -> str:
    stem = PurePosixPath(task.source_path.rstrip("/")).name or task.name
    archive_suffixes = (".tar.gz", ".tgz", ".tar", ".zip", ".gz", ".bz2", ".xz", ".7z")
    if task.pack and not stem.endswith(archive_suffixes):
        return f"{stem}.tar.gz"
    return stem


def ssh_stdout(host: str, command: str) -> str:
    proc = run(["ssh", host, command])
    return proc.stdout


def get_remote_task_state(task: dict) -> dict | None:
    state_file = f"{task['remote_workdir'].rstrip('/')}/{task['screen_name']}.state.json"
    proc = subprocess.run(
        [
            "ssh",
            "-n",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "ConnectionAttempts=1",
            task["source_host"],
            f"cat {shlex.quote(state_file)}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=4,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def remote_file_size(host: str, path: str) -> int | None:
    proc = subprocess.run(
        [
            "ssh",
            "-n",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=3",
            "-o",
            "ConnectionAttempts=1",
            host,
            f"stat -c %s -- {shlex.quote(path)}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=4,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def show_tasks(path: Path) -> None:
    tasks = load_tasks(path)
    if not tasks:
        print("[tasks] none")
        return
    for task in tasks:
        state = get_remote_task_state(task)
        print(f"- {task['name']}")
        print(f"  source_host: {task['source_host']}")
        print(f"  source_path: {task['source_path']}")
        print(f"  dest_path:   {task['dest_path']}")
        print(f"  pack:   {task.get('pack', True)}")
        if state:
            print(
                f"  upload: {state.get('status')} {state.get('present_bytes', 0)}/{state.get('total_bytes', 0)} "
                f"({state.get('percent', 0):.2f}%)"
            )
        else:
            print("  upload: no state yet")


def create_task(args: argparse.Namespace) -> None:
    task = UploadTask(
        name=args.name,
        source_host=args.source_host,
        source_path=args.source_path,
        dest_path=args.dest_path,
        pack=not args.no_pack,
        screen_name=args.screen_name or f"bjtu-{args.name}",
        remote_workdir=args.remote_workdir,
        total_bytes=args.total_bytes,
        auth_account=args.auth_account,
    )
    upsert_task(args.config, task)
    print(f"[saved] {task.name} -> {args.config}")


def pack_remote_source(task: UploadTask, archive_name: str) -> str:
    remote_archive = f"{task.remote_workdir.rstrip('/')}/{archive_name}"
    source = task.source_path
    source_q = shlex.quote(source)
    archive_q = shlex.quote(remote_archive)
    if archive_name.endswith(".tar"):
        pack_command = f"tar -cf {archive_q} \"$BASE\""
    else:
        pack_command = (
            f"if command -v pigz >/dev/null 2>&1; then "
            f"tar -I 'pigz -1 -p 8' -cf {archive_q} \"$BASE\"; "
            f"else tar -czf {archive_q} \"$BASE\"; fi"
        )
    tar_cmd = (
        f"SRC={source_q}; SRC=${{SRC/#\\~/$HOME}}; "
        f"DIR=$(dirname \"$SRC\"); BASE=$(basename \"$SRC\"); cd \"$DIR\" && "
        f"{pack_command}"
    )
    run(["ssh", task.source_host, tar_cmd])
    return remote_archive


def launch_upload(
    task: UploadTask,
    archive_name: str | None = None,
    dry_run: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = False,
    no_auto_refresh_token: bool = False,
    upload_method: str = "parallel-chunk",
    parallel: int = 4,
    chunk_mib: int = 8,
    buffer_mib: int = 4,
    source_python: str = "python3",
) -> None:
    archive_name = archive_name or derive_archive_name(task)
    source_file = f"{task.remote_workdir.rstrip('/')}/{archive_name}" if task.pack else task.source_path
    if dry_run:
        print(f"[dry-run] task={task.name}")
        print(f"[dry-run] source_host={task.source_host}")
        print(f"[dry-run] source_path={task.source_path}")
        print(f"[dry-run] packed_source={source_file if task.pack else '(none)'}")
        print(f"[dry-run] dest_path={task.dest_path}")
        print(f"[dry-run] method={upload_method}")
        if upload_method == "parallel-chunk":
            print(f"[dry-run] parallel={parallel}")
            print(f"[dry-run] chunk_mib={chunk_mib}")
            print(f"[dry-run] buffer_mib={buffer_mib}")
        print("[dry-run] token: fetched automatically when run without --dry-run")
        return

    if task.pack:
        source_file = pack_remote_source(task, archive_name)
    launcher = Path(__file__).with_name("start_resumable_upload.py")
    cmd = [
        sys.executable,
        str(launcher),
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
        "--source-python",
        source_python,
    ]
    if task.auth_account:
        cmd.extend(["--auth-account", task.auth_account])
    if refresh_headless:
        cmd.append("--refresh-headless")
    if no_auto_refresh_token:
        cmd.append("--no-auto-refresh-token")
    if upload_method == "parallel-chunk":
        cmd.extend(["--parallel", str(parallel), "--chunk-mib", str(chunk_mib), "--buffer-mib", str(buffer_mib)])
    subprocess.run(cmd, check=True)


def show_jobs(args: argparse.Namespace) -> None:
    cmd = [sys.executable, "hpc_jobs.py", "list", "--json"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0 and not args.no_auto_refresh_token:
        refresh_cmd = [
            *cmd,
            "--refresh-token",
            "--refresh-browser",
            args.refresh_browser,
        ]
        if args.refresh_headless:
            refresh_cmd.append("--refresh-headless")
        proc = subprocess.run(refresh_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "hpc_jobs.py failed")
    jobs = json.loads(proc.stdout)
    if not jobs:
        print("[jobs] none")
        return
    for row in jobs:
        print(
            f"- {row.get('name')} state={row.get('state')} origin={row.get('originState')} "
            f"done={row.get('done')} nodes={row.get('nodes')}"
        )


def dashboard(args: argparse.Namespace) -> None:
    while True:
        os.system("clear")
        print("== Upload Tasks ==")
        show_tasks(args.config)
        print()
        print("== Jobs ==")
        show_jobs(args)
        if not args.watch:
            return
        time.sleep(args.watch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BJTU HPC transfer task app")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--no-auto-refresh-token", action="store_true", help="Do not refresh the portal token automatically on auth failure.")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="Create or update an upload task")
    add.add_argument("name")
    add.add_argument("--source-host", "--source-server", default=DEFAULT_SOURCE_HOST)
    add.add_argument("--source-path", "--source", dest="source_path", required=True)
    add.add_argument("--dest-path", "--dest", dest="dest_path", required=True)
    add.add_argument("--screen-name")
    add.add_argument("--remote-workdir", default=DEFAULT_REMOTE_WORKDIR)
    add.add_argument("--total-bytes", type=int, help="Optional known source size for progress fallback.")
    add.add_argument("--auth-account", help="Saved portal auth account to use for this upload task.")
    add.add_argument("--no-pack", action="store_true")

    run_cmd = sub.add_parser("run", help="Launch an upload task")
    run_cmd.add_argument("name")
    run_cmd.add_argument("--archive-name")
    run_cmd.add_argument("--method", choices=["rsync", "paramiko", "parallel-chunk"], default="parallel-chunk")
    run_cmd.add_argument("--parallel", type=int, default=4, help="Parallel workers for --method parallel-chunk.")
    run_cmd.add_argument("--chunk-mib", type=int, default=8, help="Chunk size for --method parallel-chunk.")
    run_cmd.add_argument("--buffer-mib", type=int, default=4, help="Read buffer size for --method parallel-chunk.")
    run_cmd.add_argument("--source-python", default="python3", help="Python executable to use on the source host.")
    run_cmd.add_argument("--dry-run", action="store_true")

    sub.add_parser("tasks", help="Show configured upload tasks")
    sub.add_parser("jobs", help="Show HPC jobs")

    dash = sub.add_parser("dashboard", help="Show tasks and jobs together")
    dash.add_argument("--watch", type=int, default=0)

    parser.set_defaults(command="dashboard")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "add":
        create_task(args)
        return 0
    if args.command == "run":
        tasks = {item["name"]: item for item in load_tasks(args.config)}
        task = tasks.get(args.name)
        if not task:
            print(f"task not found: {args.name}", file=sys.stderr)
            return 1
        launch_upload(
            UploadTask(**task),
            archive_name=args.archive_name,
            dry_run=args.dry_run,
            refresh_browser=args.refresh_browser,
            refresh_headless=args.refresh_headless,
            no_auto_refresh_token=args.no_auto_refresh_token,
            upload_method=args.method,
            parallel=args.parallel,
            chunk_mib=args.chunk_mib,
            buffer_mib=args.buffer_mib,
            source_python=args.source_python,
        )
        return 0
    if args.command == "tasks":
        show_tasks(args.config)
        return 0
    if args.command == "jobs":
        show_jobs(args)
        return 0
    dashboard(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
