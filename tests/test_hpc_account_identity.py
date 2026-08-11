import unittest
from unittest.mock import patch

from hpc_account_store import AccountStoreError
import hpc_token_identity as identity


class TokenIdentityTests(unittest.TestCase):
    def test_matching_saved_identity_is_accepted(self):
        entry = {
            "portal_user": "00000002",
            "cluster": "cluster2",
            "account": "u00000002",
        }
        rows = [{"clusterId": "cluster2", "accountName": "u00000002"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "00000002"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            actual = identity.verify_token_identity("other", "token", entry)

        self.assertEqual(
            actual,
            {
                "portal_user": "00000002",
                "cluster": "cluster2",
                "account": "u00000002",
            },
        )

    def test_wrong_portal_user_is_rejected(self):
        entry = {
            "portal_user": "00000002",
            "cluster": "cluster2",
            "account": "u00000002",
        }
        rows = [{"clusterId": "cluster2", "accountName": "u00000001"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "00000001"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            with self.assertRaisesRegex(AccountStoreError, "expected portal_user=00000002.*actual=00000001"):
                identity.verify_token_identity("other", "token", entry)

    def test_preferred_account_mismatch_does_not_fall_back_to_only_row(self):
        entry = {
            "portal_user": "00000002",
            "cluster": "cluster2",
            "account": "u00000002",
        }
        rows = [{"clusterId": "cluster2", "accountName": "u00000001"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "00000002"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            with self.assertRaisesRegex(AccountStoreError, "expected cluster2:u00000002"):
                identity.verify_token_identity("other", "token", entry)

    def test_new_account_can_discover_single_bound_account(self):
        rows = [{"clusterId": "cluster2", "accountName": "u00000003"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "00000003"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            actual = identity.verify_token_identity("new", "token", {"cluster": "cluster2"})

        self.assertEqual(actual["portal_user"], "00000003")
        self.assertEqual(actual["account"], "u00000003")


if __name__ == "__main__":
    unittest.main()
