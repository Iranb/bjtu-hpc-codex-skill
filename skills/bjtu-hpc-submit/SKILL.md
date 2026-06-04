---
name: bjtu-hpc-submit
description: Use when an agent needs to refresh/save BJTU HPC portal auth, add or switch BJTU portal accounts/tokens, run the local transfer Web dashboard, upload code or data, submit CPU/GPU jobs, inspect job status including GPU counts, monitor resumable dataset uploads, or probe the runtime environment from this workspace.
---

# BJTU HPC Submit

Tool-first workflow for BJTU HPC portal work from the `slurm` helper workspace. Human step-by-step usage is in workspace `Manual.md`; detailed history and experiment notes live in `AGENTS.md` and `Readme.md`.

## Runtime Defaults

- Work from the helper workspace unless the project says otherwise: `<SLURM_DIR>`.
- On this Codex controller, prefer `<PYTHON3>` for all helper scripts. The system `python3` may be too old for helper type annotations. Use this shell prefix in examples:

```bash
PY=<PYTHON3>
SLURM_DIR="<SLURM_DIR>"
<PROJECT>_DIR="<PROJECT_DIR>"
```

- When working from <PROJECT>, save evidence under `$<PROJECT>_DIR/refine-logs/hpc_stdout/`. Run status commands with `cwd=$<PROJECT>_DIR` when possible so `hpc_pending_reason.py` writes snapshots there automatically.
- Use broad keywords such as `<project_keyword>` for general queue checks. Narrow keywords like `<project_keyword>_c100`, `<project_keyword>_im100`, or `dynprior` are only for targeted follow-ups.

## Entry Points

- Always start from the helper workspace with `cd "$SLURM_DIR" && "$PY" hpc_doctor.py --json`; it checks dependencies, account state, browser profile, and token validity without printing secrets.
- If dependencies are missing, run `cd "$SLURM_DIR" && "$PY" -m pip install -r requirements.txt` and `"$PY" -m playwright install chromium`.
- For agent-driven portal-app jobs, prefer `cd "$SLURM_DIR" && "$PY" hpc_submit_verified.py ./script.py --submit --json` over raw `hpc_submit.py`.
- For CPU/GRES-sensitive jobs, prefer uploaded native `sbatch` scripts over the portal PyTorch app, then verify with native Slurm.
- For MCP clients, prefer `hpc_auth_status`, `hpc_submit_and_verify`, `hpc_pending_reason`, `hpc_verify_slurm_allocation`, `hpc_tail_stdout`, and `hpc_get_sftp_info` from `hpc_mcp_server.py`.

Useful <PROJECT> status commands:

```bash
cd "$<PROJECT>_DIR"
"$PY" "$SLURM_DIR/hpc_jobs.py" list --keyword <project_keyword> --size 30 --paths
"$PY" "$SLURM_DIR/hpc_jobs.py" list --keyword <project_keyword> --size 30 --paths --json > refine-logs/hpc_stdout/bjtu_jobs_YYYYMMDD_HHMM.json
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

Recent <PROJECT> lesson: the smooth path is a single integrated `hpc_refresh_flow.py` command that owns validation, profile probing, optional visible login, and post-login status collection. Do not manually bounce between `hpc_doctor`, `hpc_jobs`, and visible browser attempts unless that command has exited and validation still fails.

1. For routine refreshes when no command is currently blocked, start with the fast path:

```bash
cd "$SLURM_DIR" && "$PY" hpc_refresh_flow.py main
```

2. If invalid auth blocks a user-requested status check, progress check, pending-reason check, upload, or submit, run the integrated blocked-task flow in a PTY and keep it running. Do not stop after merely reporting the invalid token. For multi-account status checks, run the same flow for each affected saved account unless the user limits the scope.

```bash
cd "$SLURM_DIR" && "$PY" hpc_refresh_flow.py main --visible-only
```

3. For <PROJECT> progress checks, use the post-login status variant so the same command continues after any refresh/login and returns the requested state automatically:

```bash
cd "$<PROJECT>_DIR"
"$PY" "$SLURM_DIR/hpc_refresh_flow.py" main --visible-only \
  --after-jobs-keyword <project_keyword> --after-jobs-size 30 --after-jobs-paths \
  --after-snapshot-dir "$<PROJECT>_DIR/refine-logs/hpc_stdout" \
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
cd "$SLURM_DIR" && "$PY" hpc_accounts.py refresh main \
  --browser playwright --headless --fresh-page --timeout 30 --sync-legacy-token
