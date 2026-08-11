#!/usr/bin/env python3.12
"""Inspect native SLURM pending reasons through the BJTU portal SSH proxy."""

import argparse
import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

from hpc_runtime import require_native_dependencies

require_native_dependencies()

import paramiko

import hpc_winscp_info as winscp


SQUEUE_FORMAT = "%.18i %.9P %.40j %.8u %.2t %.10M %.10l %.6D %R"
JOB_ID_RE = re.compile(r"^\s*(\d+)\s+")


def load_connection(args):
    winscp_args = argparse.Namespace(
        cluster=args.cluster,
        account=args.account,
        portal_user=args.portal_user,
        auth_account=args.auth_account,
        token=args.token,
        token_file=args.token_file,
        refresh_token=args.refresh_token,
        refresh_browser=args.refresh_browser,
        refresh_headless=args.refresh_headless,
        show_secret=False,
    )
    winscp.apply_auth_account_defaults(
        winscp_args,
        default_cluster=winscp.DEFAULT_CLUSTER,
        default_account=winscp.DEFAULT_ACCOUNT,
        default_portal_user=winscp.DEFAULT_PORTAL_USER,
    )
    token = winscp.load_auth(winscp_args)
    try:
        return winscp.run(winscp_args, token)
    except RuntimeError as error:
        if str(error) != winscp.AUTH_ERROR_MESSAGE:
            raise
        token = winscp.refresh_token(
            winscp_args.token_file.expanduser(),
            winscp_args.refresh_browser,
            winscp_args.refresh_headless,
            auth_account=winscp_args.auth_account,
        )
        return winscp.run(winscp_args, token)


def connect_ssh(info):
    host, port_text = info["proxy"].rsplit(":", 1)
    username = f"{info['cluster']},{info['account']}"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=int(port_text),
        username=username,
        password=info["certificate"],
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run_remote(client, command, timeout=45):
    stdin, stdout, stderr = client.exec_command(
        f"bash -lc {json.dumps(command)}",
        timeout=timeout,
    )
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    return {"command": command, "returncode": rc, "stdout": out, "stderr": err}


def target_squeue_command(targets, account, partition):
    if targets:
        numeric = [target for target in targets if str(target).isdigit()]
        names = [target for target in targets if not str(target).isdigit()]
        commands = []
        if numeric:
            commands.append(
                "squeue -j "
                + shlex.quote(",".join(numeric))
                + " -o "
                + shlex.quote(SQUEUE_FORMAT)
            )
        for name in names:
            commands.append(
                "squeue -u "
                + shlex.quote(account)
                + " --name "
                + shlex.quote(name)
                + " -o "
                + shlex.quote(SQUEUE_FORMAT)
            )
        return "; ".join(commands)
    command = "squeue -u " + shlex.quote(account)
    if partition:
        command += " -p " + shlex.quote(partition)
    command += " -o " + shlex.quote(SQUEUE_FORMAT)
    return command


def parse_job_ids(squeue_text):
    job_ids = []
    for line in squeue_text.splitlines():
        match = JOB_ID_RE.match(line)
        if match:
            job_ids.append(match.group(1))
    return sorted(set(job_ids), key=int)


def parse_state_reason(scontrol_text):
    state = None
    reason = None
    for token in scontrol_text.replace("\n", " ").split():
        if token.startswith("JobState="):
            state = token.split("=", 1)[1]
        elif token.startswith("Reason="):
            reason = token.split("=", 1)[1]
    return state, reason


def default_snapshot_dir():
    cwd_refine = Path.cwd() / "refine-logs" / "hpc_stdout"
    if cwd_refine.parent.exists():
        return cwd_refine
    return Path(__file__).resolve().parent / "hpc_stdout"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Use native squeue/scontrol to inspect pending reasons for BJTU SLURM jobs."
    )
    parser.add_argument("targets", nargs="*", help="SLURM job ids or exact job names. Omit to list the user's queue.")
    parser.add_argument("--partition", default="GPU")
    parser.add_argument("--cluster", default=winscp.DEFAULT_CLUSTER)
    parser.add_argument("--account", default=winscp.DEFAULT_ACCOUNT)
    parser.add_argument("--portal-user", default=winscp.DEFAULT_PORTAL_USER)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"))
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=winscp.DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    parser.add_argument("--snapshot-dir", type=Path, default=default_snapshot_dir())
    parser.add_argument("--no-sinfo", action="store_true", help="Skip partition/node summary commands.")
    return parser.parse_args()


def main():
    args = parse_args()
    info = load_connection(args)
    client = connect_ssh(info)
    try:
        results = {
            "squeue": run_remote(
                client,
                target_squeue_command(args.targets, info["account"], args.partition),
            )
        }
        job_ids = parse_job_ids(results["squeue"]["stdout"])
        for job_id in job_ids:
            results[f"scontrol_{job_id}"] = run_remote(client, f"scontrol show job {shlex.quote(job_id)}")
        if not args.no_sinfo:
            results["sinfo_partition"] = run_remote(
                client,
                f"sinfo -p {shlex.quote(args.partition)} -o '%P %a %l %D %t %N' 2>&1 || true",
            )
            results["partition"] = run_remote(client, f"scontrol show partition {shlex.quote(args.partition)}")
    finally:
        client.close()

    args.snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = args.snapshot_dir / f"bjtu_pending_reason_{stamp}.json"
    snapshot.write_text(
        json.dumps(
            {
                "checked_at_local": datetime.now().isoformat(timespec="seconds"),
                "cluster": info["cluster"],
                "account": info["account"],
                "home": info["home"],
                "targets": args.targets,
                "partition": args.partition,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"[snapshot] {snapshot}")
    print(results["squeue"]["stdout"].rstrip())
    for job_id in parse_job_ids(results["squeue"]["stdout"]):
        state, reason = parse_state_reason(results[f"scontrol_{job_id}"]["stdout"])
        if state or reason:
            print(f"[reason] job={job_id} state={state or '?'} reason={reason or '?'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
