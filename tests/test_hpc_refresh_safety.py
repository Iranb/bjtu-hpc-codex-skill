import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from hpc_account_store import AccountStoreError
import hpc_accounts
import hpc_core.auth as hpc_core_auth
import hpc_refresh_flow
import hpc_refresh_token


def refresh_args(**overrides):
    values = {
        "manual": False,
        "browser": "playwright",
        "profile_dir": None,
        "headless": True,
        "timeout": 30,
        "login_name": None,
        "login_password_env": "HPC_LOGIN_PASSWORD",
        "fresh_page": True,
        "clear_existing_token": False,
        "clear_auth_session": False,
        "no_open": True,
        "skip_wait": True,
        "no_validate": False,
        "discover_account": True,
        "sync_legacy_token": False,
        "cluster": None,
        "account": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class AccountRefreshSafetyTests(unittest.TestCase):
    def test_identity_mismatch_never_writes_token_or_metadata(self):
        entry = {"portal_user": "00000002", "cluster": "cluster2", "account": "u00000002"}
        args = refresh_args()
        with (
            patch.object(hpc_accounts, "get_account", return_value=("other", entry)),
            patch.object(hpc_accounts, "fetch_token", return_value="wrong-token"),
            patch.object(hpc_accounts, "validate_token", return_value={"code": 11000}),
            patch.object(
                hpc_accounts,
                "verify_token_identity",
                side_effect=AccountStoreError("identity mismatch"),
            ),
            patch.object(hpc_accounts, "save_account_token") as save,
            patch.object(hpc_accounts, "upsert_account") as upsert,
            patch.object(hpc_accounts, "sync_legacy_token") as sync,
        ):
            with self.assertRaisesRegex(AccountStoreError, "identity mismatch"):
                hpc_accounts.refresh_one("other", args)

        save.assert_not_called()
        upsert.assert_not_called()
        sync.assert_not_called()

    def test_existing_metadata_is_not_overwritten_after_match(self):
        entry = {"portal_user": "00000002", "cluster": "cluster2", "account": "u00000002"}
        args = refresh_args()
        with (
            patch.object(hpc_accounts, "get_account", return_value=("other", entry)),
            patch.object(hpc_accounts, "fetch_token", return_value="token"),
            patch.object(hpc_accounts, "validate_token", return_value={"code": 11000}),
            patch.object(hpc_accounts, "verify_token_identity", return_value=dict(entry)),
            patch.object(hpc_accounts, "save_account_token", return_value=dict(entry)) as save,
            patch.object(hpc_accounts, "upsert_account") as upsert,
            patch.object(hpc_accounts, "load_store", return_value={"default": "main"}),
        ):
            hpc_accounts.refresh_one("other", args)

        save.assert_called_once()
        upsert.assert_not_called()

    def test_validate_marks_identity_mismatch_invalid(self):
        entry = {
            "token": "wrong-token",
            "portal_user": "00000002",
            "cluster": "cluster2",
            "account": "u00000002",
        }
        args = SimpleNamespace(all=False, name="other", json=True)
        with (
            patch.object(hpc_accounts, "get_account", return_value=("other", entry)),
            patch.object(hpc_accounts, "validate_token", return_value={"code": 11000}),
            patch.object(
                hpc_accounts,
                "verify_token_identity",
                side_effect=AccountStoreError("identity mismatch"),
            ),
            patch("builtins.print"),
        ):
            self.assertEqual(hpc_accounts.handle_validate(args), 1)


class DirectRefreshSafetyTests(unittest.TestCase):
    def test_named_account_uses_saved_credentials_not_global_main_default(self):
        entry = {"portal_user": "00000003", "profile_dir": "/tmp/example-profile"}
        with (
            patch.object(hpc_refresh_token, "resolve_sync_account_name", return_value="account-a"),
            patch.object(hpc_refresh_token, "get_account", return_value=("account-a", entry)),
            patch.object(
                hpc_refresh_token,
                "credential_for_account",
                return_value={"login_name": "00000003", "login_password": "saved-password"},
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            settings = hpc_refresh_token.playwright_account_settings(
                "account-a", None, "HPC_LOGIN_PASSWORD", None
            )

        self.assertEqual(settings["login_name"], "00000003")
        self.assertEqual(settings["login_password"], "saved-password")
        self.assertEqual(settings["profile_dir"], Path("/tmp/example-profile"))

    def test_sync_rejects_identity_before_saving(self):
        entry = {"portal_user": "00000002", "cluster": "cluster2", "account": "u00000002"}
        with (
            patch.object(hpc_refresh_token, "resolve_sync_account_name", return_value="other"),
            patch.object(hpc_refresh_token, "get_account", return_value=("other", entry)),
            patch.object(
                hpc_refresh_token,
                "verify_token_identity",
                side_effect=AccountStoreError("identity mismatch"),
            ),
            patch.object(hpc_refresh_token, "save_account_token") as save,
        ):
            with self.assertRaisesRegex(AccountStoreError, "identity mismatch"):
                hpc_refresh_token.sync_auth_account_token(
                    "wrong-token", {"code": 11000}, "other", strict=True
                )
        save.assert_not_called()

    def test_implicit_default_account_mismatch_is_not_swallowed(self):
        entry = {"portal_user": "00000001", "cluster": "cluster2", "account": "u00000001"}
        with (
            patch.object(hpc_refresh_token, "resolve_sync_account_name", return_value="main"),
            patch.object(hpc_refresh_token, "get_account", return_value=("main", entry)),
            patch.object(
                hpc_refresh_token,
                "verify_token_identity",
                side_effect=AccountStoreError("identity mismatch"),
            ),
            patch.object(hpc_refresh_token, "save_account_token") as save,
        ):
            with self.assertRaisesRegex(AccountStoreError, "identity mismatch"):
                hpc_refresh_token.sync_auth_account_token("wrong-token", {"code": 11000})
        save.assert_not_called()

    def test_secondary_account_does_not_implicitly_write_default_legacy_file(self):
        with (
            patch.object(hpc_refresh_token, "resolve_sync_account_name", return_value="other"),
            patch.object(hpc_refresh_token, "load_store", return_value={"default": "main"}),
        ):
            self.assertFalse(
                hpc_refresh_token.should_write_token_file(
                    "other", hpc_refresh_token.DEFAULT_TOKEN_FILE, explicit_sync=False
                )
            )

    def test_auth_session_clear_removes_profile_cookies(self):
        context = Mock()
        hpc_refresh_token.clear_playwright_auth_session(context)
        context.clear_cookies.assert_called_once_with()


class RefreshFlowSafetyTests(unittest.TestCase):
    def test_refresh_command_never_unconditionally_syncs_legacy(self):
        with patch.object(hpc_refresh_flow, "run_step", return_value=0) as run:
            hpc_refresh_flow.refresh_account(
                "other",
                headless=False,
                timeout=60,
                fresh_page=True,
                clear_existing_token=True,
                clear_auth_session=True,
            )
        command = run.call_args.args[1]
        self.assertNotIn("--sync-legacy-token", command)
        self.assertIn("--clear-existing-token", command)
        self.assertIn("--clear-auth-session", command)

    def test_only_default_account_is_synced_to_legacy(self):
        with (
            patch.object(hpc_refresh_flow, "load_store", return_value={"default": "main"}),
            patch.object(hpc_refresh_flow, "sync_legacy_token") as sync,
        ):
            self.assertFalse(hpc_refresh_flow.sync_legacy_if_default("other"))
            self.assertTrue(hpc_refresh_flow.sync_legacy_if_default("main"))
        sync.assert_called_once_with("main")


class SharedAuthHelperSafetyTests(unittest.TestCase):
    def test_named_refresh_does_not_sync_global_legacy_token(self):
        with (
            patch.object(hpc_core_auth.subprocess, "run") as run,
            patch.object(hpc_core_auth, "load_token", return_value="token"),
        ):
            hpc_core_auth.refresh_portal_token(
                Path("/tmp/legacy-token"), auth_account="other"
            )
        self.assertNotIn("--sync-legacy-token", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
