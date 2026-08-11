#!/usr/bin/env python3
import json
import os
import re
import fcntl
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Union


DEFAULT_ACCOUNTS_FILE = Path(os.getenv("HPC_ACCOUNTS_FILE", "~/.bjtu_hpc_accounts.json")).expanduser()
DEFAULT_CREDENTIALS_FILE = Path(os.getenv("HPC_CREDENTIALS_FILE", "~/.bjtu_hpc_credentials.json")).expanduser()
DEFAULT_BROWSER_PROFILE_ROOT = Path(
    os.getenv("HPC_BROWSER_PROFILE_ROOT", "~/.bjtu_hpc_browser_accounts")
).expanduser()
DEFAULT_LEGACY_TOKEN_FILE = Path(os.getenv("HPC_PARA_ATOKEN_FILE", "~/.bjtu_hpc_token")).expanduser()
ENV_AUTH_ACCOUNT = "HPC_AUTH_ACCOUNT"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class AccountStoreError(ValueError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_account_name(name: str) -> str:
    name = str(name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise AccountStoreError(
            "account name must be 1-64 chars using letters, digits, '.', '_', or '-'."
        )
    return name


def default_store() -> dict:
    return {"version": 1, "default": None, "accounts": {}}


PathLike = Union[str, Path]


def load_store(path: Optional[PathLike] = None) -> dict:
    path = Path(path).expanduser() if path else DEFAULT_ACCOUNTS_FILE
    if not path.exists():
        return default_store()
    with path.open("r", encoding="utf-8") as file:
        store = json.load(file)
    if not isinstance(store, dict):
        raise AccountStoreError(f"invalid account store: {path}")
    store.setdefault("version", 1)
    store.setdefault("default", None)
    store.setdefault("accounts", {})
    if not isinstance(store["accounts"], dict):
        raise AccountStoreError(f"invalid account list in: {path}")
    return store


def save_store(store: dict, path: Optional[PathLike] = None) -> None:
    path = Path(path).expanduser() if path else DEFAULT_ACCOUNTS_FILE
    data = json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _locked_atomic_write(path, data, 0o600)


def _locked_atomic_write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    tmp_name = None
    try:
        with os.fdopen(lock_fd, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            try:
                os.fchmod(tmp_fd, mode)
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
                    tmp_file.write(text)
                    tmp_file.flush()
                    os.fsync(tmp_file.fileno())
                os.replace(tmp_name, path)
                os.chmod(path, mode)
                tmp_name = None
            finally:
                if tmp_name:
                    try:
                        os.unlink(tmp_name)
                    except FileNotFoundError:
                        pass
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        try:
            os.chmod(lock_path, 0o600)
        except FileNotFoundError:
            pass


def _reject_loose_secret_permissions(path: Path) -> None:
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise AccountStoreError(
            f"credential file is readable by group/others: {path} mode={mode:o}; "
            "run chmod 600 before using it."
        )


def load_credentials(path: Optional[PathLike] = None) -> dict:
    path = Path(path).expanduser() if path else DEFAULT_CREDENTIALS_FILE
    if not path.exists():
        return {"version": 1, "accounts": {}}
    _reject_loose_secret_permissions(path)
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise AccountStoreError(f"invalid credential store: {path}")
    data.setdefault("version", 1)
    data.setdefault("accounts", {})
    if not isinstance(data["accounts"], dict):
        raise AccountStoreError(f"invalid credential account list in: {path}")
    return data


def save_credentials(data: dict, path: Optional[PathLike] = None) -> None:
    path = Path(path).expanduser() if path else DEFAULT_CREDENTIALS_FILE
    data.setdefault("version", 1)
    data.setdefault("accounts", {})
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _locked_atomic_write(path, payload, 0o600)


def credential_for_account(name: str, path: Optional[PathLike] = None) -> dict:
    name = validate_account_name(name)
    data = load_credentials(path)
    entry = data["accounts"].get(name) or {}
    return dict(entry) if isinstance(entry, dict) else {}


def upsert_account_credential(
    name: str,
    *,
    login_name: Optional[str] = None,
    login_password: Optional[str] = None,
    path: Optional[PathLike] = None,
) -> dict:
    name = validate_account_name(name)
    data = load_credentials(path)
    entry = dict(data["accounts"].get(name) or {})
    if login_name is not None:
        entry["login_name"] = str(login_name).strip()
    if login_password is not None:
        entry["login_password"] = str(login_password)
    entry["updated_at"] = now_iso()
    data["accounts"][name] = entry
    save_credentials(data, path)
    return entry


def delete_account_credential(name: str, path: Optional[PathLike] = None) -> None:
    name = validate_account_name(name)
    data = load_credentials(path)
    data["accounts"].pop(name, None)
    save_credentials(data, path)


def list_credential_summaries(path: Optional[PathLike] = None) -> list:
    data = load_credentials(path)
    rows = []
    for name, entry in sorted(data["accounts"].items()):
        if not isinstance(entry, dict):
            continue
        rows.append(
            {
                "name": name,
                "login_name": entry.get("login_name"),
                "has_password": bool(entry.get("login_password")),
                "updated_at": entry.get("updated_at"),
            }
        )
    return rows


def profile_dir_for(name: str, entry: Optional[dict] = None) -> Path:
    if entry and entry.get("profile_dir"):
        return Path(str(entry["profile_dir"])).expanduser()
    return DEFAULT_BROWSER_PROFILE_ROOT / validate_account_name(name)


def resolve_account_name(name: Optional[str], store: Optional[dict] = None) -> str:
    store = store or load_store()
    selected = name or os.getenv(ENV_AUTH_ACCOUNT) or store.get("default")
    if not selected:
        raise AccountStoreError(
            "no auth account selected. Pass --auth-account NAME or run hpc_accounts.py use NAME."
        )
    return validate_account_name(selected)


def get_account(name: Optional[str] = None, path: Optional[PathLike] = None) -> Tuple[str, dict]:
    store = load_store(path)
    selected = resolve_account_name(name, store)
    account = store["accounts"].get(selected)
    if not account:
        raise AccountStoreError(f"unknown auth account: {selected}")
    return selected, account


def upsert_account(
    name: str,
    *,
    portal_user: Optional[str] = None,
    cluster: Optional[str] = None,
    account: Optional[str] = None,
    profile_dir: Optional[PathLike] = None,
    set_default: bool = False,
    path: Optional[PathLike] = None,
) -> dict:
    name = validate_account_name(name)
    store = load_store(path)
    entry = dict(store["accounts"].get(name) or {})
    entry["name"] = name
    if portal_user is not None:
        entry["portal_user"] = str(portal_user).strip()
    if cluster is not None:
        entry["cluster"] = str(cluster).strip()
    if account is not None:
        entry["account"] = str(account).strip()
    if profile_dir is not None:
        entry["profile_dir"] = str(Path(profile_dir).expanduser())
    entry.setdefault("created_at", now_iso())
    entry["updated_at"] = now_iso()
    store["accounts"][name] = entry
    if set_default or not store.get("default"):
        store["default"] = name
    save_store(store, path)
    return entry


def save_account_token(
    name: str,
    token: str,
    *,
    validation: Optional[dict] = None,
    path: Optional[PathLike] = None,
) -> dict:
    name = validate_account_name(name)
    token = str(token or "").strip()
    if not token:
        raise AccountStoreError("token is empty.")
    store = load_store(path)
    if name not in store["accounts"]:
        raise AccountStoreError(f"unknown auth account: {name}")
    entry = dict(store["accounts"][name])
    entry["token"] = token
    entry["token_updated_at"] = now_iso()
    if validation is not None:
        entry["token_validation"] = validation
        entry["token_validated_at"] = now_iso()
    entry["updated_at"] = now_iso()
    store["accounts"][name] = entry
    save_store(store, path)
    return entry


def delete_account(name: str, path: Optional[PathLike] = None) -> None:
    name = validate_account_name(name)
    store = load_store(path)
    if name not in store["accounts"]:
        raise AccountStoreError(f"unknown auth account: {name}")
    del store["accounts"][name]
    if store.get("default") == name:
        store["default"] = next(iter(store["accounts"]), None)
    save_store(store, path)


def set_default_account(name: str, path: Optional[PathLike] = None) -> dict:
    name, entry = get_account(name, path)
    store = load_store(path)
    store["default"] = name
    save_store(store, path)
    return entry


def token_for_account(name: Optional[str] = None, path: Optional[PathLike] = None) -> Optional[str]:
    _, entry = get_account(name, path)
    token = str(entry.get("token") or "").strip()
    return token or None


def sync_legacy_token(
    name: Optional[str] = None,
    *,
    token_file: Optional[PathLike] = None,
    path: Optional[PathLike] = None,
) -> Path:
    token = token_for_account(name, path)
    if not token:
        selected, _ = get_account(name, path)
        raise AccountStoreError(f"auth account has no saved token: {selected}")
    token_path = Path(token_file).expanduser() if token_file else DEFAULT_LEGACY_TOKEN_FILE
    _locked_atomic_write(token_path, token + "\n", 0o600)
    return token_path


def account_summary(name: str, entry: dict, *, is_default: bool = False) -> dict:
    return {
        "name": name,
        "default": is_default,
        "portal_user": entry.get("portal_user"),
        "cluster": entry.get("cluster"),
        "account": entry.get("account"),
        "profile_dir": entry.get("profile_dir") or str(profile_dir_for(name, entry)),
        "has_token": bool(str(entry.get("token") or "").strip()),
        "token_updated_at": entry.get("token_updated_at"),
        "token_validated_at": entry.get("token_validated_at"),
        "updated_at": entry.get("updated_at"),
    }


def list_account_summaries(path: Optional[PathLike] = None) -> list:
    store = load_store(path)
    default_name = store.get("default")
    return [
        account_summary(name, entry, is_default=(name == default_name))
        for name, entry in sorted(store["accounts"].items())
    ]


def apply_auth_account_defaults(
    args,
    *,
    default_cluster: Optional[str] = None,
    default_account: Optional[str] = None,
    default_portal_user: Optional[str] = None,
) -> None:
    name = getattr(args, "auth_account", None)
    if not name:
        return
    _, entry = get_account(name)
    if hasattr(args, "cluster") and (
        not args.cluster or (default_cluster is not None and args.cluster == default_cluster)
    ):
        args.cluster = entry.get("cluster") or args.cluster
    if hasattr(args, "account") and (
        not args.account or (default_account is not None and args.account == default_account)
    ):
        args.account = entry.get("account") or args.account
    if hasattr(args, "portal_user") and (
        not args.portal_user
        or (default_portal_user is not None and args.portal_user == default_portal_user)
    ):
        args.portal_user = entry.get("portal_user") or args.portal_user
