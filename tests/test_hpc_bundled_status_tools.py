from __future__ import annotations

import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import hpc_doctor
import hpc_queue_summary
import hpc_refresh_flow


class BundledStatusToolTests(unittest.TestCase):
    def test_queue_defaults_match_direct_start_policy(self):
        with patch.object(sys, "argv", ["hpc_queue_summary.py"]):
            args = hpc_queue_summary.parse_args()
        self.assertEqual(args.cap, 2)
        self.assertEqual(args.run_slots, 2)

    def test_queue_shared_limit_annotation_is_account_scoped(self):
        rows = [
            {
                "name": "alias-a",
                "cluster": "cluster2",
                "account": "same-os-user",
                "summary": {"pending_reasons": {"QOSMaxJobsPerUserLimit": 1}},
            },
            {
                "name": "alias-b",
                "cluster": "cluster2",
                "account": "same-os-user",
                "summary": {"pending_reasons": {}},
            },
            {
                "name": "independent",
                "cluster": "cluster2",
                "account": "other-os-user",
                "summary": {"pending_reasons": {"Priority": 1}},
            },
        ]
        hpc_queue_summary.annotate_shared_limits(rows)
        self.assertTrue(rows[0]["summary"]["shared_limit_blocked"])
        self.assertEqual(
            rows[0]["summary"]["shared_limit_ref"],
            rows[1]["summary"]["shared_limit_ref"],
        )
        self.assertNotEqual(
            rows[0]["summary"]["shared_limit_ref"],
            rows[2]["summary"]["shared_limit_ref"],
        )
        self.assertFalse(rows[2]["summary"]["shared_limit_blocked"])

    def test_refresh_verification_binds_selected_auth_account(self):
        with patch.object(hpc_refresh_flow, "run_step", return_value=0) as run_step:
            hpc_refresh_flow.verify_real_portal_call(True, "alias-a")
        command = run_step.call_args.args[1]
        self.assertIn("--auth-account", command)
        self.assertEqual(command[command.index("--auth-account") + 1], "alias-a")

    def test_post_status_binds_selected_auth_account(self):
        args = Namespace(
            after_jobs_keyword="trace",
            after_jobs_size=5,
            after_jobs_paths=False,
            after_snapshot_dir=None,
            after_pending_job=["123"],
            after_pending_no_sinfo=True,
        )
        with patch.object(hpc_refresh_flow, "run_step", return_value=0) as run_step:
            hpc_refresh_flow.run_post_status(args, "alias-a")
        for call in run_step.call_args_list:
            command = call.args[1]
            self.assertIn("--auth-account", command)
            self.assertEqual(command[command.index("--auth-account") + 1], "alias-a")

    def test_doctor_required_bundle_is_complete(self):
        root = Path(hpc_doctor.__file__).resolve().parent
        missing = [name for name in hpc_doctor.REQUIRED_SCRIPTS if not (root / name).is_file()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
