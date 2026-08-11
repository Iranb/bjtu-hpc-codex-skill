#!/usr/bin/env python3.12
import argparse
import os
import re
import sys
import warnings
from pathlib import Path
from urllib.parse import quote

from hpc_runtime import require_controller_python

require_controller_python()

warnings.filterwarnings("ignore", message=r"urllib3 v2 only supports OpenSSL.*")

from hpc_account_store import apply_auth_account_defaults
from hpc_upload import (
    AUTH_ERROR_MESSAGE,
    BASE_URL,
    DEFAULT_TOKEN_FILE,
    create_session,
    load_token,
    refresh_token,
    request_json,
)

PORTAL_ORIGIN = "https://hpc.bjtu.edu.cn"
DEFAULT_CLUSTER = os.getenv("HPC_CLUSTER", "cluster2")
DEFAULT_ACCOUNT = os.getenv("HPC_ACCOUNT")
DEFAULT_PORTAL_USER = os.getenv("HPC_PORTAL_USER", "")
METADATA_REQUEST_TIMEOUT = (10, 30)


def redact_secret(value):
    if not value:
        return ""
    if len(value) <= 12:
        return f"[REDACTED:{len(value)}]"
    return f"{value[:4]}...[REDACTED:{len(value) - 8}]...{value[-4:]}"


def redact_sftp_url(value):
    return re.sub(
        r"(sftp://[^:/?#]+,[^:/?#]+:)([^@]+)(@)",
        lambda match: match.group(1) + redact_secret(match.group(2)) + match.group(3),
        value,
    )


def get_self(session, token):
    return request_json(
        session,
        "GET",
        f"{PORTAL_ORIGIN}/as/user/self",
        headers={"PARA_ATOKEN": token},
        timeout=METADATA_REQUEST_TIMEOUT,
    )


def get_system_settings(session, token):
    data = request_json(
        session,
        "GET",
        f"{BASE_URL}/systemsettings",
        headers={"PARA_ATOKEN": token},
        timeout=METADATA_REQUEST_TIMEOUT,
    )
    if not data.get("success"):
        raise RuntimeError(f"systemsettings request failed: {data}")
    return {item.get("name"): item.get("value") for item in data.get("data") or []}


def get_bound_accounts(session, token):
    data = request_json(
        session,
        "GET",
        f"{BASE_URL}/clusters/accounts/user/binds/page",
        headers={"PARA_ATOKEN": token},
        timeout=METADATA_REQUEST_TIMEOUT,
    )
    if not data.get("success"):
        raise RuntimeError(f"bound account request failed: {data}")
    return data.get("data") or []


def select_account(accounts, cluster, account):
    matches = [
        row
        for row in accounts
        if str(row.get("clusterId")) == cluster
        and (not account or str(row.get("accountName")) == account)
    ]
    if matches:
        return matches[0]

    available = ", ".join(
        f"{row.get('clusterId')}:{row.get('accountName')}" for row in accounts
    )
    raise RuntimeError(f"no bound account matched {cluster}:{account}; available: {available}")


def verify_account(session, token, cluster):
    data = request_json(
        session,
        "POST",
        f"{BASE_URL}/clusters/{quote(cluster)}/account/password/verify",
        headers={"PARA_ATOKEN": token},
        timeout=METADATA_REQUEST_TIMEOUT,
    )
    if not data.get("success"):
        raise RuntimeError(f"account verify request failed: {data}")
    return data.get("data") or {}


def get_certificate(session, token, cluster, account):
    data = request_json(
        session,
        "GET",
        f"{PORTAL_ORIGIN}/apis/certificate/gen",
        headers={"PARA_ATOKEN": token},
        params={"clusterId": cluster, "user": account},
        timeout=METADATA_REQUEST_TIMEOUT,
    )
    if not data.get("success") or not data.get("data", {}).get("token"):
        raise RuntimeError(f"certificate request failed: {data}")
    return data["data"]["token"]


def build_sftp_url(cluster, account, certificate, proxy, home):
    return f"sftp://{cluster},{account}:{certificate}@{proxy}{home}/"


def print_result(info, show_secret):
    print(f"portal_user: {info['portal_user']}")
    print(f"cluster: {info['cluster']}")
    print(f"account: {info['account']}")
    print(f"ssh_proxy: {info['proxy']}")
    print(f"home: {info['home']}")
    print(f"certificate_token: {info['certificate'] if show_secret else redact_secret(info['certificate'])}")
    print(f"sftp_url: {info['sftp_url'] if show_secret else redact_sftp_url(info['sftp_url'])}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch the BJTU HPC WinSCP/SFTP connection URL used by the web desktop."
    )
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER)
    parser.add_argument("--auth-account", default=os.getenv("HPC_AUTH_ACCOUNT"), help="Saved auth account name from hpc_accounts.py")
    parser.add_argument("--token", default=os.getenv("HPC_PARA_ATOKEN"))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--refresh-token", action="store_true")
    parser.add_argument("--refresh-browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument("--refresh-headless", action="store_true")
    parser.add_argument(
        "--show-secret",
        action="store_true",
        help="Print the full one-time certificate token and SFTP URL.",
    )
    return parser.parse_args()


def load_auth(args):
    token_file = args.token_file.expanduser()
    auth_account = getattr(args, "auth_account", None)
    token = load_token(args.token, token_file, auth_account=auth_account)
    if args.refresh_token:
        token = refresh_token(token_file, args.refresh_browser, args.refresh_headless, auth_account=auth_account)
    if not token:
        print(
            f"Missing token. Run: {os.getenv('HPC_PYTHON', 'python3.12')} hpc_refresh_token.py "
            f"or retry with --refresh-token. "
            f"You can also set HPC_PARA_ATOKEN / --token / --token-file {args.token_file}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return token


def run(args, token):
    session = create_session()
    user = get_self(session, token)
    settings = get_system_settings(session, token)
    proxy = settings.get("sshProxyIp") or ""
    if not proxy:
        raise RuntimeError("sshProxyIp is not configured in /pcp/systemsettings")

    accounts = get_bound_accounts(session, token)
    account_row = select_account(accounts, args.cluster, args.account)
    account = account_row["accountName"]
    home = account_row["home"]

    verify = verify_account(session, token, args.cluster)
    if verify.get("success") is False:
        raise RuntimeError(f"cluster account requires password verification: {verify}")

    certificate = get_certificate(session, token, args.cluster, account)
    return {
        "portal_user": user.get("userName") or args.portal_user,
        "cluster": args.cluster,
        "account": account,
        "proxy": proxy,
        "home": home,
        "certificate": certificate,
        "sftp_url": build_sftp_url(args.cluster, account, certificate, proxy, home),
    }


def main():
    args = parse_args()
    apply_auth_account_defaults(
        args,
        default_cluster=DEFAULT_CLUSTER,
        default_account=DEFAULT_ACCOUNT,
        default_portal_user=DEFAULT_PORTAL_USER,
    )
    token = load_auth(args)
    try:
        info = run(args, token)
    except RuntimeError as error:
        if str(error) == AUTH_ERROR_MESSAGE:
            token = refresh_token(
                args.token_file.expanduser(),
                args.refresh_browser,
                args.refresh_headless,
                auth_account=args.auth_account,
            )
            info = run(args, token)
        else:
            raise

    print_result(info, args.show_secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
