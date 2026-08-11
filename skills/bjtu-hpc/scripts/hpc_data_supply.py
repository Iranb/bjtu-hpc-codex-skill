#!/usr/bin/env python3
"""Plan and record content-addressed HPC data supply without remote mutation.

Production upload, ACL, commit, and delete adapters are deliberately disabled
until a native BJTU provider and exact remote transaction are separately
authorized.  Local registry mutations require explicit confirmation and CAS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hpc_core.data_artifacts import (
    ArtifactRegistry,
    ContractError,
    RegistryConflict,
    canonical_sha256,
    load_json,
    require_hash,
    utc_now,
    validate_artifact_manifest,
    validate_validation_attestation,
)
from hpc_storage_snapshot import normalize_snapshot, parse_time


PLAN_SCHEMA = "autoreskill.hpc_data_supply_plan.v1"
VERIFY_REPORT_SCHEMA = "autoreskill.hpc_data_supply_verify_report.v1"
REMOTE_RECEIPT_SCHEMA = "autoreskill.hpc_data_supply_remote_receipt.v1"
DEFAULT_REGISTRY = Path(__file__).resolve().parent / "work/data_supply/DATA_ARTIFACT_REGISTRY.json"


def _load_ready(path: str | Path) -> dict[str, Any]:
    ready = load_json(path)
    errors = validate_validation_attestation(ready)
    if errors:
        raise ContractError(f"validation_attestation_invalid:{','.join(errors)}")
    return ready


def make_plan(
    *,
    manifest_path: str | Path,
    ready_path: str | Path,
    snapshot_path: str | Path,
    max_snapshot_age_seconds: int,
    share_accounts: list[str],
    reserve_bytes: int,
    reserve_fraction: float,
    now: Any = None,
) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    errors = validate_artifact_manifest(manifest)
    if errors:
        raise ContractError(f"artifact_manifest_invalid:{','.join(errors)}")
    ready = _load_ready(ready_path)
    if ready["artifact_content_sha256"] != manifest["artifact_content_sha256"]:
        raise ContractError("attestation_artifact_content_mismatch")
    validation_report_path = Path(ready_path).resolve().parent / "VALIDATION_REPORT.json"
    if not validation_report_path.is_file():
        raise ContractError("validation_report_missing_next_to_attestation")
    validation_report = load_json(validation_report_path)
    if canonical_sha256(validation_report) != ready["validation_report_sha256"]:
        raise ContractError("validation_report_sha256_mismatch")
    if validation_report.get("valid") is not True or validation_report.get("errors") != []:
        raise ContractError("validation_report_not_successful")
    for field in (
        "artifact_schema",
        "artifact_content_sha256",
        "consumer_contract_sha256",
        "validator_bundle_sha256",
        "source_inventory_sha256",
        "converter_bundle_sha256",
    ):
        expected = ready.get(field) if field in ready else manifest.get(field)
        if validation_report.get(field) != expected:
            raise ContractError(f"validation_report_{field}_mismatch")
    if reserve_bytes < 0 or not 0 <= reserve_fraction < 1:
        raise ContractError("invalid_storage_reserve")
    snapshot = normalize_snapshot(
        load_json(snapshot_path),
        max_age_seconds=max_snapshot_age_seconds,
        now=now,
    )
    account_by_alias = {item["alias"]: item for item in snapshot["accounts"]}
    unknown = sorted(set(share_accounts) - set(account_by_alias))
    if unknown:
        raise ContractError(f"share_accounts_missing_from_snapshot:{','.join(unknown)}")
    required_bytes = int(manifest["total_size_bytes"])
    candidates: list[dict[str, Any]] = []
    for account in snapshot["accounts"]:
        reserve = max(reserve_bytes, int(account["quota_bytes"] * reserve_fraction))
        post_upload_free = account["free_bytes"] - required_bytes
        shares_other_accounts = any(alias != account["alias"] for alias in share_accounts)
        acl_fitting = not shares_other_accounts or account["acl_capabilities"]["read_only_share"]
        if post_upload_free >= reserve and acl_fitting:
            candidates.append({**account, "reserve_bytes": reserve, "post_upload_free_bytes": post_upload_free})
    if not candidates:
        raise ContractError("no_storage_owner_satisfies_quota_and_acl")
    owner = max(candidates, key=lambda item: (item["post_upload_free_bytes"], item["alias"]))
    artifact_hash = manifest["artifact_content_sha256"]
    final_root = f"{owner['artifact_root'].rstrip('/')}/{artifact_hash}"
    incoming_root = f"{owner['artifact_root'].rstrip('/')}/.incoming/{artifact_hash}"
    immutable = {
        "schema": PLAN_SCHEMA,
        "artifact_schema": manifest["artifact_schema"],
        "artifact_content_sha256": artifact_hash,
        "artifact_build_contract_sha256": manifest["artifact_build_contract_sha256"],
        "dataset_semantics_sha256": manifest["dataset_semantics_sha256"],
        "source_inventory_sha256": manifest["source_inventory_sha256"],
        "converter_bundle_sha256": manifest["converter_bundle_sha256"],
        "consumer_contract_sha256": ready["consumer_contract_sha256"],
        "validator_bundle_sha256": ready["validator_bundle_sha256"],
        "validation_attestation_id": ready["validation_attestation_id"],
        "validation_report_sha256": ready["validation_report_sha256"],
        "snapshot_checked_at": snapshot["checked_at"],
        "snapshot_provider_bundle_sha256": snapshot["provider"]["bundle_sha256"],
        "storage_owner": {
            "alias": owner["alias"],
            "portal_user": owner["portal_user"],
            "cluster": owner["cluster"],
            "account": owner["account"],
        },
        "required_bytes": required_bytes,
        "reserve_bytes": owner["reserve_bytes"],
        "incoming_root": incoming_root,
        "final_root": final_root,
        "share_accounts": sorted(set(share_accounts)),
        "shards": manifest["shards"],
    }
    return {
        "success": True,
        "action": "plan",
        "side_effects": False,
        "remote_mutation_authorized": False,
        "plan_sha256": canonical_sha256(immutable),
        **immutable,
        "rollback": {
            "owned_incoming_root_only": incoming_root,
            "delete_final_root": False,
            "delete_legacy_data": False,
        },
    }


def prepare_intent(
    registry: ArtifactRegistry,
    *,
    plan: dict[str, Any],
    expected_revision: int,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("confirm_local_intent_required")
    if plan.get("schema") != PLAN_SCHEMA:
        raise ContractError("data_supply_plan_schema_mismatch")
    plan_sha256 = require_hash(plan.get("plan_sha256"), "plan_sha256")
    immutable = {key: value for key, value in plan.items() if key not in {"success", "action", "side_effects", "remote_mutation_authorized", "plan_sha256", "rollback"}}
    if canonical_sha256(immutable) != plan_sha256:
        raise ContractError("data_supply_plan_sha256_mismatch")

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        existing = current["intents"].get(plan_sha256)
        if existing and existing.get("plan") != immutable:
            raise ContractError("intent_plan_conflict")
        if not existing:
            current["intents"][plan_sha256] = {
                "state": "prepared_local",
                "prepared_at": utc_now(),
                "plan": immutable,
                "shards": {item["path"]: {"state": "pending"} for item in immutable["shards"]},
                "remote_receipt": None,
            }
        return current

    updated = registry.compare_and_swap(expected_revision, mutate)
    return {
        "success": True,
        "action": "prepare-intent",
        "registry_revision": updated["revision"],
        "intent_id": plan_sha256,
        "state": updated["intents"][plan_sha256]["state"],
        "remote_mutation_performed": False,
    }


def record_fake_shard(
    registry: ArtifactRegistry,
    *,
    intent_id: str,
    shard_path: str,
    observed_sha256: str,
    expected_revision: int,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("confirm_fixture_state_required")
    require_hash(intent_id, "intent_id")
    require_hash(observed_sha256, "observed_sha256")

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        intent = current["intents"].get(intent_id)
        if not intent:
            raise ContractError("intent_not_found")
        expected = {item["path"]: item for item in intent["plan"]["shards"]}.get(shard_path)
        if not expected:
            raise ContractError("intent_shard_not_found")
        if expected["sha256"] != observed_sha256:
            raise ContractError("fixture_shard_sha256_mismatch")
        intent["shards"][shard_path] = {"state": "verified", "sha256": observed_sha256}
        if all(item.get("state") == "verified" for item in intent["shards"].values()):
            intent["state"] = "fixture_shards_verified"
        return current

    updated = registry.compare_and_swap(expected_revision, mutate)
    intent = updated["intents"][intent_id]
    return {
        "success": True,
        "action": "record-fixture-shard",
        "registry_revision": updated["revision"],
        "intent_id": intent_id,
        "state": intent["state"],
    }


def record_remote_receipt(
    registry: ArtifactRegistry,
    *,
    receipt: dict[str, Any],
    expected_revision: int,
    adapter_authorized: bool,
) -> dict[str, Any]:
    """Record a receipt produced by a separately authorized remote adapter.

    This function is intentionally not exposed as a CLI command while the
    production adapter is disabled.  Tests may exercise the transition by
    setting ``adapter_authorized=True``; ordinary local confirmation is not a
    substitute for a mutation receipt.
    """

    if not adapter_authorized:
        raise ContractError("authorized_remote_adapter_required")
    allowed = {
        "schema",
        "intent_id",
        "plan_sha256",
        "artifact_content_sha256",
        "native_provider_bundle_sha256",
        "storage_owner",
        "final_root",
        "operation",
        "committed_at",
    }
    if set(receipt) != allowed or receipt.get("schema") != REMOTE_RECEIPT_SCHEMA:
        raise ContractError("remote_receipt_schema_or_fields_mismatch")
    intent_id = require_hash(receipt.get("intent_id"), "intent_id")
    require_hash(receipt.get("native_provider_bundle_sha256"), "native_provider_bundle_sha256")
    require_hash(receipt.get("artifact_content_sha256"), "artifact_content_sha256")
    parse_time(receipt.get("committed_at"), "committed_at")
    if receipt.get("operation") != "upload_commit_share":
        raise ContractError("remote_receipt_operation_mismatch")
    receipt_hash = canonical_sha256(receipt)

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        intent = current["intents"].get(intent_id)
        if not intent:
            raise ContractError("intent_not_found")
        plan = intent["plan"]
        for field in ("plan_sha256", "artifact_content_sha256", "storage_owner", "final_root"):
            expected = intent_id if field == "plan_sha256" else plan[field]
            if receipt.get(field) != expected:
                raise ContractError(f"remote_receipt_{field}_mismatch")
        if receipt["native_provider_bundle_sha256"] != plan["snapshot_provider_bundle_sha256"]:
            raise ContractError("remote_receipt_native_provider_mismatch")
        existing = intent.get("remote_receipt")
        if existing and existing.get("sha256") != receipt_hash:
            raise ContractError("remote_receipt_conflict")
        intent["remote_receipt"] = {"sha256": receipt_hash, "payload": receipt}
        intent["state"] = "remote_receipt_recorded"
        return current

    updated = registry.compare_and_swap(expected_revision, mutate)
    return {
        "success": True,
        "action": "record-remote-receipt",
        "registry_revision": updated["revision"],
        "intent_id": intent_id,
        "remote_mutation_receipt_sha256": receipt_hash,
    }


def record_verify_report(
    registry: ArtifactRegistry,
    *,
    report: dict[str, Any],
    expected_revision: int,
    confirmed: bool,
) -> dict[str, Any]:
    if not confirmed:
        raise ContractError("confirm_local_registry_required")
    if report.get("schema") != VERIFY_REPORT_SCHEMA:
        raise ContractError("verify_report_schema_mismatch")
    intent_id = require_hash(report.get("intent_id"), "intent_id")
    artifact_hash = require_hash(report.get("artifact_content_sha256"), "artifact_content_sha256")
    provider_bundle_sha256 = require_hash(
        report.get("native_provider_bundle_sha256"), "native_provider_bundle_sha256"
    )
    require_hash(report.get("remote_mutation_receipt_sha256"), "remote_mutation_receipt_sha256")
    parse_time(report.get("verified_at"), "verified_at")
    report_hash = canonical_sha256(report)

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        intent = current["intents"].get(intent_id)
        if not intent:
            raise ContractError("intent_not_found")
        plan = intent["plan"]
        remote_receipt = intent.get("remote_receipt") if isinstance(intent.get("remote_receipt"), dict) else {}
        if remote_receipt.get("sha256") != report.get("remote_mutation_receipt_sha256"):
            raise ContractError("verify_report_remote_receipt_mismatch")
        if report.get("plan_sha256") != intent_id:
            raise ContractError("verify_report_plan_sha256_mismatch")
        if provider_bundle_sha256 != plan["snapshot_provider_bundle_sha256"]:
            raise ContractError("verify_report_native_provider_mismatch")
        if plan["artifact_content_sha256"] != artifact_hash:
            raise ContractError("verify_report_artifact_mismatch")
        for field in (
            "artifact_schema",
            "dataset_semantics_sha256",
            "source_inventory_sha256",
            "artifact_build_contract_sha256",
            "converter_bundle_sha256",
            "consumer_contract_sha256",
            "validator_bundle_sha256",
            "validation_attestation_id",
            "validation_report_sha256",
        ):
            if report.get(field) != plan[field]:
                raise ContractError(f"verify_report_{field}_mismatch")
        if report.get("final_root") != plan["final_root"]:
            raise ContractError("verify_report_final_root_mismatch")
        if report.get("storage_owner") != plan["storage_owner"]:
            raise ContractError("verify_report_storage_owner_mismatch")
        observed = report.get("shards")
        if not isinstance(observed, list):
            raise ContractError("verify_report_shards_missing")
        expected_shards = {item["path"]: item for item in plan["shards"]}
        observed_shards = {item.get("path"): item for item in observed if isinstance(item, dict)}
        if set(observed_shards) != set(expected_shards):
            raise ContractError("verify_report_shard_set_mismatch")
        for path, expected in expected_shards.items():
            actual = observed_shards[path]
            if actual.get("sha256") != expected["sha256"] or actual.get("size_bytes") != expected["size_bytes"]:
                raise ContractError(f"verify_report_shard_mismatch:{path}")
        readable = sorted(report.get("readable_accounts", []))
        if readable != sorted(plan["share_accounts"]):
            raise ContractError("verify_report_acl_readability_mismatch")
        if report.get("core_marker_verified") is not True or report.get("attestation_ready_verified") is not True:
            raise ContractError("verify_report_commit_markers_not_verified")
        next_revision = current["revision"] + 1
        if report.get("registry_revision") != next_revision:
            raise ContractError("verify_report_registry_revision_mismatch")
        current["artifacts"][artifact_hash] = {
            "state": "available",
            "final_root": plan["final_root"],
            "storage_owner": plan["storage_owner"],
            "readable_accounts": readable,
            "consumer_contract_sha256": plan["consumer_contract_sha256"],
            "validation_attestation_id": plan["validation_attestation_id"],
            "validation_report_sha256": plan["validation_report_sha256"],
            "verify_report_sha256": report_hash,
            "registry_revision": next_revision,
            "verified_at": report.get("verified_at"),
        }
        intent["state"] = "available"
        intent["verify_report_sha256"] = report_hash
        return current

    updated = registry.compare_and_swap(expected_revision, mutate)
    return {
        "success": True,
        "action": "record-verify",
        "registry_revision": updated["revision"],
        "artifact_content_sha256": artifact_hash,
        "state": "available",
        "verify_report_sha256": report_hash,
    }


def release_plan(registry: ArtifactRegistry, artifact_hash: str) -> dict[str, Any]:
    require_hash(artifact_hash, "artifact_content_sha256")
    current = registry.read()
    artifact = current["artifacts"].get(artifact_hash)
    if not artifact:
        raise ContractError("artifact_not_found")
    active_leases = [
        lease_id
        for lease_id, lease in current["leases"].items()
        if lease.get("artifact_content_sha256") == artifact_hash and lease.get("state") == "active"
    ]
    return {
        "success": True,
        "action": "release-plan",
        "side_effects": False,
        "artifact_content_sha256": artifact_hash,
        "final_root": artifact["final_root"],
        "active_leases": sorted(active_leases),
        "deletable": not active_leases,
        "remote_mutation_authorized": False,
        "requires_exact_path_approval": True,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    sub = result.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--manifest", required=True)
    plan.add_argument("--ready", required=True)
    plan.add_argument("--snapshot", required=True)
    plan.add_argument("--max-snapshot-age-seconds", type=int, default=300)
    plan.add_argument("--share-accounts", required=True)
    plan.add_argument("--reserve-bytes", type=int, default=20 * 1024**3)
    plan.add_argument("--reserve-fraction", type=float, default=0.10)
    prepare = sub.add_parser("prepare-intent")
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--expected-revision", required=True, type=int)
    prepare.add_argument("--confirm-local-intent", action="store_true")
    shard = sub.add_parser("record-fixture-shard")
    shard.add_argument("--intent-id", required=True)
    shard.add_argument("--shard-path", required=True)
    shard.add_argument("--observed-sha256", required=True)
    shard.add_argument("--expected-revision", required=True, type=int)
    shard.add_argument("--confirm-fixture-state", action="store_true")
    verify = sub.add_parser("record-verify")
    verify.add_argument("--report", required=True)
    verify.add_argument("--expected-revision", required=True, type=int)
    verify.add_argument("--confirm-local-registry", action="store_true")
    sub.add_parser("status")
    release = sub.add_parser("release-plan")
    release.add_argument("--artifact-content-sha256", required=True)
    for name in ("upload", "share", "commit", "delete"):
        remote = sub.add_parser(name)
        remote.add_argument("--expected-revision", type=int)
        remote.add_argument("--confirm-remote-mutation", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    registry = ArtifactRegistry(args.registry)
    try:
        if args.command == "plan":
            result = make_plan(
                manifest_path=args.manifest,
                ready_path=args.ready,
                snapshot_path=args.snapshot,
                max_snapshot_age_seconds=args.max_snapshot_age_seconds,
                share_accounts=[item.strip() for item in args.share_accounts.split(",") if item.strip()],
                reserve_bytes=args.reserve_bytes,
                reserve_fraction=args.reserve_fraction,
            )
        elif args.command == "prepare-intent":
            result = prepare_intent(
                registry,
                plan=load_json(args.plan),
                expected_revision=args.expected_revision,
                confirmed=args.confirm_local_intent,
            )
        elif args.command == "record-fixture-shard":
            result = record_fake_shard(
                registry,
                intent_id=args.intent_id,
                shard_path=args.shard_path,
                observed_sha256=args.observed_sha256,
                expected_revision=args.expected_revision,
                confirmed=args.confirm_fixture_state,
            )
        elif args.command == "record-verify":
            result = record_verify_report(
                registry,
                report=load_json(args.report),
                expected_revision=args.expected_revision,
                confirmed=args.confirm_local_registry,
            )
        elif args.command == "status":
            result = {"success": True, "action": "status", "registry": registry.read()}
        elif args.command == "release-plan":
            result = release_plan(registry, args.artifact_content_sha256)
        else:
            raise ContractError("remote_adapter_not_enabled")
    except (ContractError, RegistryConflict, OSError) as exc:
        result = {"success": False, "action": args.command, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
