"""Portable BJTU portal auth/HTTP helpers without remote file-upload code."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import requests

from hpc_account_store import AccountStoreError, token_for_account


AUTH_ERROR_CODES = {11009, 11011, 11012}
AUTH_ERROR_MESSAGE = "HPC token is missing or expired; refresh the selected saved account"
BASE_URL = "https://hpc.bjtu.edu.cn/pcp"
DEFAULT_TOKEN_FILE = Path(
    os.getenv("HPC_PARA_ATOKEN_FILE", "~/.bjtu_hpc_token")
).expanduser()


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
        command = [
            sys.executable,
            str(Path(__file__).with_name("hpc_accounts.py")),
            "refresh",
            auth_account,
            "--browser",
            browser,
        ]
        if headless:
            command.append("--headless")
        subprocess.run(command, check=True)
        return load_token(None, token_file.expanduser(), auth_account=auth_account)

    command = [
        sys.executable,
        str(Path(__file__).with_name("hpc_refresh_token.py")),
        "--browser",
        browser,
        "--token-file",
        str(token_file.expanduser()),
    ]
    if headless:
        command.append("--headless")
    subprocess.run(command, check=True)
    return load_token(None, token_file.expanduser())


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    return session


def request_json(session: requests.Session, method: str, url: str, **kwargs):
    response = session.request(method, url, **kwargs)
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}

    if isinstance(data, dict) and data.get("code") in AUTH_ERROR_CODES:
        raise RuntimeError(AUTH_ERROR_MESSAGE)
    if not response.ok:
        raise RuntimeError(
            f"{method} {url} failed: HTTP {response.status_code} {data}"
        )
    return data
