---
name: bjtu-hpc
description: "BJTU HPC portal workflow for a local helper workspace: configure a Python 3.12 controller, migrate saved account metadata/secrets through passphrase-encrypted private JSON, refresh/save portal tokens, inspect native quota and queue state, upload/download only with authorization, and run one or many anonymized native Slurm tasks through a durable refresh-gated submit cycle with exact-script byte binding, receipts, verification, and crash-safe reconciliation."
---

# BJTU HPC

Use the helper scripts in the local `<SLURM_DIR>` workspace as the canonical interface to the BJTU HPC portal. Use the bundled `scripts/` directory to bootstrap account storage, credential management, encrypted migration, token refresh, and identity validation on a new controller.

## Read First

Controller runtime contract: use a Python 3.12 interpreter with Paramiko for every local helper command. Set `HPC_PYTHON=<PYTHON3.12>` and never fall back to an older system `python3`.

Treat live helper output (`hpc_doctor.py --json`, `hpc_accounts.py`, `hpc_queue_summary.py --json`, monitor snapshots, and helper `--help` defaults) as authoritative for auth, queue state, account caps, and resources. New evidence-producing work uses direct-start admission: default to at most two current-project running/nonterminal jobs per independent account pool, keep queued follow-up disabled, and refresh after every physical submit. A current-project `PENDING` job blocks only its account/pool unless live Slurm evidence identifies a shared QOS/user limit.

For any live HPC or remote GPU work, start read-only unless the user explicitly asked to submit, cancel, delete, reserve, chmod, upload, or otherwise mutate state.

Never place portal tokens, cookies, CAS passwords, temporary certificate tokens, account migration packages, decryption passphrases, or raw credential material in Git, logs, skill files, or chat.

## Reference Index

Load only the references needed for the task:

- `references/environment_setup.md`: Python 3.12 setup plus scrypt/AES-256-GCM account JSON export/import and decryption.
- `references/auth_dashboard.md`: token refresh, saved accounts, CAS credential prefill, dashboard, Guardian, and SSH/SFTP proxy discovery.
- `references/submit_cycle.md`: one/multi-task controller, exact-script binding, rolling snapshots, receipts, and crash recovery.
- `references/data_transfer.md`: uploads/downloads, reusable dataset layout, cross-account access, and transfer safety.
- `references/gpu_scheduling.md`: native Slurm GPU submission and resource-shape rules.
- `references/job_inspection.md`: portal/native queue inspection and pending-reason diagnosis.
- `references/guardrails.md`: credential, submission, data, and mutation safety.
- `references/hpc_workflow.md`: validated platform notes that may require fresh local verification.

## Core Commands

```bash
export SLURM_DIR="<SLURM_DIR>"
export HPC_PYTHON="<PYTHON3.12>"
export HPC_ACCOUNT_TOOLS="<SKILL_DIR>/scripts"
cd "$SLURM_DIR"

"$HPC_PYTHON" "$HPC_ACCOUNT_TOOLS/hpc_accounts.py" list
"$HPC_PYTHON" "$HPC_ACCOUNT_TOOLS/hpc_accounts.py" export-json <private.json> \
  --include-tokens --include-credentials
"$HPC_PYTHON" "$HPC_ACCOUNT_TOOLS/hpc_accounts.py" import-json <private.json> \
  --use-exported-default --sync-legacy-token
"$HPC_PYTHON" hpc_queue_summary.py --details
"$HPC_PYTHON" hpc_submit_cycle.py validate --manifest <manifest.json>
"$HPC_PYTHON" hpc_submit_cycle.py run --manifest <manifest.json>
"$HPC_PYTHON" hpc_submit_cycle.py run --manifest <manifest.json> --submit
```

Install the bundled account-tool dependencies from `scripts/requirements.txt`. The bundled identity path is read-only and deliberately excludes the broader upload CLI.

Use `--auth-account NAME` for multi-account work. Prefer `hpc_queue_summary.py` because it queries native Slurm state through the portal SSH proxy and can catch jobs omitted from portal rows.

## Account Migration

Metadata-only `export-json` is plaintext and excludes token/CAS secrets. `--include-tokens` or `--include-credentials` must automatically encrypt the complete JSON payload with a user-entered passphrase. The portable envelope uses scrypt for a 256-bit key and AES-256-GCM authenticated encryption; the passphrase is never accepted as a command-line argument or written into the envelope.

Import requires a regular non-symlink file with mode `0600`. It authenticates/decrypts in memory, validates the inner schema and digest, then applies one explicit conflict policy. Default `error` makes no changes if an alias exists; `skip` preserves existing aliases; `replace` is explicit. Never export browser profiles or cookies. After import, validate every token and use visible account-local Playwright refresh for expired identities.

## Submission Essentials

For prepared single or multi-task work, prefer `hpc_submit_cycle.py` over manually chaining helper calls. Default `run` is no-submit planning; real work requires explicit `--submit` and one durable intent per candidate. The controller consumes only the fresh planner `next_action`, submits one physical job or compliant array, verifies it, refreshes, and replans.

Freeze a separate account-specific script and intent for every candidate. Before durable `submitting`, rehash both files. The native helper reads each local file once, uploads the exact script bytes losslessly to a content-addressed remote path, verifies the remote SHA before both `sbatch --test-only` and real `sbatch`, records local/remote script and intent hashes in the receipt, and verifies that `scontrol Command` matches the checked remote script path. Any mismatch fails closed and is not retried.

Every real GPU submission runs local/remote syntax checks, `sbatch --test-only`, real `sbatch`, immediate `scontrol` shape/command verification, and a fresh queue snapshot. Unknown outcomes require trace-based reconciliation; they never authorize a duplicate submit.

Default evidence-producing shape is one seed/process per Slurm allocation boundary with `--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding`. Use a 4-CPU fallback only when exact-script preflight shows a direct resource-shape constraint. Do not pack independent seeds into background child processes or create speculative queued follow-up work.

Use anonymous per-submission trace ids such as `hpc_<12-16hex>` for Slurm-visible job names, logs, run/output basenames, and public evidence. Keep reversible account mappings only in a private mode-`0600` local ledger.

## Auth And Status

Use `hpc_accounts.py` as the saved-account source of truth and treat the legacy token file as compatibility only. If a requested status/upload/submit operation is blocked by an expired-token code or HTTP `401`, run the integrated visible refresh for the affected alias unless the user explicitly forbids browser/token refresh.

For current queue/running slots/pending reasons, run `hpc_queue_summary.py --details` first. Report running, pending, other, total, available run/cap slots, and exact pending reasons per account without printing credentials or private identifiers.
