---
name: bjtu-hpc-submit
description: Use when an agent needs to refresh/save BJTU HPC auth, inspect native storage/queue state, upload only with authorization, preflight native sbatch runability, or submit one or many anonymized Slurm Job Array/independent 1GPU tasks through a durable refresh-gated cycle with manifest validation, rolling snapshots, cycle-local connection reuse, receipts, task-level verification, and crash-safe reconciliation.
---

# BJTU HPC Submit

Tool-first workflow for BJTU HPC portal work from the `slurm` helper workspace. Human step-by-step usage is in workspace `Manual.md`; detailed history and experiment notes live in `AGENTS.md` and `Readme.md`.

## Runtime Defaults

- Work from the helper workspace unless the project says otherwise: `<SLURM_DIR>`.
- On this Codex controller, use `<PYTHON3.12>` for all helper scripts. The system `python3` is unsupported. Use this shell prefix in examples:

```bash
PY=<PYTHON3.12>
SLURM_DIR="<SLURM_DIR>"
PROJECT_DIR="/path/to/current/project"
PROJECT_SLUG="project_slug"
PLAN_HASH="89abcdef0123"
TRACE_HASH="0123456789ab"
TRACE_ID="hpc_${TRACE_HASH}"
```

- Save local HPC evidence under `$PROJECT_DIR/hpc_evidence/`. Run status commands with `cwd=$PROJECT_DIR` when possible so `hpc_pending_reason.py` writes snapshots next to the project evidence.
- Use a neutral, lowercase `PROJECT_SLUG` for the remote project root and private trace metadata. Use an anonymous `TRACE_ID` for job names, queue keywords, per-job remote run/log/output basenames, and Git-safe evidence filenames. Avoid legacy framework-specific folder or job-prefix names for new HPC work.
- Before every evidence-producing submit, generate a stable `PLAN_HASH` for deduplicating planned work, a stable launch identity hash for reusable validation, and a per-submission `TRACE_ID=hpc_<12-16hex>`, then append a private mapping record to `$PROJECT_DIR/hpc_evidence/private/hpc_trace_ledger.jsonl` with mode `0600`. The reversible mapping may include saved auth-account alias, cluster user id, project/profile, seed, resource shape, script checksum, Slurm job id, and minimal `launch_identity` fields. Do not commit or paste this ledger.
- Do not copy saved auth-account aliases, cluster usernames, portal usernames, emails, real person names, or credential labels into `#SBATCH --job-name`, Slurm output/error basenames, remote `runs/`, `logs/`, `outputs/` per-job basenames, local evidence filenames, or queue keywords. Hash-based queue names are allowed and preferred.
- Runtime environment policy for new GPU training: never use `pytorch1.7-python3.8` or `/data/apps/anaconda/anaconda3/envs/pytorch1.7-python3.8/bin/python`. Prefer an account-local Python `3.10` environment such as `/data/home/<account>/envs/torch251-cu121-py310` with PyTorch `2.5.1+cu121`, which is the closest validated PyTorch CUDA wheel to the BJTU CUDA `12.0`/driver `525.105.17` stack. Use PyTorch `2.5.1+cu118` only as a recorded GPU-node smoke-test fallback. Use `module purge && module load PyTorch-GPU` only as a recorded fallback when the account-local environment is unavailable or fails and the platform module passes GPU-node smoke test. Do not point one account's jobs at another account's `/data/home/.../envs` path.
- Seed cap policy: one experiment family means one dataset/config/method profile
  and may use at most three unique random seeds across jobs, arrays, accounts,
  HPO, and retries. Retries reuse their original seed. If all three seeds fail,
  treat effectiveness as doubtful; do not generate a fourth seed to find a lucky
  run.
- Dataset naming and staging policy: new AutoResearch runs use the project `DATA_SUPPLY_CONTRACT.json`. In `external_hdf5_enforced`, submit only when the row, capability passport, receipt-bound native verify report, backend preflight and submit intent bind the same artifact schema plus semantics, source-inventory, build, converter, content, consumer and validator hashes and detached attestation/report. HPC stores one immutable owner copy at `/data/home/<owner>/autoreskill_data/artifacts/<artifact_content_sha256>/`; raw trees, tar archives and silent account-local copies are prohibited. Runtime uses `bjtu_stage_hdf5_artifact_to_shm`, keyed by the full artifact hash plus exact READY-file hash, copies only manifest-declared members and the selected attestation, and may fall back only to that verified owner root. The archive/packed dataset-name cache policy remains legacy-only.
- Persistent symlink prohibition: do not create or consume project-managed symlinks for code, datasets, checkpoints, weights, environments, manifests, configs, runs, logs, outputs, or artifacts. Cross-account jobs must use the verified owner's direct absolute path plus minimal read-only/traverse ACLs, or a verified physical copy when adjacent writes are unavoidable. Before transfer, capability admission, and `sbatch --test-only`, audit every declared root and required file with `lstat`, `readlink`, and `realpath`; reject persistent symlinks and registered-root escapes. The only project-managed exception is node-local disposable dataset/cache data when both link and resolved target are under `/dev/shm/bjtu_data_artifacts/` or `/dev/shm/bjtu_dataset_cache/` after allocation-side identity/readiness checks. A persistent path pointing into `/dev/shm` is not allowed.
- Repeated-variant launch optimization: when an existing manifest, run record, or private trace ledger for the same project/account identifies the same code export, dataset/profile manifest, runtime environment, launcher template, and resource shape, treat data/env/code validation as reusable evidence for a seed or hyperparameter-only follow-up. Do not rerun full dataset conversion, environment discovery, code upload audit, broad queue sync, or human wiki/status rewrite just to submit that repeated variant.
- For repeated-variant reuse, prefer a `launch_identity` object or equivalent private trace fields covering code export ref/hash, dataset profile and manifest ref/hash, runtime environment ref/probe, launcher template hash, resource shape, method profile, and data backend. The stable identity hash excludes the seed and hyperparameters being intentionally varied. `PLAN_HASH` alone is not enough because it may include seed or array identity.
- Never cache volatile submit safety. Every real GPU submit, including a repeated-variant launch, still requires fresh queue/resource evidence for the selected account, a new anonymous trace id, the exact generated sbatch script, local/remote syntax checks, `sbatch --test-only`, real submission only after passing preflight, and post-submit `scontrol` allocation verification. If any stable element changed or the manifest evidence is missing, fall back to first-use checks.
- For AutoResearch placement, a project capability passport proves only stable
  code/data/checkpoint/runtime/path compatibility for an exact execution
  profile. Live Slurm snapshots and exact-script preflight still own volatile
  launchability. A failed path/hash/runtime preflight invalidates only the
  implicated capability components; idle GPU state alone never proves fit.

