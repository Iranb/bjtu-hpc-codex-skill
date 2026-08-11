#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import paramiko

import hpc_winscp_info as winscp
from hpc_account_store import apply_auth_account_defaults
from hpc_upload import AUTH_ERROR_MESSAGE
from transfer_dataset_to_cluster import (
    DEFAULT_DEST_ROOT,
    DEFAULT_DIR_ITEMS,
    DEFAULT_FILE_ITEMS,
    SOURCE_HOST,
    SOURCE_ROOT,
)


DEFAULT_ITEMS = DEFAULT_FILE_ITEMS + DEFAULT_DIR_ITEMS
DEFAULT_ARCHIVE_SOURCE_DIR = os.getenv(
    "DATASET_ARCHIVE_SOURCE_DIR", "~/dataset/data/_bjtu_upload"
)
DEFAULT_ARCHIVE_DEST_DIR = os.getenv("DATASET_ARCHIVE_DEST_DIR", "")
DEFAULT_TASKS_FILE = Path("hpc_transfer_tasks.json")


@dataclass
class SourceFile:
    rel_path: str
    size: int


@dataclass
class FileStatus:
    rel_path: str
    source_size: int
    final_size: int | None
    part_size: int | None
    status: str
    chunk_size: int = 0

    @property
    def present_size(self) -> int:
        if self.status == "complete":
            return self.source_size
        partial_size = (self.part_size or 0) + self.chunk_size
        candidates = [size for size in (partial_size, self.final_size) if size is not None]
        return min(max(candidates), self.source_size) if candidates else 0

    @property
    def percent(self) -> float:
        if self.source_size <= 0:
            return 100.0
        return 100.0 * self.present_size / self.source_size


def format_bytes(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024.0 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{size} B"


def remote_source_manifest(
    source_host: str,
    source_root: str,
    items: list[str],
    *,
    source_connect_timeout: int,
    source_timeout: int,
) -> list[SourceFile]:
    script = r'''
set -Eeuo pipefail
SOURCE_ROOT="${1/#\~/$HOME}"
shift
cd "$SOURCE_ROOT"
for item in "$@"; do
  if [ -d "$item" ]; then
    find "$item" -type f -printf '%p\t%s\n'
  elif [ -e "$item" ]; then
    size="$(stat -c '%s' -- "$item")"
    printf '%s\t%s\n' "$item" "$size"
  else
    printf 'source path not found: %s/%s\n' "$SOURCE_ROOT" "$item" >&2
    exit 2
  fi
done
'''
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={source_connect_timeout}",
            "-o",
            "ConnectionAttempts=1",
            source_host,
            "bash",
            "-s",
            "--",
            source_root,
            *items,
        ],
        input=script,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=source_timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())

    ordered: list[SourceFile] = []
    seen: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        if "\t" in line:
            rel_path, size_text = line.rsplit("\t", 1)
        elif "\\t" in line:
            rel_path, size_text = line.rsplit("\\t", 1)
        else:
            raise RuntimeError(f"unexpected source manifest line: {line!r}")
        if rel_path in seen:
            continue
        seen.add(rel_path)
        ordered.append(SourceFile(rel_path=rel_path, size=int(size_text)))
    ordered.sort(key=lambda row: row.rel_path)
    return ordered


def archive_sizes_from_tasks(archive_names: list[str], tasks_path: Path = DEFAULT_TASKS_FILE) -> list[SourceFile] | None:
    if not tasks_path.exists():
        return None

    try:
        tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None

    wanted = {name: None for name in archive_names}
    for item in tasks:
        source_path = item.get("source_path") or item.get("source")
        if not source_path or item.get("total_bytes") is None:
            continue
        archive_name = PurePosixPath(str(source_path)).name
        if archive_name in wanted:
            wanted[archive_name] = int(item["total_bytes"])

    if any(size is None for size in wanted.values()):
        return None

    return [SourceFile(rel_path=name, size=int(wanted[name])) for name in archive_names]


def connect_cluster(args):
    auth_args = SimpleNamespace(
        cluster=args.cluster,
        account=args.account,
        portal_user=args.portal_user,
        auth_account=args.auth_account,
        token=args.token,
        token_file=args.token_file,
        refresh_token=args.refresh_token,
        refresh_browser=args.refresh_browser,
        refresh_headless=args.refresh_headless,
    )
    token = winscp.load_auth(auth_args)
    try:
        info = winscp.run(auth_args, token)
    except RuntimeError as error:
        if str(error) != AUTH_ERROR_MESSAGE:
            raise
        token = winscp.refresh_token(
            auth_args.token_file.expanduser(),
            auth_args.refresh_browser,
            auth_args.refresh_headless,
            auth_account=auth_args.auth_account,
        )
        info = winscp.run(auth_args, token)

    host, port = info["proxy"].rsplit(":", 1) if ":" in info["proxy"] else (info["proxy"], "22")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=int(port),
        username=f"{info['cluster']},{info['account']}",
        password=info["certificate"],
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
        auth_timeout=20,
        banner_timeout=20,
    )
    return client, client.open_sftp(), info


