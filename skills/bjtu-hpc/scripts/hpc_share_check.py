#!/usr/bin/env python3
import argparse
import os
import shlex
import sys
from pathlib import Path, PurePosixPath

try:
    import paramiko
except ImportError as error:
    raise SystemExit(
        "paramiko is required. Use the configured Python 3.12 controller."
    ) from error

import hpc_winscp_info as winscp
from hpc_account_store import apply_auth_account_defaults
from hpc_upload import AUTH_ERROR_MESSAGE, DEFAULT_TOKEN_FILE


DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_ACCOUNT = os.getenv("HPC_ACCOUNT")
DEFAULT_PORTAL_USER = os.getenv("HPC_PORTAL_USER", "")
DEFAULT_DATA_ROOT = os.getenv("BJTU_SHARE_DATA_ROOT")


def quote(value):
    return shlex.quote(str(value))


def remote_parent_paths(path):
    pure = PurePosixPath(path)
    parents = []
    current = PurePosixPath("/")
    for part in pure.parts[1:]:
        current = current / part
        parents.append(str(current))
    return parents


def connect(args):
    auth_args = argparse.Namespace(
        cluster=args.cluster,
        account=args.account,
        portal_user=args.portal_user,
        auth_account=args.auth_account,
        token=args.token,
        token_file=args.token_file,
        refresh_token=args.refresh_token,
        refresh_browser=args.refresh_browser,
        refresh_headless=args.refresh_headless,
    )
    token = winscp.load_auth(auth_args)
    try:
        info = winscp.run(auth_args, token)
    except RuntimeError as error:
        if str(error) != AUTH_ERROR_MESSAGE:
            raise
        token = winscp.refresh_token(
            auth_args.token_file.expanduser(),
            auth_args.refresh_browser,
            auth_args.refresh_headless,
            auth_account=auth_args.auth_account,
        )
        info = winscp.run(auth_args, token)

    host, port = info["proxy"].rsplit(":", 1) if ":" in info["proxy"] else (info["proxy"], "22")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=int(port),
        username=f"{info['cluster']},{info['account']}",
        password=info["certificate"],
        look_for_keys=False,
        allow_agent=False,
        timeout=20,
        auth_timeout=20,
        banner_timeout=20,
    )
    return client, info


def run(client, command, timeout=30):
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


def print_command(client, command, timeout=30):
    print(f"\n$ {command}")
    code, out, err = run(client, command, timeout=timeout)
    if out:
        print(out.rstrip())
    if err:
        print(f"[stderr] {err.rstrip()}")
    if code and code != -1:
        print(f"[exit] {code}")
    return code, out, err


def acl_commands(source_account, target_user, data_root):
    home = f"/data/home/{source_account}"
    return [
        f"setfacl -m u:{target_user}:--x {quote(home)}",
        f"setfacl -m u:{target_user}:r-x {quote(str(PurePosixPath(data_root).parent))}",
        f"setfacl -m u:{target_user}:r-x {quote(data_root)}",
        f"setfacl -R -m u:{target_user}:rX {quote(data_root)}",
    ]


def check(args):
    client, info = connect(args)
    try:
        data_root = args.data_root
        source_account = args.account
        parent_list = " ".join(quote(path) for path in remote_parent_paths(data_root))

        print(f"source_account: {source_account}")
        print(f"data_root: {data_root}")
        if args.target_user:
            print(f"target_user: {args.target_user}")

        print_command(client, "hostname; id; umask")
        print_command(client, f"ls -ld {parent_list} 2>/dev/null || true")
        print_command(client, "command -v getfacl setfacl || true")
        print_command(
            client,
            f"getfacl -p {quote(f'/data/home/{source_account}')} {quote(data_root)} 2>/dev/null || true",
        )

        home_path = f"/data/home/{source_account}"
        code, out, _ = run(
            client,
            f"python3 - <<'PY'\n"
            f"import os, stat\n"
            f"for path in [{home_path!r}, {data_root!r}]:\n"
            f"    st = os.stat(path)\n"
            f"    print(path, oct(stat.S_IMODE(st.st_mode)))\n"
            f"PY"
        )
        if out:
            modes = dict(line.rsplit(" ", 1) for line in out.strip().splitlines() if " " in line)
            home_mode = modes.get(home_path)
            data_mode = modes.get(data_root)
            if home_mode == "0o700":
                print(
                    "\n[result] Cross-account reads are blocked by the home directory mode "
                    f"({home_path} is {home_mode})."
                )
            elif data_mode and data_mode[-1] in {"0", "1", "2", "3"}:
                print(f"\n[result] The data root mode {data_mode} may block other users from reading.")
            else:
                print("\n[result] Basic Unix mode bits do not obviously block read-only sharing.")

        if args.target_user:
            commands = acl_commands(source_account, args.target_user, data_root)
            print("\n[acl-plan]")
            for command in commands:
                print(command)
            if args.apply:
                print("\n[apply]")
                for command in commands:
                    print_command(client, command)
                print_command(
                    client,
                    f"getfacl -p {quote(f'/data/home/{source_account}')} {quote(data_root)} 2>/dev/null || true",
                )
            else:
                print("\n[dry-run] add --apply to grant read-only ACLs to the target user.")
    finally:
        client.close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="Check whether BJTU HPC data under one account can be shared with another account."
    )
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="Source cluster OS account")
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"))
    parser.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    parser.add_argument("--target-user", help="Target cluster OS account to grant/read-check, e.g. u22xxxxxx")
    parser.add_argument("--apply", action="store_true", help="Apply read-only ACLs for --target-user")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    args = parser.parse_args()
    apply_auth_account_defaults(
        args,
        default_cluster=DEFAULT_CLUSTER,
        default_account=DEFAULT_ACCOUNT,
        default_portal_user=DEFAULT_PORTAL_USER,
    )
    if args.apply and not args.target_user:
        parser.error("--apply requires --target-user")
    return args


def main():
    args = parse_args()
    check(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
