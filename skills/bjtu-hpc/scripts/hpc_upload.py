#!/usr/bin/env python3.12
import argparse
import base64
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from hpc_runtime import require_controller_python

require_controller_python()

warnings.filterwarnings("ignore", message=r"urllib3 .*doesn't match.*")

import requests

from hpc_account_store import AccountStoreError, apply_auth_account_defaults, token_for_account

BASE_URL = "https://hpc.bjtu.edu.cn/pcp"
CHUNK_SIZE = 2 * 1024 * 1024
DEFAULT_TOKEN_FILE = Path(os.getenv("HPC_PARA_ATOKEN_FILE", "~/.bjtu_hpc_token")).expanduser()
AUTH_ERROR_CODES = {11009, 11011, 11012}
AUTH_ERROR_MESSAGE = "HPC token is missing or expired. Run: python3 hpc_refresh_token.py"


@dataclass
class UploadItem:
    local_path: Path
    relative_path: str
    remote_dir: str
    size: int


class ProgressPrinter:
    def __init__(self, total_bytes, total_files, enabled=True):
        self.total_bytes = max(total_bytes, 0)
        self.total_files = max(total_files, 0)
        self.enabled = enabled
        self.done_bytes = 0
        self.done_files = 0
        self.start = time.monotonic()
        self.last_render = 0.0
        self.current = ""

    def set_current(self, current):
        self.current = current
        self.render()

    def add_bytes(self, count):
        self.done_bytes = min(self.total_bytes, self.done_bytes + max(count, 0))
        self.render()

    def file_done(self):
        self.done_files = min(self.total_files, self.done_files + 1)
        self.render(force=True)

    def finish(self):
        self.done_bytes = self.total_bytes
        self.done_files = self.total_files
        self.render(force=True)
        if self.enabled:
            print(file=sys.stderr)

    def render(self, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last_render < 0.2:
            return
        self.last_render = now

        percent = 100.0 if self.total_bytes == 0 else self.done_bytes * 100.0 / self.total_bytes
        elapsed = max(now - self.start, 0.001)
        speed = self.done_bytes / elapsed
        remaining = max(self.total_bytes - self.done_bytes, 0)
        eta = remaining / speed if speed > 0 else 0

        width = 28
        filled = width if self.total_bytes == 0 else int(width * self.done_bytes / self.total_bytes)
        bar = "#" * filled + "-" * (width - filled)
        label = truncate_middle(self.current, 42)
        line = (
            f"\r[{bar}] {percent:6.2f}% "
            f"{format_bytes(self.done_bytes)}/{format_bytes(self.total_bytes)} "
            f"{format_bytes(speed)}/s eta {format_seconds(eta)} "
            f"files {self.done_files}/{self.total_files} {label}"
        )
        print(line, end="", file=sys.stderr, flush=True)


def truncate_middle(text, max_len):
    if len(text) <= max_len:
        return text
    keep = max_len - 3
    left = keep // 2
    right = keep - left
    return text[:left] + "..." + text[-right:]


def format_bytes(value):
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)}{unit}"
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def format_seconds(value):
    value = int(max(value, 0))
    if value < 60:
        return f"{value}s"
    minutes, seconds = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def md5_hex(path):
    digest = hashlib.md5()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def md5_base64_urlsafe(data):
    digest = hashlib.md5(data).digest()
    return base64.b64encode(digest).decode("ascii").replace("+", "-").replace("/", "_")


def request_json(session, method, url, **kwargs):
    response = session.request(method, url, **kwargs)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}

    if isinstance(data, dict) and data.get("code") in AUTH_ERROR_CODES:
        raise RuntimeError(AUTH_ERROR_MESSAGE)

    if not response.ok:
        raise RuntimeError(f"{method} {url} failed: HTTP {response.status_code} {data}")

    return data


def parse_ranges(value, file_size):
    if not value:
        return []

    ranges = []
    for part in value.split(","):
        start_text, end_text = part.split("-", 1)
        start = int(start_text)
        end = int(end_text)

        offset = start
        while offset <= end:
            chunk_end = min(offset + CHUNK_SIZE - 1, end, file_size - 1)
            ranges.append((offset, chunk_end))
            offset = chunk_end + 1

    return ranges


