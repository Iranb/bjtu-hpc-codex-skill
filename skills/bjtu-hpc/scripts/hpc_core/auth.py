import os
import subprocess
import sys
from pathlib import Path
from typing import Optional, Union

from hpc_upload import DEFAULT_TOKEN_FILE, load_token


def refresh_portal_token(
    token_file: Path,
    browser: str = "playwright",
    headless: bool = True,
    auth_account: Optional[str] = None,
) -> Optional[str]:
    root = Path(__file__).resolve().parent.parent
    if auth_account:
        command = [
            sys.executable,
            str(root / "hpc_accounts.py"),
            "refresh",
            auth_account,
            "--browser",
            browser,
        ]
    else:
        command = [
            sys.executable,
            str(root / "hpc_refresh_token.py"),
            "--browser",
            browser,
            "--token-file",
            str(token_file.expanduser()),
        ]
    if headless:
        command.append("--headless")

    # MCP stdio uses stdout for protocol frames, so keep helper output captured.
    subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return load_token(None, token_file.expanduser(), auth_account=auth_account)


def load_portal_token(
    *,
    token_file: Optional[Union[str, Path]] = None,
    refresh: bool = False,
    refresh_browser: str = "playwright",
    refresh_headless: bool = True,
    auth_account: Optional[str] = None,
) -> str:
    path = Path(token_file).expanduser() if token_file else DEFAULT_TOKEN_FILE
    auth_account = auth_account or os.getenv("HPC_AUTH_ACCOUNT")
    token = load_token(os.getenv("HPC_PARA_ATOKEN"), path, auth_account=auth_account)
    if refresh or not token:
        token = refresh_portal_token(path, refresh_browser, refresh_headless, auth_account=auth_account)
    if not token:
        raise RuntimeError(
            f"Missing HPC portal token. Run hpc_refresh_token.py or create {path}."
        )
    return token
