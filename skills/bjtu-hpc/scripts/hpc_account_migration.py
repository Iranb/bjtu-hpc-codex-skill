#!/usr/bin/env python3.12
"""Portable, private JSON import/export for BJTU HPC account metadata and secrets."""

from __future__ import annotations

import hashlib
import base64
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from hpc_account_store import (
    AccountStoreError,
    DEFAULT_ACCOUNTS_FILE,
    DEFAULT_CREDENTIALS_FILE,
    load_credentials,
    load_store,
    now_iso,
    save_credentials,
    save_store,
    validate_account_name,
)


MIGRATION_FORMAT = "bjtu-hpc-account-migration"
ENCRYPTED_MIGRATION_FORMAT = "bjtu-hpc-account-migration-encrypted"
MIGRATION_SCHEMA_VERSION = 1
MAX_MIGRATION_BYTES = 4 * 1024 * 1024
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
NONCE_BYTES = 12
ACCOUNT_FIELDS = {
    "name",
    "portal_user",
    "cluster",
    "account",
    "created_at",
    "updated_at",
    "token",
    "token_updated_at",
    "token_validated_at",
    "token_validation",
}
CREDENTIAL_FIELDS = {"login_name", "login_password", "updated_at"}


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _migration_digest(payload: dict[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("payload_sha256", None)
    return _canonical_sha256(unsigned)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _derive_encryption_key(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise AccountStoreError("encrypted migration requires a non-empty passphrase")
    return Scrypt(salt=salt, length=32, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P).derive(
        passphrase.encode("utf-8")
    )


def _encrypt_payload(payload: dict[str, Any], passphrase: str) -> dict[str, Any]:
    if len(passphrase) < 12:
        raise AccountStoreError("migration passphrase must contain at least 12 characters")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    header: dict[str, Any] = {
        "format": ENCRYPTED_MIGRATION_FORMAT,
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "encryption": {
            "cipher": "AES-256-GCM",
            "kdf": "scrypt",
            "n": SCRYPT_N,
            "r": SCRYPT_R,
            "p": SCRYPT_P,
            "salt_b64": base64.b64encode(salt).decode("ascii"),
            "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        },
    }
    key = _derive_encryption_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, _canonical_bytes(payload), _canonical_bytes(header))
    return {**header, "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii")}


def _decode_b64(value: Any, *, field: str, expected_length: int | None = None) -> bytes:
    if not isinstance(value, str):
        raise AccountStoreError(f"encrypted migration {field} must be base64 text")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as error:
        raise AccountStoreError(f"encrypted migration {field} is invalid base64") from error
    if expected_length is not None and len(decoded) != expected_length:
        raise AccountStoreError(f"encrypted migration {field} has an invalid length")
    return decoded


def _decrypt_payload(envelope: dict[str, Any], passphrase: str | None) -> dict[str, Any]:
    if not passphrase:
        raise AccountStoreError("encrypted migration requires the export passphrase")
    if set(envelope) != {"format", "schema_version", "encryption", "ciphertext_b64"}:
        raise AccountStoreError("encrypted migration contains unsupported envelope fields")
    encryption = envelope.get("encryption")
    if not isinstance(encryption, dict):
        raise AccountStoreError("encrypted migration encryption metadata must be an object")
    expected = {
        "cipher": "AES-256-GCM",
        "kdf": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
    }
    if any(encryption.get(key) != value for key, value in expected.items()):
        raise AccountStoreError("unsupported encrypted migration cipher or KDF parameters")
    if set(encryption) != {*expected, "salt_b64", "nonce_b64"}:
        raise AccountStoreError("encrypted migration contains unsupported encryption fields")
    salt = _decode_b64(encryption["salt_b64"], field="salt_b64", expected_length=SALT_BYTES)
    nonce = _decode_b64(encryption["nonce_b64"], field="nonce_b64", expected_length=NONCE_BYTES)
    ciphertext = _decode_b64(envelope["ciphertext_b64"], field="ciphertext_b64")
    header = {key: envelope[key] for key in ["format", "schema_version", "encryption"]}
    try:
        plaintext = AESGCM(_derive_encryption_key(passphrase, salt)).decrypt(
            nonce,
            ciphertext,
            _canonical_bytes(header),
        )
    except InvalidTag as error:
        raise AccountStoreError(
            "migration decryption failed; passphrase is wrong or the encrypted file was modified"
        ) from error
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AccountStoreError("decrypted migration payload is invalid JSON") from error
    if not isinstance(payload, dict):
        raise AccountStoreError("decrypted migration JSON root must be an object")
    return payload


def _require_private_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise AccountStoreError(f"migration file not found: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AccountStoreError(f"migration input must be a regular non-symlink file: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077:
        raise AccountStoreError(
            f"migration file is readable by group/others: {path} mode={mode:o}; run chmod 600 first"
        )
    if metadata.st_size > MAX_MIGRATION_BYTES:
        raise AccountStoreError(
            f"migration file exceeds {MAX_MIGRATION_BYTES} bytes: {path}"
        )


def _write_private_json(path: Path, payload: dict[str, Any], *, overwrite: bool) -> None:
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    if path.exists() and not overwrite:
        raise AccountStoreError(f"refusing to overwrite existing migration file without --force: {path}")
    if path.exists() and path.is_symlink():
        raise AccountStoreError(f"refusing to overwrite symlink migration path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary: Path | None = None
    descriptor = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _selected_names(store: dict[str, Any], names: Iterable[str] | None) -> list[str]:
    if names is None:
        selected = sorted(store["accounts"])
    else:
        selected = sorted({validate_account_name(name) for name in names})
    missing = [name for name in selected if name not in store["accounts"]]
    if missing:
        raise AccountStoreError(f"unknown auth account(s): {', '.join(missing)}")
    if not selected:
        raise AccountStoreError("no saved auth accounts selected for export")
    return selected


def export_account_migration(
    output: Path,
    *,
    names: Iterable[str] | None = None,
    include_tokens: bool = False,
    include_credentials: bool = False,
    encrypt: bool = False,
    passphrase: str | None = None,
    overwrite: bool = False,
    accounts_path: Path | None = None,
    credentials_path: Path | None = None,
) -> dict[str, Any]:
    store = load_store(accounts_path or DEFAULT_ACCOUNTS_FILE)
    selected = _selected_names(store, names)
    exported_accounts: dict[str, dict[str, Any]] = {}
    for name in selected:
        source = store["accounts"][name]
        if not isinstance(source, dict):
            raise AccountStoreError(f"invalid saved auth account entry: {name}")
        entry = {key: source[key] for key in ACCOUNT_FIELDS if key in source}
        entry["name"] = name
        if not include_tokens:
            for key in ["token", "token_updated_at", "token_validated_at", "token_validation"]:
                entry.pop(key, None)
        exported_accounts[name] = entry

    exported_credentials: dict[str, dict[str, Any]] = {}
    if include_credentials:
        credentials = load_credentials(credentials_path or DEFAULT_CREDENTIALS_FILE)
        for name in selected:
            source = credentials["accounts"].get(name)
            if not isinstance(source, dict):
                continue
            exported_credentials[name] = {
                key: source[key] for key in CREDENTIAL_FIELDS if key in source
            }

    exported_default = store.get("default")
    if exported_default not in exported_accounts:
        exported_default = None
    payload: dict[str, Any] = {
        "format": MIGRATION_FORMAT,
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "exported_at": now_iso(),
        "default": exported_default,
        "contains": {
            "tokens": any("token" in entry for entry in exported_accounts.values()),
            "credentials": bool(exported_credentials),
            "browser_profiles": False,
        },
        "profile_policy": "reset_to_target_default",
        "accounts": exported_accounts,
        "credentials": exported_credentials,
    }
    payload["payload_sha256"] = _migration_digest(payload)
    encrypted = bool(encrypt or include_tokens or include_credentials)
    if encrypted and not passphrase:
        raise AccountStoreError(
            "exports containing tokens or CAS credentials require an encryption passphrase"
        )
    output_payload = _encrypt_payload(payload, passphrase or "") if encrypted else payload
    _write_private_json(Path(output), output_payload, overwrite=overwrite)
    return {
        "path": str(Path(output).expanduser().resolve()),
        "accounts": selected,
        "default": exported_default,
        "includes_tokens": payload["contains"]["tokens"],
        "includes_credentials": payload["contains"]["credentials"],
        "encrypted": encrypted,
        "payload_sha256": payload["payload_sha256"],
    }


def account_migration_is_encrypted(path: Path) -> bool:
    path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    _require_private_regular_file(path)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AccountStoreError(f"invalid migration JSON: {path}: {error}") from error
    if not isinstance(envelope, dict):
        raise AccountStoreError("migration JSON root must be an object")
    return envelope.get("format") == ENCRYPTED_MIGRATION_FORMAT


def load_account_migration(path: Path, *, passphrase: str | None = None) -> dict[str, Any]:
    path = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    _require_private_regular_file(path)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AccountStoreError(f"invalid migration JSON: {path}: {error}") from error
    if not isinstance(envelope, dict):
        raise AccountStoreError("migration JSON root must be an object")
    encrypted = envelope.get("format") == ENCRYPTED_MIGRATION_FORMAT
    if encrypted:
        if envelope.get("schema_version") != MIGRATION_SCHEMA_VERSION:
            raise AccountStoreError(
                f"unsupported encrypted migration schema_version: {envelope.get('schema_version')!r}"
            )
        payload = _decrypt_payload(envelope, passphrase)
    else:
        payload = envelope
    if payload.get("format") != MIGRATION_FORMAT:
        raise AccountStoreError("unsupported migration JSON format")
    if payload.get("schema_version") != MIGRATION_SCHEMA_VERSION:
        raise AccountStoreError(
            f"unsupported migration schema_version: {payload.get('schema_version')!r}"
        )
    expected_digest = str(payload.get("payload_sha256") or "")
    if len(expected_digest) != 64 or _migration_digest(payload) != expected_digest:
        raise AccountStoreError("migration JSON payload_sha256 mismatch; file may be corrupted or modified")
    contains = payload.get("contains")
    if not isinstance(contains, dict) or contains.get("browser_profiles") is not False:
        raise AccountStoreError("migration JSON contains invalid portability metadata")
    accounts = payload.get("accounts")
    credentials = payload.get("credentials")
    if not isinstance(accounts, dict) or not accounts:
        raise AccountStoreError("migration JSON must contain at least one account")
    if not isinstance(credentials, dict):
        raise AccountStoreError("migration JSON credentials must be an object")

    has_tokens = False
    for raw_name, entry in accounts.items():
        name = validate_account_name(raw_name)
        if not isinstance(entry, dict):
            raise AccountStoreError(f"invalid migration account entry: {name}")
        unknown = sorted(set(entry) - ACCOUNT_FIELDS)
        if unknown:
            raise AccountStoreError(f"unsupported account fields for {name}: {unknown}")
        if entry.get("name") != name:
            raise AccountStoreError(f"migration account name mismatch: {name}")
        if "token" in entry:
            if not isinstance(entry["token"], str) or not entry["token"].strip():
                raise AccountStoreError(f"invalid token value for migration account: {name}")
            has_tokens = True
        for key in ["portal_user", "cluster", "account"]:
            if key in entry and not isinstance(entry[key], str):
                raise AccountStoreError(f"migration account {name} field {key} must be a string")

    has_credentials = bool(credentials)
    for raw_name, entry in credentials.items():
        name = validate_account_name(raw_name)
        if name not in accounts:
            raise AccountStoreError(f"credential has no matching migration account: {name}")
        if not isinstance(entry, dict):
            raise AccountStoreError(f"invalid migration credential entry: {name}")
        unknown = sorted(set(entry) - CREDENTIAL_FIELDS)
        if unknown:
            raise AccountStoreError(f"unsupported credential fields for {name}: {unknown}")
        if "login_password" in entry:
            if not isinstance(entry["login_password"], str) or not entry["login_password"]:
                raise AccountStoreError(f"invalid login password for migration account: {name}")

    if bool(contains.get("tokens")) != has_tokens:
        raise AccountStoreError("migration token-content marker does not match payload")
    if bool(contains.get("credentials")) != has_credentials:
        raise AccountStoreError("migration credential-content marker does not match payload")
    if not encrypted and (has_tokens or has_credentials):
        raise AccountStoreError(
            "plaintext migration files may not contain tokens or CAS credentials; re-export with encryption"
        )
    exported_default = payload.get("default")
    if exported_default is not None and exported_default not in accounts:
        raise AccountStoreError("migration default account is not present in accounts")
    return payload


def import_account_migration(
    source: Path,
    *,
    on_conflict: str = "error",
    use_exported_default: bool = False,
    passphrase: str | None = None,
    accounts_path: Path | None = None,
    credentials_path: Path | None = None,
) -> dict[str, Any]:
    if on_conflict not in {"error", "skip", "replace"}:
        raise AccountStoreError("on_conflict must be one of: error, skip, replace")
    payload = load_account_migration(source, passphrase=passphrase)
    account_store_path = accounts_path or DEFAULT_ACCOUNTS_FILE
    credential_store_path = credentials_path or DEFAULT_CREDENTIALS_FILE
    store = load_store(account_store_path)
    credentials = load_credentials(credential_store_path)
    incoming = payload["accounts"]
    conflicts = sorted(set(incoming) & set(store["accounts"]))
    if conflicts and on_conflict == "error":
        raise AccountStoreError(
            "account conflict(s); import made no changes: " + ", ".join(conflicts)
        )

    imported: list[str] = []
    skipped: list[str] = []
    for name in sorted(incoming):
        if name in store["accounts"] and on_conflict == "skip":
            skipped.append(name)
            continue
        entry = dict(incoming[name])
        existing = store["accounts"].get(name)
        if isinstance(existing, dict):
            if existing.get("profile_dir"):
                entry["profile_dir"] = existing["profile_dir"]
            if "token" not in entry:
                for key in ["token", "token_updated_at", "token_validated_at", "token_validation"]:
                    if key in existing:
                        entry[key] = existing[key]
        store["accounts"][name] = entry
        imported.append(name)
        incoming_credential = payload["credentials"].get(name)
        if isinstance(incoming_credential, dict):
            credentials["accounts"][name] = dict(incoming_credential)

    exported_default = payload.get("default")
    if exported_default and exported_default in store["accounts"]:
        if use_exported_default or not store.get("default"):
            store["default"] = exported_default
    if not store.get("default") and store["accounts"]:
        store["default"] = next(iter(sorted(store["accounts"])))

    save_store(store, account_store_path)
    if payload["credentials"]:
        save_credentials(credentials, credential_store_path)
    return {
        "source": str(Path(source).expanduser().resolve()),
        "imported": imported,
        "skipped": skipped,
        "conflicts": conflicts,
        "default": store.get("default"),
        "included_tokens": bool(payload["contains"]["tokens"]),
        "included_credentials": bool(payload["contains"]["credentials"]),
        "browser_profiles_imported": False,
    }
