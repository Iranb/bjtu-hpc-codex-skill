#!/usr/bin/env python3.12
import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from hpc_runtime import require_controller_python

require_controller_python()

from hpc_account_store import apply_auth_account_defaults
from hpc_upload import (
    AUTH_ERROR_MESSAGE,
    BASE_URL,
    DEFAULT_TOKEN_FILE,
    create_session,
    join_remote_path,
    load_token,
    mkdir_remote,
    normalize_remote_dir,
    refresh_token,
    request_json,
    upload_path,
)

APP_CONFIG = {
    "gpu": {
        "app_id": 13,
        "name": "PyTorch-GPU",
        "default_ntasks": "1",
        "default_cpus_per_task": "8",
        "default_gres_flags": "disable-binding",
        "default_partition": "GPU",
        "partitions": {"GPU"},
    },
    "cpu": {
        "app_id": 16,
        "name": "PyTorch-CPU",
        "default_ntasks": "48",
        "default_partition": "COMPUTE",
        "partitions": {"COMPUTE", "FAT"},
    },
}

DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_ACCOUNT = os.getenv("HPC_ACCOUNT")
DEFAULT_PORTAL_USER = os.getenv("HPC_PORTAL_USER", "")
DEFAULT_REMOTE_DIR = os.getenv("HPC_REMOTE_DIR", "home")
DEFAULT_PYTORCH = os.getenv("HPC_PYTORCH", "pytorch1.7-python3.8")
TERMINAL_STATES = {"DONE", "FAILED", "CANCELLED", "CANCEL"}


def remote_input_for_upload(cluster, account, remote_dir, script_path, input_path_kind):
    virtual_dir = normalize_remote_dir(cluster, account, remote_dir)
    virtual_path = join_remote_path(virtual_dir, script_path.name)
    if input_path_kind == "virtual":
        return virtual_path
    return virtual_to_real_path(virtual_path, cluster, account)


def virtual_to_real_path(path, cluster, account):
    home_prefix = f"[PATH]/{cluster}/{account}/home"
    if path == home_prefix:
        return f"/data/home/{account}"
    if path.startswith(home_prefix + "/"):
        return f"/data/home/{account}/{path[len(home_prefix) + 1:]}"
    return path


def build_payload(args, input_path):
    app = APP_CONFIG[args.app]
    partition = args.partition or app["default_partition"]
    if partition not in app["partitions"]:
        allowed = ", ".join(sorted(app["partitions"]))
        raise ValueError(f"{app['name']} partition must be one of: {allowed}")

    ntasks = args.ntasks or app["default_ntasks"]
    cpus_per_task = args.cpus_per_task or app.get("default_cpus_per_task")
    cmd = [
        {"--job-name": args.job_name},
        {"--ntasks": str(ntasks)},
    ]
    if cpus_per_task:
        cmd.append({"--cpus-per-task": str(cpus_per_task)})
    if args.app == "gpu":
        cmd.append({"--gpu": str(args.gpu)})
        gres_flags = args.gres_flags if args.gres_flags is not None else app.get("default_gres_flags")
        if gres_flags:
            cmd.append({"--gres-flags": str(gres_flags)})
    cmd.append({"--partition": partition})

    params = [
        {"--input": [input_path]},
        {"--pytorch": args.pytorch},
    ]
    if args.console:
        params.append({"--console": "true"})

    return {
        "appId": app["app_id"],
        "cluster_id": args.cluster,
        "userName": args.portal_user,
        "osUser": args.account,
        "cmd": cmd,
        "params": params,
    }


def submit_job(session, token, payload):
    url = f"{BASE_URL}/job/submit"
    return request_json(
        session,
        "POST",
        url,
        headers={"PARA_ATOKEN": token},
        json=payload,
    )


def query_job_by_name(session, token, cluster, portal_user, job_name):
    params = {
        "pno": "0",
        "psize": "5",
        "cluster_id": cluster,
        "keyword": job_name,
        "order": "time",
        "reverse": "0",
        "user": portal_user,
    }
    url = f"{BASE_URL}/job/list?{urlencode(params)}"
    return request_json(session, "GET", url, headers={"PARA_ATOKEN": token})


def is_terminal_job(row):
    return row.get("done") == 1 or str(row.get("state") or "").upper() in TERMINAL_STATES


def print_job_status(row):
    print(
        "[job] "
        f"name={row.get('name')} platform_id={row.get('id')} slurm_id={row.get('jobId')} "
        f"state={row.get('state')} origin={row.get('originState')} done={row.get('done')} "
        f"nodes={row.get('nodes')}"
    )
    if row.get("stdOutput"):
        print(f"[stdout] {row.get('stdOutput')}")
    if row.get("workDir"):
        print(f"[workDir] {row.get('workDir')}")


def wait_for_job(session, token, cluster, portal_user, job_name, interval, timeout):
    deadline = time.monotonic() + timeout if timeout else None
    last_state = None

    while True:
        jobs = query_job_by_name(session, token, cluster, portal_user, job_name)
        rows = jobs.get("data") or []
        row = rows[0] if rows else None
        if row:
            state = (row.get("state"), row.get("originState"), row.get("done"), row.get("nodes"))
            if state != last_state:
                print_job_status(row)
                last_state = state
            if is_terminal_job(row):
                return row
        else:
            print(f"[job] no matching job yet: {job_name}")

        if deadline and time.monotonic() >= deadline:
            raise TimeoutError(f"job did not finish within {timeout}s: {job_name}")
        time.sleep(interval)


