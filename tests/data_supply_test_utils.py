import json
from datetime import datetime, timezone
from pathlib import Path

from hpc_core.data_artifacts import build_artifact_manifest, build_validation_attestation, canonical_sha256


NOW = datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc)


def write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def supply_inputs(root: Path) -> tuple[Path, Path, Path, dict, dict]:
    manifest = build_artifact_manifest(
        artifact_schema="image_bytes_indexed_v1",
        dataset_semantics_sha256="1" * 64,
        source_inventory_sha256="a" * 64,
        artifact_build_contract_sha256="2" * 64,
        converter_bundle_sha256="b" * 64,
        shards=[
            {"path": "shards/part-00000.h5", "size_bytes": 100, "sha256": "3" * 64},
            {"path": "shards/part-00001.h5", "size_bytes": 200, "sha256": "4" * 64},
        ],
    )
    report = {
        "schema": "autoreskill.data_validation_report.v1",
        "artifact_schema": manifest["artifact_schema"],
        "artifact_content_sha256": manifest["artifact_content_sha256"],
        "consumer_contract_sha256": "5" * 64,
        "validator_bundle_sha256": "6" * 64,
        "source_inventory_sha256": manifest["source_inventory_sha256"],
        "converter_bundle_sha256": manifest["converter_bundle_sha256"],
        "sample_count": 2,
        "probe": {"returncode": 0},
        "errors": [],
        "valid": True,
    }
    ready = build_validation_attestation(
        artifact_content_sha256=manifest["artifact_content_sha256"],
        consumer_contract_sha256="5" * 64,
        validator_bundle_sha256="6" * 64,
        validation_report_sha256=canonical_sha256(report),
    )
    snapshot = {
        "schema": "autoreskill.bjtu_native_quota_snapshot.v1",
        "checked_at": "2026-07-18T04:00:00Z",
        "provider": {"name": "fixture-native", "bundle_sha256": "8" * 64},
        "accounts": [
            {
                "alias": "main",
                "portal_user": "100",
                "cluster": "cluster2",
                "account": "u100",
                "quota_bytes": 10000,
                "used_bytes": 1000,
                "artifact_root": "/gpfs/home/u100/autoreskill_data",
                "acl_capabilities": {"setfacl": True, "read_only_share": True},
            },
            {
                "alias": "other",
                "portal_user": "200",
                "cluster": "cluster2",
                "account": "u200",
                "quota_bytes": 10000,
                "used_bytes": 3000,
                "artifact_root": "/gpfs/home/u200/autoreskill_data",
                "acl_capabilities": {"setfacl": False, "read_only_share": False},
            },
        ],
    }
    write_json(root / "VALIDATION_REPORT.json", report)
    return (
        write_json(root / "manifest.json", manifest),
        write_json(root / "READY.json", ready),
        write_json(root / "snapshot.json", snapshot),
        manifest,
        ready,
    )