def posix_join(root: str, rel_path: str) -> str:
    return str(PurePosixPath(root) / PurePosixPath(rel_path))


def stat_size(sftp, path: str) -> int | None:
    try:
        return int(sftp.stat(path).st_size)
    except IOError:
        return None


def chunk_bytes(sftp, dest_path: str) -> int:
    try:
        entries = sftp.listdir_attr(dest_path + ".chunks")
    except IOError:
        return 0
    return sum(int(entry.st_size) for entry in entries if entry.filename.endswith(".chunk"))


def classify(source: SourceFile, final_size: int | None, part_size: int | None, chunk_size: int) -> FileStatus:
    if final_size == source.size:
        status = "complete"
    elif part_size is not None or chunk_size > 0:
        status = "partial"
    elif final_size is not None:
        status = "mismatch"
    else:
        status = "missing"
    return FileStatus(
        rel_path=source.rel_path,
        source_size=source.size,
        final_size=final_size,
        part_size=part_size,
        chunk_size=chunk_size,
        status=status,
    )


def collect_statuses(sftp, dest_root: str, source_files: list[SourceFile]) -> list[FileStatus]:
    statuses = []
    for source in source_files:
        dest_path = posix_join(dest_root, source.rel_path)
        final_size = stat_size(sftp, dest_path)
        part_size = stat_size(sftp, dest_path + ".part")
        statuses.append(classify(source, final_size, part_size, chunk_bytes(sftp, dest_path)))
    return statuses


def summarize(statuses: list[FileStatus]) -> dict:
    total_bytes = sum(row.source_size for row in statuses)
    complete_bytes = sum(row.source_size for row in statuses if row.status == "complete")
    present_bytes = sum(row.present_size for row in statuses)
    counts = {name: 0 for name in ("complete", "partial", "mismatch", "missing")}
    for row in statuses:
        counts[row.status] = counts.get(row.status, 0) + 1
    return {
        "files": len(statuses),
        "total_bytes": total_bytes,
        "complete_bytes": complete_bytes,
        "present_bytes": present_bytes,
        "complete_percent": 100.0 * complete_bytes / total_bytes if total_bytes else 100.0,
        "present_percent": 100.0 * present_bytes / total_bytes if total_bytes else 100.0,
        "counts": counts,
    }


def print_report(args, statuses: list[FileStatus], cluster_info: dict | None) -> None:
    summary = summarize(statuses)
    source_root = getattr(args, "_report_source_root", args.source_root)
    dest_root = getattr(args, "_report_dest_root", args.dest_root)
    print(f"[time] {time.strftime('%F %T %z')}")
    print(f"[source] {args.source_host}:{source_root}")
    print(f"[dest] {dest_root}")
    if cluster_info:
        print(f"[cluster] {cluster_info['cluster']},{cluster_info['account']} via {cluster_info['proxy']}")
    print(
        "[summary] "
        f"files={summary['files']} "
        f"complete={summary['counts'].get('complete', 0)} "
        f"partial={summary['counts'].get('partial', 0)} "
        f"mismatch={summary['counts'].get('mismatch', 0)} "
        f"missing={summary['counts'].get('missing', 0)}"
    )
    print(
        "[bytes] "
        f"complete={format_bytes(summary['complete_bytes'])}/{format_bytes(summary['total_bytes'])} "
        f"({summary['complete_percent']:.1f}%) "
        f"present_with_partials={format_bytes(summary['present_bytes'])} "
        f"({summary['present_percent']:.1f}%)"
    )

    rows = statuses if args.all else [row for row in statuses if row.status != "complete"]
    if not rows:
        print("[files] all files complete")
        return

    print("[files]")
    for row in rows:
        print(
            f"{row.status:8} {row.percent:6.1f}% "
            f"{format_bytes(row.present_size):>10}/{format_bytes(row.source_size):<10} "
            f"{row.rel_path}"
        )