Policy authority and drift checks: live helper output (`hpc_doctor.py --json`, `hpc_accounts.py`, `hpc_queue_summary.py --json`, monitor/widget snapshots, and helper `--help` defaults) is authoritative for current state. New evidence-producing experiments use direct-start admission: `max_project_running_per_account=2` and `max_hpc_admissions_per_cycle=8` by default, with queued follow-up disabled. A current-project `PENDING` job blocks its account/resource pool, not every monitored account. Propagate a block only through an explicit live `shared_limit_ref` or shared QOS/user-cap rejection. The queue helper marks its anonymized per-user ref blocked only for exact per-user Slurm limit reasons; ordinary `Priority`, `Resources`, or account-local pending never justify propagation. Every physical submit requires its own fresh snapshot, exact-script preflight, submit, verification, and refresh; the cycle cap never authorizes stale batch submission. Do not use older fill-to-cap or global-pending-stop examples. Before editing this policy, scan both `bjtu-hpc` and `bjtu-hpc-submit` for `--cap`, `run-slots`, `admission-mode`, `max-admissions-per-cycle`, `queued follow-up`, `allow-queued`, and `QOSMaxJobsPerUserLimit`, then update both skills and helpers consistently.

## Entry Points

- Always start from the helper workspace with `cd "$SLURM_DIR" && "$PY" hpc_doctor.py --json`; it checks dependencies, account state, browser profile, and token validity without printing secrets.
- If dependencies are missing, run `cd "$SLURM_DIR" && "$PY" -m pip install -r requirements.txt` and `"$PY" -m playwright install chromium`.
- For agent-driven portal-app jobs, prefer `cd "$SLURM_DIR" && "$PY" hpc_submit_verified.py ./script.py --submit --json` over raw `hpc_submit.py`.
- For CPU/GRES-sensitive jobs, prefer uploaded native `sbatch` scripts over the portal PyTorch app, then verify with native Slurm.
- For one or more already prepared native tasks, prefer `cd "$SLURM_DIR" && "$PY" hpc_submit_cycle.py validate --manifest <manifest.json>`, followed by `run --manifest <manifest.json>` for no-submit live planning. Use `run --manifest <manifest.json> --submit` only with explicit submission authority and a durable intent for every account-specific candidate. The cycle reuses portal/SSH setup but never reuses volatile safety evidence: each physical job or atomic array consumes one fresh `next_action`, performs exact path/script checks, writes its receipt, verifies native shape, refreshes, and replans. A batch command is not batch authorization. Read `<BJTU_HPC_SKILL_DIR>/references/submit_cycle.md` for manifest, resume, and reconciliation details.
- Before each native GPU submission, run `cd "$SLURM_DIR" && "$PY" hpc_plan_from_snapshot.py --admission-mode direct-start --max-admissions-per-cycle 8 --cap 2 --run-slots 2 --workload single --no-queued --planner-json`. It captures one bounded queue/resource snapshot and plans from that same file. Treat only `next_action` as eligible for the current snapshot; `admission_frontier[1:]` requires refresh and replanning. Skip accounts with current-project `PENDING` or two current-project `RUNNING` jobs, but continue independent accounts unless live evidence identifies a shared blocked limit. Submit and verify one compliant job or array, refresh, and rerun before the next admission. A controller cycle may repeat this sequence up to eight times; it must never submit the stale frontier as a batch. If the helper is unavailable, pass the same `--admission-mode direct-start --max-admissions-per-cycle 8 --cap 2 --run-slots 2 --workload single --no-queued` arguments to `hpc_resource_planner.py --queue-json ...`. Plain `queue_probe` remains non-submittable. Use `--submit-mode batch` only for legacy dry-run planning.
- For AutoResearch `admission_scope=global`, accept only the current hashed first
  assignment as upstream project-admission evidence. It is not Slurm launch
  permission. Submit exactly one job only after the normal BJTU live refresh,
  exact-script checks, `sbatch --test-only`, and post-submit verification; then
  refresh BJTU state and require a new global schedule before another submit.
- For every AutoResearch submit, require the queue row to be durably
  `submitting` first. Bind its intent to the exact script hash, launch identity,
  trace id, allocation, and queue revision; pass that intent and a private
  receipt path to `hpc_native_submit.py`. Record backend acceptance as
  `needs_sync`, and set `running` only from authoritative Slurm observation. On
  recovery, search by trace/script/launch identity before any retry.
- For MCP clients, prefer `hpc_auth_status`, `hpc_submit_and_verify`, `hpc_pending_reason`, `hpc_verify_slurm_allocation`, `hpc_tail_stdout`, and `hpc_get_sftp_info` from `hpc_mcp_server.py`.

Useful project status commands:

```bash
cd "$PROJECT_DIR"
"$PY" "$SLURM_DIR/hpc_jobs.py" list --keyword "$TRACE_ID" --size 30 --paths
"$PY" "$SLURM_DIR/hpc_jobs.py" list --keyword "$TRACE_ID" --size 30 --paths --json > "hpc_evidence/bjtu_${TRACE_ID}_jobs_YYYYMMDD_HHMM.json"
"$PY" "$SLURM_DIR/hpc_pending_reason.py" <slurm_job_id> --no-sinfo
```

## Auth

