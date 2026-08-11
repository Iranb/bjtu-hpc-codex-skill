#!/usr/bin/env python3
import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import paramiko


SOURCE_HOST = os.getenv("DATASET_SOURCE_HOST", "")
SOURCE_ROOT = os.getenv("DATASET_SOURCE_ROOT", "~/dataset/data")
DEFAULT_DEST_ROOT = os.getenv("BJTU_DEST_ROOT", "")

# Dataset selections are controller-private. Configure them with repeated
# ``--item`` options instead of committing a local inventory.
DEFAULT_FILE_ITEMS: list[str] = []
DEFAULT_DIR_ITEMS: list[str] = []


def run_ssh_text(command, check=True):
    if not SOURCE_HOST:
        raise RuntimeError("DATASET_SOURCE_HOST is required")
    proc = subprocess.run(
        ["ssh", SOURCE_HOST, command],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"ssh {SOURCE_HOST} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def remote_exists(rel_path):
    cmd = f"cd {SOURCE_ROOT} && test -e {shlex.quote(rel_path)}"
    proc = subprocess.run(["ssh", SOURCE_HOST, cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def remote_is_dir(rel_path):
    cmd = f"cd {SOURCE_ROOT} && test -d {shlex.quote(rel_path)}"
    proc = subprocess.run(["ssh", SOURCE_HOST, cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def remote_size(rel_path):
    cmd = f"cd {SOURCE_ROOT} && stat -c %s -- {shlex.quote(rel_path)}"
    out = run_ssh_text(cmd).strip()
    return int(out)


def list_dir_files(rel_dir):
    cmd = (
        f"cd {SOURCE_ROOT} && "
        f"find {shlex.quote(rel_dir)} -type f -printf '%P\\0'"
    )
    proc = subprocess.run(
        ["ssh", SOURCE_HOST, cmd],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    data = proc.stdout.decode("utf-8", errors="surrogateescape")
    # Split NUL-terminated records.
    rel_files = [part for part in data.split("\x00") if part]
    return [f"{rel_dir}/{name}" for name in rel_files]


def ensure_remote_dir(sftp, remote_dir):
    remote_dir = remote_dir.rstrip("/")
    if not remote_dir:
        return

    parts = []
    path = Path(remote_dir)
    for part in path.parts:
        parts.append(part)
        current = str(Path(*parts))
        try:
            sftp.stat(current)
        except IOError:
            try:
                sftp.mkdir(current)
            except IOError:
                # Another process may have created it between stat and mkdir.
                try:
                    sftp.stat(current)
                except IOError as exc:
                    raise RuntimeError(f"cannot create remote directory {current}: {exc}") from exc


def parse_cluster_info():
    out = subprocess.check_output(
        [sys.executable, "hpc_winscp_info.py", "--show-secret"],
        text=True,
    )
    info = {}
    for line in out.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        info[key.strip()] = value.strip()
    proxy_host, proxy_port = info["ssh_proxy"].split(":", 1)
    return {
        "cluster": info["cluster"],
        "account": info["account"],
        "proxy_host": proxy_host,
        "proxy_port": int(proxy_port),
        "home": info["home"],
        "certificate": info["certificate_token"],
    }


def connect_cluster():
    info = parse_cluster_info()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    username = f"{info['cluster']},{info['account']}"
    client.connect(
        info["proxy_host"],
        port=info["proxy_port"],
        username=username,
        password=info["certificate"],
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    sftp = client.open_sftp()
    return client, sftp, info


def remote_temp_path(dest_path):
    return dest_path + ".part"


def transfer_file(sftp, rel_path, dest_root, dry_run=False):
    src_path = f"{SOURCE_ROOT}/{rel_path}"
    dst_path = f"{dest_root}/{rel_path}"
    dst_dir = str(Path(dst_path).parent)
    ensure_remote_dir(sftp, dst_dir)

    src_size = remote_size(rel_path)
    try:
        dst_stat = sftp.stat(dst_path)
        if dst_stat.st_size == src_size:
            print(f"[skip] {rel_path} ({src_size} bytes)")
            return
    except IOError:
        pass

    if dry_run:
        print(f"[plan] {rel_path} ({src_size} bytes)")
        return

    tmp_path = remote_temp_path(dst_path)
    for path in (tmp_path, dst_path):
        try:
            sftp.remove(path)
        except IOError:
            pass

    ensure_remote_dir(sftp, str(Path(tmp_path).parent))

    cmd = f"cd {SOURCE_ROOT} && cat -- {shlex.quote(rel_path)}"
    proc = subprocess.Popen(
        ["ssh", SOURCE_HOST, cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )

    start = time.monotonic()
    written = 0
    remote_file = sftp.open(tmp_path, "wb")
    try:
        while True:
            chunk = proc.stdout.read(8 * 1024 * 1024)
            if not chunk:
                break
            remote_file.write(chunk)
            written += len(chunk)
            elapsed = max(time.monotonic() - start, 0.001)
            speed = written / elapsed
            percent = 100.0 * written / src_size if src_size else 100.0
            print(
                f"\r[copy] {rel_path} {written}/{src_size} bytes "
                f"({percent:5.1f}%) {speed / (1024 * 1024):.1f} MiB/s",
                end="",
                flush=True,
            )
        rc = proc.wait()
        if rc != 0:
            stderr = proc.stderr.read().decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"source transfer failed for {rel_path}: {stderr}")
        remote_file.flush()
    except Exception:
        remote_file.close()
        try:
            sftp.remove(tmp_path)
        except IOError:
            pass
        raise
    else:
        remote_file.close()
        try:
            sftp.remove(dst_path)
        except IOError:
            pass
        sftp.rename(tmp_path, dst_path)
        elapsed = max(time.monotonic() - start, 0.001)
        speed = written / elapsed
        print(
            f"\r[done] {rel_path} {written} bytes in {elapsed:.1f}s "
            f"({speed / (1024 * 1024):.1f} MiB/s)".ljust(120)
        )


def build_plan(items):
    file_entries = []
    for item in items:
        if remote_is_dir(item):
            for rel_file in list_dir_files(item):
                file_entries.append(rel_file)
        else:
            if not remote_exists(item):
                raise FileNotFoundError(f"source path not found: {item}")
            file_entries.append(item)
    # Stable order, remove duplicates while keeping first occurrence.
    seen = set()
    ordered = []
    for entry in file_entries:
        if entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return ordered


def parse_args():
    parser = argparse.ArgumentParser(description="Transfer BJTU source-server datasets to the cluster.")
    parser.add_argument(
        "--dest-root",
        default=DEFAULT_DEST_ROOT,
        help="Destination directory on the cluster.",
    )
    parser.add_argument(
        "--item",
        action="append",
        dest="items",
        help="Relative path under ~/dataset/data to transfer. Can be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned files and sizes without transferring.",
    )
    args = parser.parse_args()
    if not args.dest_root:
        parser.error("--dest-root or BJTU_DEST_ROOT is required")
    if not SOURCE_HOST:
        parser.error("DATASET_SOURCE_HOST is required")
    if not args.items:
        parser.error("at least one --item is required")
    return args


def main():
    args = parse_args()
    items = args.items or (DEFAULT_FILE_ITEMS + DEFAULT_DIR_ITEMS)
    print("[info] resolving source manifest...")
    files = build_plan(items)
    total_bytes = 0
    file_sizes = {}
    for rel_path in files:
        size = remote_size(rel_path)
        file_sizes[rel_path] = size
        total_bytes += size

    print(f"[info] files: {len(files)} total_bytes: {total_bytes}")
    for rel_path in files:
        print(f"  - {rel_path} ({file_sizes[rel_path]} bytes)")

    if args.dry_run:
        return 0

    client, sftp, info = connect_cluster()
    print(
        f"[info] connected to cluster proxy {info['proxy_host']}:{info['proxy_port']} "
        f"as {info['cluster']},{info['account']}"
    )
    try:
        ensure_remote_dir(sftp, args.dest_root)
        done_bytes = 0
        for rel_path in files:
            transfer_file(sftp, rel_path, args.dest_root)
            done_bytes += file_sizes[rel_path]
            print(
                f"[progress] {done_bytes}/{total_bytes} bytes "
                f"({100.0 * done_bytes / total_bytes:.1f}%)"
            )
        print("[done] all selected files transferred")
    finally:
        sftp.close()
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
