import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import external_hdf5_artifact as artifact
from hpc_core.data_artifacts import sha256_file


class Hdf5SemanticValidatorTests(unittest.TestCase):
    def test_swapped_rows_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            samples = []
            for index, color in enumerate(((10, 20, 30), (40, 50, 60))):
                source = root / f"{index}.png"
                Image.new("RGB", (2, 2), color).save(source)
                samples.append(
                    {
                        "source_path": str(source),
                        "relative_path": source.name,
                        "source_sha256": sha256_file(source),
                        "sample_key": f"key-{index}",
                        "split": "train",
                        "data_role": "train_labeled",
                        "target": index,
                        "semantic_class_id": index,
                        "class_name": f"class-{index}",
                        "class_order_rank": index,
                        "uq_idx": index,
                    }
                )
            inventory = root / "inventory.json"
            inventory.write_text(
                json.dumps({"schema": artifact.SOURCE_INVENTORY_SCHEMA, "samples": samples}),
                encoding="utf-8",
            )
            contract = root / "contract.json"
            contract.write_text(
                json.dumps(
                    {
                        "schema": artifact.BUILD_CONTRACT_SCHEMA,
                        "artifact_schema": artifact.ARTIFACT_SCHEMA,
                        "dataset_semantics": {"dataset_id": "swap-fixture"},
                        "converter": {"name": "fixture", "bundle_sha256": "1" * 64},
                        "records_per_shard": 10,
                    }
                ),
                encoding="utf-8",
            )
            consumer = root / "consumer.json"
            consumer.write_text(
                json.dumps(
                    {
                        "schema": artifact.CONSUMER_CONTRACT_SCHEMA,
                        "loader_bundle_sha256": "2" * 64,
                        "evaluator_bundle_sha256": "3" * 64,
                    }
                ),
                encoding="utf-8",
            )
            built = artifact.build_artifact(contract, inventory, root / "out")
            swapped = root / "inventory-swapped.json"
            swapped.write_text(
                json.dumps({"schema": artifact.SOURCE_INVENTORY_SCHEMA, "samples": list(reversed(samples))}),
                encoding="utf-8",
            )
            result = artifact.validate_artifact(built["artifact_root"], swapped, consumer)
            self.assertFalse(result["success"])
            self.assertIn("artifact_inventory_semantic_parity_mismatch", result["report"]["errors"])


if __name__ == "__main__":
    unittest.main()