- Saved accounts live in `~/.bjtu_hpc_accounts.json`; legacy token cache is `~/.bjtu_hpc_token`.
- Treat the saved account store as the source of truth; the legacy file is only a compatibility cache for older scripts. Refresh flows should keep both in sync, and low-level `hpc_refresh_token.py` / Web-dashboard saves now sync the default auth account unless `--no-sync-auth-account` is used. Low-level Playwright refresh also defaults to the selected account profile instead of the older shared `~/.bjtu_hpc_browser` profile.
- If the user explicitly requests a captcha/verification-code-only flow, store CAS login credentials with `hpc_credentials.py set NAME --login-name PORTAL_USER`. The helper writes `~/.bjtu_hpc_credentials.json` with mode `0600`; never write passwords into skill files, AGENTS files, Git-tracked files, logs, or final answers. Saved credentials only pre-fill the CAS username/password in Playwright; the user still enters the captcha/verification code and submits.
- Do not run `hpc_accounts.py import-legacy NAME` over an account that already has a valid token unless you know the legacy file is newer; the command refuses this by default and requires `--force`.
- Select accounts with `--auth-account NAME` or `HPC_AUTH_ACCOUNT=NAME`.
- Never print portal tokens, cookies, temporary certificates, or passwords.
- Treat portal codes `11009`, `11011`, and `11012` as expired/invalid auth.
- Treat portal HTTP `401`, `validate_token` `ConnectionRefusedError` / `URLError`, and missing profile tokens as auth-blocked for user-requested live status until a fresh validation succeeds. Stale snapshots may be reported only as `last trusted`; never present them as current portal state.
- Auth refresh is not an experiment launch. If a BJTU token is expired/invalid during a user-requested portal task, immediately run `hpc_refresh_flow.py NAME --visible-only`; do not ask whether to open Playwright. A "do not launch/start new experiments" request does not block token refresh. Only skip visible Playwright when the user explicitly says not to refresh token, not to open a browser, or to use last-trusted evidence only.

### Auth Recovery State Machine

Recent auth lesson: the smooth path is a single integrated `hpc_refresh_flow.py` command that owns validation, profile probing, optional visible login, and post-login status collection. Do not manually bounce between `hpc_doctor`, `hpc_jobs`, and visible browser attempts unless that command has exited and validation still fails.

1. For routine refreshes when no command is currently blocked, start with the fast path:

```bash
cd "$SLURM_DIR" && "$PY" hpc_refresh_flow.py NAME
```

2. If invalid auth blocks a user-requested status check, progress check, pending-reason check, upload, or submit, run the integrated blocked-task flow in a PTY and keep it running. Do not stop after merely reporting the invalid token. For multi-account status checks, run the same flow for each affected saved account unless the user limits the scope.

```bash
cd "$SLURM_DIR" && "$PY" hpc_refresh_flow.py NAME --visible-only
```

3. For project progress checks, use the post-login status variant so the same command continues after any refresh/login and returns the requested state automatically:

```bash
cd "$PROJECT_DIR"
"$PY" "$SLURM_DIR/hpc_refresh_flow.py" NAME --visible-only \
  --after-jobs-keyword "$TRACE_ID" --after-jobs-size 30 --after-jobs-paths \
  --after-snapshot-dir "$PROJECT_DIR/hpc_evidence" \
  --after-pending-job <job_id> --after-pending-no-sinfo
```

Interpret the integrated command by its output:

- `validate saved token ... ok`: token was already usable. Continue; do not open a browser.
- `refreshed ... headlessly` or `from the existing Playwright profile`: profile recovery succeeded. Continue; do not ask the user to log in.
- `[action] A Playwright Chromium window should open now`: only now ask the user to finish CAS/captcha, wait for the HPC portal home page to load, then close the Playwright window. The helper reads the persisted profile token, validates it, and runs any `--after-*` status commands.
- If saved credentials exist for that auth account, the CAS username/password should already be filled and focus should land on the captcha field; tell the user to enter only the captcha/verification code, submit, wait for the portal home page, then close the window.
- A Playwright/Chromium window that opens and closes almost immediately after a recent successful login is usually normal profile validation. Keep the command running and wait for `[ok]`, the post-login job table, or an explicit validation error.

Operational rules:

- Run the refresh command in a PTY and keep it running while the user logs in. Do not end the turn while this command is active unless the user explicitly asks to pause.
- `--visible-only` does not blindly open a browser. It first validates the saved account token and does a short headless probe of the selected Playwright profile. This is expected; do not describe it as a hang unless the command exits or remains silent beyond the expected timeout.
- If token validation returns `11009`, `11011`, `11012`, HTTP `401`, or an auth transport error, the next action is always the integrated `hpc_refresh_flow.py NAME --visible-only ...` command. Do not ask first.
- If the user explicitly says they can help refresh the token, immediately run the integrated `hpc_refresh_flow.py NAME --visible-only ...` command. Do not wait for a separate confirmation, because the visible window plus captcha is the requested human handoff.
- If the user requests a progress/status check and saved credentials exist, an expired token should lead to a visible Playwright window with username/password pre-filled. Reporting only `11011`/`401` is incomplete unless the user explicitly disallowed refresh/browser use.
- After starting the flow, poll the PTY regularly. If there is no stdout for about 30 seconds, check whether `Google Chrome for Testing` or `hpc_refresh_flow` is running with `pgrep -afil "Google Chrome for Testing|playwright|hpc_refresh_flow"`, then tell the user to switch to that window if needed. Do not screenshot or inspect login pages because they may contain account, CAPTCHA, or token material.
- A visible-browser timeout does not prove that login failed. The user may have completed CAS login and closed the window after the helper missed the completion event, leaving a usable token in the selected Playwright profile. If the command exits with `timed out waiting for token in visible browser`, or the user says the browser windows were closed but the helper is still waiting, first capture the token from that same profile headlessly:

```bash
cd "$SLURM_DIR" && "$PY" hpc_accounts.py refresh NAME \
  --browser playwright --headless --fresh-page --timeout 30 --sync-legacy-token
cd "$SLURM_DIR" && "$PY" hpc_accounts.py validate NAME
```

  If validation succeeds, continue with the originally requested status/upload/submit command. Rerun the integrated `--visible-only` flow only if the headless profile capture and validation still fail.
- Use `--force --visible-only --no-profile-probe-before-visible` only after one integrated attempt exits without a usable token and `hpc_accounts.py validate NAME` still fails, or when the user explicitly requests a visible login window. Do not use this as the first attempt, because it skips the profile-recovery fast path and creates unnecessary login windows.
- If the user says login is done but the command exits without `[ok]` and without the post-login job table, first run the headless profile capture above, then `hpc_accounts.py validate NAME` and the originally requested command. Rerun visible-only once only if validation still reports `11009`, `11011`, `11012`, HTTP `401`, or token-validation transport errors.
- If the second visible attempt still fails to save a usable token, report the auth/token-save failure as the blocker. For project progress checks, keep the job at its latest trusted snapshot and state the exact timestamp of that evidence.
- After any refresh, validate with `cd "$SLURM_DIR" && "$PY" hpc_doctor.py --json` or `cd "$SLURM_DIR" && "$PY" hpc_accounts.py validate NAME`; do not trust browser completion alone.

