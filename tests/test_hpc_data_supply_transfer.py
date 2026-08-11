import tempfile
import unittest
from pathlib import Path

from hpc_core.data_artifacts import ArtifactRegistry
from hpc_data_supply import make_plan, prepare_intent, record_fake_shard
from tests.data_supply_test_utils import NOW, supply_inputs


class HpcDataSupplyTransferStateTests(unittest.TestCase):
    def test_verified_fixture_shards_resume_without_resetting_prior_state(self):
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
            first = manifest["shards"][0]
            second = manifest["shards"][1]
            record_fake_shard(
                registry,
                intent_id=plan["plan_sha256"],
                shard_path=first["path"],
                observed_sha256=first["sha256"],
                expected_revision=1,
                confirmed=True,
            )
            mid = registry.read()["intents"][plan["plan_sha256"]]
            self.assertEqual(mid["shards"][first["path"]]["state"], "verified")
            final = record_fake_shard(
                registry,
                intent_id=plan["plan_sha256"],
                shard_path=second["path"],
                observed_sha256=second["sha256"],
                expected_revision=2,
                confirmed=True,
            )
            self.assertEqual(final["state"], "fixture_shards_verified")
            self.assertEqual(registry.read()["intents"][plan["plan_sha256"]]["shards"][first["path"]]["state"], "verified")

    def test_wrong_observed_hash_is_rejected(self):
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
            with self.assertRaisesRegex(ValueError, "fixture_shard_sha256_mismatch"):
                record_fake_shard(
                    registry,
                    intent_id=plan["plan_sha256"],
                    shard_path=manifest["shards"][0]["path"],
                    observed_sha256="f" * 64,
                    expected_revision=1,
                    confirmed=True,
                )


if __name__ == "__main__":
    unittest.main()
