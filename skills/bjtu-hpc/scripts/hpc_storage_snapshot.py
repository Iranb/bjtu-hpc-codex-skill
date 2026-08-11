#!/usr/bin/env python3
"""Normalize native BJTU quota evidence without inventing storage facts.

The local implementation intentionally does not use ``df`` as a quota source.
A live provider must be configured separately and produce the exact native
schema accepted here.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpc_core.data_artifacts import ContractError, load_json, require_hash


SNAPSHOT_SCHEMA = "autoreskill.bjtu_native_quota_snapshot.v1"


def parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{field}:expected_rfc3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field}:invalid_rfc3339") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field}:timezone_required")
    return parsed.astimezone(timezone.utc)


def normalize_snapshot(
    payload: dict[str, Any],
    *,
    max_age_seconds: int,
    now: datetime | None = None,
    account_filter: set[str] | None = None,
) -> dict[str, Any]:
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ContractError("native_quota_snapshot_schema_mismatch")
    if max_age_seconds < 1:
        raise ContractError("max_age_seconds_must_be_positive")
    checked_at = parse_time(payload.get("checked_at"), "checked_at")
    current = now or datetime.now(timezone.utc)
    age_seconds = (current - checked_at).total_seconds()
    if age_seconds < -60:
        raise ContractError("native_quota_snapshot_from_future")
    if age_seconds > max_age_seconds:
        raise ContractError(f"native_quota_snapshot_stale:age_seconds={int(age_seconds)}")
    provider = payload.get("provider")
    if not isinstance(provider, dict):
        raise ContractError("native_quota_provider_missing")
    provider_bundle_sha256 = require_hash(provider.get("bundle_sha256"), "provider.bundle_sha256")
    provider_name = provider.get("name")
    if not isinstance(provider_name, str) or not provider_name:
        raise ContractError("native_quota_provider_name_missing")
    accounts = payload.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise ContractError("native_quota_accounts_missing")
    normalized: list[dict[str, Any]] = []
    seen_aliases: set[str] = set()
    for row in accounts:
        if not isinstance(row, dict):
            raise ContractError("native_quota_account_not_object")
        alias = row.get("alias")
        if not isinstance(alias, str) or not alias or alias in seen_aliases:
            raise ContractError(f"native_quota_account_alias_invalid:{alias}")
        seen_aliases.add(alias)
        if account_filter and alias not in account_filter:
            continue
        quota = row.get("quota_bytes")
        used = row.get("used_bytes")
        if not isinstance(quota, int) or quota <= 0:
            raise ContractError(f"native_quota_bytes_invalid:{alias}")
        if not isinstance(used, int) or used < 0 or used > quota:
            raise ContractError(f"native_used_bytes_invalid:{alias}")
        account = row.get("account")
        cluster = row.get("cluster")
        portal_user = row.get("portal_user")
        if not all(isinstance(value, str) and value for value in (account, cluster, portal_user)):
            raise ContractError(f"native_account_identity_incomplete:{alias}")
        artifact_root = row.get("artifact_root")
        if not isinstance(artifact_root, str) or not artifact_root.startswith("/"):
            raise ContractError(f"native_artifact_root_invalid:{alias}")
        acl = row.get("acl_capabilities", {})
        if not isinstance(acl, dict):
            raise ContractError(f"native_acl_capabilities_invalid:{alias}")
        normalized.append(
            {
                "alias": alias,
                "portal_user": portal_user,
                "cluster": cluster,
                "account": account,
                "quota_bytes": quota,
                "used_bytes": used,
                "free_bytes": quota - used,
                "artifact_root": artifact_root,
                "acl_capabilities": {
                    "setfacl": acl.get("setfacl") is True,
                    "read_only_share": acl.get("read_only_share") is True,
                },
            }
        )
    if account_filter:
        missing = sorted(account_filter - {item["alias"] for item in normalized})
        if missing:
            raise ContractError(f"native_quota_accounts_not_found:{','.join(missing)}")
    normalized.sort(key=lambda item: item["alias"])
    return {
        "success": True,
        "schema": SNAPSHOT_SCHEMA,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "age_seconds": max(0, int(age_seconds)),
        "max_age_seconds": max_age_seconds,
        "provider": {"name": provider_name, "bundle_sha256": provider_bundle_sha256},
        "accounts": normalized,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    normalize = sub.add_parser("normalize")
    normalize.add_argument("--input", required=True)
    normalize.add_argument("--max-age-seconds", type=int, default=300)
    normalize.add_argument("--accounts")
    normalize.add_argument("--now", help="RFC3339 override for deterministic tests")
    normalize.add_argument("--json", action="store_true")
    live = sub.add_parser("live")
    live.add_argument("--provider")
    live.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "live":
            if not args.provider:
                raise ContractError("native_quota_provider_required")
            raise ContractError("native_quota_provider_not_enabled")
        accounts = {item.strip() for item in args.accounts.split(",") if item.strip()} if args.accounts else None
        now = parse_time(args.now, "now") if args.now else None
        result = normalize_snapshot(
            load_json(Path(args.input)),
            max_age_seconds=args.max_age_seconds,
            now=now,
            account_filter=accounts,
        )
    except (ContractError, OSError) as exc:
        result = {"success": False, "error": str(exc), "action": args.command}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