## Job Rules

- Default single-process GPU shape on `cluster2`: try native `--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding` first.
- Native Slurm equivalent for one GPU: `#SBATCH --ntasks=1`, `#SBATCH --cpus-per-task=6`, `#SBATCH --gres=gpu:1`, `#SBATCH --gres-flags=disable-binding`.
- Every evidence-producing submit must use an anonymous trace-hash job name such as `#SBATCH --job-name=hpc_<trace_hash>`. Reject scripts that place saved account names, cluster usernames, portal usernames, emails, real person names, or credential labels in Slurm-visible names or Git-safe evidence filenames.
- Reject scripts, manifests, and launch identities that bind a persistent project-managed symlink or a path escaping its registered real root. Record the `lstat` type and resolved real path for every experiment-bound code/data/checkpoint/environment/manifest/config/run/log/output/artifact root before `sbatch --test-only`.
- Every GPU sbatch template must log `python`, `torch.__version__`, `torch.version.cuda`, `torch.cuda.is_available()`, CUDA device count, and GPU name before starting training. Reject templates that hide the Python path or hardcode the deprecated PyTorch 1.7 environment.
- For multi-seed runs, use a Slurm Job Array or independent single-GPU sbatch
  jobs, with at most three unique seeds for one experiment family. Each task or
  job runs one seed. Do not use packed/wide manifests, background child
  processes, or a fourth seed. Poor results across the three seeds make the
  method doubtful/unstable and stop seed expansion.
- Before every real GPU training submission, run native pre-submit runability checks through the portal SSH proxy. Use the monitor resource snapshot to choose a compliant `1GPU` shape for the selected account. Start with `1GPU/6CPU` (`--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding`). If that exact script cannot start directly because of `Resources`, reservation constraints, same-node CPU availability, or another resource-shape allocation failure, test `1GPU/4CPU`. Do not lower CPU for pure `Priority`, dependency holds, or `QOSMaxJobsPerUserLimit`. Use CPU-rich `1:8`, `1:12`, or `1:16` shapes only when the user explicitly wants CPU-rich work or snapshot plus test-only proves immediate start without reducing GPU availability. True DDP/multi-GPU single-experiment jobs may request multiple GPUs only when the code actually uses them as one distributed process group; this is not allowed for seed packing.
- New AutoResearch evidence-producing jobs should set `DATA_BACKEND=hdf5` and consume the exact external artifact. Before `sbatch --test-only`, require `data_supply_state=available`, fresh native placement evidence, exact artifact/consumer/attestation hashes, a data lease and a passing implementation-conformance runtime smoke over that same artifact. A missing or mismatched artifact is `data_supply_invalid`; do not submit and do not classify it as a scientific result.
- External-mode scripts source `hpc_shm_cache.sh` and call `bjtu_stage_hdf5_artifact_to_shm "$HPC_DATA_ARTIFACT_ROOT" "$ARTIFACT_CONTENT_SHA256" "$CONSUMER_CONTRACT_SHA256" "$DATA_ATTESTATION_READY" DATA_ARTIFACT_ROOT`. The strict helper uses the artifact hash plus exact READY-file hash as its cache key, copies only manifest-declared HDF5 members and the selected report/READY pair, recomputes all identities and shard hashes before copy and reuse, writes `.ready` last, never overwrites an unverified cache and falls back only to the same verified owner artifact. The long relaxed dataset-name template below is retained solely for legacy scripts; do not copy it into an external-mode sbatch file.
- Request more GPUs only when the code actually uses them.
- Avoid `--gpu 1 --ntasks 8` without `--gres-flags disable-binding`; it has produced `BadConstraints`.
- After every submit, verify the portal job row and native state. If the job is `PENDING`, report Slurm `Reason`, block that account or evidenced shared-limit group, refresh, and continue only with independent eligible pools.
- If CPU/GRES shape matters, verify native `NumCPUs`, `NumTasks`, `CPUs/Task`, and GPU TRES with `scontrol`; portal request fields are not enough.
- If a portal PyTorch-GPU submit requested a multi-CPU shape such as `--cpus-per-task 6` but native `scontrol` reports `NumCPUs=1` or `CPUs/Task=1`, treat the launch as CPU-degraded rather than resource-verified. Keep it only when the user explicitly accepts the risk or the run has already entered useful training; otherwise use the uploaded native `sbatch` path for CPU-sensitive training.
- Do not cancel unrelated jobs. For `QOSMaxJobsPerUserLimit`, inspect existing jobs before canceling anything.
- Always run `sbatch --test-only` for a new native script or a new resource shape before real submission.

Known-good shapes on `cluster2`:

```text
1 GPU single-process default: --ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding
1 GPU resource-wait fallback: --ntasks=1 --cpus-per-task=4 --gres=gpu:1 --gres-flags=disable-binding
True DDP/multi-GPU exception: request the GPU count and CPU shape the DDP code actually uses; not for seed packing
```

## Native Slurm Compliance Jobs

Default launch policy: when a user selects one or more auth accounts for evidence-producing GPU experiments, submit only approved compliant work that is expected to start directly. Use Slurm Job Array or independent `1GPU` sbatch jobs. Do not switch back to packed child processes, wide multi-child allocations, GPU-fill fragments, low-VRAM GPU-sharing, or queued follow-up backlog just to improve apparent utilization.

Use this algorithm before submitting:

```text
for each account/resource pool:
  block pending/auth/cap/resource failures locally
  propagate only a live shared_limit_ref or shared QOS/user-cap failure
  eligible = current-project RUNNING < max_project_running_per_account
approved_work = scientifically admitted, non-duplicate work within seed/compute budgets
frontier = up to max_hpc_admissions_per_cycle eligible account/work pairs
submit_now = the first freshly planned and exact-script-preflighted pair only
after every submit: verify, refresh, and recompute the frontier
```