def normalize_remote_dir(cluster, account, remote_dir):
    clean = remote_dir.strip("/")
    if clean:
        if clean.startswith("[PATH]/"):
            return clean
        return f"[PATH]/{cluster}/{account}/{clean}"
    return f"[PATH]/{cluster}/{account}"


def join_remote_path(*parts):
    clean_parts = []
    for part in parts:
        part = str(part)
        if not part or part == ".":
            continue
        if part == "[PATH]":
            clean_parts.append(part)
        else:
            clean_parts.append(part.strip("/"))
    if not clean_parts:
        return ""
    result = clean_parts[0]
    for part in clean_parts[1:]:
        if result.endswith("/"):
            result += part
        else:
            result += "/" + part
    return result


def load_token(cli_token, token_file, auth_account=None):
    if cli_token:
        return cli_token.strip()

    auth_account = auth_account or os.getenv("HPC_AUTH_ACCOUNT")
    if auth_account:
        try:
            return token_for_account(auth_account)
        except AccountStoreError as error:
            raise RuntimeError(str(error)) from error

    if token_file and token_file.is_file():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token

    return None


def refresh_token(token_file, browser, headless, auth_account=None):
    auth_account = auth_account or os.getenv("HPC_AUTH_ACCOUNT")
    if auth_account:
        script = Path(__file__).with_name("hpc_accounts.py")
        command = [
            sys.executable,
            str(script),
            "refresh",
            auth_account,
            "--browser",
            browser,
        ]
        if headless:
            command.append("--headless")
        subprocess.run(command, check=True)
        return load_token(None, token_file.expanduser(), auth_account=auth_account)

    script = Path(__file__).with_name("hpc_refresh_token.py")
    command = [
        sys.executable,
        str(script),
        "--browser",
        browser,
        "--token-file",
        str(token_file.expanduser()),
    ]
    if headless:
        command.append("--headless")
    subprocess.run(command, check=True)
    return load_token(None, token_file.expanduser())


def create_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session


def log_json(prefix, data, verbose):
    if verbose:
        print(prefix, json.dumps(data, ensure_ascii=False))


def check_quota(session, cluster, total_size, token, verbose):
    check_url = f"{BASE_URL}/upload/check?cluster_id={quote(cluster)}&size={(total_size + 1023) // 1024}"
    check = request_json(session, "GET", check_url, headers={"PARA_ATOKEN": token})
    log_json("[check]", check, verbose)
    if isinstance(check, dict) and check.get("success") is False:
        raise RuntimeError(f"quota check failed: {check}")


def mkdir_remote(session, cluster, account, remote_path, token, verbose):
    url = f"{BASE_URL}/clusters/{quote(cluster)}/file/mkdir"
    data = {"path": remote_path, "cluster_id": cluster, "user_name": account}
    result = request_json(
        session,
        "POST",
        url,
        headers={"PARA_ATOKEN": token},
        data=data,
    )
    log_json("[mkdir]", result, verbose)

    if isinstance(result, dict) and result.get("success") is False:
        message = str(result.get("msg") or result.get("message") or result)
        lowered = message.lower()
        if "exist" not in lowered and "exists" not in lowered and "already" not in lowered and "存在" not in message:
            raise RuntimeError(f"mkdir failed for {remote_path}: {result}")


