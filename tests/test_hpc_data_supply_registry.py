import tempfile
import unittest
from pathlib import Path

from hpc_core.data_artifacts import ArtifactRegistry, RegistryConflict
from hpc_data_supply import make_plan, prepare_intent
from tests.data_supply_test_utils import NOW, supply_inputs


class HpcDataSupplyRegistryTests(unittest.TestCase):
    def test_plan_selects_acl_capable_owner_and_cas_protects_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path, ready_path, snapshot_path, _, _ = supply_inputs(root)
            plan = make_plan(
                manifest_path=manifest_path,
                ready_path=ready_path,
                snapshot_path=snapshot_path,
                max_snapshot_age_seconds=300,
                share_accounts=["main", "other"],
                reserve_bytes=500,
                reserve_fraction=0.1,
                now=NOW,
            )
            self.assertEqual(plan["storage_owner"]["alias"], "main")
            self.assertFalse(plan["remote_mutation_authorized"])
            registry = ArtifactRegistry(root / "registry.json")
            prepared = prepare_intent(registry, plan=plan, expected_revision=0, confirmed=True)
            self.assertEqual(prepared["registry_revision"], 1)
            with self.assertRaises(RegistryConflict):
                prepare_intent(registry, plan=plan, expected_revision=0, confirmed=True)

    def test_prepare_requires_explicit_local_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path, ready_path, snapshot_path, _, _ = supply_inputs(root)
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
            with self.assertRaisesRegex(ValueError, "confirm_local_intent_required"):
                prepare_intent(ArtifactRegistry(root / "registry.json"), plan=plan, expected_revision=0, confirmed=False)


if __name__ == "__main__":
    unittest.main()
