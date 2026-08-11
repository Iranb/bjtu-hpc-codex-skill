import unittest
from unittest.mock import patch

import hpc_transfer_web as web


class VisibleRefreshSelectionTests(unittest.TestCase):
    def request(self, payload, selected_rows):
        with (
            patch.object(web, "selected_guardian_accounts", return_value=selected_rows) as selected,
            patch.object(
                web,
                "launch_guardian_visible_refresh",
                side_effect=lambda name, *_args, **_kwargs: {"status": "running", "account": name},
            ) as launch,
            patch.object(web, "guardian_event"),
            patch.object(web, "guardian_snapshot", return_value={}),
        ):
            result = web.request_visible_refresh(payload)
        return result, selected, launch

    def test_plural_accounts_selects_only_requested_account(self):
        result, selected, launch = self.request({"accounts": ["account-a"]}, [{"name": "account-a"}])

        selected.assert_called_once_with(["account-a"])
        launch.assert_called_once()
        self.assertEqual(result["visible_refresh_request"][0]["account"], "account-a")

    def test_legacy_singular_account_remains_single_account(self):
        _result, selected, launch = self.request({"account": "account-a"}, [{"name": "account-a"}])

        selected.assert_called_once_with(["account-a"])
        launch.assert_called_once()

    def test_missing_selection_never_defaults_to_all_accounts(self):
        with patch.object(web, "launch_guardian_visible_refresh") as launch:
            with self.assertRaisesRegex(ValueError, "explicit account selection"):
                web.request_visible_refresh({})

        launch.assert_not_called()

    def test_empty_selection_never_defaults_to_all_accounts(self):
        with patch.object(web, "launch_guardian_visible_refresh") as launch:
            with self.assertRaisesRegex(ValueError, "selection is empty"):
                web.request_visible_refresh({"accounts": []})

        launch.assert_not_called()

    def test_all_accounts_requires_explicit_all_value(self):
        _result, selected, launch = self.request(
            {"accounts": "all"},
            [{"name": "first"}, {"name": "second"}],
        )

        selected.assert_called_once_with([])
        self.assertEqual(launch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
