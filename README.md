# BJTU HPC Codex Skill

Sanitized Codex skills for operating a BJTU-like HPC portal workflow from local helper scripts.

The skills cover:

- Portal token refresh with Playwright.
- Saved multi-account auth.
- Captcha-only login when local credentials are stored outside Git.
- Token Guardian background validation and headless refresh after an initial visible CAS login.
- Fast native queue summaries across saved accounts, portal job listing, native Slurm pending-reason checks, and post-submit evidence collection.
- Optional macOS menu bar monitor and compact desktop widget for queue and GPU-node status.
- Stable dataset layout and cross-account dataset reuse.
- Safe GPU job submission patterns for single-GPU and packed Slurm jobs, including monitor-snapshot resource selection, exact native Slurm preflight, ordinary `1GPU/6CPU` requests, resource-wait `1GPU/4CPU` fallback, low-VRAM GPU-sharing, GPU-fill fragment jobs, and 2GPU-to-1GPU compatibility fallback.

## Skills

- `skills/bjtu-hpc/SKILL.md`: general BJTU HPC workflow and operational guardrails.
- `skills/bjtu-hpc-submit/SKILL.md`: tool-first submit/status/auth workflow for agents.
- `skills/bjtu-hpc-submit/mac_hpc_monitor/`: sanitized optional macOS monitor/widget scripts.
- `skills/bjtu-hpc/references/`: split reference files for auth/dashboard, data transfer, GPU scheduling, inspection, guardrails, and validated platform notes.

## Sanitization

This repository intentionally replaces site-specific or private values with placeholders:

- `<SLURM_DIR>`: local helper-script workspace.
- `<PROJECT_DIR>`: local project workspace.
- `<PYTHON3>`: local Python interpreter used for helper scripts.
- `<portal_user_main>` / `<portal_user_other>`: portal login usernames.
- `<cluster_account_main>` / `<cluster_account_other>`: cluster OS accounts.
- `<dataset_name>`: stable dataset directory name.
- `<proxy_host>:<proxy_port>`: temporary portal SSH/SFTP proxy endpoint.

Do not commit portal tokens, cookies, temporary SSH certificates, passwords, personal paths, browser profiles, or real account IDs.

## Recommended Auth Flow

For an expired token, run the integrated refresh command rather than only reporting the error:

```bash
cd "<SLURM_DIR>"
"<PYTHON3>" hpc_refresh_flow.py <auth_account> --visible-only
```

If a visible Playwright login times out, or the user closes the browser but the helper still waits, first recover from the same Playwright profile headlessly:

```bash
cd "<SLURM_DIR>"
"<PYTHON3>" hpc_accounts.py refresh <auth_account> \
  --browser playwright --headless --fresh-page --timeout 30 --sync-legacy-token
"<PYTHON3>" hpc_accounts.py validate <auth_account>
```

Only reopen the visible browser if profile capture and validation still fail.

## Token Guardian

After one visible CAS login has populated the account Playwright profile, the dashboard Token Guardian can keep saved account tokens warm:

```bash
cd "<SLURM_DIR>"
"<PYTHON3>" hpc_dashboard_service.py install --guardian-accounts all
"<PYTHON3>" hpc_dashboard_service.py status
```

The guardian should validate saved accounts on a schedule, refresh headlessly with `--clear-existing-token` when a token becomes stale or invalid, and use a 5-day token-age warning as pre-expiry maintenance. A token-age warning is not proof that the token is invalid. The guardian should mark an account as needing visible login when CAS/OAuth can no longer complete without a captcha. It must never print token, password, cookie, browser-storage, or temporary certificate values.

## Dataset Layout

Use one stable dataset root per dataset and keep transfer artifacts separate:

```text
/data/home/<cluster_account>/dataset/<dataset_name>/
/data/home/<cluster_account>/dataset/_uploads/<dataset_name>/
/data/home/<cluster_account>/dataset/_archives/<dataset_name>/
/data/home/<cluster_account>/dataset/_manifests/<dataset_name>_manifest.json
```

Training configs should point to the canonical dataset root, not `_uploads`, `_archives`, or a temporary extraction directory.

## License

MIT.
