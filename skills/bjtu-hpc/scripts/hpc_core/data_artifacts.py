"""Content-addressed data artifact and local registry primitives.

This module contains no network code.  Remote adapters may consume these
contracts, but all identity and state transitions stay testable locally.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ARTIFACT_MANIFEST_SCHEMA = "autoreskill.data_artifact_manifest.v1"
ATTESTATION_SCHEMA = "autoreskill.data_validation_attestation.v1"
REGISTRY_SCHEMA = "autoreskill.hpc_data_artifact_registry.v1"
# v1 intentionally supports only encoded image bytes. A feature artifact must
# first gain an explicit encoder/preprocess/layer/dtype identity contract.
SUPPORTED_ARTIFACT_SCHEMAS = {"image_bytes_indexed_v1"}


class ContractError(ValueError):
    """Raised when a scientific data contract is malformed or inconsistent."""


class RegistryConflict(RuntimeError):
    """Raised when a registry compare-and-swap revision is stale."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def load_json(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid_json:{source}:{exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError(f"json_root_must_be_object:{source}")
    return payload


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def sha256_file(path: str | Path, *, chunk_bytes: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def require_hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ContractError(f"{field}:expected_sha256")
    return value


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write_json(path: str | Path, payload: Any, *, mode: int = 0o644) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json_bytes(payload))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def artifact_identity_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest.get("schema"),
        "artifact_schema": manifest.get("artifact_schema"),
        "dataset_semantics_sha256": manifest.get("dataset_semantics_sha256"),
        "source_inventory_sha256": manifest.get("source_inventory_sha256"),
        "artifact_build_contract_sha256": manifest.get("artifact_build_contract_sha256"),
        "converter_bundle_sha256": manifest.get("converter_bundle_sha256"),
        "shards": manifest.get("shards"),
    }


def compute_artifact_content_sha256(manifest: dict[str, Any]) -> str:
    return canonical_sha256(artifact_identity_payload(manifest))


def build_artifact_manifest(
    *,
    artifact_schema: str,
    dataset_semantics_sha256: str,
    source_inventory_sha256: str,
    artifact_build_contract_sha256: str,
    converter_bundle_sha256: str,
    shards: list[dict[str, Any]],
) -> dict[str, Any]:
    if artifact_schema not in SUPPORTED_ARTIFACT_SCHEMAS:
        raise ContractError(f"unsupported_artifact_schema:{artifact_schema}")
    require_hash(dataset_semantics_sha256, "dataset_semantics_sha256")
    require_hash(source_inventory_sha256, "source_inventory_sha256")
    require_hash(artifact_build_contract_sha256, "artifact_build_contract_sha256")
    require_hash(converter_bundle_sha256, "converter_bundle_sha256")
    normalized_shards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for shard in sorted(shards, key=lambda item: str(item.get("path", ""))):
        relative_path = str(shard.get("path", ""))
        if not relative_path or relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise ContractError(f"invalid_shard_path:{relative_path}")
        if relative_path in seen:
            raise ContractError(f"duplicate_shard_path:{relative_path}")
        seen.add(relative_path)
        size_bytes = shard.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise ContractError(f"invalid_shard_size:{relative_path}")
        normalized_shards.append(
            {
                "path": relative_path,
                "size_bytes": size_bytes,
                "sha256": require_hash(shard.get("sha256"), f"shards[{relative_path}].sha256"),
            }
        )
    if not normalized_shards:
        raise ContractError("artifact_requires_at_least_one_shard")
    manifest = {
        "schema": ARTIFACT_MANIFEST_SCHEMA,
        "artifact_schema": artifact_schema,
        "dataset_semantics_sha256": dataset_semantics_sha256,
        "source_inventory_sha256": source_inventory_sha256,
        "artifact_build_contract_sha256": artifact_build_contract_sha256,
        "converter_bundle_sha256": converter_bundle_sha256,
        "shards": normalized_shards,
    }
    manifest["artifact_content_sha256"] = compute_artifact_content_sha256(manifest)
    manifest["total_size_bytes"] = sum(item["size_bytes"] for item in normalized_shards)
    return manifest