def print_json(statuses: list[FileStatus], args, cluster_info: dict | None) -> None:
    source_root = getattr(args, "_report_source_root", args.source_root)
    dest_root = getattr(args, "_report_dest_root", args.dest_root)
    payload = {
        "time": time.strftime("%F %T %z"),
        "source": {"host": args.source_host, "root": source_root},
        "dest": {"root": dest_root},
        "cluster": {
            "cluster": cluster_info.get("cluster"),
            "account": cluster_info.get("account"),
            "proxy": cluster_info.get("proxy"),
        } if cluster_info else None,
        "summary": summarize(statuses),
        "files": [
            {
                "path": row.rel_path,
                "status": row.status,
                "source_size": row.source_size,
                "final_size": row.final_size,
                "part_size": row.part_size,
                "chunk_size": row.chunk_size,
                "present_size": row.present_size,
                "percent": row.percent,
            }
            for row in statuses
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Show source-to-BJTU-HPC dataset upload progress by comparing source files with cluster files."
    )
    parser.add_argument("--source-host", default=os.getenv("DATASET_SOURCE_HOST", SOURCE_HOST))
    parser.add_argument("--source-root", default=os.getenv("DATASET_SOURCE_ROOT", SOURCE_ROOT))
    parser.add_argument("--dest-root", default=os.getenv("BJTU_DEST_ROOT", DEFAULT_DEST_ROOT))
    parser.add_argument("--item", action="append", dest="items", help="Relative source item to include; can repeat.")
    parser.add_argument(
        "--archive",
        action="append",
        dest="archives",
        help="Archive filename under --archive-source-dir to compare with --archive-dest-dir; can repeat.",
    )
    parser.add_argument("--archive-source-dir", default=os.getenv("DATASET_ARCHIVE_SOURCE_DIR", DEFAULT_ARCHIVE_SOURCE_DIR))
    parser.add_argument("--archive-dest-dir", default=os.getenv("DATASET_ARCHIVE_DEST_DIR", DEFAULT_ARCHIVE_DEST_DIR))
    parser.add_argument("--all", action="store_true", help="Show completed files too.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--watch", type=int, default=0, help="Refresh every N seconds.")
    parser.add_argument("--source-connect-timeout", type=int, default=5, help="Seconds for source-host SSH connection attempts.")
    parser.add_argument("--source-timeout", type=int, default=30, help="Seconds before a source-host manifest command times out.")
    default_cluster = os.getenv("HPC_CLUSTER", "cluster2")
    default_account = os.getenv("HPC_ACCOUNT")
    default_portal_user = os.getenv("HPC_PORTAL_USER", "")
    parser.add_argument("--cluster", default=default_cluster)
    parser.add_argument("--account", default=default_account)
    parser.add_argument("--portal-user", default=default_portal_user)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"), help="Saved auth account name from hpc_accounts.py")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=Path("~/.bjtu_hpc_token"))
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    args = parser.parse_args()
    apply_auth_account_defaults(
        args,
        default_cluster=default_cluster,
        default_account=default_account,
        default_portal_user=default_portal_user,
    )
    if not args.items and not args.archives:
        parser.error("provide at least one --item or --archive")
    if args.archives and not args.archive_dest_dir:
        parser.error("--archive-dest-dir or DATASET_ARCHIVE_DEST_DIR is required")
    if not args.archives and not args.dest_root:
        parser.error("--dest-root or BJTU_DEST_ROOT is required")
    return args


def run_once(args) -> int:
    if args.archives:
        items = args.archives
        source_root = args.archive_source_dir
        dest_root = args.archive_dest_dir
        source_files = archive_sizes_from_tasks(items)
    else:
        items = args.items or DEFAULT_ITEMS
        source_root = args.source_root
        dest_root = args.dest_root
        source_files = None

    args._report_source_root = source_root
    args._report_dest_root = dest_root
    if source_files is None:
        source_files = remote_source_manifest(
            args.source_host,
            source_root,
            items,
            source_connect_timeout=args.source_connect_timeout,
            source_timeout=args.source_timeout,
        )
    client, sftp, cluster_info = connect_cluster(args)
    try:
        statuses = collect_statuses(sftp, dest_root, source_files)
    finally:
        sftp.close()
        client.close()

    if args.json:
        print_json(statuses, args, cluster_info)
    else:
        print_report(args, statuses, cluster_info)
    return 0


def main() -> int:
    args = parse_args()
    if args.watch and args.json:
        print("--watch cannot be combined with --json", file=sys.stderr)
        return 2

    while True:
        try:
            run_once(args)
        except KeyboardInterrupt:
            return 130
        if not args.watch:
            return 0
        time.sleep(args.watch)
        print()


if __name__ == "__main__":
    raise SystemExit(main())