def collect_upload_items(local_path, cluster, account, remote_dir, include_parent_dir, excludes, follow_symlinks):
    local_path = Path(local_path).expanduser().resolve()
    remote_base = normalize_remote_dir(cluster, account, remote_dir)

    if local_path.is_file():
        return [
            UploadItem(
                local_path=local_path,
                relative_path=local_path.name,
                remote_dir=remote_base,
                size=local_path.stat().st_size,
            )
        ], []

    if not local_path.is_dir():
        raise FileNotFoundError(local_path)

    remote_root = join_remote_path(remote_base, local_path.name) if include_parent_dir else remote_base
    items = []
    remote_dirs = {remote_root}

    for root, dirs, files in os.walk(local_path, followlinks=follow_symlinks):
        root_path = Path(root)
        if not follow_symlinks:
            dirs[:] = [name for name in dirs if not (root_path / name).is_symlink()]

        dirs[:] = [
            name for name in sorted(dirs)
            if not should_exclude((root_path / name).relative_to(local_path), excludes)
        ]

        relative_root = root_path.relative_to(local_path)
        relative_root_posix = "" if str(relative_root) == "." else relative_root.as_posix()
        current_remote_dir = join_remote_path(remote_root, relative_root_posix)
        remote_dirs.add(current_remote_dir)

        for name in sorted(files):
            file_path = root_path / name
            if file_path.is_symlink() and not follow_symlinks:
                continue
            relative_path = file_path.relative_to(local_path)
            if should_exclude(relative_path, excludes):
                continue
            remote_parent = join_remote_path(remote_root, relative_path.parent.as_posix())
            remote_dirs.add(remote_parent)
            items.append(
                UploadItem(
                    local_path=file_path,
                    relative_path=relative_path.as_posix(),
                    remote_dir=remote_parent,
                    size=file_path.stat().st_size,
                )
            )

    return items, sorted(remote_dirs, key=lambda value: (value.count("/"), value))


def should_exclude(relative_path, patterns):
    if not patterns:
        return False
    path = relative_path.as_posix()
    name = relative_path.name
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def upload_one_file(session, item, cluster, account, token, progress, rewrite=False, user_id=None, verbose=False):
    local_file = item.local_path
    file_name = local_file.name
    file_size = item.size
    remote_path = item.remote_dir

    progress.set_current(item.relative_path)
    file_md5 = md5_hex(local_file)
    file_id = None
    counted_for_file = 0

    while True:
        params = {
            "file_name": file_name,
            "file_length": str(file_size),
            "path": remote_path,
            "md5": file_md5,
            "reWrite": str(bool(rewrite)).lower(),
            "user_name": account,
        }
        if file_id:
            params["file_id"] = file_id

        query_url = f"{BASE_URL}/clusters/{quote(cluster)}/file/upload/query?{urlencode(params)}"
        state = request_json(session, "GET", query_url, headers={"PARA_ATOKEN": token})
        log_json("[query]", state, verbose)

        if state.get("scpStatus") == "ok" or state.get("missedRange") == "":
            if counted_for_file < file_size:
                progress.add_bytes(file_size - counted_for_file)
            if user_id:
                save_upload_log(session, cluster, remote_path, user_id, file_name, token)
            progress.file_done()
            if verbose:
                print(f"\n[done] {item.relative_path} -> {remote_path}/{file_name}")
            return

        file_id = state.get("fileId") or file_id
        missed_range = state.get("missedRange")
        if not file_id or missed_range is None:
            raise RuntimeError(f"unexpected upload query response: {state}")

        ranges = parse_ranges(missed_range, file_size)
        missing_bytes = sum(end - start + 1 for start, end in ranges)
        already_on_server = max(file_size - missing_bytes - counted_for_file, 0)
        if already_on_server:
            progress.add_bytes(already_on_server)
            counted_for_file += already_on_server

        for start, end in ranges:
            with local_file.open("rb") as file:
                file.seek(start)
                data = file.read(end - start + 1)

            part_md5 = md5_base64_urlsafe(data)
            part_url = (
                f"{BASE_URL}/clusters/{quote(cluster)}/file/upload/partly?"
                f"md5={quote(part_md5)}&file_id={quote(str(file_id))}&user_name={quote(account)}"
            )
            result = request_json(
                session,
                "POST",
                part_url,
                headers={
                    "Range": f"bytes={start}-{end}",
                    "para_atoken": token,
                    "Content-Type": "application/octet-stream",
                },
                data=data,
            )
            log_json(f"[part] bytes={start}-{end}", result, verbose)
            progress.add_bytes(len(data))
            counted_for_file += len(data)


def save_upload_log(session, cluster, remote_path, user_id, file_name, token):
    process_params = {
        "path": remote_path,
        "user_id": user_id,
        "file_name": file_name,
        "process": "1",
        "is_dir": "true",
    }
    process_url = f"{BASE_URL}/cluster/{quote(cluster)}/file/upload/process?{urlencode(process_params)}"
    session.get(process_url, headers={"PARA_ATOKEN": token})