def validate_artifact_manifest(
    manifest: dict[str, Any],
    *,
    artifact_root: str | Path | None = None,
    verify_shards: bool = False,
) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != ARTIFACT_MANIFEST_SCHEMA:
        errors.append("manifest_schema_mismatch")
    for field in (
        "dataset_semantics_sha256",
        "source_inventory_sha256",
        "artifact_build_contract_sha256",
        "converter_bundle_sha256",
        "artifact_content_sha256",
    ):
        try:
            require_hash(manifest.get(field), field)
        except ContractError as exc:
            errors.append(str(exc))
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        errors.append("manifest_shards_missing")
        return errors
    try:
        rebuilt = build_artifact_manifest(
            artifact_schema=str(manifest.get("artifact_schema", "")),
            dataset_semantics_sha256=str(manifest.get("dataset_semantics_sha256", "")),
            source_inventory_sha256=str(manifest.get("source_inventory_sha256", "")),
            artifact_build_contract_sha256=str(manifest.get("artifact_build_contract_sha256", "")),
            converter_bundle_sha256=str(manifest.get("converter_bundle_sha256", "")),
            shards=shards,
        )
        if rebuilt["artifact_content_sha256"] != manifest.get("artifact_content_sha256"):
            errors.append("artifact_content_sha256_mismatch")
        if rebuilt["total_size_bytes"] != manifest.get("total_size_bytes"):
            errors.append("artifact_total_size_mismatch")
    except ContractError as exc:
        errors.append(str(exc))
        return errors
    if verify_shards:
        if artifact_root is None:
            errors.append("artifact_root_required_for_shard_verification")
        else:
            root = Path(artifact_root).resolve()
            for shard in rebuilt["shards"]:
                candidate = (root / shard["path"]).resolve()
                if root not in candidate.parents:
                    errors.append(f"shard_outside_artifact_root:{shard['path']}")
                    continue
                if not candidate.is_file():
                    errors.append(f"shard_missing:{shard['path']}")
                    continue
                if candidate.stat().st_size != shard["size_bytes"]:
                    errors.append(f"shard_size_mismatch:{shard['path']}")
                elif sha256_file(candidate) != shard["sha256"]:
                    errors.append(f"shard_sha256_mismatch:{shard['path']}")
    return errors


def attestation_identity_payload(attestation: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": attestation.get("schema"),
        "artifact_content_sha256": attestation.get("artifact_content_sha256"),
        "consumer_contract_sha256": attestation.get("consumer_contract_sha256"),
        "validator_bundle_sha256": attestation.get("validator_bundle_sha256"),
        "validation_report_sha256": attestation.get("validation_report_sha256"),
    }


def build_validation_attestation(
    *,
    artifact_content_sha256: str,
    consumer_contract_sha256: str,
    validator_bundle_sha256: str,
    validation_report_sha256: str,
) -> dict[str, Any]:
    payload = {
        "schema": ATTESTATION_SCHEMA,
        "artifact_content_sha256": require_hash(artifact_content_sha256, "artifact_content_sha256"),
        "consumer_contract_sha256": require_hash(consumer_contract_sha256, "consumer_contract_sha256"),
        "validator_bundle_sha256": require_hash(validator_bundle_sha256, "validator_bundle_sha256"),
        "validation_report_sha256": require_hash(validation_report_sha256, "validation_report_sha256"),
    }
    payload["validation_attestation_id"] = canonical_sha256(payload)
    return payload


def validate_validation_attestation(attestation: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if attestation.get("schema") != ATTESTATION_SCHEMA:
        errors.append("attestation_schema_mismatch")
    for field in (
        "artifact_content_sha256",
        "consumer_contract_sha256",
        "validator_bundle_sha256",
        "validation_report_sha256",
        "validation_attestation_id",
    ):
        try:
            require_hash(attestation.get(field), field)
        except ContractError as exc:
            errors.append(str(exc))
    if not errors:
        expected = canonical_sha256(attestation_identity_payload(attestation))
        if expected != attestation.get("validation_attestation_id"):
            errors.append("validation_attestation_id_mismatch")
    return errors


def empty_registry() -> dict[str, Any]:
    return {
        "schema": REGISTRY_SCHEMA,
        "revision": 0,
        "updated_at": None,
        "artifacts": {},
        "leases": {},
        "intents": {},
    }


class ArtifactRegistry:
    """Atomic local registry with a process lock and revision CAS."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return empty_registry()
        payload = load_json(self.path)
        if payload.get("schema") != REGISTRY_SCHEMA:
            raise ContractError("registry_schema_mismatch")
        if not isinstance(payload.get("revision"), int) or payload["revision"] < 0:
            raise ContractError("registry_revision_invalid")
        for field in ("artifacts", "leases", "intents"):
            if not isinstance(payload.get(field), dict):
                raise ContractError(f"registry_{field}_invalid")
        return payload

    def compare_and_swap(self, expected_revision: int, mutator: Any) -> dict[str, Any]:
        with self.locked():
            current = self.read()
            if current["revision"] != expected_revision:
                raise RegistryConflict(
                    f"registry_revision_conflict:expected={expected_revision}:actual={current['revision']}"
                )
            updated = mutator(json.loads(json.dumps(current)))
            if not isinstance(updated, dict):
                raise ContractError("registry_mutator_must_return_object")
            updated["schema"] = REGISTRY_SCHEMA
            updated["revision"] = current["revision"] + 1
            updated["updated_at"] = utc_now()
            atomic_write_json(self.path, updated)
            return updated