def ensure_remote_dir(session, token, cluster, account, remote_dir, verbose):
    virtual_dir = normalize_remote_dir(cluster, account, remote_dir)
    if virtual_dir.endswith(f"/{account}/home") or virtual_dir.endswith(f"/{account}"):
        return

    parts = virtual_dir.split("/")
    paths = []
    for index in range(4, len(parts) + 1):
        paths.append("/".join(parts[:index]))
    for path in paths:
        mkdir_remote(session, cluster, account, path, token, verbose)


def default_job_name(script, app):
    stem = Path(script).stem if script else app
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return f"{stem}_{app}_{stamp}"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Submit a PyTorch CPU/GPU application job through BJTU HPC web APIs."
    )
    parser.add_argument("script", nargs="?", help="Local .py file to upload and use as --input")
    parser.add_argument("--remote-input", help="Use an existing remote .py path instead of uploading a local file")
    parser.add_argument("--app", choices=sorted(APP_CONFIG), default=os.getenv("HPC_APP", "gpu"))
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="Cluster OS account")
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER, help="Portal login user")
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"), help="Saved auth account name from hpc_accounts.py")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR, help="Upload target, e.g. home or home/jobs")
    parser.add_argument("--input-path-kind", choices=["virtual", "real"], default="virtual")
    parser.add_argument("--job-name", help="SLURM job name")
    parser.add_argument("--ntasks", help="SLURM task count. Default for --app gpu is 1.")
    parser.add_argument(
        "--cpus-per-task",
        default=os.getenv("HPC_CPUS_PER_TASK"),
        help="CPUs per SLURM task. Default for --app gpu is 8.",
    )
    parser.add_argument(
        "--gpu",
        default=os.getenv("HPC_GPU", "1"),
        help="GPU count for --app gpu. Default is 1; request more only when the code uses multi-GPU.",
    )
    parser.add_argument(
        "--gres-flags",
        default=os.getenv("HPC_GRES_FLAGS"),
        help="SLURM GRES flags. GPU jobs default to disable-binding; pass an empty string to omit.",
    )
    parser.add_argument("--partition")
    parser.add_argument("--pytorch", default=DEFAULT_PYTORCH)
    parser.add_argument("--console", action="store_true", help="Add --console=true to params")
    parser.add_argument("--submit", action="store_true", help="Actually submit the job. Default is dry-run.")
    parser.add_argument("--wait", action="store_true", help="After submit, poll until the job reaches a terminal state")
    parser.add_argument("--wait-interval", type=int, default=10)
    parser.add_argument("--wait-timeout", type=int, default=0, help="Seconds; 0 means no timeout")
    parser.add_argument("--rewrite", action="store_true", help="Overwrite uploaded script if supported")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true", help="Open browser login flow before submit/upload")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    parser.add_argument("--no-progress", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    apply_auth_account_defaults(
        args,
        default_cluster=DEFAULT_CLUSTER,
        default_account=DEFAULT_ACCOUNT,
        default_portal_user=DEFAULT_PORTAL_USER,
    )

    if bool(args.script) == bool(args.remote_input):
        parser.error("provide exactly one of: local script positional argument OR --remote-input")

    if args.script:
        script = Path(args.script).expanduser()
        if not script.is_file():
            parser.error(f"script not found: {script}")
        if script.suffix != ".py":
            parser.error("script must be a .py file")
        args.script = script

    if args.remote_input and not args.remote_input.endswith(".py"):
        parser.error("--remote-input must point to a .py file")

    if not args.job_name:
        args.job_name = default_job_name(args.script or args.remote_input, args.app)

    return args


def main():
    args = parse_args()
    token_file = args.token_file.expanduser()
    token = load_token(args.token, token_file, auth_account=args.auth_account)
    if args.refresh_token:
        token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=args.auth_account)
    if not token:
        print(
            f"Missing token. Run: python3 hpc_refresh_token.py or retry with --refresh-token. "
            f"You can also set HPC_PARA_ATOKEN / --token / --token-file {args.token_file}",
            file=sys.stderr,
        )
        return 2

    if args.remote_input:
        input_path = args.remote_input
    else:
        input_path = remote_input_for_upload(
            args.cluster,
            args.account,
            args.remote_dir,
            args.script,
            args.input_path_kind,
        )

    payload = build_payload(args, input_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not args.submit:
        print("\n[dry-run] add --submit to upload the script and submit this job")
        return 0

    session = create_session()
    session.headers.update({"Accept": "application/json, text/plain, */*"})

    try:
        if args.script:
            ensure_remote_dir(session, token, args.cluster, args.account, args.remote_dir, args.verbose)
            upload_path(
                args.script,
                args.cluster,
                args.account,
                args.remote_dir,
                token,
                rewrite=args.rewrite,
                verbose=args.verbose,
                show_progress=not args.no_progress,
                include_parent_dir=False,
            )

        result = submit_job(session, token, payload)
    except RuntimeError as error:
        if args.refresh_token and str(error) == AUTH_ERROR_MESSAGE:
            token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=args.auth_account)
            result = submit_job(session, token, payload)
        else:
            raise

    print("[submit]", json.dumps(result, ensure_ascii=False))
    if result.get("success"):
        jobs = query_job_by_name(session, token, args.cluster, args.portal_user, args.job_name)
        print("[job-list]", json.dumps(jobs, ensure_ascii=False))
        if args.wait:
            row = wait_for_job(
                session,
                token,
                args.cluster,
                args.portal_user,
                args.job_name,
                args.wait_interval,
                args.wait_timeout,
            )
            return 0 if str(row.get("originState") or row.get("state")).upper() == "COMPLETED" else 1
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
