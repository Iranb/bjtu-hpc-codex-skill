import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from hpc_core.data_artifacts import ContractError, build_artifact_manifest


SCHEMA_ROOT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "bjtu-hpc"
    / "scripts"
    / "schemas"
)


class DataArtifactSchemaTests(unittest.TestCase):
    def test_all_data_supply_schemas_are_valid_draft_2020_12(self):
        names = (
            "data_artifact_manifest.schema.json",
            "data_validation_attestation.schema.json",
            "data_validation_report.schema.json",
            "data_supply_contract.schema.json",
            "hpc_data_artifact_registry.schema.json",
            "hpc_data_supply_plan.schema.json",
            "hpc_data_supply_remote_receipt.schema.json",
            "hpc_data_supply_verify_report.schema.json",
        )
        for name in names:
            with self.subTest(name=name):
                payload = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(payload)

    def test_feature_artifacts_fail_closed_until_full_identity_contract_exists(self):
        with self.assertRaisesRegex(ContractError, "unsupported_artifact_schema:frozen_feature_v1"):
            build_artifact_manifest(
                artifact_schema="frozen_feature_v1",
                dataset_semantics_sha256="1" * 64,
                source_inventory_sha256="2" * 64,
                artifact_build_contract_sha256="3" * 64,
                converter_bundle_sha256="4" * 64,
                shards=[{"path": "shards/part-00000.h5", "size_bytes": 1, "sha256": "5" * 64}],
            )


if __name__ == "__main__":
    unittest.main()
