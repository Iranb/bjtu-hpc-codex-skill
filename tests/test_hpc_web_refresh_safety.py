import unittest
from pathlib import Path
from unittest.mock import patch

import hpc_transfer_web as web
from hpc_account_store import AccountStoreError


class WebRefreshSafetyTests(unittest.TestCase):
    def setUp(self):
        self.config = web.WebConfig(
            config=Path("/tmp/tasks.json"),
            token_file=Path("/tmp/legacy-token"),
            auto_refresh_token=True,
            refresh_browser="playwright",
            refresh_headless=False,
        )

    def test_refresh_requires_explicit_saved_account(self):
        with self.assertRaisesRegex(ValueError, "explicit saved account"):
            web.refresh_token(self.config, {})

    def test_visible_refresh_uses_account_aware_command_and_clears_auth_session(self):
        rows = [
            {"name": "main", "default": True},
            {"name": "other", "default": False},
        ]
        with (
            patch.object(web, "list_account_summaries", return_value=rows),
            patch.object(web, "run_background") as run,
            patch.object(web, "action_snapshot", return_value=None),
        ):
            web.refresh_token(
                self.config,
                {"account": "other", "browser": "playwright", "timeout": 180},
            )

        command = run.call_args.args[1]
        self.assertIn("hpc_accounts.py", " ".join(command))
        self.assertNotIn("hpc_refresh_token.py", " ".join(command))
        self.assertIn("other", command)
        self.assertIn("--clear-existing-token", command)
        self.assertIn("--clear-auth-session", command)
        self.assertNotIn("--sync-legacy-token", command)

    def test_default_account_refresh_may_sync_legacy(self):
        rows = [{"name": "main", "default": True}]
        with (
            patch.object(web, "list_account_summaries", return_value=rows),
            patch.object(web, "run_background") as run,
            patch.object(web, "action_snapshot", return_value=None),
        ):
            web.refresh_token(self.config, {"account": "main", "headless": True})
        self.assertIn("--sync-legacy-token", run.call_args.args[1])

    def test_manual_save_checks_account_identity_before_legacy_write(self):
        rows = [{"name": "other", "default": False}]
        with (
            patch.object(web, "list_account_summaries", return_value=rows),
            patch.object(web, "validate_token", return_value={"code": 11000}),
            patch.object(web, "sync_auth_account_token", side_effect=ValueError("identity mismatch")),
            patch.object(web, "write_token") as write,
        ):
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                web.save_token(
                    self.config,
                    {"account": "other", "token": "wrong-token", "validate": True},
                )
        write.assert_not_called()

    def test_legacy_status_rejects_token_that_does_not_match_default_identity(self):
        entry = {"portal_user": "00000001", "cluster": "cluster2", "account": "u00000001"}
        with (
            patch.object(web, "read_saved_token", return_value="wrong-token"),
            patch.object(web, "validate_token", return_value={"code": 11000}),
            patch.object(web, "list_account_summaries", return_value=[{"name": "main", "default": True}]),
            patch.object(web, "get_account", return_value=("main", entry)),
            patch.object(
                web,
                "verify_token_identity",
                side_effect=AccountStoreError("identity mismatch"),
            ),
        ):
            status = web.token_status(self.config, validate=True)
        self.assertFalse(status["validation"]["ok"])
        self.assertIn("identity mismatch", status["validation"]["error"])

    def test_guardian_validation_rejects_alias_identity_mismatch(self):
        entry = {"portal_user": "00000002", "cluster": "cluster2", "account": "u00000002"}
        with (
            patch.object(web, "validate_token", return_value={"code": 11000}),
            patch.object(web, "get_account", return_value=("other", entry)),
            patch.object(
                web,
                "verify_token_identity",
                side_effect=AccountStoreError("identity mismatch"),
            ),
        ):
            status = web.guardian_validation_summary("other", "wrong-token")
        self.assertFalse(status["ok"])
        self.assertIn("identity mismatch", status["message"])


if __name__ == "__main__":
    unittest.main()
