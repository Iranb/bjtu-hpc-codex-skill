from collections import deque
from pathlib import Path
from typing import Any

from hpc_core.auth import load_portal_token
from hpc_download import download_file, normalize_remote_path
from hpc_upload import create_session


def workspace_root() -> Path:
    return Path(__file__).resolve().parent.parent


def validate_output_path(path_text: str, allow_external_path: bool) -> Path:
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
                f"output path must be inside {root}; set allow_external_path=True to override."
            ) from exc
    return path


def download_remote_file(
    remote_path: str,
    *,
    output: str = ".",
    cluster: str = "cluster2",
    account: str = "",
    remote_dir: str = "home",
    allow_external_path: bool = False,
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    auth_account: str | None = None,
) -> dict[str, Any]:
    token = load_portal_token(token_file=token_file, refresh=refresh_token, auth_account=auth_account)
    session = create_session()
    normalized_remote = normalize_remote_path(remote_path, cluster, account, remote_dir)
    output_path = validate_output_path(output, allow_external_path)
    local_path = download_file(
        session,
        token,
        cluster,
        normalized_remote,
        output_path,
        show_progress=False,
    )
    return {
        "success": True,
        "remote_path": normalized_remote,
        "local_path": str(Path(local_path).resolve()),
    }


def read_remote_text(
    remote_path: str,
    *,
    max_bytes: int = 12000,
    cluster: str = "cluster2",
    account: str = "",
    remote_dir: str = "home",
    token_file: str | Path | None = None,
    refresh_token: bool = False,
    auth_account: str | None = None,
) -> dict[str, Any]:
    # Reuse the portal download URL flow without writing state to local files.
    from hpc_download import get_download_url

    token = load_portal_token(token_file=token_file, refresh=refresh_token, auth_account=auth_account)
    session = create_session()
    normalized_remote = normalize_remote_path(remote_path, cluster, account, remote_dir)
    second_url = get_download_url(session, token, cluster, normalized_remote)
    response = session.get(second_url, headers={"PARA_ATOKEN": token}, stream=True, timeout=60)
    if not response.ok:
        raise RuntimeError(f"download failed: HTTP {response.status_code} {response.text[:500]}")

    keep = deque()
    kept = 0
    total = 0
    limit = max(1, int(max_bytes))
    for chunk in response.iter_content(8192):
        if not chunk:
            continue
        total += len(chunk)
        keep.append(chunk)
        kept += len(chunk)
        while kept > limit and keep:
            removed = keep.popleft()
            kept -= len(removed)

    raw = b"".join(keep)
    if len(raw) > limit:
        raw = raw[-limit:]
    return {
        "success": True,
        "remote_path": normalized_remote,
        "bytes_read": total,
        "truncated": total > len(raw),
        "text": raw.decode("utf-8", errors="replace"),
    }