If running-only cross-account submission is active, accounts with two current-project `RUNNING` jobs are full and accounts with local `PENDING` work are blocked. Other accounts may receive direct-running work after refresh. If submission hits `QOSMaxSubmitJobPerUserLimit` or another cap, stop retries for that account or explicit shared-limit group, record the remaining work, and continue only with independent pools.

Scheduled running-only monitor policy: when the user asks to keep BJTU HPC busy under running-only rules, create or update one scheduled monitor for the project instead of relying on manual status checks or in-thread sleep loops. The monitor wake action is:

1. Run a live native snapshot, usually `cd "$SLURM_DIR" && "$PY" hpc_queue_summary.py --accounts ... --json`, and treat that as authoritative over portal job lists.
2. Sync lightweight terminal results first, then update the experiment index/status artifacts before submitting more work.
3. Mark accounts with current-project `PENDING`/queued work blocked; propagate only explicit shared-limit blocks.
4. Consider independent accounts below `max_project_running_per_account`. Admit only approved non-duplicate work through the normal exact-script gate.
5. Refresh the live queue after every material submission before selecting another job or account. If the new job is `PENDING`, block that account/shared group; if `RUNNING`, count it against that account's cap.
6. If `sbatch --test-only` or real submission hits a submit/job cap, keep remaining work in the plan and stop retrying the affected account/shared group in this cycle.
7. If pending for pure `Priority`, preserve queue position. If pending for `Resources`, reservation pressure, or same-node CPU pressure, use the documented `1GPU/4CPU` fallback only for future direct-running candidates or for an explicitly authorized same project/seed/parameters replacement.
8. Recompute the next monitor interval from live state and record the selected interval and reason in the status artifact.

Monitor resource-state policy: before choosing resource parameters for a new running-only candidate or authorized pending-job replacement, use the same live snapshot consumed by the macOS desktop widget/menu bar monitor. Prefer the latest `hpc_queue_summary.py --json` payload, and inspect `checked_at_local`, `cluster_resources.summary`, `cluster_resources.nodes`, `cluster_resources.excluded_reserved_nodes`, account summaries, pending reasons, and each job's native `resources`. Treat this snapshot as the candidate-generation source, not final proof; `sbatch --test-only` and post-submit `scontrol` remain the authority.

Refresh-gated planner policy: run the direct-start command before each new Slurm job or array. The planner output is advisory, cannot fill an old non-terminal cap, and cannot exceed `max_project_running_per_account`. After submit and verification, refresh and replan. If the job becomes `PENDING`, stop that account/shared group; reject packed/wide/GPU-sharing candidates and continue only with newly planned independent pools.

Resource history ledger: keep recent CPU/GPU request and queue outcomes in `$SLURM_DIR/work/hpc_resource_history.jsonl`. The macOS monitor records changed queue/resource snapshots automatically through `hpc_queue_summary.py --history-log`; for manual updates run:

```bash
cd "$SLURM_DIR" && "$PY" hpc_queue_summary.py --json \
  --history-log work/hpc_resource_history.jsonl \
  >/tmp/bjtu_hpc_queue_summary_current.json
```

Backfill recent native Slurm evidence with:

```bash
cd "$SLURM_DIR" && "$PY" hpc_resource_history.py --backfill-days 14 --summary
```

Use this ledger before optimization or scheduling-policy changes to measure observed shapes, states, pending reasons, submit/start timing, and cluster node CPU/GPU availability. Keep it local and uncommitted; it must not contain portal tokens, cookies, passwords, temporary certificates, or local absolute paths. Prefer trace ids and redacted account tags here; keep reversible account aliases and cluster user ids only in `$PROJECT_DIR/hpc_evidence/private/hpc_trace_ledger.jsonl`.

Pre-submit runability gate:

