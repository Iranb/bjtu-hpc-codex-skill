import tempfile
import unittest
from pathlib import Path

from hpc_core.data_artifacts import ArtifactRegistry, canonical_sha256
from hpc_data_supply import (
    make_plan,
    prepare_intent,
    record_remote_receipt,
    record_verify_report,
    release_plan,
)
from tests.data_supply_test_utils import NOW, supply_inputs


class HpcDataSupplyReleaseTests(unittest.TestCase):
    def test_release_is_dry_run_and_reports_active_lease(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path, ready_path, snapshot_path, manifest, _ = supply_inputs(root)
            plan = make_plan(
                manifest_path=manifest_path,
                ready_path=ready_path,
                snapshot_path=snapshot_path,
                max_snapshot_age_seconds=300,
                share_accounts=["main"],
                reserve_bytes=500,
                reserve_fraction=0.1,
                now=NOW,
            )
            registry = ArtifactRegistry(root / "registry.json")
            prepare_intent(registry, plan=plan, expected_revision=0, confirmed=True)
            receipt = {
                "schema": "autoreskill.hpc_data_supply_remote_receipt.v1",
                "intent_id": plan["plan_sha256"],
                "plan_sha256": plan["plan_sha256"],
                "artifact_content_sha256": manifest["artifact_content_sha256"],
                "native_provider_bundle_sha256": "8" * 64,
                "storage_owner": plan["storage_owner"],
                "final_root": plan["final_root"],
                "operation": "upload_commit_share",
                "committed_at": "2026-07-18T04:00:30Z",
            }
            report = {
                "schema": "autoreskill.hpc_data_supply_verify_report.v1",
                "intent_id": plan["plan_sha256"],
                "plan_sha256": plan["plan_sha256"],
                "artifact_content_sha256": manifest["artifact_content_sha256"],
                "native_provider_bundle_sha256": "8" * 64,
                "remote_mutation_receipt_sha256": canonical_sha256(receipt),
                "artifact_schema": plan["artifact_schema"],
                "dataset_semantics_sha256": plan["dataset_semantics_sha256"],
                "source_inventory_sha256": plan["source_inventory_sha256"],
                "artifact_build_contract_sha256": plan["artifact_build_contract_sha256"],
                "converter_bundle_sha256": plan["converter_bundle_sha256"],
                "consumer_contract_sha256": plan["consumer_contract_sha256"],
                "validator_bundle_sha256": plan["validator_bundle_sha256"],
                "validation_attestation_id": plan["validation_attestation_id"],
                "validation_report_sha256": plan["validation_report_sha256"],
                "storage_owner": plan["storage_owner"],
                "final_root": plan["final_root"],
                "shards": manifest["shards"],
                "readable_accounts": ["main"],
                "core_marker_verified": True,
                "attestation_ready_verified": True,
                "verified_at": "2026-07-18T04:01:00Z",
                "registry_revision": 3,
            }
            with self.assertRaisesRegex(ValueError, "verify_report_remote_receipt_mismatch"):
                record_verify_report(registry, report=report, expected_revision=1, confirmed=True)
            receipt_result = record_remote_receipt(
                registry,
                receipt=receipt,
                expected_revision=1,
                adapter_authorized=True,
            )
            self.assertEqual(
                receipt_result["remote_mutation_receipt_sha256"],
                report["remote_mutation_receipt_sha256"],
            )
            record_verify_report(registry, report=report, expected_revision=2, confirmed=True)

            def add_lease(current):
                current["leases"]["lease-1"] = {
                    "artifact_content_sha256": manifest["artifact_content_sha256"],
                    "state": "active",
                }
                return current

            registry.compare_and_swap(3, add_lease)
            result = release_plan(registry, manifest["artifact_content_sha256"])
            self.assertFalse(result["deletable"])
            self.assertEqual(result["active_leases"], ["lease-1"])
            self.assertFalse(result["remote_mutation_authorized"])


if __name__ == "__main__":
    unittest.main()
