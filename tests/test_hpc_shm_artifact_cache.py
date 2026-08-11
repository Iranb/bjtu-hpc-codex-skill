import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import external_hdf5_artifact as artifact
from hpc_core.data_artifacts import sha256_file


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "bjtu-hpc"
    / "scripts"
    / "hpc_shm_cache.sh"
)


class HpcShmArtifactCacheTests(unittest.TestCase):
    def build_fixture(self, root: Path) -> tuple[dict, dict]:
        source = root / "image.png"
        Image.new("RGB", (2, 2), (1, 2, 3)).save(source)
        inventory = root / "inventory.json"
        inventory.write_text(
            json.dumps(
                {
                    "schema": artifact.SOURCE_INVENTORY_SCHEMA,
                    "samples": [
                        {
                            "source_path": str(source),
                            "relative_path": "image.png",
                            "source_sha256": sha256_file(source),
                            "sample_key": "train:0",
                            "split": "train",
                            "data_role": "train_labeled",
                            "target": 0,
                            "semantic_class_id": 0,
                            "class_name": "zero",
                            "class_order_rank": 0,
                            "uq_idx": 0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        contract = root / "contract.json"
        contract.write_text(
            json.dumps(
                {
                    "schema": artifact.BUILD_CONTRACT_SCHEMA,
                    "artifact_schema": artifact.ARTIFACT_SCHEMA,
                    "dataset_semantics": {"dataset_id": "cache-fixture"},
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
                    "probe_required": True,
                    "probe_timeout_seconds": 30,
                }
            ),
            encoding="utf-8",
        )
        built = artifact.build_artifact(contract, inventory, root / "artifacts")
        probe = [
            sys.executable,
            "-c",
            (
                "import os,pathlib,h5py;"
                "r=pathlib.Path(os.environ['AUTORESEARCH_DATA_ARTIFACT_ROOT']);"
                "p=next((r/'shards').glob('*.h5'));"
                "f=h5py.File(p,'r');assert len(f['sample_key'])==1;f.close()"
            ),
        ]
        validated = artifact.validate_artifact(
            built["artifact_root"], inventory, consumer, probe_argv=probe
        )
        return built, validated

    def run_stage(self, root: Path, built: dict, validated: dict, *, available: int) -> subprocess.CompletedProcess[str]:
        fake_bin = root / "bin"
        fake_bin.mkdir(exist_ok=True)
        flock = fake_bin / "flock"
        flock.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        flock.chmod(0o755)
        ready = Path(validated["attestation_root"]) / "READY.json"
        command = (
            f'source "{SCRIPT}"\n'
            f'bjtu_stage_hdf5_artifact_to_shm "{built["artifact_root"]}" '
            f'"{built["artifact_content_sha256"]}" "{validated["consumer_contract_sha256"]}" '
            f'"{ready}" TEST_ARTIFACT_ROOT\n'
            'printf "RESULT=%s\\n" "$TEST_ARTIFACT_ROOT"\n'
        )
        environment = dict(os.environ)
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "SHM_CACHE_ROOT": str(root / "cache"),
                "BJTU_SHM_TEST_MODE": "1",
                "BJTU_SHM_TEST_TOTAL_BYTES": str(10**9),
                "BJTU_SHM_TEST_AVAILABLE_BYTES": str(available),
                "MIN_SHM_FREE_BYTES": "0",
                "MAX_SHM_STAGE_PCT": "100",
            }
        )
        return subprocess.run(["bash", "-c", command], text=True, capture_output=True, env=environment, check=False)

    def test_exact_artifact_is_cached_and_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built, validated = self.build_fixture(root)
            first = self.run_stage(root, built, validated, available=10**9)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            ready = Path(validated["attestation_root"]) / "READY.json"
            destination = root / "cache" / built["artifact_content_sha256"] / sha256_file(ready)
            self.assertTrue((destination / ".ready").is_file())
            second = self.run_stage(root, built, validated, available=10**9)
            self.assertIn("shm_stage=reused", second.stdout)

    def test_capacity_falls_back_only_to_verified_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built, validated = self.build_fixture(root)
            result = self.run_stage(root, built, validated, available=0)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("reason=capacity", result.stdout)
            self.assertIn(f"RESULT={built['artifact_root']}", result.stdout)

    def test_corrupted_ready_cache_is_never_reused_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built, validated = self.build_fixture(root)
            first = self.run_stage(root, built, validated, available=10**9)
            self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
            ready = Path(validated["attestation_root"]) / "READY.json"
            destination = root / "cache" / built["artifact_content_sha256"] / sha256_file(ready)
            shard = next((destination / "shards").glob("*.h5"))
            shard.write_bytes(shard.read_bytes() + b"corrupt")
            second = self.run_stage(root, built, validated, available=10**9)
            self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
            self.assertNotIn("shm_stage=reused", second.stdout)
            self.assertIn("existing_unverified_cache_not_overwritten", second.stdout)
            self.assertIn(f"RESULT={built['artifact_root']}", second.stdout)

    def test_cache_copies_only_manifest_declared_members(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            built, validated = self.build_fixture(root)
            private = Path(built["artifact_root"]) / "private-source-paths.json"
            private.write_text('{"source_path":"/private/factory/path"}', encoding="utf-8")
            result = self.run_stage(root, built, validated, available=10**9)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            ready = Path(validated["attestation_root"]) / "READY.json"
            destination = root / "cache" / built["artifact_content_sha256"] / sha256_file(ready)
            self.assertTrue((destination / ".ready").is_file())
            self.assertFalse((destination / private.name).exists())


if __name__ == "__main__":
    unittest.main()