def upload_path(local_path, cluster, account, remote_dir, token, rewrite=False, user_id=None, verbose=False,
                show_progress=True, include_parent_dir=True, excludes=None, follow_symlinks=False):
    excludes = excludes or []
    items, remote_dirs = collect_upload_items(
        local_path,
        cluster,
        account,
        remote_dir,
        include_parent_dir=include_parent_dir,
        excludes=excludes,
        follow_symlinks=follow_symlinks,
    )
    if not items:
        print("[skip] no files to upload")
        return

    total_size = sum(item.size for item in items)
    session = create_session()
    check_quota(session, cluster, total_size, token, verbose)

    if Path(local_path).expanduser().is_dir():
        for remote_path in remote_dirs:
            mkdir_remote(session, cluster, account, remote_path, token, verbose)

    progress = ProgressPrinter(total_size, len(items), enabled=show_progress)
    print(f"[start] uploading {len(items)} file(s), total {format_bytes(total_size)}")
    for item in items:
        upload_one_file(session, item, cluster, account, token, progress, rewrite, user_id, verbose)
    progress.finish()
    print(f"[done] uploaded {len(items)} file(s), total {format_bytes(total_size)}")


def main():
    parser = argparse.ArgumentParser(description="Upload a file or directory to BJTU HPC web file manager.")
    parser.add_argument("path", help="Local file or directory to upload")
    default_cluster = os.getenv("HPC_CLUSTER", "cluster2")
    default_account = os.getenv("HPC_ACCOUNT")
    parser.add_argument("--cluster", default=default_cluster)
    parser.add_argument("--account", default=default_account)
    parser.add_argument("--remote-dir", default=os.getenv("HPC_REMOTE_DIR", "home"))
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"), help="Saved auth account name from hpc_accounts.py")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument(
        "--token-file",
        default=DEFAULT_TOKEN_FILE,
        type=Path,
        help="Read token from this file when --token/HPC_PARA_ATOKEN is not set",
    )
    parser.add_argument(
        "--refresh-token",
        action="store_true",
        help="Open a browser login flow before upload, and retry once if the token is expired",
    )
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true", help="Use headless Playwright for token refresh")
    parser.add_argument("--user-id", default=os.getenv("HPC_USER_ID"))
    parser.add_argument("--rewrite", action="store_true", help="Ask the server to overwrite if supported")
    parser.add_argument("--no-progress", action="store_true", help="Disable progress bar")
    parser.add_argument("--verbose", action="store_true", help="Print raw API responses")
    parser.add_argument(
        "--no-parent-dir",
        action="store_true",
        help="When uploading a directory, put its contents directly into --remote-dir",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude files/dirs by fnmatch pattern, e.g. --exclude '*.pt' --exclude __pycache__",
    )
    parser.add_argument("--follow-symlinks", action="store_true", help="Follow symlinks when uploading directories")
    args = parser.parse_args()
    apply_auth_account_defaults(args, default_cluster=default_cluster, default_account=default_account)

    token_file = args.token_file.expanduser()
    token = load_token(args.token, token_file, auth_account=args.auth_account)
    if args.refresh_token:
        token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=args.auth_account)

    if not token:
        print(
            f"Missing token. Run: python3 hpc_refresh_token.py or retry with --refresh-token. "
            f"You can also set HPC_PARA_ATOKEN / --token / --token-file {args.token_file}",
            file=sys.stderr,
        )
        return 2

    try:
        upload_path(
            args.path,
            args.cluster,
            args.account,
            args.remote_dir,
            token,
            rewrite=args.rewrite,
            user_id=args.user_id,
            verbose=args.verbose,
            show_progress=not args.no_progress,
            include_parent_dir=not args.no_parent_dir,
            excludes=args.exclude,
            follow_symlinks=args.follow_symlinks,
        )
    except RuntimeError as error:
        if args.refresh_token and str(error) == AUTH_ERROR_MESSAGE:
            token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=args.auth_account)
            upload_path(
                args.path,
                args.cluster,
                args.account,
                args.remote_dir,
                token,
                rewrite=args.rewrite,
                user_id=args.user_id,
                verbose=args.verbose,
                show_progress=not args.no_progress,
                include_parent_dir=not args.no_parent_dir,
                excludes=args.exclude,
                follow_symlinks=args.follow_symlinks,
            )
        else:
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
