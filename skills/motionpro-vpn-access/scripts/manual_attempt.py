#!/usr/bin/env python3
"""Coordinate GPT manual attempts with Guard's budget; never perform login."""
import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from preflight import SUPPORT, SWIFT_EPOCH, number, policy_valid, read_object


class Refused(Exception):
    pass


def default_policy():
    return {"enabled": False, "submissions": [], "consecutiveFailures": 0,
            "successCount": 0, "manualHold": False}


def get_policy(root):
    value, status = read_object(root / "recovery-policy.json")
    if status == "missing":
        return default_policy()
    if not policy_valid(value):
        raise Refused("invalid_policy_preserved")
    return value


def get_record(root):
    value, status = read_object(root / "manual-attempt.json")
    if status == "missing":
        return None
    if (not isinstance(value, dict) or not isinstance(value.get("attempt_id"), str)
            or not number(value.get("submitted_at")) or value["submitted_at"] < 0
            or value.get("outcome") not in ("pending", "success", "failure", "unknown")):
        raise Refused("invalid_manual_record_preserved")
    try:
        uuid.UUID(value["attempt_id"])
    except ValueError:
        raise Refused("invalid_manual_record_preserved")
    return value


def atomic_json(path, value):
    fd, tmp = tempfile.mkstemp(prefix=".motionpro-access-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def guard_stopped_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / "guard.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("guard_running_pause_and_quit_first")
        yield
    finally:
        os.close(fd)


def summary(policy, record, now):
    recent = [t for t in policy["submissions"] if now - SWIFT_EPOCH - t < 3600]
    return {"enabled": policy["enabled"], "attempts_in_rolling_hour": len(recent),
            "consecutive_failures": policy["consecutiveFailures"],
            "manual_hold": policy["manualHold"],
            "pending_manual_attempt": bool(record and record["outcome"] == "pending"),
            "pending_attempt_id": record["attempt_id"] if record and record["outcome"] == "pending" else None}


def operate(action, root=SUPPORT, now=None, attempt_id=None, outcome=None):
    now = time.time() if now is None else now
    if (root / "manual-attempt.json").exists() and not (root / "recovery-policy.json").exists():
        raise Refused("missing_shared_policy_with_existing_manual_history")
    if action == "status":
        return summary(get_policy(root), get_record(root), now)
    with guard_stopped_lock(root):
        policy, record = get_policy(root), get_record(root)
        policy_path = root / "recovery-policy.json"
        record_path = root / "manual-attempt.json"
        if action == "pause":
            previous = policy["enabled"]
            policy["enabled"] = False
            atomic_json(policy_path, policy)
            return {"paused": True, "previous_enabled": previous}
        if policy["enabled"]:
            raise Refused("policy_enabled_pause_first")
        if action == "begin":
            if record and record["outcome"] == "pending":
                raise Refused("unsettled_manual_attempt")
            state = summary(policy, record, now)
            if (policy["manualHold"] or policy["consecutiveFailures"] >= 2
                    or state["attempts_in_rolling_hour"] >= 2):
                raise Refused("shared_retry_budget_or_failure_hold")
            # Keep all prior timestamps; never reset history to obtain another attempt.
            timestamp = now - SWIFT_EPOCH
            policy["submissions"].append(timestamp)
            record = {"attempt_id": str(uuid.uuid4()), "submitted_at": timestamp,
                      "outcome": "pending"}
            # If interrupted between writes, the budget remains charged conservatively.
            atomic_json(policy_path, policy)
            atomic_json(record_path, record)
            return {"reserved": True, "attempt_id": record["attempt_id"],
                    **summary(policy, record, now)}
        if action != "finish" or outcome not in ("success", "failure", "unknown"):
            raise Refused("invalid_operation")
        if not record or record["attempt_id"] != attempt_id:
            raise Refused("attempt_id_mismatch")
        if record["outcome"] != "pending":
            if record["outcome"] != outcome:
                raise Refused("result_already_recorded_differently")
            return {"recorded": True, "already_recorded": True, **summary(policy, record, now)}
        # Marker makes interrupted finish idempotent before the receipt is written.
        if policy.get("motionproAccessResultId") != attempt_id:
            if outcome == "success":
                policy["consecutiveFailures"] = 0
            else:
                policy["consecutiveFailures"] += 1
                if policy["consecutiveFailures"] >= 2:
                    policy["manualHold"] = True
            policy["motionproAccessResultId"] = attempt_id
            policy["motionproAccessResultOutcome"] = outcome
            atomic_json(policy_path, policy)
        elif policy.get("motionproAccessResultOutcome") != outcome:
            raise Refused("interrupted_result_conflict")
        record["outcome"] = outcome
        atomic_json(record_path, record)
        return {"recorded": True, "outcome": outcome, **summary(policy, record, now)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pause", "begin", "finish"))
    parser.add_argument("--attempt-id")
    parser.add_argument("--outcome", choices=("success", "failure", "unknown"))
    args = parser.parse_args()
    if args.action == "finish" and (not args.attempt_id or not args.outcome):
        parser.error("finish requires --attempt-id and --outcome")
    if args.action != "finish" and (args.attempt_id or args.outcome):
        parser.error("result arguments are only valid with finish")
    try:
        result = operate(args.action, attempt_id=args.attempt_id, outcome=args.outcome)
    except Refused as error:
        result = {"refused": str(error)}
    except (OSError, ValueError, UnicodeError):
        result = {"refused": "local_state_write_or_read_failed"}
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 2 if "refused" in result else 0


if __name__ == "__main__":
    sys.exit(main())
