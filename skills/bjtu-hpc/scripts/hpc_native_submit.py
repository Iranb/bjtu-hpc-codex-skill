#!/usr/bin/env python3.12
"""Preflight and optionally submit a native Slurm sbatch script through the BJTU SSH proxy."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import tempfile
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpc_runtime import require_native_dependencies

require_native_dependencies()

import hpc_winscp_info as winscp
from hpc_core.native import connect_ssh, load_connection, run_remote, verify_slurm_allocation


JOB_ID_RE = re.compile(r"Submitted batch job\s+(\d+)")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ARRAY_DIRECTIVE_RE = re.compile(r"^\s*#SBATCH\s+--array(?:=|\s+)(\S+)\s*$", re.MULTILINE)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strict_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_script(args: argparse.Namespace) -> tuple[bytes | None, str]:
    if args.remote_script:
        return None, f"remote:{args.remote_script}"
    if not args.script:
        raise ValueError("provide a local script path or --remote-script")
    path = args.script.expanduser()
    return path.read_bytes(), str(path)


def remote_script_path(args: argparse.Namespace, script_sha256: str) -> str:
    if args.remote_script:
        return args.remote_script
    if args.remote_path:
        return args.remote_path
    stem = args.script.expanduser().stem if args.script else "native"
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem)[:64] or "native"
    return f"/tmp/bjtu_native_{os.getpid()}_{script_sha256[:16]}_{safe_stem}.sbatch"


def _remote_sha256_expression(remote_path: str) -> str:
    code = "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())"
    return f"python3 -c {shlex.quote(code)} {shlex.quote(remote_path)}"


def _remote_hash_guard(remote_path: str, expected_sha256: str) -> str:
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ValueError("expected remote script SHA must be lowercase 64-hex")
    return (
        f"observed_script_sha256=$({_remote_sha256_expression(remote_path)})\n"
        f"if [[ \"$observed_script_sha256\" != {shlex.quote(expected_sha256)} ]]; then\n"
        "  echo \"remote script SHA mismatch: expected="
        f"{expected_sha256} observed=$observed_script_sha256\" >&2\n"
        "  exit 74\n"
        "fi"
    )


def upload_command(script_bytes: bytes, remote_path: str, expected_sha256: str) -> str:
    observed = hashlib.sha256(script_bytes).hexdigest()
    if observed != expected_sha256:
        raise ValueError("upload bytes do not match the frozen script SHA")
    encoded = base64.b64encode(script_bytes).decode("ascii")
    encoded_lines = "\n".join(textwrap.wrap(encoded, 76))
    temporary = remote_path + ".upload"
    decode_code = (
        "import base64,sys; "
        "payload=b''.join(sys.stdin.buffer.read().split()); "
        "open(sys.argv[1],'wb').write(base64.b64decode(payload, validate=True))"
    )
    return (
        "set -euo pipefail\n"
        f"cleanup_upload() {{ rm -f -- {shlex.quote(temporary)}; }}\n"
        "trap cleanup_upload EXIT\n"
        f"python3 -c {shlex.quote(decode_code)} {shlex.quote(temporary)} <<'__BJTU_SCRIPT_BASE64__'\n"
        f"{encoded_lines}\n"
        "__BJTU_SCRIPT_BASE64__\n"
        f"chmod 700 {shlex.quote(temporary)}\n"
        + _remote_hash_guard(temporary, expected_sha256)
        + "\n"
        f"mv -- {shlex.quote(temporary)} {shlex.quote(remote_path)}\n"
        f"chmod 700 {shlex.quote(remote_path)}\n"
        "trap - EXIT\n"
        f"printf 'remote_script_sha256=%s\\n' {shlex.quote(expected_sha256)}"
    )


def preflight_command(remote_path: str, expected_sha256: str) -> str:
    return (
        "set -euo pipefail\n"
        + _remote_hash_guard(remote_path, expected_sha256)
        + "\n"
        + f"bash -n {shlex.quote(remote_path)} && sbatch --test-only {shlex.quote(remote_path)}"
    )


def submit_command(remote_path: str, intent: dict[str, Any], expected_sha256: str) -> str:
    trace = str(intent.get("anonymous_trace_id") or "")
    safe_trace = re.sub(r"[^A-Za-z0-9_]+", "", trace)[:32]
    job_name = safe_trace
    export_value = f"ALL,AUTORESEARCH_TRACE={trace}"
    return (
        "set -euo pipefail\n"
        + _remote_hash_guard(remote_path, expected_sha256)
        + "\n"
        + f"sbatch --job-name={shlex.quote(job_name)} "
        f"--comment={shlex.quote('autoreskill:' + trace)} "
        f"--export={shlex.quote(export_value)} {shlex.quote(remote_path)}"
    )


def prepared_submit_intent(
    args: argparse.Namespace,
    observed_script_sha256: str,
) -> tuple[dict[str, Any], str, str]:
    if not args.submit_intent:
        raise ValueError("--submit requires --submit-intent from prepare-backend-submit")
    intent_path = args.submit_intent.expanduser().resolve()
    intent_bytes = intent_path.read_bytes()
    intent_sha256 = hashlib.sha256(intent_bytes).hexdigest()
    frozen_intent_sha256 = str(getattr(args, "frozen_intent_sha256", "") or "")
    if frozen_intent_sha256 and intent_sha256 != frozen_intent_sha256:
        raise ValueError("submit intent changed after cycle validation")
    payload = json.loads(intent_bytes)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {intent_path}")
    intent = payload.get("submit_intent") if isinstance(payload.get("submit_intent"), dict) else payload
    required = [
        "submit_attempt_id",
        "backend_idempotency_key",
        "anonymous_trace_id",
        "launch_identity_hash",
        "script_or_command_sha256",
        "preflight_sha256",
        "pool_id",
        "execution_route",
    ]
    missing = [field for field in required if not str(intent.get(field) or "").strip()]
    if missing:
        raise ValueError(f"submit intent missing fields: {missing}")
    if str(intent.get("execution_route") or "").strip().lower() != "bjtu_hpc":
        raise ValueError("submit intent execution_route must be bjtu_hpc")
    for field in ["launch_identity_hash", "script_or_command_sha256", "preflight_sha256"]:
        if not SHA256_RE.fullmatch(str(intent.get(field) or "").strip().lower()):
            raise ValueError(f"submit intent {field} must be a lowercase 64-hex digest")
    embedding = intent.get("trace_embedding") if isinstance(intent.get("trace_embedding"), dict) else {}
    if str(embedding.get("anonymous_trace_id") or "") != str(intent.get("anonymous_trace_id") or ""):
        raise ValueError("submit intent trace_embedding does not bind anonymous_trace_id")
    if str(embedding.get("surface") or "").strip().lower() not in {
        "slurm_job_name",
        "slurm_comment",
        "slurm_environment",
    }:
        raise ValueError("submit intent must use a Slurm-searchable trace surface")
    frozen_script_sha256 = str(getattr(args, "frozen_script_sha256", "") or "")
    if frozen_script_sha256 and observed_script_sha256 != frozen_script_sha256:
        raise ValueError("exact script changed after cycle validation")
    if observed_script_sha256 != str(intent.get("script_or_command_sha256") or ""):
        raise ValueError("exact script hash does not match the prepared submit intent")
    return dict(intent), observed_script_sha256, intent_sha256


def connection_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "cluster": args.cluster,
        "account": args.account,
        "portal_user": args.portal_user,
        "auth_account": args.auth_account,
        "token_file": args.token_file,
        "refresh_token": args.refresh_token,
        "refresh_browser": args.refresh_browser,
        "refresh_headless": args.refresh_headless,
    }


def parse_job_id(text: str) -> str | None:
    match = JOB_ID_RE.search(text)
    return match.group(1) if match else None


def parse_array_task_ids(script_text: str | None) -> list[str]:
    if not script_text:
        return []
    match = ARRAY_DIRECTIVE_RE.search(script_text)
    if not match:
        return []
    task_text = match.group(1).split("%", 1)[0]
    if any(character in task_text for character in "-:"):
        return []
    return [part for part in task_text.split(",") if part.isdigit()]


def run(
    args: argparse.Namespace,
    *,
    connection_info: dict[str, Any] | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    script_bytes, source = read_script(args)
    if script_bytes is not None:
        script_sha256 = hashlib.sha256(script_bytes).hexdigest()
        try:
            script_text = script_bytes.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("local sbatch script must be UTF-8 text") from error
    else:
        script_sha256 = str(args.script_sha256 or "").strip().lower()
        script_text = None
        if not SHA256_RE.fullmatch(script_sha256):
            raise ValueError("--remote-script requires --script-sha256 as lowercase 64-hex")
    frozen_script_sha256 = str(getattr(args, "frozen_script_sha256", "") or "")
    if frozen_script_sha256 and script_sha256 != frozen_script_sha256:
        raise ValueError("exact script changed after cycle validation")
    intent: dict[str, Any] = {}
    intent_sha256 = None
    if args.submit:
        if not args.receipt_out:
            raise ValueError("--submit requires --receipt-out for immediate durable receipt recording")
        intent, script_sha256, intent_sha256 = prepared_submit_intent(args, script_sha256)
    remote_path = remote_script_path(args, script_sha256)
    conn = connection_kwargs(args)
    info = connection_info or load_connection(**conn)
    owns_client = client is None
    active_client = client or connect_ssh(info)
    verification = None
    try:
        upload = None
        if script_bytes is not None and not args.remote_script:
            upload = run_remote(
                active_client,
                upload_command(script_bytes, remote_path, script_sha256),
                timeout=args.timeout,
            )
            if upload["returncode"] != 0:
                return {
                    "success": False,
                    "source": source,
                    "remote_path": remote_path,
                    "upload": upload,
                    "message": "failed to write remote sbatch script",
                }
        preflight = run_remote(
            active_client,
            preflight_command(remote_path, script_sha256),
            timeout=args.timeout,
        )
        if preflight["returncode"] != 0:
            return {
                "success": False,
                "source": source,
                "remote_path": remote_path,
                "upload": upload,
                "preflight": preflight,
                "message": "bash -n or sbatch --test-only failed; not submitted",
            }
        submit = None
        job_id = None
        if args.submit:
            try:
                submit = run_remote(
                    active_client,
                    submit_command(remote_path, intent, script_sha256),
                    timeout=args.timeout,
                )
            except Exception as exc:
                return {
                    "success": False,
                    "submitted": True,
                    "submit_outcome": "unknown",
                    "anonymous_trace_id": intent.get("anonymous_trace_id"),
                    "script_or_command_sha256": script_sha256,
                    "message": f"backend command started but no definitive receipt was returned: {type(exc).__name__}: {exc}",
                    "recovery": "leave queue row submitting and search Slurm by trace/script identity; do not retry",
                }
            job_id = parse_job_id((submit["stdout"] or "") + "\n" + (submit["stderr"] or ""))
            if submit["returncode"] != 0 or not job_id:
                return {
                    "success": False,
                    "source": source,
                    "remote_path": remote_path,
                    "upload": upload,
                    "preflight": preflight,
                    "submit": submit,
                    "anonymous_trace_id": intent.get("anonymous_trace_id"),
                    "submit_outcome": "definitive_failure" if submit["returncode"] != 0 else "unknown",
                    "message": "sbatch submit failed or no job id was returned; unknown outcomes require trace search",
                }
            accepted_at = now_iso()
            receipt = {
                "submit_attempt_id": intent.get("submit_attempt_id"),
                "backend_idempotency_key": intent.get("backend_idempotency_key"),
                "anonymous_trace_id": intent.get("anonymous_trace_id"),
                "launch_identity_hash": intent.get("launch_identity_hash"),
                "script_or_command_sha256": intent.get("script_or_command_sha256"),
                "remote_script_sha256": script_sha256,
                "submit_intent_sha256": intent_sha256,
                "preflight_sha256": intent.get("preflight_sha256"),
                "pool_id": intent.get("pool_id"),
                "execution_route": "bjtu_hpc",
                "backend": "bjtu_hpc",
                "queue_row_id": intent.get("queue_row_id"),
                "queue_revision": intent.get("queue_revision"),
                "global_schedule_sha256": intent.get("global_schedule_sha256"),
                "assignment_sha256": intent.get("assignment_sha256"),
                "account_ref": intent.get("account_ref"),
                "host_ref": intent.get("host_ref"),
                "native_id": job_id,
                "accepted_at": accepted_at,
                "evidence_ref": str(args.receipt_out.expanduser().resolve()),
                "remote_script_ref": remote_path,
            }
            receipt_payload = {"schema_version": 1, "submit_receipt": receipt}
            receipt_payload["submit_receipt_sha256"] = canonical_sha256(receipt)
            atomic_write_json(args.receipt_out.expanduser().resolve(), receipt_payload)

        if args.submit and job_id and not args.no_verify:
            parent_verification = verify_slurm_allocation(
                job_id,
                expected_total_cpus=args.expected_total_cpus,
                expected_ntasks=args.expected_ntasks,
                expected_cpus_per_task=args.expected_cpus_per_task,
                expected_gpus=args.expected_gpus,
                expected_command=remote_path,
                connection_info=info,
                client=active_client,
                **conn,
            )
            array_task_ids = parse_array_task_ids(script_text if not args.remote_script else None)
            if array_task_ids:
                task_verifications = []
                for task_id in array_task_ids:
                    task_verifications.append(
                        verify_slurm_allocation(
                            f"{job_id}_{task_id}",
                            expected_total_cpus=args.expected_total_cpus,
                            expected_ntasks=args.expected_ntasks,
                            expected_cpus_per_task=args.expected_cpus_per_task,
                            expected_gpus=args.expected_gpus,
                            expected_command=remote_path,
                            connection_info=info,
                            client=active_client,
                            **conn,
                        )
                    )
                verification = dict(parent_verification)
                verification["parent"] = parent_verification
                verification["array_tasks"] = task_verifications
                verification["success"] = bool(
                    parent_verification.get("success")
                    and len(task_verifications) == len(array_task_ids)
                    and all(item.get("success") for item in task_verifications)
                )
            else:
                verification = parent_verification
    finally:
        if owns_client:
            active_client.close()

    success = True
    if verification is not None:
        success = bool(verification.get("success"))
    return {
        "success": success,
        "source": source,
        "remote_path": remote_path,
        "preflight": preflight,
        "submitted": bool(args.submit),
        "submit": submit if args.submit else None,
        "job_id": job_id,
        "anonymous_trace_id": intent.get("anonymous_trace_id") if intent else None,
        "script_or_command_sha256": script_sha256 or None,
        "remote_script_sha256": script_sha256 or None,
        "submit_intent_sha256": intent_sha256,
        "receipt_out": str(args.receipt_out.expanduser().resolve()) if args.receipt_out else None,
        "verification": verification,
    }


def print_text(result: dict[str, Any]) -> None:
    print(f"success: {result.get('success')}")
    print(f"remote_path: {result.get('remote_path')}")
    preflight = result.get("preflight") or {}
    print(f"preflight: rc={preflight.get('returncode')}")
    preflight_text = ((preflight.get("stdout") or "") + "\n" + (preflight.get("stderr") or "")).strip()
    if preflight_text:
        print(f"preflight_output: {preflight_text}")
    if result.get("submitted"):
        print(f"job_id: {result.get('job_id') or '-'}")
    verification = result.get("verification") or {}
    if verification:
        observed = verification.get("observed") or {}
        print(
            "verification: "
            f"ok={verification.get('success')} "
            f"cpus={observed.get('num_cpus')} "
            f"ntasks={observed.get('num_tasks')} "
            f"cpus_per_task={observed.get('cpus_per_task')} "
            f"gpus={observed.get('gpu_count')}"
        )
        if verification.get("mismatches"):
            print(f"mismatches: {verification.get('mismatches')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run native sbatch preflight and optional submit through BJTU SSH proxy.")
    parser.add_argument("script", nargs="?", type=Path, help="Local sbatch script to copy to the cluster.")
    parser.add_argument("--remote-script", help="Already-existing remote sbatch path.")
    parser.add_argument("--remote-path", help="Remote path for a copied local sbatch script.")
    parser.add_argument("--submit", action="store_true", help="Actually run sbatch after preflight succeeds.")
    parser.add_argument("--submit-intent", type=Path, help="Prepared queue-bound submit intent JSON; required with --submit.")
    parser.add_argument("--receipt-out", type=Path, help="Mode-0600 strict submit receipt path; required with --submit.")
    parser.add_argument("--script-sha256", help="Exact remote script hash when --remote-script is used.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--expected-total-cpus", type=int)
    parser.add_argument("--expected-ntasks", type=int)
    parser.add_argument("--expected-cpus-per-task", type=int)
    parser.add_argument("--expected-gpus", type=int)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--cluster", default=winscp.DEFAULT_CLUSTER)
    parser.add_argument("--account", default=winscp.DEFAULT_ACCOUNT)
    parser.add_argument("--portal-user", default=winscp.DEFAULT_PORTAL_USER)
    parser.add_argument("--auth-account")
    parser.add_argument("--token-file", type=Path, default=winscp.DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"success": False, "message": str(exc), "submitted": False}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_text(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
