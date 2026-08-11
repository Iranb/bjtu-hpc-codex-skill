# BJTU HPC Codex Skill

Sanitized Codex skills for operating a BJTU-like HPC portal workflow from local helper scripts.

The skills cover:

- Portal token refresh with Playwright.
- Saved multi-account auth.
- Passphrase-encrypted JSON account migration with in-memory authenticated decryption.
- Captcha-only login when local credentials are stored outside Git.
- Token Guardian background validation and headless refresh after an initial visible CAS login.
- Fast native queue summaries across saved accounts, portal job listing, native Slurm pending-reason checks, and post-submit evidence collection.
- Redacted Windows 10 desktop and Windows 11 Widgets Board components, plus the optional macOS monitor/widget, for queue and GPU-node status.
- Stable dataset layout and cross-account dataset reuse.
- Refresh-gated native Slurm admission with exact-script byte binding, durable receipts, post-submit verification, and crash-safe reconciliation.

## Skills

- `skills/bjtu-hpc/SKILL.md`: general BJTU HPC workflow and operational guardrails.
- `skills/bjtu-hpc/scripts/`: sanitized portable mirror of the locally installed controller helpers, including account/auth, transfer, queue, planning, native submission, submit-cycle, data-supply, MCP, Widget snapshot, schemas, and their Python requirements.
- `skills/bjtu-hpc/assets/windows-widget/`: canonical Windows widget source locked to the current cross-platform HPC widget generation.
- `skills/bjtu-hpc-submit/SKILL.md`: tool-first submit/status/auth workflow for agents.
- `skills/bjtu-hpc/references/`: split reference files for auth/dashboard, data transfer, GPU scheduling, inspection, guardrails, and validated platform notes.
- `skills/bjtu-hpc/references/environment_setup.md`: Python 3.12 bootstrap and encrypted account migration/decryption.
- `LOCAL_SYNC.md`: local-first source scope, required redaction overlay, exclusions, and publication checks.

The local installed skills and the local `slurm` helper workspace are the source
of truth. This public mirror keeps their operational behavior while applying a
small portability and privacy overlay: controller paths and identities become
environment variables/placeholders, private state is excluded, and commands
that formerly depended on a private dataset inventory require explicit input.
The Apple-native UI and Kindle implementation remain in their dedicated local
repositories. This repository includes their BJTU helper-side contracts and
redacted Widget snapshot adapter, plus the independent canonical Windows UI.

## Portable Controller Tools

```bash
export HPC_PYTHON="<PYTHON3.12>"
export HPC_TOOLS="$PWD/skills/bjtu-hpc/scripts"

"$HPC_PYTHON" -m pip install -r "$HPC_TOOLS/requirements.txt"
"$HPC_PYTHON" -m playwright install chromium
"$HPC_PYTHON" "$HPC_TOOLS/hpc_doctor.py" --json --no-validate
"$HPC_PYTHON" "$HPC_TOOLS/hpc_accounts.py" list
"$HPC_PYTHON" "$HPC_TOOLS/hpc_credentials.py" list
"$HPC_PYTHON" "$HPC_TOOLS/hpc_queue_summary.py" --details
"$HPC_PYTHON" "$HPC_TOOLS/hpc_submit_cycle.py" validate --manifest <manifest.json>
"$HPC_PYTHON" "$HPC_TOOLS/hpc_native_submit.py" ./candidate.sbatch \
  --auth-account <auth_account> --expected-gpus 1 \
  --expected-ntasks 1 --expected-cpus-per-task 6
```

## Sanitization

This repository intentionally replaces site-specific or private values with placeholders:

- `<SLURM_DIR>`: local helper-script workspace.
- `<PROJECT_DIR>`: local project workspace.
- `<PYTHON3>`: local Python interpreter used for helper scripts.
- `<portal_user_main>` / `<portal_user_other>`: portal login usernames.
- `<cluster_account_main>` / `<cluster_account_other>`: cluster OS accounts.
- `<dataset_name>`: stable dataset directory name.
- `<proxy_host>:<proxy_port>`: temporary portal SSH/SFTP proxy endpoint.
- `<CONTROLLER_HOME>` / `<SLURM_DIR>`: private local paths that must be configured outside Git.
- `<SOURCE_HOST>` / `<SOURCE_USER>`: private data-source infrastructure values.

Do not commit portal tokens, cookies, temporary SSH certificates, passwords, personal paths, browser profiles, or real account IDs.

Generated account stores, transfer-task JSON, snapshots, receipts, intents,
private trace ledgers, logs, keys, certificates, and migration envelopes are
excluded by `.gitignore` and must remain outside the repository.

## Recommended Auth Flow

For an expired token, run the integrated refresh command rather than only reporting the error:

```bash
"<PYTHON3>" "<SKILL_DIR>/scripts/hpc_refresh_flow.py" \
  <auth_account> --visible-only
```

If a visible Playwright login times out, or the user closes the browser but the helper still waits, first recover from the same Playwright profile headlessly:

```bash
"<PYTHON3>" "<SKILL_DIR>/scripts/hpc_accounts.py" refresh <auth_account> \
  --browser playwright --headless --fresh-page --timeout 30 --sync-legacy-token
"<PYTHON3>" "<SKILL_DIR>/scripts/hpc_accounts.py" validate <auth_account>
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
