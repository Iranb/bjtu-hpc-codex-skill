"""Fail-closed identity checks for BJTU HPC portal tokens."""

from hpc_account_store import AccountStoreError


PORTAL_ORIGIN = "https://hpc.bjtu.edu.cn"
PORTAL_PCP_URL = f"{PORTAL_ORIGIN}/pcp"


def create_session():
    from hpc_upload import create_session as make_session

    return make_session()


def get_portal_self(session, token):
    from hpc_upload import request_json

    return request_json(
        session,
        "GET",
        f"{PORTAL_ORIGIN}/as/user/self",
        headers={"PARA_ATOKEN": token},
    )


def get_bound_accounts(session, token):
    from hpc_upload import request_json

    data = request_json(
        session,
        "GET",
        f"{PORTAL_PCP_URL}/clusters/accounts/user/binds/page",
        headers={"PARA_ATOKEN": token},
    )
    if not data.get("success"):
        raise AccountStoreError("could not verify token-bound cluster accounts")
    return data.get("data") or []


def format_bound_accounts(rows):
    return ", ".join(
        f"{row.get('clusterId')}:{row.get('accountName')}" for row in rows
    ) or "-"


def select_discovered_account(rows, cluster, preferred_account):
    cluster_rows = [
        row for row in rows if not cluster or str(row.get("clusterId") or "") == str(cluster)
    ]
    if preferred_account:
        for row in cluster_rows:
            if str(row.get("accountName") or "") == str(preferred_account):
                return row
        expected = f"{cluster or '*'}:{preferred_account}"
        raise AccountStoreError(
            "token identity mismatch: "
            f"expected {expected}; available: {format_bound_accounts(rows)}"
        )

    if len(cluster_rows) == 1:
        return cluster_rows[0]
    if not cluster_rows:
        raise AccountStoreError(
            "token identity could not be resolved: "
            f"no bound account matched cluster={cluster or '*'}; "
            f"available: {format_bound_accounts(rows)}"
        )
    raise AccountStoreError(
        "token identity is ambiguous: multiple bound accounts matched "
        f"cluster={cluster or '*'}; available: {format_bound_accounts(cluster_rows)}"
    )


def verify_token_identity(name, token, entry, *, cluster=None, account=None):
    """Return verified portal metadata or raise before any token is persisted."""
    try:
        session = create_session()
        user = get_portal_self(session, token)
        rows = get_bound_accounts(session, token)
    except AccountStoreError:
        raise
    except Exception as error:
        raise AccountStoreError(f"token identity verification failed: {error}") from error

    actual_portal_user = str(user.get("userName") or "").strip()
    if not actual_portal_user:
        raise AccountStoreError(
            f"token identity for auth account {name} did not include portal_user"
        )

    expected_portal_user = str(entry.get("portal_user") or "").strip()
    if expected_portal_user and actual_portal_user != expected_portal_user:
        raise AccountStoreError(
            f"token identity mismatch for auth account {name}: "
            f"expected portal_user={expected_portal_user}, actual={actual_portal_user}"
        )

    expected_cluster = str(cluster or entry.get("cluster") or "").strip()
    expected_account = str(account or entry.get("account") or "").strip()
    selected = select_discovered_account(rows, expected_cluster, expected_account)
    actual_cluster = str(selected.get("clusterId") or "").strip()
    actual_account = str(selected.get("accountName") or "").strip()
    if not actual_cluster or not actual_account:
        raise AccountStoreError(
            f"token identity for auth account {name} did not include a cluster account"
        )

    return {
        "portal_user": actual_portal_user,
        "cluster": actual_cluster,
        "account": actual_account,
    }
