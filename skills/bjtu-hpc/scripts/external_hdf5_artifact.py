#!/usr/bin/env python3
"""Build and independently validate portable image-byte HDF5 artifacts.

The command runs on the external data factory or on local fixtures.  It never
connects to BJTU HPC and never embeds source host paths in portable artifacts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Any

import h5py
import numpy as np
from PIL import Image

from hpc_core.data_artifacts import (
    ARTIFACT_MANIFEST_SCHEMA,
    ContractError,
    atomic_write_json,
    build_artifact_manifest,
    build_validation_attestation,
    canonical_sha256,
    load_json,
    require_hash,
    sha256_file,
    validate_artifact_manifest,
)


BUILD_CONTRACT_SCHEMA = "autoreskill.external_hdf5_build_contract.v1"
SOURCE_INVENTORY_SCHEMA = "autoreskill.source_inventory.v1"
CONSUMER_CONTRACT_SCHEMA = "autoreskill.hdf5_consumer_contract.v1"
ARTIFACT_SCHEMA = "image_bytes_indexed_v1"
BUILD_STATE_SCHEMA = "autoreskill.external_hdf5_build_state.v1"
REQUIRED_SAMPLE_FIELDS = (
    "source_path",
    "relative_path",
    "source_sha256",
    "sample_key",
    "split",
    "data_role",
    "target",
    "semantic_class_id",
    "class_name",
    "class_order_rank",
    "uq_idx",
)
PORTABLE_SAMPLE_FIELDS = tuple(field for field in REQUIRED_SAMPLE_FIELDS if field != "source_path")


def portable_inventory_payload(inventory: dict[str, Any]) -> dict[str, Any]:
    samples = []
    for sample in inventory.get("samples", []):
        item = {field: sample.get(field) for field in PORTABLE_SAMPLE_FIELDS}
        if "domain" in sample:
            item["domain"] = sample["domain"]
        samples.append(item)
    return {
        "schema": inventory.get("schema"),
        "samples": samples,
    }


def load_inventory(path: str | Path, *, verify_sources: bool) -> dict[str, Any]:
    inventory = load_json(path)
    if inventory.get("schema") != SOURCE_INVENTORY_SCHEMA:
        raise ContractError("source_inventory_schema_mismatch")
    samples = inventory.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ContractError("source_inventory_samples_missing")
    seen: set[str] = set()
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise ContractError(f"sample_not_object:{index}")
        missing = [field for field in REQUIRED_SAMPLE_FIELDS if field not in sample]
        if missing:
            raise ContractError(f"sample_missing_fields:{index}:{','.join(missing)}")
        key = str(sample["sample_key"])
        if not key or key in seen:
            raise ContractError(f"duplicate_or_empty_sample_key:{key}")
        seen.add(key)
        require_hash(sample["source_sha256"], f"samples[{index}].source_sha256")
        for field in ("target", "semantic_class_id", "class_order_rank", "uq_idx"):
            if not isinstance(sample[field], int):
                raise ContractError(f"sample_integer_required:{index}:{field}")
        if verify_sources:
            source = Path(str(sample["source_path"])).expanduser()
            if not source.is_file():
                raise ContractError(f"source_missing:{index}:{source}")
            actual = sha256_file(source)
            if actual != sample["source_sha256"]:
                raise ContractError(f"source_sha256_mismatch:{index}:{key}")
    return inventory


def build_identity(contract: dict[str, Any], inventory_sha256: str, dataset_semantics_sha256: str) -> dict[str, Any]:
    return {
        "schema": contract.get("schema"),
        "artifact_schema": contract.get("artifact_schema"),
        "dataset_semantics_sha256": dataset_semantics_sha256,
        "source_inventory_sha256": inventory_sha256,
        "converter": contract.get("converter"),
        "records_per_shard": contract.get("records_per_shard"),
        "hdf5_settings": contract.get("hdf5_settings", {}),
    }


def load_build_inputs(
    contract_path: str | Path,
    inventory_path: str | Path,
    *,
    verify_sources: bool,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    contract = load_json(contract_path)
    if contract.get("schema") != BUILD_CONTRACT_SCHEMA:
        raise ContractError("build_contract_schema_mismatch")
    if contract.get("artifact_schema") != ARTIFACT_SCHEMA:
        raise ContractError("unsupported_artifact_schema")
    records_per_shard = contract.get("records_per_shard")
    if not isinstance(records_per_shard, int) or records_per_shard < 1:
        raise ContractError("records_per_shard_must_be_positive")
    dataset_semantics = contract.get("dataset_semantics")
    if not isinstance(dataset_semantics, dict) or not dataset_semantics:
        raise ContractError("dataset_semantics_missing")
    inventory = load_inventory(inventory_path, verify_sources=verify_sources)
    inventory_sha256 = canonical_sha256(portable_inventory_payload(inventory))
    declared_inventory = contract.get("source_inventory_sha256")
    if declared_inventory is not None and declared_inventory != inventory_sha256:
        raise ContractError("source_inventory_sha256_mismatch")
    dataset_semantics_sha256 = canonical_sha256(
        {
            "declared_semantics": dataset_semantics,
            "portable_source_inventory_sha256": inventory_sha256,
        }
    )
    declared_semantics = contract.get("dataset_semantics_sha256")
    if declared_semantics is not None and declared_semantics != dataset_semantics_sha256:
        raise ContractError("dataset_semantics_sha256_mismatch")
    converter = contract.get("converter")
    if not isinstance(converter, dict) or not str(converter.get("name") or "").strip():
        raise ContractError("converter_identity_missing")
    require_hash(converter.get("bundle_sha256"), "converter.bundle_sha256")
    artifact_build_contract_sha256 = canonical_sha256(
        build_identity(contract, inventory_sha256, dataset_semantics_sha256)
    )
    return contract, inventory, {
        "dataset_semantics_sha256": dataset_semantics_sha256,
        "source_inventory_sha256": inventory_sha256,
        "artifact_build_contract_sha256": artifact_build_contract_sha256,
    }


def _utf8_dtype() -> Any:
    return h5py.string_dtype(encoding="utf-8")


def write_shard(path: Path, samples: list[dict[str, Any]]) -> None:
    byte_dtype = h5py.vlen_dtype(np.dtype("uint8"))
    with h5py.File(path, "w", libver="latest", track_order=True) as handle:
        handle.attrs["artifact_schema"] = ARTIFACT_SCHEMA
        encoded = handle.create_dataset("encoded_image_bytes", (len(samples),), dtype=byte_dtype, track_times=False)
        for index, sample in enumerate(samples):
            encoded[index] = np.frombuffer(Path(str(sample["source_path"])).read_bytes(), dtype=np.uint8)
        for field in ("relative_path", "source_sha256", "sample_key", "split", "data_role", "class_name"):
            handle.create_dataset(
                field,
                data=np.asarray([str(sample[field]) for sample in samples], dtype=object),
                dtype=_utf8_dtype(),
                track_times=False,
            )
        for field in ("target", "semantic_class_id", "class_order_rank", "uq_idx"):
            handle.create_dataset(
                field,
                data=np.asarray([int(sample[field]) for sample in samples], dtype=np.int64),
                dtype=np.int64,
                track_times=False,
            )
        handle.create_dataset(
            "domain",
            data=np.asarray([str(sample.get("domain", "")) for sample in samples], dtype=object),
            dtype=_utf8_dtype(),
            track_times=False,
        )
        handle.flush()


def plan_artifact(contract_path: str | Path, inventory_path: str | Path) -> dict[str, Any]:
    contract, inventory, identities = load_build_inputs(contract_path, inventory_path, verify_sources=False)
    records_per_shard = int(contract["records_per_shard"])
    sample_count = len(inventory["samples"])
    return {
        "success": True,
        "action": "plan",
        "side_effects": False,
        "artifact_schema": ARTIFACT_SCHEMA,
        "sample_count": sample_count,
        "records_per_shard": records_per_shard,
        "shard_count": (sample_count + records_per_shard - 1) // records_per_shard,
        **identities,
    }


def build_state_identity(
    contract: dict[str, Any], inventory: dict[str, Any], identities: dict[str, str]
) -> dict[str, Any]:
    return {
        "artifact_schema": ARTIFACT_SCHEMA,
        "dataset_semantics_sha256": identities["dataset_semantics_sha256"],
        "source_inventory_sha256": identities["source_inventory_sha256"],
        "artifact_build_contract_sha256": identities["artifact_build_contract_sha256"],
        "converter_bundle_sha256": contract["converter"]["bundle_sha256"],
        "records_per_shard": int(contract["records_per_shard"]),
        "sample_count": len(inventory["samples"]),
    }


def initial_build_state(build_key: str, identity: dict[str, Any]) -> dict[str, Any]:
    records_per_shard = int(identity["records_per_shard"])
    sample_count = int(identity["sample_count"])
    shards = []
    for shard_index, start in enumerate(range(0, sample_count, records_per_shard)):
        shards.append(
            {
                "path": f"shards/part-{shard_index:05d}.h5",
                "start": start,
                "stop": min(start + records_per_shard, sample_count),
                "state": "pending",
            }
        )
    return {
        "schema": BUILD_STATE_SCHEMA,
        "build_key": build_key,
        "identity": identity,
        "shards": shards,
    }


def build_artifact(contract_path: str | Path, inventory_path: str | Path, output_root: str | Path) -> dict[str, Any]:
    contract, inventory, identities = load_build_inputs(contract_path, inventory_path, verify_sources=True)
    output = Path(output_root).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = build_state_identity(contract, inventory, identities)
    build_key = canonical_sha256(identity)
    building_root = output / ".building"
    building_root.mkdir(parents=True, exist_ok=True)
    work = building_root / build_key
    lock_path = building_root / f"{build_key}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        work.mkdir(parents=True, exist_ok=True)
        state_path = work / "BUILD_STATE.json"
        if state_path.is_file():
            state = load_json(state_path)
            if (
                state.get("schema") != BUILD_STATE_SCHEMA
                or state.get("build_key") != build_key
                or state.get("identity") != identity
            ):
                raise ContractError(f"build_state_identity_conflict:{work}")
        else:
            state = initial_build_state(build_key, identity)
            atomic_write_json(state_path, state)

        resumed_shards = 0
        shard_records: list[dict[str, Any]] = []
        samples = inventory["samples"]
        for shard in state["shards"]:
            relative = shard["path"]
            shard_path = work / relative
            shard_path.parent.mkdir(parents=True, exist_ok=True)
            observed_size = shard_path.stat().st_size if shard_path.is_file() else None
            observed_sha = sha256_file(shard_path) if shard_path.is_file() else None
            if (
                shard.get("state") == "complete"
                and observed_size == shard.get("size_bytes")
                and observed_sha == shard.get("sha256")
            ):
                resumed_shards += 1
            else:
                part = shard_path.with_suffix(shard_path.suffix + ".part")
                if part.exists():
                    part.unlink()
                write_shard(part, samples[int(shard["start"]) : int(shard["stop"])])
                os.replace(part, shard_path)
                shard["size_bytes"] = shard_path.stat().st_size
                shard["sha256"] = sha256_file(shard_path)
                shard["state"] = "complete"
                atomic_write_json(state_path, state)
            shard_records.append(
                {"path": relative, "size_bytes": shard["size_bytes"], "sha256": shard["sha256"]}
            )
        manifest = build_artifact_manifest(
            artifact_schema=ARTIFACT_SCHEMA,
            dataset_semantics_sha256=identities["dataset_semantics_sha256"],
            source_inventory_sha256=identities["source_inventory_sha256"],
            artifact_build_contract_sha256=identities["artifact_build_contract_sha256"],
            converter_bundle_sha256=contract["converter"]["bundle_sha256"],
            shards=shard_records,
        )
        atomic_write_json(work / "DATA_ARTIFACT_MANIFEST.json", manifest)
        marker = {
            "schema": "autoreskill.artifact_complete.v1",
            "artifact_content_sha256": manifest["artifact_content_sha256"],
            "manifest_sha256": sha256_file(work / "DATA_ARTIFACT_MANIFEST.json"),
        }
        atomic_write_json(work / "ARTIFACT_COMPLETE.json", marker)
        final = output / manifest["artifact_content_sha256"]
        if final.exists():
            existing = load_json(final / "DATA_ARTIFACT_MANIFEST.json")
            errors = validate_artifact_manifest(existing, artifact_root=final, verify_shards=True)
            errors.extend(artifact_complete_marker_errors(final, existing))
            if errors or existing != manifest:
                raise ContractError(f"artifact_destination_conflict:{final}:{','.join(errors)}")
            shutil.rmtree(work)
            reused = True
        else:
            state_path.unlink()
            os.replace(work, final)
            reused = False
        return {
            "success": True,
            "action": "build",
            "artifact_root": str(final),
            "artifact_content_sha256": manifest["artifact_content_sha256"],
            "artifact_build_contract_sha256": identities["artifact_build_contract_sha256"],
            "dataset_semantics_sha256": identities["dataset_semantics_sha256"],
            "source_inventory_sha256": identities["source_inventory_sha256"],
            "converter_bundle_sha256": contract["converter"]["bundle_sha256"],
            "sample_count": len(samples),
            "shard_count": len(shard_records),
            "resumed_shard_count": resumed_shards,
            "build_key": build_key,
            "reused_existing": reused,
        }


def read_hdf5_records(artifact_root: Path, manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    decode_errors: list[str] = []
    text_fields = ("relative_path", "source_sha256", "sample_key", "split", "data_role", "class_name", "domain")
    int_fields = ("target", "semantic_class_id", "class_order_rank", "uq_idx")
    for shard in manifest["shards"]:
        with h5py.File(artifact_root / shard["path"], "r") as handle:
            count = len(handle["sample_key"])
            required = {"encoded_image_bytes", *text_fields, *int_fields}
            missing = sorted(required - set(handle.keys()))
            if missing:
                raise ContractError(f"hdf5_fields_missing:{shard['path']}:{','.join(missing)}")
            if any(len(handle[field]) != count for field in required):
                raise ContractError(f"hdf5_column_length_mismatch:{shard['path']}")
            for index in range(count):
                image_bytes = bytes(np.asarray(handle["encoded_image_bytes"][index], dtype=np.uint8))
                try:
                    with Image.open(BytesIO(image_bytes)) as image:
                        image.verify()
                except Exception as exc:  # Pillow exposes format-specific exceptions.
                    decode_errors.append(f"decode_failed:{shard['path']}:{index}:{type(exc).__name__}")
                record: dict[str, Any] = {}
                for field in text_fields:
                    value = handle[field][index]
                    record[field] = value.decode("utf-8") if isinstance(value, bytes) else str(value)
                for field in int_fields:
                    record[field] = int(handle[field][index])
                if hashlib.sha256(image_bytes).hexdigest() != record["source_sha256"]:
                    decode_errors.append(f"encoded_source_sha256_mismatch:{shard['path']}:{index}")
                records.append(record)
    return records, decode_errors


def artifact_complete_marker_errors(root: Path, manifest: dict[str, Any]) -> list[str]:
    marker_path = root / "ARTIFACT_COMPLETE.json"
    if not marker_path.is_file():
        return ["artifact_complete_marker_missing"]
    marker = load_json(marker_path)
    errors: list[str] = []
    if marker.get("schema") != "autoreskill.artifact_complete.v1":
        errors.append("artifact_complete_marker_schema_mismatch")
    if marker.get("artifact_content_sha256") != manifest.get("artifact_content_sha256"):
        errors.append("artifact_complete_marker_identity_mismatch")
    manifest_path = root / "DATA_ARTIFACT_MANIFEST.json"
    if marker.get("manifest_sha256") != sha256_file(manifest_path):
        errors.append("artifact_complete_marker_manifest_sha256_mismatch")
    return errors


def _text_sha256(value: str | bytes | None) -> str:
    if value is None:
        payload = b""
    elif isinstance(value, bytes):
        payload = value
    else:
        payload = value.encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def validate_artifact(
    artifact_root: str | Path,
    inventory_path: str | Path,
    consumer_contract_path: str | Path,
    *,
    probe_argv: list[str] | None = None,
) -> dict[str, Any]:
    root = Path(artifact_root).expanduser().resolve()
    manifest = load_json(root / "DATA_ARTIFACT_MANIFEST.json")
    core_errors = validate_artifact_manifest(manifest, artifact_root=root, verify_shards=True)
    core_errors.extend(artifact_complete_marker_errors(root, manifest))
    errors = list(core_errors)
    inventory = load_inventory(inventory_path, verify_sources=True)
    expected_inventory_sha256 = canonical_sha256(portable_inventory_payload(inventory))
    if manifest.get("source_inventory_sha256") != expected_inventory_sha256:
        errors.append("artifact_source_inventory_sha256_mismatch")
    records: list[dict[str, Any]] = []
    if not core_errors:
        records, decode_errors = read_hdf5_records(root, manifest)
        errors.extend(decode_errors)
        expected_records = []
        for sample in inventory["samples"]:
            expected = {field: sample[field] for field in PORTABLE_SAMPLE_FIELDS}
            expected["domain"] = str(sample.get("domain", ""))
            expected_records.append(expected)
        if records != expected_records:
            errors.append("artifact_inventory_semantic_parity_mismatch")
    consumer = load_json(consumer_contract_path)
    if consumer.get("schema") != CONSUMER_CONTRACT_SCHEMA:
        errors.append("consumer_contract_schema_mismatch")
    for field in ("loader_bundle_sha256", "evaluator_bundle_sha256"):
        try:
            require_hash(consumer.get(field), f"consumer_contract.{field}")
        except ContractError as exc:
            errors.append(str(exc))
    if consumer.get("probe_required") is not True:
        errors.append("consumer_contract.probe_required_must_be_true")
    probe_timeout_seconds = consumer.get("probe_timeout_seconds")
    if (
        not isinstance(probe_timeout_seconds, int)
        or isinstance(probe_timeout_seconds, bool)
        or not 1 <= probe_timeout_seconds <= 900
    ):
        errors.append("consumer_contract.probe_timeout_seconds_must_be_1_to_900")
        probe_timeout_seconds = 300
    consumer_contract_sha256 = canonical_sha256(consumer)
    probe_result: dict[str, Any] | None = None
    if not probe_argv:
        errors.append("consumer_probe_required")
    elif not errors:
        environment = dict(os.environ)
        environment["AUTORESEARCH_DATA_ARTIFACT_ROOT"] = str(root)
        try:
            completed = subprocess.run(
                probe_argv,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
                env=environment,
                timeout=probe_timeout_seconds,
            )
            probe_result = {
                "argv_sha256": canonical_sha256(probe_argv),
                "command_basename": Path(probe_argv[0]).name,
                "timeout_seconds": probe_timeout_seconds,
                "returncode": completed.returncode,
                "stdout_sha256": _text_sha256(completed.stdout),
                "stderr_sha256": _text_sha256(completed.stderr),
            }
            if completed.returncode != 0:
                errors.append("consumer_probe_failed")
        except subprocess.TimeoutExpired as exc:
            probe_result = {
                "argv_sha256": canonical_sha256(probe_argv),
                "command_basename": Path(probe_argv[0]).name,
                "timeout_seconds": probe_timeout_seconds,
                "timed_out": True,
                "stdout_sha256": _text_sha256(exc.stdout),
                "stderr_sha256": _text_sha256(exc.stderr),
            }
            errors.append("consumer_probe_timeout")
    validator_bundle_sha256 = sha256_file(Path(__file__).resolve())
    report = {
        "schema": "autoreskill.data_validation_report.v1",
        "artifact_schema": manifest.get("artifact_schema"),
        "artifact_content_sha256": manifest.get("artifact_content_sha256"),
        "consumer_contract_sha256": consumer_contract_sha256,
        "validator_bundle_sha256": validator_bundle_sha256,
        "source_inventory_sha256": expected_inventory_sha256,
        "converter_bundle_sha256": manifest.get("converter_bundle_sha256"),
        "sample_count": len(records),
        "probe": probe_result,
        "errors": sorted(set(errors)),
        "valid": not errors,
    }
    report_sha256 = canonical_sha256(report)
    result = {
        "success": not errors,
        "action": "validate",
        "report": report,
        "validation_report_sha256": report_sha256,
    }
    if errors:
        return result
    attestation = build_validation_attestation(
        artifact_content_sha256=manifest["artifact_content_sha256"],
        consumer_contract_sha256=consumer_contract_sha256,
        validator_bundle_sha256=validator_bundle_sha256,
        validation_report_sha256=report_sha256,
    )
    attestation_root = root / "attestations" / attestation["validation_attestation_id"]
    attestation_root.mkdir(parents=True, exist_ok=True)
    atomic_write_json(attestation_root / "VALIDATION_REPORT.json", report)
    atomic_write_json(attestation_root / "READY.json", attestation)
    result.update(
        {
            "validation_attestation_id": attestation["validation_attestation_id"],
            "consumer_contract_sha256": consumer_contract_sha256,
            "attestation_root": str(attestation_root),
        }
    )
    return result


def inspect_artifact(artifact_root: str | Path, *, verify_shards: bool) -> dict[str, Any]:
    root = Path(artifact_root).expanduser().resolve()
    manifest = load_json(root / "DATA_ARTIFACT_MANIFEST.json")
    errors = validate_artifact_manifest(manifest, artifact_root=root, verify_shards=verify_shards)
    return {
        "success": not errors,
        "action": "inspect",
        "artifact_root": str(root),
        "manifest": manifest,
        "errors": errors,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--contract", required=True)
    plan.add_argument("--inventory", required=True)
    build = sub.add_parser("build")
    build.add_argument("--contract", required=True)
    build.add_argument("--inventory", required=True)
    build.add_argument("--output-root", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--artifact-root", required=True)
    validate.add_argument("--inventory", required=True)
    validate.add_argument("--consumer-contract", required=True)
    validate.add_argument("--probe-argv-json")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--artifact-root", required=True)
    inspect.add_argument("--verify-shards", action="store_true")
    for command in (plan, build, validate, inspect):
        command.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "plan":
            result = plan_artifact(args.contract, args.inventory)
        elif args.command == "build":
            result = build_artifact(args.contract, args.inventory, args.output_root)
        elif args.command == "validate":
            probe = json.loads(args.probe_argv_json) if args.probe_argv_json else None
            if probe is not None and (not isinstance(probe, list) or not all(isinstance(item, str) for item in probe)):
                raise ContractError("probe_argv_json_must_be_string_array")
            result = validate_artifact(
                args.artifact_root,
                args.inventory,
                args.consumer_contract,
                probe_argv=probe,
            )
        else:
            result = inspect_artifact(args.artifact_root, verify_shards=args.verify_shards)
    except (ContractError, OSError, json.JSONDecodeError) as exc:
        result = {"success": False, "error": str(exc), "action": args.command}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