1. Load or refresh the monitor resource snapshot (`hpc_queue_summary.py --json`) and generate the exact remote sbatch script for one compliant job or one job array.
2. Reject the script if it contains `srun --exclusive`, background child splitting, multiple independent seeds inside one allocation, hardcoded physical `CUDA_VISIBLE_DEVICES`, deprecated PyTorch paths, cross-account env paths, cross-account raw small-file dataset paths, persistent dataset outputs under per-job trace/run directories, persistent project-managed symlinks or registered-root escapes, or account/person identifiers in Slurm-visible names. Permit a symlink only when both its entry and resolved data/cache target are inside the approved allocation-local `/dev/shm` cache roots and identity/readiness checks have passed.
3. For each CPU candidate, update both `#SBATCH --cpus-per-task` and thread limits (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`) before testing.
4. Run `bash -n <script>` and `sbatch --test-only <script>` through the portal SSH proxy before any real `sbatch`.
5. Test `1GPU/6CPU` first. If that cannot run directly because of `Resources`, reservation constraints, node CPU availability, or another resource-shape allocation failure, test `1GPU/4CPU`.
6. Do not lower CPU for pure `Priority`, dependency holds, or `QOSMaxJobsPerUserLimit`.
7. Submit only the highest-scoring compliant candidate that passes this gate. If no candidate passes, stop and report the blocker.

Job Array template:

```bash
#!/usr/bin/env bash
#SBATCH --partition=GPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=disable-binding
#SBATCH --array=<seed_a>,<seed_b>,<seed_c>%<max_concurrent>
#SBATCH --time=12:00:00
#SBATCH --job-name=hpc_<trace_hash>
#SBATCH --output=/data/home/<account>/projects/<project_slug>/logs/hpc_<trace_hash>_%A_%a.out
#SBATCH --error=/data/home/<account>/projects/<project_slug>/logs/hpc_<trace_hash>_%A_%a.err

set -euo pipefail
source /data/home/<account>/envs/torch251-cu121-py310/bin/activate

export DATA_BACKEND="${DATA_BACKEND:-lmdb}"
export DATASET_NAME="${DATASET_NAME:-<dataset_name>}"
export EXPERIMENT_PROFILE="${EXPERIMENT_PROFILE:-<experiment_profile>}"
export PACKED_DATA_ROOT="${PACKED_DATA_ROOT:-/data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>}"
export MIN_SHM_FREE_BYTES="${MIN_SHM_FREE_BYTES:-21474836480}"
export MAX_SHM_STAGE_PCT="${MAX_SHM_STAGE_PCT:-70}"
export SHM_CACHE_ROOT="${SHM_CACHE_ROOT:-/dev/shm/bjtu_dataset_cache}"
export SHM_STRICT_CACHE_CHECK="${SHM_STRICT_CACHE_CHECK:-0}"
export SHM_DATASET_KEY="${SHM_DATASET_KEY:-${DATASET_NAME}}"
export SHM_STAGE_DIR="${SHM_CACHE_ROOT}/${SHM_DATASET_KEY}"
export SHM_SHARED_ACL_USERS="${SHM_SHARED_ACL_USERS:-}"
export SHM_NODE_NAME="${SHM_NODE_NAME:-$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo unknown)}"
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export OPENBLAS_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export NUMEXPR_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"

echo "trace_id=hpc_<trace_hash>"
echo "packed_data_root=${PACKED_DATA_ROOT}"
echo "shm_node=${SHM_NODE_NAME}"
echo "shm_dataset_cache=${SHM_STAGE_DIR}"

cleanup_shm_stage() {
  if [[ -n "${SHM_COPY_TMP:-}" && "${SHM_COPY_TMP}" == /dev/shm/* ]]; then
    rm -rf "${SHM_COPY_TMP}"
  fi
}
trap cleanup_shm_stage EXIT

stage_packed_data_to_shm() {
  local src="$1"
  local src_bytes shm_total shm_avail shm_limit lock_dir lock staged src_manifest_sha staged_manifest_sha
  [[ -e "$src" ]] || { echo "shm_stage=skipped node=${SHM_NODE_NAME} dataset_key=${SHM_DATASET_KEY} reason=missing_source src=$src"; return 0; }

  apply_shm_shared_acl() {
    local path="$1"
    local mode="${2:-recursive}"
    local user acl_spec="" default_acl_spec=""
    [[ -n "$SHM_SHARED_ACL_USERS" && -d "$path" ]] || return 0
    command -v setfacl >/dev/null 2>&1 || { echo "shm_stage=warning reason=setfacl_missing path=$path"; return 0; }
    for user in $(printf '%s\n' "$SHM_SHARED_ACL_USERS" | tr ',:' '  '); do
      [[ "$user" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "shm_stage=warning reason=invalid_acl_user user=$user"; continue; }
      acl_spec="${acl_spec:+$acl_spec,}u:${user}:rwx"
      default_acl_spec="${default_acl_spec:+$default_acl_spec,}d:u:${user}:rwx"
    done
    [[ -n "$acl_spec" ]] || return 0
    if [[ "$mode" == "shallow" ]]; then
      setfacl -m "${acl_spec},m:rwx,${default_acl_spec},d:m:rwx" "$path" 2>/dev/null || echo "shm_stage=warning reason=setfacl_apply_failed path=$path"
    else
      setfacl -R -m "${acl_spec},m:rwx" "$path" 2>/dev/null || echo "shm_stage=warning reason=setfacl_apply_failed path=$path"
      find "$path" -type d -exec setfacl -m "${default_acl_spec},d:m:rwx" {} + 2>/dev/null || echo "shm_stage=warning reason=setfacl_default_failed path=$path"
    fi
  }

  shm_cache_ready() {
    [[ -f "$SHM_STAGE_DIR/.ready" ]] || return 1
    [[ -n "$(find "$SHM_STAGE_DIR" -mindepth 1 ! -name .ready ! -name .source_manifest.sha256 ! -name '.cache_*' -print -quit 2>/dev/null)" ]] || return 1
    [[ -f "$SHM_STAGE_DIR/manifest.json" ]] || echo "shm_stage=warning reason=missing_manifest staged=${SHM_STAGE_DIR}"
    [[ -f "$SHM_STAGE_DIR/validation_report.json" ]] || echo "shm_stage=warning reason=missing_validation_report staged=${SHM_STAGE_DIR}"
    if [[ -f "$src/manifest.json" ]]; then
      src_manifest_sha="$(sha256sum "$src/manifest.json" | awk '{print $1}')"
      staged_manifest_sha="$(cat "$SHM_STAGE_DIR/.source_manifest.sha256" 2>/dev/null || true)"
      if [[ "$src_manifest_sha" != "$staged_manifest_sha" ]]; then
        echo "shm_stage=warning reason=manifest_sha_mismatch staged=${SHM_STAGE_DIR}"
        [[ "$SHM_STRICT_CACHE_CHECK" != "1" ]] || return 1
      fi
    fi
    return 0
  }

  use_shm_cache() {
    export PACKED_DATA_ROOT="$SHM_STAGE_DIR"
    export DATA_ROOT="$SHM_STAGE_DIR"
    export DATA_PACKED_ROOT="$SHM_STAGE_DIR"
    echo "shm_stage=reused node=${SHM_NODE_NAME} dataset_key=${SHM_DATASET_KEY} staged=${SHM_STAGE_DIR}"
  }

  if shm_cache_ready; then
    apply_shm_shared_acl "$SHM_CACHE_ROOT" shallow
    apply_shm_shared_acl "${SHM_CACHE_ROOT}/.locks" shallow
    apply_shm_shared_acl "$SHM_STAGE_DIR"
    use_shm_cache
    return 0
  fi

  lock_dir="${SHM_CACHE_ROOT}/.locks"
  umask 0002
  mkdir -p "$lock_dir" "$(dirname "$SHM_STAGE_DIR")"
  chmod 1777 "$SHM_CACHE_ROOT" "$lock_dir" 2>/dev/null || echo "shm_stage=warning reason=chmod_1777_failed root=$SHM_CACHE_ROOT"
  apply_shm_shared_acl "$SHM_CACHE_ROOT" shallow
  apply_shm_shared_acl "$lock_dir" shallow
  lock="${lock_dir}/$(echo "$SHM_DATASET_KEY" | tr '/ ' '__').lock"
  {
    flock 9
    if shm_cache_ready; then
      apply_shm_shared_acl "$SHM_CACHE_ROOT" shallow
      apply_shm_shared_acl "$lock_dir" shallow
      apply_shm_shared_acl "$SHM_STAGE_DIR"
      use_shm_cache
    else
      src_bytes="$(du -sb "$src" | awk '{print $1}')"
      read -r shm_total shm_avail < <(df -B1 --output=size,avail /dev/shm | awk 'NR==2{print $1, $2}')
      shm_limit=$(( shm_total * MAX_SHM_STAGE_PCT / 100 ))
      if (( src_bytes <= shm_limit && shm_avail > src_bytes + MIN_SHM_FREE_BYTES )); then
        SHM_COPY_TMP="${SHM_STAGE_DIR}.copying.${SLURM_JOB_ID:-manual}.${SLURM_ARRAY_TASK_ID:-0}"
        rm -rf "$SHM_COPY_TMP"
        mkdir -p "$SHM_COPY_TMP"
        if [[ -d "$src" ]]; then
          cp -a "$src"/. "$SHM_COPY_TMP"/
        else
          cp -a "$src" "$SHM_COPY_TMP"/
        fi
        if [[ -f "$src/manifest.json" ]]; then
          sha256sum "$src/manifest.json" | awk '{print $1}' > "$SHM_COPY_TMP/.source_manifest.sha256"
        fi
        printf '%s\n' "$SHM_NODE_NAME" > "$SHM_COPY_TMP/.cache_node"
        printf '%s\n' "$SHM_DATASET_KEY" > "$SHM_COPY_TMP/.cache_key"
        date -Is > "$SHM_COPY_TMP/.cache_created_at" 2>/dev/null || true
        apply_shm_shared_acl "$SHM_COPY_TMP"
        touch "$SHM_COPY_TMP/.ready"
        rm -rf "$SHM_STAGE_DIR"
        mv "$SHM_COPY_TMP" "$SHM_STAGE_DIR"
        apply_shm_shared_acl "$SHM_STAGE_DIR"
        unset SHM_COPY_TMP
        staged="$SHM_STAGE_DIR"
        export PACKED_DATA_ROOT="$staged"
        export DATA_ROOT="$staged"
        export DATA_PACKED_ROOT="$staged"
        echo "shm_stage=enabled node=${SHM_NODE_NAME} dataset_key=${SHM_DATASET_KEY} src_bytes=${src_bytes} shm_avail_before=${shm_avail} staged=${staged}"
      else
        echo "shm_stage=skipped node=${SHM_NODE_NAME} dataset_key=${SHM_DATASET_KEY} reason=capacity src_bytes=${src_bytes} shm_avail=${shm_avail} shm_limit=${shm_limit}"
        export DATA_ROOT="$src"
        export DATA_PACKED_ROOT="$src"
      fi
    fi
  } 9>"$lock"
}
stage_packed_data_to_shm "$PACKED_DATA_ROOT"

python - <<'INNER_PY'
import sys, torch
print("python", sys.executable)
print("torch", torch.__version__)
print("torch_cuda", torch.version.cuda)
print("cuda_available", torch.cuda.is_available())
print("device_count", torch.cuda.device_count())
if torch.cuda.is_available():
    print("device0", torch.cuda.get_device_name(0))
INNER_PY

python train.py --seed "${SLURM_ARRAY_TASK_ID}" --dataset_name <dataset>
```

Independent single-GPU jobs use the same directives without `#SBATCH --array` and pass one explicit seed to the training command.

Native exact-script helpers:

```bash
cd "$SLURM_DIR"
"$PY" hpc_native_submit.py candidate.sbatch --auth-account NAME \
  --expected-gpus 1 --expected-ntasks 1 --expected-cpus-per-task 6
"$PY" hpc_native_submit.py candidate.sbatch --auth-account NAME \
  --expected-gpus 1 --expected-ntasks 1 --expected-cpus-per-task 6 --submit \
  --submit-intent <queue-bound-intent.json> --receipt-out <private-receipt.json>
```

The submit helper freezes the manifest-bound script and intent digests, reads
each local file once, uploads the exact script bytes losslessly, and verifies the
remote SHA before both `sbatch --test-only` and real `sbatch`. It writes the
local/remote script SHA and intent SHA into the mode-`0600` receipt immediately
after `sbatch`, embeds the trace in Slurm identity, then verifies shape and that
`scontrol Command` equals the content-addressed remote script path. Any digest or
command-path mismatch fails closed and must not be retried. Missing local receipt
or lease expiry is not proof that no job started and must not trigger a duplicate
submit.

If using the `1GPU/4CPU` fallback, change both `--expected-cpus-per-task` and script thread limits to `4`.

Pending diagnosis policy: when an account has fewer running jobs than expected, do not assume the submit pass failed. Use native Slurm state first:

```bash
cd "$SLURM_DIR" && "$PY" hpc_pending_reason.py --auth-account NAME
```

For pending jobs, inspect `scontrol show job -dd <job_id>` fields including `JobState`, `Reason`, `Dependency`, `ReqNodeList`, `ExcNodeList`, `Features`, `OverSubscribe`, `GresEnforceBind`, `NumCPUs`, `NumTasks`, `CPUs/Task`, `TRES`, `TresPerNode`, `SchedNodeList`, `StartTime`, and `LastSchedEval`. If the reason is `QOSMaxJobsPerUserLimit`, the account is at the scheduler cap. If the reason is pure `Priority`, lowering CPU is unlikely to repair ordering. If the reason is `Resources` or reservation/node-CPU pressure, a future submit or explicitly authorized replacement may test `1GPU/4CPU` for the same project/seed/parameters. Never cancel `RUNNING`, terminal, or unrelated jobs.

When free GPUs appear to exist but a job still waits for `Resources`, check CPU and reservations, not just GPU counts:

```bash
sinfo -N -p GPU -o '%N|%t|%C|%G'
scontrol show node=<node> -o
scontrol show reservation
```

An apparently free node is usable only if the same node has enough unallocated CPUs for the requested shape and the current user is allowed by any active reservation. Do not submit beyond the configured account cap to work around a scheduler-side `Resources` or `Priority` blocker.

Checklist:

1. Confirm the target account token is valid, account/QOS caps are not exceeded, that account has no current-project `PENDING`/queued job, and its current-project `RUNNING` count is below `max_project_running_per_account`; honor any explicit shared-limit block.
2. Generate a stable plan hash, launch identity hash, and per-submission
   anonymous trace hash for this planned work, then write the private ledger
   record.
3. Confirm the task/profile stays within the three-seed default cap.
4. Confirm the account-local environment and packed dataset validation exist, and that every experiment-bound project path passes the persistent-symlink/realpath audit.
5. Generate one exact compliant sbatch script or one compliant job array.
6. Run hard-reject lint, `bash -n`, and `sbatch --test-only`.
7. Submit only after preflight passes and the live snapshot indicates a direct-running opportunity, not a queued follow-up.
8. Verify `NumTasks=1`, expected `CPUs/Task`, and `gres/gpu=1` with `scontrol`, unless this is a documented true DDP/multi-GPU exception.
9. Update the private trace ledger with Slurm job id, final script checksum,
   resource shape, launch identity, and initial state.
10. Download or tail startup logs and verify environment output, `DATA_BACKEND`, and at least one real training/progress line before calling the launch successful.

## Paths

- Portal SSH/SFTP must go through `hpc_winscp_info.py`; observed proxy is `<HPC_PROXY_HOST>:<HPC_PROXY_PORT>`, username shape `cluster2,<cluster_account>`.
- Portal SSH uses a temporary certificate token, not the local SSH key.
- Reusable code belongs under `/data/home/<account>/projects/<project_slug>/code`; portal path `home/projects/<project_slug>/code`.
- Portal job work/output dirs are under `/data/home/<account>/projects/<project_slug>/runs/<trace_id>/`, with per-job logs under `/data/home/<account>/projects/<project_slug>/logs/<trace_id>_*.out` and outputs under `/data/home/<account>/projects/<project_slug>/outputs/<trace_id>/`. These per-job paths are not dataset storage locations.
- Trust job-side probes for runtime facts, not login-node inference.

For local project evidence:

- Portal snapshots: `$PROJECT_DIR/hpc_evidence/bjtu_jobs_YYYYMMDD_HHMM*.json`
- Native Slurm snapshots: `$PROJECT_DIR/hpc_evidence/bjtu_pending_reason_YYYYMMDD_HHMMSS*.json`
- Downloaded launch logs: `$PROJECT_DIR/hpc_evidence/bjtu_<trace_id>_<jobid>_launch_YYYYMMDD.log`
- After a material launch/status change, immediately update only the machine-readable run ledger, private trace ledger, and server path fields needed for recovery. For repeated-variant launches, batch human-facing server-path notes and result summaries until first metric, failure, terminal state, or explicit user request.

Download pattern:

```bash
"$PY" "$SLURM_DIR/hpc_download.py" "/data/home/<account>/projects/<project_slug>/logs/<trace_id>_<jobid>.out" -o "$PROJECT_DIR/hpc_evidence/bjtu_<trace_id>_<jobid>_launch_YYYYMMDD.log" --no-progress
```

## Dataset Upload

For external AutoResearch mode, do not use the legacy archive upload recipe below. Build and validate on the external SSH factory, then use the content-addressed plan/registry contract documented by `$bjtu-hpc` in `references/external_hdf5_data_supply.md`. Until the native remote adapter is explicitly enabled, `hpc_data_supply.py upload/share/commit/delete` must fail with `remote_adapter_not_enabled`; a local intent is not a durable submit or upload receipt.

- Use stable, reusable dataset roots on BJTU `cluster2`; never scatter persistent datasets under jobs, code, runs, logs, outputs, `/tmp`, `/dev/shm`, per-job trace/hash directories, or ad hoc folders. `/dev/shm` may contain node-local reusable caches named by dataset name, but those caches are disposable accelerators, not source-of-truth dataset storage.

```text
archive source:         /data/home/<account>/dataset_archives/<dataset_name>.tar
archive manifest:       /data/home/<account>/dataset_archives/<dataset_name>.manifest.json
packed training data:   /data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/
packed manifest:        /data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/manifest.json
upload staging:         /data/home/<account>/dataset_uploads/<dataset_name>/
legacy raw root:        /data/home/<account>/dataset/<dataset_name>  (debug/legacy only)
```

- Choose a dataset name that encodes family, split/source, and version, for example `vision100_split_seed0_v1`. Do not reuse one `<dataset_name>` for different class splits or preprocessing variants.
- Keep dataset names independent of Slurm job hashes and user-task trace tokens. A later seed or account should be able to reuse the same `<dataset_name>/<experiment_profile>` when the dataset contract is identical.
- For aligned ImageNet-100, the legacy raw source `/data/home/<source_account>/dataset/data_aligned_split_v1/ImageNet` is a conversion source or single-account debug path. New multi-account training should use account-local archives and packed outputs such as `/data/home/<account>/dataset_packed/imagenet100/aligned_seed0_v1/`. Do not use `/data/home/<source_account>/dataset/data/ImageNet` for aligned-split ImageNet-100.
- Before using a newly uploaded dataset in training, validate counts and write archive plus packed-data manifests. Training scripts should point to the packed root with `DATA_BACKEND=lmdb`, `hdf5`, or `tfrecord`, not upload staging, archives, temporary extraction directories, or another account's raw small-file tree.
- Current archive task: `dataset-archive`; source-side screen: `bjtu-resume-archive`.
- Preferred command: `cd "$SLURM_DIR" && "$PY" hpc_transfer_app.py run dataset-archive --method parallel-chunk --parallel 4 --chunk-mib 8 --buffer-mib 4`.
- Never delete or reset an active archive `.part` file such as `/data/home/<account>/dataset/data/_archives/<archive>.tar.gz.part` unless explicitly asked.
- Never run two upload workers writing the same archive `.part`; stop the old source-side `screen` first.
- When `<SOURCE_SSH_ALIAS>` SSH is slow or hangs, use cluster-side file size/progress as truth.

## Post-Submit Evidence Checklist

Before reporting a job as running:

- Portal row is present and the expected `jobId`, `ngpus`, `ncpus`, and node are recorded.
- Native Slurm reason was checked. If pending, report the exact `Reason`, block the affected account/shared group, and do not call the launch running-successful; if running, record `Reason=None`.
- CPU/GPU allocation matches the intended shape.
- Startup logs were downloaded or tailed locally.
- Startup logs include the Python/PyTorch/CUDA probe, `DATA_BACKEND`, and packed dataset manifest or validation-report path.
- At least one real training/progress line was observed, not only environment setup.
- Evidence files were saved under `$PROJECT_DIR/hpc_evidence/` or the project's equivalent neutral evidence directory.

## Safety

- For cross-account dataset sharing, inspect ACLs first; do not apply ACL/chmod changes without explicit confirmation.
- Use `Readme.md` for tested limits, dataset layout, speed findings, and native Slurm fallback examples.
