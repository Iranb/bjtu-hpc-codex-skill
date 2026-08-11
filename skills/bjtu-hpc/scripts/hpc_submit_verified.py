#!/usr/bin/env python3.12
"""Submit a BJTU portal job and verify the native Slurm shape."""

import argparse
import json
import os
import sys
from pathlib import Path

from hpc_runtime import require_native_dependencies

require_native_dependencies()

from hpc_account_store import apply_auth_account_defaults
from hpc_core.jobs import (
    DEFAULT_ACCOUNT,
    DEFAULT_CLUSTER,
    DEFAULT_PORTAL_USER,
    DEFAULT_PYTORCH,
    DEFAULT_REMOTE_DIR,
    JobSpec,
    plan_job,
    submit_job,
)
from hpc_core.native import pending_reason, verify_slurm_allocation
from hpc_upload import DEFAULT_TOKEN_FILE


def expected_total_cpus_for(app: str, ntasks: int | None, cpus_per_task: int | None) -> int | None:
    effective_ntasks = ntasks if ntasks is not None else (1 if app == "gpu" else 48)
    effective_cpus_per_task = cpus_per_task if cpus_per_task is not None else (8 if app == "gpu" else None)
    if effective_ntasks is None or effective_cpus_per_task is None:
        return None
    return int(effective_ntasks) * int(effective_cpus_per_task)


def build_spec(args: argparse.Namespace) -> JobSpec:
    return JobSpec(
        app=args.app,
        job_name=args.job_name,
        local_script_path=args.local_script,
        remote_input=args.remote_input,
        cluster=args.cluster,
        account=args.account,
        portal_user=args.portal_user,
        remote_dir=args.remote_dir,
        input_path_kind=args.input_path_kind,
        ntasks=args.ntasks,
        cpus_per_task=args.cpus_per_task,
        gpu_count=args.gpu,
        gres_flags=args.gres_flags,
        partition=args.partition,
        pytorch=args.pytorch,
        console=args.console,
        rewrite=args.rewrite,
        allow_external_path=args.allow_external_path,
    )


def connection_kwargs(args: argparse.Namespace) -> dict:
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


def verify_after_submit(args: argparse.Namespace, result: dict) -> dict:
    job = result.get("job") or (result.get("wait") or {}).get("job") or {}
    slurm_id = str(job.get("slurm_id") or "")
    state = str(job.get("state") or "").upper()
    verification = {"portal_job": job}
    if not slurm_id:
        verification["success"] = False
        verification["message"] = "portal row has no Slurm job id yet"
        return verification

    conn = connection_kwargs(args)
    native_partition = args.partition or ("GPU" if args.app == "gpu" else "COMPUTE")
    if state == "PENDING":
        verification["pending_reason"] = pending_reason(
            [slurm_id],
            partition=native_partition,
            include_sinfo=False,
            **conn,
        )

    if not args.no_native_check:
        verification["allocation"] = verify_slurm_allocation(
            slurm_id,
            expected_total_cpus=args.expected_total_cpus
            or expected_total_cpus_for(args.app, args.ntasks, args.cpus_per_task),
            expected_ntasks=args.ntasks if args.ntasks is not None else (1 if args.app == "gpu" else 48),
            expected_cpus_per_task=args.cpus_per_task if args.cpus_per_task is not None else (8 if args.app == "gpu" else None),
            expected_gpus=args.expected_gpus if args.expected_gpus is not None else (args.gpu if args.app == "gpu" else None),
            **conn,
        )
        verification["success"] = bool(verification["allocation"].get("success"))
    else:
        verification["success"] = True
    return verification


def print_text(result: dict) -> None:
    job = result.get("job") or {}
    print(f"success: {result.get('success')}")
    print(f"job: {job.get('name')} platform_id={job.get('platform_id')} slurm_id={job.get('slurm_id')} state={job.get('state')}")
    verification = result.get("verification") or {}
    if verification.get("message"):
        print(f"verification: {verification.get('message')}")
    pending = verification.get("pending_reason")
    if pending:
        for item in pending.get("reasons") or []:
            print(f"pending_reason: job={item.get('job_id')} state={item.get('state')} reason={item.get('reason')}")
    allocation = verification.get("allocation")
    if allocation:
        observed = allocation.get("observed") or {}
        print(
            "allocation: "
            f"ok={allocation.get('success')} cpus={observed.get('num_cpus')} "
            f"ntasks={observed.get('num_tasks')} cpus_per_task={observed.get('cpus_per_task')} "
            f"gpus={observed.get('gpu_count')}"
        )
        if allocation.get("mismatches"):
            print(f"mismatches: {allocation['mismatches']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a BJTU HPC portal job and verify native Slurm allocation.")
    parser.add_argument("local_script", nargs="?", help="Local .py script to upload and submit.")
    parser.add_argument("--remote-input", help="Already-uploaded remote .py path.")
    parser.add_argument("--submit", action="store_true", help="Actually upload/submit. Omit for plan-only mode.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--app", choices=["gpu", "cpu"], default=os.getenv("HPC_APP", "gpu"))
    parser.add_argument("--job-name")
    parser.add_argument("--gpu", type=int, default=int(os.getenv("HPC_GPU", "1")))
    parser.add_argument("--ntasks", type=int, default=int(os.getenv("HPC_NTASKS")) if os.getenv("HPC_NTASKS") else None)
    parser.add_argument("--cpus-per-task", type=int, default=int(os.getenv("HPC_CPUS_PER_TASK")) if os.getenv("HPC_CPUS_PER_TASK") else None)
    parser.add_argument("--gres-flags", default=os.getenv("HPC_GRES_FLAGS"))
    parser.add_argument("--partition")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--input-path-kind", choices=["virtual", "real"], default="virtual")
    parser.add_argument("--pytorch", default=DEFAULT_PYTORCH)
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--rewrite", action="store_true")
    parser.add_argument("--allow-external-path", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--wait-timeout", type=int, default=0)
    parser.add_argument("--wait-interval", type=int, default=10)
    parser.add_argument("--no-native-check", action="store_true")
    parser.add_argument("--expected-total-cpus", type=int)
    parser.add_argument("--expected-gpus", type=int)
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    apply_auth_account_defaults(
        args,
        default_cluster=DEFAULT_CLUSTER,
        default_account=DEFAULT_ACCOUNT,
        default_portal_user=DEFAULT_PORTAL_USER,
    )
    spec = build_spec(args)
    if not args.submit:
        result = plan_job(spec)
        result["requires_confirmation"] = True
        result["message"] = "Add --submit to upload and submit this job."
    else:
        result = submit_job(
            spec,
            confirm=True,
            wait=args.wait,
            wait_interval=args.wait_interval,
            wait_timeout=args.wait_timeout,
            token_file=args.token_file,
            refresh_token=args.refresh_token,
            refresh_browser=args.refresh_browser,
            refresh_headless=args.refresh_headless,
            auth_account=args.auth_account,
        )
        if not result.get("job") and (result.get("wait") or {}).get("job"):
            result["job"] = result["wait"]["job"]
        if result.get("submit_result", {}).get("success"):
            result["verification"] = verify_after_submit(args, result)
            allocation = result["verification"].get("allocation")
            if result["verification"].get("success") is False:
                result["success"] = False
            elif allocation:
                result["success"] = bool(result.get("success")) and bool(allocation.get("success"))

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_text(result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