cd "$SLURM_DIR" && "$PY" hpc_accounts.py validate main
```

  If validation succeeds, continue with the originally requested status/upload/submit command. Rerun the integrated `--visible-only` flow only if the headless profile capture and validation still fail.
- Use `--force --visible-only --no-profile-probe-before-visible` only after one integrated attempt exits without a usable token and `hpc_accounts.py validate main` still fails, or when the user explicitly requests a visible login window. Do not use this as the first attempt, because it skips the profile-recovery fast path and creates unnecessary login windows.
- If the user says login is done but the command exits without `[ok]` and without the post-login job table, first run the headless profile capture above, then `hpc_accounts.py validate main` and the originally requested command. Rerun visible-only once only if validation still reports `11009`, `11011`, `11012`, HTTP `401`, or token-validation transport errors.
- If the second visible attempt still fails to save a usable token, report the auth/token-save failure as the blocker. For <PROJECT> progress checks, keep the job at its latest trusted snapshot and state the exact timestamp of that evidence.
- After any refresh, validate with `cd "$SLURM_DIR" && "$PY" hpc_doctor.py --json` or `cd "$SLURM_DIR" && "$PY" hpc_accounts.py validate main`; do not trust browser completion alone.

## Job Rules

- Default single-process GPU shape on `cluster2`: `--gpu 1 --ntasks 1 --cpus-per-task 8 --gres-flags disable-binding`.
- Native Slurm equivalent for one GPU: `#SBATCH --ntasks=1`, `#SBATCH --cpus-per-task=8`, `#SBATCH --gres=gpu:1`, `#SBATCH --gres-flags=disable-binding`.
- Request more GPUs only when the code actually uses them.
- Avoid `--gpu 1 --ntasks 8` without `--gres-flags disable-binding`; it has produced `BadConstraints`.
- After every submit, verify the portal job row. If the job is `PENDING`, report native Slurm `Reason`, not just portal state.
- If CPU/GRES shape matters, verify native `NumCPUs`, `NumTasks`, `CPUs/Task`, and GPU TRES with `scontrol`; portal request fields are not enough.
- Do not cancel unrelated jobs. For `QOSMaxJobsPerUserLimit`, inspect existing jobs before canceling anything.
- Always run `sbatch --test-only` for a new native script or a new resource shape before real submission.

Known-good shapes on `cluster2`:

```text
1 GPU single process: --ntasks=1 --cpus-per-task=8 --gres=gpu:1 --gres-flags=disable-binding
2 GPU packed job:     --ntasks=1 --cpus-per-task=8 --gres=gpu:2 --gres-flags=disable-binding
```

## Native Slurm Packed Jobs

Use packed jobs only when one Slurm allocation intentionally launches multiple child experiments. This helps stay within per-user job-count limits, but it must be done carefully.

Checklist:

1. Request one batch allocation with the required GPU count, for example `--gres=gpu:2`, `--ntasks=1`, `--cpus-per-task=8`, and `--gres-flags=disable-binding`.
2. In the batch script, read the allocation-provided `CUDA_VISIBLE_DEVICES` and split it into child lanes. Do not hardcode physical `0/1`; prior tests showed this can escape the allocated GPU ids.
3. For each child, set `CUDA_VISIBLE_DEVICES` to exactly one allocated id, run a lightweight `nvidia-smi` and `torch.cuda.device_count()` sanity check, then launch the experiment.
4. Save a batch stdout plus one child log per lane under `/data/home/<cluster_account_main>/jobs/<job_name>_<job_id>_pair/`.
5. After submission, run native checks:

```bash
cd "$<PROJECT>_DIR"
"$PY" "$SLURM_DIR/hpc_pending_reason.py" <job_id> --no-sinfo
```

6. Verify the expected fields: `JobState=RUNNING`, `Reason=None`, `NumCPUs`, `NumTasks`, `CPUs/Task`, `TRES=...gres/gpu=<N>`, `TresPerNode=gpu:<N>`, and the node name.
7. Download or tail child logs and verify that each child reports one visible CUDA device and has entered real training before calling the launch successful.

Do not submit additional packed jobs just because slots appear idle; first check existing <PROJECT> jobs and the `twojobonly` QoS behavior.

## Paths

