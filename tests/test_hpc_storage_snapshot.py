import unittest
from datetime import datetime, timezone

from hpc_storage_snapshot import normalize_snapshot


class HpcStorageSnapshotTests(unittest.TestCase):
    def test_stale_native_snapshot_is_rejected(self):
        payload = {
            "schema": "autoreskill.bjtu_native_quota_snapshot.v1",
            "checked_at": "2026-07-18T03:00:00Z",
            "provider": {"name": "fixture", "bundle_sha256": "1" * 64},
            "accounts": [
                {
                    "alias": "main",
                    "portal_user": "100",
                    "cluster": "cluster2",
                    "account": "u100",
                    "quota_bytes": 1000,
                    "used_bytes": 100,
                    "artifact_root": "/gpfs/a",
                    "acl_capabilities": {},
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "native_quota_snapshot_stale"):
            normalize_snapshot(
                payload,
                max_age_seconds=60,
                now=datetime(2026, 7, 18, 4, 0, tzinfo=timezone.utc),
            )


if __name__ == "__main__":
    unittest.main()
