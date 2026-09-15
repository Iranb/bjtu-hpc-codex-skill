#!/usr/bin/env python3
"""Synthetic tests only: never access live VPN, Keychain, OTP or Guard state."""
import json
import stat
import subprocess
import tempfile
import unittest
try:
    import fcntl
except ImportError:
    raise unittest.SkipTest("MotionPro macOS budget tests require POSIX flock")
from pathlib import Path
from unittest.mock import patch

import manual_attempt as ma
import preflight as pf


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = pf.SWIFT_EPOCH + 800000000

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value), encoding="utf-8")

    def policy(self):
        return json.loads((self.root / "recovery-policy.json").read_text())

    def call(self, action, **kwargs):
        return ma.operate(action, self.root, self.now, **kwargs)


class PreflightTests(Fixture):
    def test_all_status_codes_and_routes(self):
        for code in range(8):
            with self.subTest(code=code):
                self.assertEqual(pf.parse_status(f"VPN status:{code}\n"), code)
        for code in (2, 6):
            self.assertEqual(pf.decision(code), ("tunnel_connected_check_target", 0))
        self.assertEqual(pf.decision(5), ("inspect_client", 4))
        self.assertEqual(pf.decision(None), ("inspect_client", 4))
        self.assertEqual(pf.decision(0)[1], 2)
        self.assertEqual(pf.decision(7)[1], 2)
        self.assertEqual(pf.decision(1)[1], 3)
        self.assertEqual(pf.decision(3)[1], 3)
        self.assertEqual(pf.decision(4)[1], 3)

    def test_fail_closed_on_unrecognized_or_ambiguous_output(self):
        for text in ("", "status:2", "VPN status:9", "prefix VPN status:2", "VPN status:2\nVPN status:7", "VPN status:2 SECRET"):
            self.assertIsNone(pf.parse_status(text))

    def test_vendor_exit_two_is_success_and_cwd_is_required(self):
        fake = subprocess.CompletedProcess([], 2, b"VPN status:2\n")
        with patch.object(pf.subprocess, "run", return_value=fake) as runner:
            self.assertEqual(pf.query_vendor(self.root)["status_code"], 2)
            self.assertEqual(runner.call_args.kwargs["cwd"], self.root)
            self.assertFalse(runner.call_args.kwargs["check"])
            self.assertEqual(runner.call_args.kwargs["timeout"], 5)

    def test_vendor_timeout_and_missing_fail_closed(self):
        for exc in (subprocess.TimeoutExpired("fake", 5), FileNotFoundError()):
            with patch.object(pf.subprocess, "run", side_effect=exc):
                self.assertIsNone(pf.query_vendor(self.root)["status_code"])

    def test_snapshot_swift_epoch_freshness_and_redaction(self):
        self.write("status.json", {"updatedAt": self.now - pf.SWIFT_EPOCH - 12,
                  "state": "connected", "credentialReady": True, "detail": "SECRET",
                  "password": "SECRET", "otp": "SECRET", "gateway": "SECRET"})
        result = pf.snapshot_summary(self.root / "status.json", self.now)
        self.assertTrue(result["fresh"])
        self.assertEqual(result["age_seconds"], 12)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertFalse(pf.snapshot_summary(self.root / "status.json", self.now + 200)["fresh"])
        self.assertFalse(pf.snapshot_summary(self.root / "status.json", self.now - 30)["fresh"])

    def test_invalid_files_do_not_become_healthy(self):
        for data in ([], {"updatedAt": True}, {"updatedAt": float("nan")}, {"updatedAt": "SECRET"}):
            self.write("status.json", data)
            self.assertFalse(pf.snapshot_summary(self.root / "status.json", self.now)["fresh"])
        self.assertEqual(pf.snapshot_summary(self.root / "missing", self.now)["snapshot"], "missing")

    def test_policy_rollover_uses_swift_epoch(self):
        p = ma.default_policy()
        p["submissions"] = [800000000 - 3601, 800000000 - 2]
        self.write("recovery-policy.json", p)
        self.assertEqual(pf.policy_summary(self.root / "recovery-policy.json", self.now)["attempts_in_rolling_hour"], 1)


