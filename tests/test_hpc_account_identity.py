import unittest
from unittest.mock import patch

from hpc_account_store import AccountStoreError
import hpc_token_identity as identity


class TokenIdentityTests(unittest.TestCase):
    def test_matching_saved_identity_is_accepted(self):
        entry = {
            "portal_user": "portal-other",
            "cluster": "cluster2",
            "account": "os-other",
        }
        rows = [{"clusterId": "cluster2", "accountName": "os-other"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "portal-other"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            actual = identity.verify_token_identity("other", "token", entry)

        self.assertEqual(
            actual,
            {
                "portal_user": "portal-other",
                "cluster": "cluster2",
                "account": "os-other",
            },
        )

    def test_wrong_portal_user_is_rejected(self):
        entry = {
            "portal_user": "portal-other",
            "cluster": "cluster2",
            "account": "os-other",
        }
        rows = [{"clusterId": "cluster2", "accountName": "os-main"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "portal-main"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            with self.assertRaisesRegex(AccountStoreError, "expected portal_user=portal-other.*actual=portal-main"):
                identity.verify_token_identity("other", "token", entry)

    def test_preferred_account_mismatch_does_not_fall_back_to_only_row(self):
        entry = {
            "portal_user": "portal-other",
            "cluster": "cluster2",
            "account": "os-other",
        }
        rows = [{"clusterId": "cluster2", "accountName": "os-main"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "portal-other"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            with self.assertRaisesRegex(AccountStoreError, "expected cluster2:os-other"):
                identity.verify_token_identity("other", "token", entry)

    def test_new_account_can_discover_single_bound_account(self):
        rows = [{"clusterId": "cluster2", "accountName": "os-new"}]
        with (
            patch.object(identity, "create_session", return_value=object()),
            patch.object(identity, "get_portal_self", return_value={"userName": "portal-new"}),
            patch.object(identity, "get_bound_accounts", return_value=rows),
        ):
            actual = identity.verify_token_identity("new", "token", {"cluster": "cluster2"})

        self.assertEqual(actual["portal_user"], "portal-new")
        self.assertEqual(actual["account"], "os-new")


if __name__ == "__main__":
    unittest.main()
