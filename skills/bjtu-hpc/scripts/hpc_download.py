#!/usr/bin/env python3
import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

from hpc_account_store import apply_auth_account_defaults
from hpc_upload import (
    AUTH_ERROR_MESSAGE,
    BASE_URL,
    DEFAULT_TOKEN_FILE,
    create_session,
    load_token,
    refresh_token,
    request_json,
)

DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_ACCOUNT = os.getenv("HPC_ACCOUNT")
DEFAULT_REMOTE_DIR = os.getenv("HPC_REMOTE_DIR", "home")
PORTAL_ORIGIN = "https://hpc.bjtu.edu.cn"


class DownloadProgress:
    def __init__(self, total, enabled=True):
        self.total = total if total and total > 0 else None
        self.enabled = enabled
        self.done = 0
        self.start = time.monotonic()
        self.last_render = 0.0

    def add(self, size):
        self.done += size
        self.render()

    def finish(self):
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

        elapsed = max(now - self.start, 0.001)
        speed = self.done / elapsed
        if self.total:
            percent = self.done * 100.0 / self.total
            eta = max(self.total - self.done, 0) / speed if speed > 0 else 0
            line = (
                f"\r[{bar(self.done, self.total)}] {percent:6.2f}% "
                f"{format_bytes(self.done)}/{format_bytes(self.total)} "
                f"{format_bytes(speed)}/s eta {format_seconds(eta)}"
            )
        else:
            line = f"\r{format_bytes(self.done)} downloaded at {format_bytes(speed)}/s"
        print(line, end="", file=sys.stderr, flush=True)


def bar(done, total):
    width = 28
    filled = int(width * done / total) if total else 0
    return "#" * min(filled, width) + "-" * max(width - filled, 0)


def format_bytes(value):
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)}{unit}" if unit == "B" else f"{value:.1f}{unit}"
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


def normalize_remote_path(value, cluster, account, remote_dir):
    value = value.strip()
    if value.startswith("[PATH]/"):
        return value

    real_home = f"/data/home/{account}"
    if value == real_home:
        return f"[PATH]/{cluster}/{account}/home"
    if value.startswith(real_home + "/"):
        suffix = value[len(real_home) + 1:]
        return f"[PATH]/{cluster}/{account}/home/{suffix}"

    clean = value.strip("/")
    if clean.startswith(f"{cluster}/{account}/"):
        return f"[PATH]/{clean}"
    if clean.startswith(f"{account}/"):
        return f"[PATH]/{cluster}/{clean}"
    if clean.startswith("home/") or clean == "home":
        return f"[PATH]/{cluster}/{account}/{clean}"
    return f"[PATH]/{cluster}/{account}/{remote_dir.strip('/')}/{clean}"


def filename_from_disposition(value):
    if not value:
        return None
    match = re.search(r"filename\\*?=(?:UTF-8'')?\"?([^\";]+)\"?", value)
    if not match:
        return None
    return Path(unquote(match.group(1))).name


def output_path_for(target, remote_path, response):
    target = Path(target).expanduser()
    if target.exists() and target.is_dir():
        name = filename_from_disposition(response.headers.get("content-disposition"))
        if not name:
            name = Path(remote_path.rstrip("/")).name or "download"
        return target / name
    return target


def get_download_url(session, token, cluster, remote_path):
    url = f"{BASE_URL}/clusters/{quote(cluster)}/file/download"
    data = request_json(
        session,
        "GET",
        url,
        headers={"PARA_ATOKEN": token},
        params={"atoken": token, "path": remote_path},
    )
    if not data.get("success") or not data.get("data", {}).get("url"):
        raise RuntimeError(f"download URL request failed: {data}")
    return urljoin(PORTAL_ORIGIN + "/", data["data"]["url"])


def download_file(session, token, cluster, remote_path, output, show_progress=True):
    second_url = get_download_url(session, token, cluster, remote_path)
    response = session.get(second_url, headers={"PARA_ATOKEN": token}, stream=True, timeout=60)
    if not response.ok:
        raise RuntimeError(f"download failed: HTTP {response.status_code} {response.text[:500]}")

    total_text = response.headers.get("content-length")
    total = int(total_text) if total_text and total_text.isdigit() else None
    output_path = output_path_for(output, remote_path, response)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    progress = DownloadProgress(total, enabled=show_progress)
    with output_path.open("wb") as file:
        for chunk in response.iter_content(1024 * 1024):
            if not chunk:
                continue
            file.write(chunk)
            progress.add(len(chunk))
    progress.finish()
    return output_path


def parse_args():
    parser = argparse.ArgumentParser(description="Download a file from BJTU HPC web file manager.")
    parser.add_argument("remote_path", help="Remote path, e.g. home/a.py or [PATH]/cluster2/<account>/home/a.py")
    parser.add_argument(
        "-o",
        "--output",
        default=".",
        help="Local output file or directory. Defaults to current directory.",
    )
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"), help="Saved auth account name from hpc_accounts.py")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    apply_auth_account_defaults(args, default_cluster=DEFAULT_CLUSTER, default_account=DEFAULT_ACCOUNT)
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

    remote_path = normalize_remote_path(args.remote_path, args.cluster, args.account, args.remote_dir)
    session = create_session()
    try:
        output = download_file(
            session,
            token,
            args.cluster,
            remote_path,
            args.output,
            show_progress=not args.no_progress,
        )
    except RuntimeError as error:
        if args.refresh_token and str(error) == AUTH_ERROR_MESSAGE:
            token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=args.auth_account)
            output = download_file(
                session,
                token,
                args.cluster,
                remote_path,
                args.output,
                show_progress=not args.no_progress,
            )
        else:
            raise

    print(f"[done] {remote_path} -> {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