class ManualBudgetTests(Fixture):
    def test_status_is_read_only(self):
        self.assertEqual(self.call("status")["attempts_in_rolling_hour"], 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_running_guard_lock_refuses_mutation(self):
        lock = self.root / "guard.lock"
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ma.Refused, "guard_running"):
                self.call("begin")
        self.assertFalse((self.root / "recovery-policy.json").exists())

    def test_enabled_guard_requires_pause_preserving_history(self):
        p = ma.default_policy()
        p.update(enabled=True, submissions=[799999999], successCount=7, custom_field="preserve")
        self.write("recovery-policy.json", p)
        with self.assertRaisesRegex(ma.Refused, "pause_first"):
            self.call("begin")
        self.assertTrue(self.call("pause")["previous_enabled"])
        p["enabled"] = False
        self.assertEqual(self.policy(), p)

    def test_manual_and_automatic_share_two_attempt_budget(self):
        p = ma.default_policy()
        p["submissions"] = [799999900]
        self.write("recovery-policy.json", p)
        result = self.call("begin")
        self.assertEqual(result["attempts_in_rolling_hour"], 2)
        self.call("finish", attempt_id=result["attempt_id"], outcome="success")
        with self.assertRaisesRegex(ma.Refused, "shared_retry"):
            self.call("begin")
        self.assertEqual(len(self.policy()["submissions"]), 2)

    def test_pending_survives_restart_and_prevents_second_begin(self):
        first = self.call("begin")
        self.assertEqual(self.call("status")["pending_attempt_id"], first["attempt_id"])
        with self.assertRaisesRegex(ma.Refused, "unsettled"):
            self.call("begin")

    def test_two_failures_hold_even_after_hour(self):
        for outcome in ("failure", "unknown"):
            attempt = self.call("begin")
            self.call("finish", attempt_id=attempt["attempt_id"], outcome=outcome)
        self.assertTrue(self.policy()["manualHold"])
        self.now += 4000
        with self.assertRaisesRegex(ma.Refused, "shared_retry"):
            self.call("begin")

    def test_success_does_not_increment_auto_recovery_stat(self):
        p = ma.default_policy()
        p.update(consecutiveFailures=1, successCount=3, lastSuccess=799999999)
        self.write("recovery-policy.json", p)
        attempt = self.call("begin")
        self.call("finish", attempt_id=attempt["attempt_id"], outcome="success")
        after = self.policy()
        self.assertEqual(after["consecutiveFailures"], 0)
        self.assertEqual(after["successCount"], 3)
        self.assertEqual(after["lastSuccess"], p["lastSuccess"])
        self.assertFalse(after["enabled"])

    def test_finish_idempotent_and_conflicting_result_refused(self):
        attempt = self.call("begin")
        self.call("finish", attempt_id=attempt["attempt_id"], outcome="failure")
        result = self.call("finish", attempt_id=attempt["attempt_id"], outcome="failure")
        self.assertTrue(result["already_recorded"])
        self.assertEqual(self.policy()["consecutiveFailures"], 1)
        with self.assertRaisesRegex(ma.Refused, "differently"):
            self.call("finish", attempt_id=attempt["attempt_id"], outcome="success")

    def test_interrupted_finish_is_idempotent(self):
        attempt = self.call("begin")
        original_write = ma.atomic_json
        def interrupt(path, value):
            if path.name == "manual-attempt.json":
                raise OSError("synthetic interruption")
            original_write(path, value)
        with patch.object(ma, "atomic_json", side_effect=interrupt):
            with self.assertRaises(OSError):
                self.call("finish", attempt_id=attempt["attempt_id"], outcome="failure")
        self.call("finish", attempt_id=attempt["attempt_id"], outcome="failure")
        self.assertEqual(self.policy()["consecutiveFailures"], 1)

    def test_wrong_attempt_id_refused_without_changes(self):
        self.call("begin")
        before = self.policy()
        with self.assertRaisesRegex(ma.Refused, "mismatch"):
            self.call("finish", attempt_id="wrong", outcome="success")
        self.assertEqual(self.policy(), before)

    def test_corrupt_policy_preserved(self):
        self.write("recovery-policy.json", {"enabled": "false"})
        before = (self.root / "recovery-policy.json").read_bytes()
        with self.assertRaisesRegex(ma.Refused, "invalid_policy"):
            self.call("begin")
        self.assertEqual(before, (self.root / "recovery-policy.json").read_bytes())

    def test_future_timestamps_cannot_bypass_budget(self):
        p = ma.default_policy()
        p["submissions"] = [800001000, 800002000]
        self.write("recovery-policy.json", p)
        with self.assertRaisesRegex(ma.Refused, "shared_retry"):
            self.call("begin")

    def test_deleted_policy_with_receipt_refused(self):
        self.call("begin")
        (self.root / "recovery-policy.json").unlink()
        with self.assertRaisesRegex(ma.Refused, "missing_shared_policy"):
            self.call("status")

    def test_new_policy_and_receipt_permissions(self):
        self.call("begin")
        for name in ("recovery-policy.json", "manual-attempt.json", "guard.lock"):
            self.assertEqual(stat.S_IMODE((self.root / name).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