- Portal SSH/SFTP must go through `hpc_winscp_info.py`; observed proxy is `<proxy_host>:<proxy_port>`, username shape `cluster2,<cluster_account_main>`.
- Portal SSH uses a temporary certificate token, not the local SSH key.
- Reusable code belongs under `/data/home/<cluster_account_main>/code/<project>`; portal path `home/code/<project>`.
- Portal job work/output dirs are under `/data/home/<cluster_account_main>/jobs/<job_name>_<timestamp>`.
- Trust job-side probes for runtime facts, not login-node inference.

For <PROJECT> experiment evidence:

- Portal snapshots: `$<PROJECT>_DIR/refine-logs/hpc_stdout/bjtu_jobs_YYYYMMDD_HHMM*.json`
- Native Slurm snapshots: `$<PROJECT>_DIR/refine-logs/hpc_stdout/bjtu_pending_reason_YYYYMMDD_HHMMSS*.json`
- Downloaded launch logs: `$<PROJECT>_DIR/refine-logs/hpc_stdout/bjtu_<jobid>_<shortname>_launch_YYYYMMDD.log`
- After a material launch/status change, update `AGENTS.md`, `refine-logs/EXPERIMENT_TRACKER.md`, `EXPERIMENT_SERVER_PATHS.md`, and `NARRATIVE_REPORT.md`.

Download pattern:

```bash
"$PY" "$SLURM_DIR/hpc_download.py" "/data/home/<cluster_account_main>/path/to/remote.log" -o "$<PROJECT>_DIR/refine-logs/hpc_stdout/bjtu_<jobid>_<shortname>_launch_YYYYMMDD.log" --no-progress
```

## Dataset Upload

- Use stable dataset roots on BJTU `cluster2`; never scatter datasets under jobs, code, outputs, `/tmp`, or ad hoc folders.

```text
canonical dataset root: /data/home/<account>/dataset/<dataset_name>
upload staging:         /data/home/<account>/dataset/_uploads/<dataset_name>/
archive staging:        /data/home/<account>/dataset/_archives/<dataset_name>/
manifest:               /data/home/<account>/dataset/_manifests/<dataset_name>_manifest.json
cross-account symlink:  /data/home/<other_account>/dataset/<dataset_name> -> canonical root
```

- Choose a dataset name that encodes family, split/source, and version, for example `imagenet100_simgcd_seed0_v1`. Do not reuse one `<dataset_name>` for different class splits or preprocessing variants.
- For <PROJECT> aligned ImageNet-100, the canonical BJTU root is `/data/home/<cluster_account_main>/dataset/<dataset_name>/<dataset_subdir>`. The optional other-account symlink may live under `/data/home/<cluster_account_other>/dataset/<dataset_name>/<dataset_subdir>`. Do not use `/data/home/<cluster_account_main>/dataset/<legacy_dataset_name>/<dataset_subdir>` for aligned-split ImageNet-100.
- Before using a newly uploaded dataset in training, validate counts and write a manifest under `_manifests`. Training scripts should point to the canonical root, not `_uploads`, `_archives`, or a temporary extraction directory.
- Current archive task: `dataset-archive`; source-side screen: `bjtu-resume-archive`.
- Preferred command: `cd "$SLURM_DIR" && "$PY" hpc_transfer_app.py run dataset-archive --method parallel-chunk --parallel 4 --chunk-mib 8 --buffer-mib 4`.
- Never delete or reset `/data/home/<cluster_account_main>/dataset/data/_archives/<dataset_archive>.tar.gz.part` unless explicitly asked.
- Never run two upload workers writing the same archive `.part`; stop the old source-side `screen` first.
- When `<source_server_alias>` SSH is slow or hangs, use cluster-side file size/progress as truth.

## Post-Submit Evidence Checklist

Before reporting a job as running:

- Portal row is present and the expected `jobId`, `ngpus`, `ncpus`, and node are recorded.
- Native Slurm reason was checked. If pending, report the exact `Reason`; if running, record `Reason=None`.
- CPU/GPU allocation matches the intended shape.
- Startup logs were downloaded or tailed locally.
- At least one real training/progress line was observed, not only environment setup.
- Evidence files were saved under the project `refine-logs/hpc_stdout/` when working from <PROJECT>.

## Safety

- For cross-account dataset sharing, inspect ACLs first; do not apply ACL/chmod changes without explicit confirmation.
- Use `Readme.md` for tested limits, dataset layout, speed findings, and native Slurm fallback examples.
