import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

import external_hdf5_artifact as artifact


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExternalHdf5ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.images = self.root / "source"
        self.images.mkdir()
        for index, color in enumerate(((255, 0, 0), (0, 255, 0), (0, 0, 255))):
            Image.new("RGB", (4, 3), color).save(self.images / f"{index}.png", format="PNG")
        samples = []
        for index in range(3):
            source = self.images / f"{index}.png"
            samples.append(
                {
                    "source_path": str(source),
                    "relative_path": f"class-{index % 2}/{index}.png",
                    "source_sha256": file_hash(source),
                    "sample_key": f"train:{index}",
                    "split": "train",
                    "data_role": "train_labeled" if index < 2 else "train_unlabeled",
                    "target": index % 2,
                    "semantic_class_id": index % 2,
                    "class_name": f"class-{index % 2}",
                    "class_order_rank": index % 2,
                    "uq_idx": 100 + index,
                }
            )
        self.inventory = self.root / "inventory.json"
        write_json(
            self.inventory,
            {"schema": artifact.SOURCE_INVENTORY_SCHEMA, "samples": samples},
        )
        self.contract = self.root / "contract.json"
        write_json(
            self.contract,
            {
                "schema": artifact.BUILD_CONTRACT_SCHEMA,
                "artifact_schema": artifact.ARTIFACT_SCHEMA,
                "dataset_semantics": {
                    "dataset_id": "tiny",
                    "split_protocol": "fixture-v1",
                    "class_order": ["class-0", "class-1"],
                },
                "converter": {"name": "fixture", "bundle_sha256": "1" * 64},
                "records_per_shard": 2,
                "hdf5_settings": {"libver": "latest", "encoded_bytes": "original"},
            },
        )
        self.consumer = self.root / "consumer.json"
        write_json(
            self.consumer,
            {
                "schema": artifact.CONSUMER_CONTRACT_SCHEMA,
                "loader_bundle_sha256": "2" * 64,
                "evaluator_bundle_sha256": "3" * 64,
                "sample_identity_policy": "exact_order_and_uq_idx",
                "probe_required": True,
                "probe_timeout_seconds": 30,
            },
        )

    def probe_argv(self):
        code = (
            "import os,pathlib,h5py;"
            "r=pathlib.Path(os.environ['AUTORESEARCH_DATA_ARTIFACT_ROOT']);"
            "p=sorted((r/'shards').glob('*.h5'));"
            "assert p;"
            "f=h5py.File(p[0],'r');"
            "assert len(f['sample_key'])>0;"
            "f.close()"
        )
        return [sys.executable, "-c", code]

    def tearDown(self):
        self.tempdir.cleanup()

    def test_rebuild_is_content_stable_and_validates(self):
        first = artifact.build_artifact(self.contract, self.inventory, self.root / "out-a")
        second = artifact.build_artifact(self.contract, self.inventory, self.root / "out-b")
        self.assertEqual(first["artifact_content_sha256"], second["artifact_content_sha256"])
        result = artifact.validate_artifact(
            first["artifact_root"], self.inventory, self.consumer, probe_argv=self.probe_argv()
        )
        self.assertTrue(result["success"], result)
        ready = Path(result["attestation_root"]) / "READY.json"
        self.assertTrue(ready.is_file())

    def test_consumer_change_reuses_core_but_changes_attestation(self):
        built = artifact.build_artifact(self.contract, self.inventory, self.root / "out")
        first = artifact.validate_artifact(
            built["artifact_root"], self.inventory, self.consumer, probe_argv=self.probe_argv()
        )
        changed_consumer = self.root / "consumer-changed.json"
        payload = json.loads(self.consumer.read_text(encoding="utf-8"))
        payload["loader_bundle_sha256"] = "4" * 64
        write_json(changed_consumer, payload)
        second = artifact.validate_artifact(
            built["artifact_root"], self.inventory, changed_consumer, probe_argv=self.probe_argv()
        )
        self.assertEqual(first["report"]["artifact_content_sha256"], second["report"]["artifact_content_sha256"])
        self.assertNotEqual(first["validation_attestation_id"], second["validation_attestation_id"])

    def test_source_hash_mismatch_fails_before_build(self):
        (self.images / "0.png").write_bytes(b"changed")
        with self.assertRaisesRegex(artifact.ContractError, "source_sha256_mismatch"):
            artifact.build_artifact(self.contract, self.inventory, self.root / "out")

    def test_attestation_requires_exact_consumer_probe(self):
        built = artifact.build_artifact(self.contract, self.inventory, self.root / "out")
        result = artifact.validate_artifact(built["artifact_root"], self.inventory, self.consumer)
        self.assertFalse(result["success"])
        self.assertIn("consumer_probe_required", result["report"]["errors"])

    def test_interrupted_build_resumes_completed_shards(self):
        output = self.root / "out"
        original_write_shard = artifact.write_shard
        call_count = 0

        def interrupt_on_second_shard(path, samples):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("fixture_interrupt")
            return original_write_shard(path, samples)

        with mock.patch.object(artifact, "write_shard", side_effect=interrupt_on_second_shard):
            with self.assertRaisesRegex(RuntimeError, "fixture_interrupt"):
                artifact.build_artifact(self.contract, self.inventory, output)

        states = list((output / ".building").glob("*/BUILD_STATE.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual([item["state"] for item in state["shards"]], ["complete", "pending"])

        resumed = artifact.build_artifact(self.contract, self.inventory, output)
        self.assertEqual(resumed["resumed_shard_count"], 1)
        self.assertFalse(resumed["reused_existing"])
        self.assertFalse(states[0].exists())
        inspected = artifact.inspect_artifact(resumed["artifact_root"], verify_shards=True)
        self.assertTrue(inspected["success"], inspected)


if __name__ == "__main__":
    unittest.main()
