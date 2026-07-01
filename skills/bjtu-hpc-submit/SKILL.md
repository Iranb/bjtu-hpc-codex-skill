---
name: bjtu-hpc-submit
description: Use when an agent needs to refresh/save BJTU HPC auth, manage saved accounts/tokens, run the local dashboard, upload code/data, preflight native sbatch runability, submit CPU/GPU jobs with ordinary 1GPU/6CPU shapes and 1GPU/4CPU resource-wait fallback, use monitor or `hpc_queue_summary.py --json` snapshots to choose CPU/GPU shapes, use GPU-fill fragments, low-VRAM GPU-sharing packed jobs with at most two code executions per V100, or 2GPU-to-1GPU compatibility fallbacks, fill selected accounts to the native packed GPU job cap, create queue-monitor heartbeats, inspect job status and pending reasons, monitor uploads, or probe runtime environment.
---

# BJTU HPC Submit

Tool-first workflow for BJTU HPC portal work from a local helper workspace. This public version is sanitized: replace placeholder paths, account names, and project directories with your own local values before use.

## Runtime Defaults

- Work from the helper workspace unless the project says otherwise: `<SLURM_DIR>`.
- Prefer the Python environment used to install the helper dependencies. Use this shell prefix in examples:

```bash
PY=<PYTHON3>
SLURM_DIR="<SLURM_DIR>"
PROJECT_DIR="<PROJECT_DIR>"
```

- When working from project, save evidence under `$PROJECT_DIR/hpc_stdout/`. Run status commands with `cwd=$PROJECT_DIR` when possible so `hpc_pending_reason.py` writes snapshots there automatically.
- Use broad keywords such as `<keyword>` for general queue checks. Narrow keywords like `<keyword_c100>`, `<keyword_im100>`, or `<keyword>` are only for targeted follow-ups.

Policy authority and drift checks: live helper output (`hpc_doctor.py --json`, `hpc_accounts.py`, `hpc_queue_summary.py --json`, monitor/widget snapshots, and helper `--help` defaults) overrides stale skill prose when they disagree. Current validated scheduling policy is four non-terminal jobs per auth account: two run-slot jobs plus two queued follow-up jobs. Do not use older eight-job or six-follow-up monitor examples unless the user explicitly requests a new policy and live Slurm/helper evidence verifies it on that date. Before editing cap-related behavior, scan both `bjtu-hpc` and `bjtu-hpc-submit` for `--cap`, `HPC_MONITOR_ACCOUNT_CAP`, `run-slots`, `queued follow-up`, and `QOSMaxJobsPerUserLimit`, then update both skills and helper defaults consistently.

## Entry Points

- Always start from the helper workspace with `cd "$SLURM_DIR" && "$PY" hpc_doctor.py --json`; it checks dependencies, account state, browser profile, and token validity without printing secrets.
- If dependencies are missing, run `cd "$SLURM_DIR" && "$PY" -m pip install -r requirements.txt` and `"$PY" -m playwright install chromium`.
- For agent-driven portal-app jobs, prefer `cd "$SLURM_DIR" && "$PY" hpc_submit_verified.py ./script.py --submit --json` over raw `hpc_submit.py`.
- For CPU/GRES-sensitive jobs, prefer uploaded native `sbatch` scripts over the portal PyTorch app, then verify with native Slurm.
- Before each one-by-one native GPU submission, prefer `cd "$SLURM_DIR" && "$PY" hpc_plan_from_snapshot.py --planner-json`. It runs one bounded `hpc_queue_summary.py --json --jobs 4` snapshot and feeds `hpc_resource_planner.py --queue-json <snapshot>`, avoiding a second live SSH/proxy sweep. Follow only its `next_action`; submit and verify that one job, refresh queue/resources with a new snapshot, then rerun the snapshot-backed planner for the next job. If the helper is unavailable, run `hpc_queue_summary.py --json --jobs 4 > /tmp/bjtu_hpc_queue_summary_current.json` and then `hpc_resource_planner.py --queue-json /tmp/bjtu_hpc_queue_summary_current.json`. Pass `--available-children N` or `--child-manifest children.json` before allowing candidates wider than the normal two-child packed job. Use `--gpu-first` when explicitly prioritizing GPU occupancy and `--test-only-probe --probe-script ./candidate.template.sbatch --write-selected-script ./candidate.selected.sbatch` when you need the planner to rewrite and test the exact sbatch template for each candidate; without `--probe-script`, the probe is resource-shape-only and still requires exact-script preflight. If the planner returns plain `queue_probe`, require `do_not_submit=true` and `totals.submissions_to_do_now=0`; use `--allow-queued-submit` only for intentional queued backlog after exact-script `sbatch --test-only` evidence. Use `--summary-serial` or `hpc_queue_summary.py --serial` only when the portal proxy misbehaves under bounded parallel account queries. Use `--submit-mode batch` only for dry-run batch planning.
- For MCP clients, prefer `hpc_auth_status`, `hpc_submit_and_verify`, `hpc_pending_reason`, `hpc_verify_slurm_allocation`, `hpc_tail_stdout`, and `hpc_get_sftp_info` from `hpc_mcp_server.py`.

Useful project status commands:

```bash
cd "$PROJECT_DIR"
"$PY" "$SLURM_DIR/hpc_jobs.py" list --keyword <keyword> --size 30 --paths
"$PY" "$SLURM_DIR/hpc_jobs.py" list --keyword <keyword> --size 30 --paths --json > hpc_stdout/bjtu_jobs_YYYYMMDD_HHMM.json
"$PY" "$SLURM_DIR/hpc_pending_reason.py" <slurm_job_id> --no-sinfo
```

## Dashboard And Guardian

- Use `hpc_transfer_web.py` for local dashboard workflows: token validation, visible token refresh, saved CAS login management, resumable upload launch, upload progress, and portal job listing.
- The dashboard's saved-login UI must store credentials only in the local credential helper/store with restrictive permissions. It must never display saved passwords; it may only show whether a password exists.
- Use the Token Guardian only after each selected account has completed at least one visible CAS login and has a usable account-local Playwright profile.
- Conservative guardian defaults are a 300 second validation interval, a 1800 second headless-refresh threshold, and a 5-day token-age warning threshold. Shorter intervals should be treated as diagnostic probes, not normal background policy. `token_age`/`age_warning` is pre-expiry maintenance; do not report it as token failure unless validation also fails.
- To keep the dashboard and guardian alive outside a terminal, prefer a per-user LaunchAgent or equivalent user service. Run it as the same OS user that owns the Playwright profiles and `~/.bjtu_hpc_*` stores.
- Service status commands must redact environment variables, tokens, cookies, passwords, certificates, and long token-like strings before printing raw service manager output.

## macOS Monitor And Desktop Widget

- The optional `mac_hpc_monitor/` directory contains a menu bar monitor plus a compact floating desktop widget. Treat them as read-only status surfaces.
- Require users to set `HPC_MONITOR_SLURM_DIR=/path/to/bjtu-hpc-helper` when the monitor is installed from outside the helper workspace.
- The monitor may display account aliases, job states, pending reasons, running GPU/CPU totals, GPU-node allocation summaries, and redacted Token Guardian attention states. It must not display portal tokens, passwords, cookies, browser storage, temporary certificates, or raw service environments.
- If the desktop widget marks an account purple because of auth failure, token age, headless refresh failures, or `needs_visible_login`, clicking that account card may request `/api/token-guardian/visible-refresh` through the local dashboard. The click only opens or requests a visible login window; the user must still complete CAS verification.
- Use adaptive idempotent refresh scheduling: compare a stable state signature after each refresh, excluding timestamps. If jobs and cluster GPU/CPU resources are unchanged, grow the next interval linearly up to `HPC_MONITOR_MAX_INTERVAL`; if any job state, pending reason, node GPU/CPU allocation/free count, reservation exclusion, query error, or account error changes, reset to `HPC_MONITOR_INTERVAL`.

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

### Multi-Account Tokens

Use `hpc_accounts.py` for account-local tokens instead of copying a legacy token file between users:

```bash
cd "$SLURM_DIR"
"$PY" hpc_accounts.py list
"$PY" hpc_accounts.py add <account_name> --refresh --browser playwright --fresh-page --timeout 600
"$PY" hpc_accounts.py refresh <account_name> --browser playwright --headless --fresh-page
"$PY" hpc_accounts.py validate <account_name>
"$PY" hpc_accounts.py use <account_name>
```

- Adding or refreshing an account should discover the portal user, cluster, and cluster OS account from the portal token when the helper supports it. Do not copy metadata from another saved account unless the user explicitly provides it.
- Do not sync a secondary account into `~/.bjtu_hpc_token` unless the user intentionally wants to change the legacy default.
- Use `--auth-account <account_name>` on every submit, job-list, upload, download, or proxy-info command in a multi-account workflow.

### Token Guardian Interpretation

- Headless guardian refresh is best-effort. It can validate an existing token and attempt profile-based renewal, but it cannot create a fresh CAS session when the profile no longer has a usable CAS/OAuth state.
- A successful keepalive/renewal must be evidenced by a fresh validation plus either `token_changed=true` or an advanced account `token_updated_at` timestamp. A status like `valid_refresh_failed` means the old saved token still validates, but the attempted headless renewal did not prove a new token was issued.
- Treat `token_age`/`age_warning` as a maintenance warning based on the default 5-day threshold. It should trigger a visible-refresh affordance or notification, but it is not by itself evidence that the saved token has expired.
- If final validation fails with expired-token portal codes, HTTP `401`, or token-validation transport errors, mark the account as needing visible login and run the integrated visible flow for that account.
- Use guardian logs only for redacted status summaries. They should contain event names, account aliases, reasons, final validation status, and sanitized errors; they should not contain token values, passwords, localStorage dumps, or temporary certificates.
- To estimate token longevity, use saved account metadata such as `token_updated_at` plus redacted probe logs. Report ranges such as "at least X and less than Y" when the probe interval only bounds the expiry time.

### Auth Recovery State Machine

Recent project lesson: the smooth path is a single integrated `hpc_refresh_flow.py` command that owns validation, profile probing, optional visible login, and post-login status collection. Do not manually bounce between `hpc_doctor`, `hpc_jobs`, and visible browser attempts unless that command has exited and validation still fails.

1. For routine refreshes when no command is currently blocked, start with the fast path:

```bash
cd "$SLURM_DIR" && "$PY" hpc_refresh_flow.py main
```

2. If invalid auth blocks a user-requested status check, progress check, pending-reason check, upload, or submit, run the integrated blocked-task flow in a PTY and keep it running. Do not stop after merely reporting the invalid token. For multi-account status checks, run the same flow for each affected saved account unless the user limits the scope.

```bash
cd "$SLURM_DIR" && "$PY" hpc_refresh_flow.py main --visible-only
```

3. For project progress checks, use the post-login status variant so the same command continues after any refresh/login and returns the requested state automatically:

```bash
cd "$PROJECT_DIR"
"$PY" "$SLURM_DIR/hpc_refresh_flow.py" main --visible-only \
  --after-jobs-keyword <keyword> --after-jobs-size 30 --after-jobs-paths \
  --after-snapshot-dir "$PROJECT_DIR/hpc_stdout" \
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
- If the second visible attempt still fails to save a usable token, report the auth/token-save failure as the blocker. For project progress checks, keep the job at its latest trusted snapshot and state the exact timestamp of that evidence.
- After any refresh, validate with `cd "$SLURM_DIR" && "$PY" hpc_doctor.py --json` or `cd "$SLURM_DIR" && "$PY" hpc_accounts.py validate main`; do not trust browser completion alone.

## Job Rules

- Default single-process GPU shape on `cluster2`: try native `--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding` first.
- Native Slurm equivalent for one GPU: `#SBATCH --ntasks=1`, `#SBATCH --cpus-per-task=6`, `#SBATCH --gres=gpu:1`, `#SBATCH --gres-flags=disable-binding`.
- Before every real GPU training submission, run native pre-submit runability checks through the portal SSH proxy. For queued follow-ups, refills, and authorized pending replacements, first use the monitor resource snapshot to choose the largest GPU-count same-node shape that currently fits; for ordinary packed jobs, start at `2GPU/12CPU` (`--ntasks=2 --cpus-per-task=6 --gres=gpu:2`). If native `2GPU/12CPU` cannot start directly because of `Resources`, reservation constraints, same-node CPU availability, or another resource-shape allocation failure, test `2GPU/8CPU` (`--ntasks=2 --cpus-per-task=4 --gres=gpu:2`). Do not lower CPU for pure `Priority`, dependency holds, or `QOSMaxJobsPerUserLimit`. Use CPU-rich `1:8`, `1:12`, or `1:16` shapes only when the user explicitly wants CPU-rich work or snapshot plus test-only proves immediate start without reducing GPU occupancy. If each child has observed or strongly bounded peak VRAM below `16GB` on BJTU V100-32GB, low-memory GPU-sharing may request `N` physical GPUs and launch up to `2N` child processes with at most two code executions mapped to each allocated GPU; use `--ntasks=2N --gres=gpu:N` and do not use this when VRAM is unknown or close to `16GB`. If the packed 2GPU shape still cannot be scheduled but the child experiments are single-GPU capable, split the affected experiment pair into native 1GPU singleton jobs after `sbatch --test-only` shows 1GPU can run. For those singletons, test `1GPU/6CPU` first and then `1GPU/4CPU`; do not launch below 4 CPUs except explicit GPU-fill fragments.
- Request more GPUs only when the code actually uses them.
- Avoid `--gpu 1 --ntasks 8` without `--gres-flags disable-binding`; it has produced `BadConstraints`.
- After every submit, verify the portal job row. If the job is `PENDING`, report native Slurm `Reason`, not just portal state.
- If CPU/GRES shape matters, verify native `NumCPUs`, `NumTasks`, `CPUs/Task`, and GPU TRES with `scontrol`; portal request fields are not enough.
- If a portal PyTorch-GPU submit requested a multi-CPU shape such as `--cpus-per-task 16` but native `scontrol` reports `NumCPUs=1` or `CPUs/Task=1`, treat the launch as CPU-degraded rather than resource-verified. Keep it only when the user explicitly accepts the risk or the run has already entered useful training; otherwise use the uploaded native `sbatch` path for CPU-sensitive training.
- Do not cancel unrelated jobs. For `QOSMaxJobsPerUserLimit`, inspect existing jobs before canceling anything.
- Always run `sbatch --test-only` for a new native script or a new resource shape before real submission.

Known-good shapes on `cluster2`:

```text
1 GPU single-process default: --ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding
1 GPU resource-wait fallback: --ntasks=1 --cpus-per-task=4 --gres=gpu:1 --gres-flags=disable-binding
2 GPU packed default: --ntasks=2 --cpus-per-task=6 --gres=gpu:2 --gres-flags=disable-binding
2 GPU packed resource-wait fallback: --ntasks=2 --cpus-per-task=4 --gres=gpu:2 --gres-flags=disable-binding
CPU-rich optional packed shapes: --ntasks=2 --cpus-per-task=<8|12|16> --gres=gpu:2 --gres-flags=disable-binding, only with explicit CPU-rich intent or immediate-start proof
GPU-fill fragment exception: --ntasks=<free_gpu> --cpus-per-task=2 --gres=gpu:<free_gpu> --gres-flags=disable-binding
Low-VRAM GPU-sharing exception: --ntasks=<2N> --cpus-per-task=<C> --gres=gpu:<N> --gres-flags=disable-binding, max two children per V100 when each child peaks below 16GB
```

## Native Slurm Packed Jobs

Use packed jobs only when one Slurm allocation intentionally launches multiple child experiments. This helps stay within per-user job-count limits, but it must be done carefully.

Default launch policy: when a user selects an auth account for evidence-producing GPU experiments, fill that account to four non-terminal jobs in the same launch pass. Current Slurm `QOSMaxJobsPerUserLimit` behavior caps each account at four non-terminal jobs total: two run-slot jobs plus two queued follow-up jobs. A normal packed job is `2GPU/12CPU` and runs two child experiments, so a full account holds eight child experiments when all jobs are packed. Count `RUNNING`, `PENDING`, dependency-held, configuring, and other non-terminal jobs against the cap. Do not count terminal jobs such as `DONE`, `FAIL`/`FAILED`, `CANCEL`/`CANCELLED`, `COMPLETED`, or `TIMEOUT`. A 1GPU singleton consumes a full job slot while using only one GPU; treat accounts with two running jobs but fewer than four running GPUs as slot-fragmented, not fully GPU-utilized.

Use this algorithm before submitting:

```text
current = count non-terminal packed jobs for --auth-account NAME
open_slots = max(0, 4 - current)
experiment_pairs = floor(number of unlaunched child experiments / 2)
submit_now = min(open_slots, experiment_pairs)
```

If `submit_now > 0`, submit that many native packed jobs for the selected account before switching accounts or stopping. Do not stop after the first successful packed job while the account still has open packed slots. If the account currently has 0/1/2/3 non-terminal packed jobs, submit up to 4/3/2/1 additional packed jobs respectively. If strict refill order is required, submit jobs three and four with native Slurm dependencies such as `--dependency=afterany:<earlier_job_id>`; dependency-held follow-ups still count toward the four-job cap. If submission hits `QOSMaxSubmitJobPerUserLimit` or another submit cap, record the remaining pairs in the local launch plan and retry later instead of looping.

Scheduled refill monitor policy: when the user asks to keep BJTU HPC queues full, create or update one scheduled monitor for the project instead of relying on manual status checks or in-thread sleep loops. If a Codex automation or heartbeat tool is available, update the existing matching monitor by automation id; do not create duplicates. If no scheduler tool is available, write the next-check instruction and backlog state into the project status/index so the next heartbeat can resume it. The monitor wake action is:

1. Run a live native snapshot, usually `cd "$SLURM_DIR" && "$PY" hpc_queue_summary.py --accounts ... --json --cap 4 --run-slots 2`, and treat that as authoritative over portal job lists.
2. Sync lightweight terminal results first, then update the experiment index/status artifacts before submitting more work.
3. For each valid account below the configured non-terminal cap, submit prepared non-duplicate experiment pairs through the normal exact-script pre-submit gate. Refresh the live queue after material submissions before selecting the next job.
4. If `sbatch --test-only` or real submission is rejected by `QOSMaxSubmitJobPerUserLimit`, `QOSMaxJobsPerUserLimit`, or a similar submit cap, keep the remaining pairs in the local launch plan/status file and stop retrying that account in the current wake. Retry only after a later live snapshot shows terminal jobs reduced that account's non-terminal count, or after the user explicitly changes the cap policy.
5. If the account is full and pending for pure `Priority`, preserve queue position. If pending for `Resources`, reservation pressure, same-node CPU pressure, or 2GPU shape pressure, use the documented fallback/replacement policy only when authorized and only for same-project children whose labels/parameters are preserved.
6. Recompute the next monitor interval from live state: about 10-20 minutes after terminal progress, failed auth that was just repaired, or jobs near expected start; about 30-60 minutes for mixed running/pending queues; about 60-120 minutes when every account is full and the only blocker is `Priority`, `Resources`, or submit cap. Record the selected interval and reason in the status artifact.
7. Each monitor report must include observed progress, refill actions, submitted/not-submitted reasons, accounts still below cap, exact wait condition, and the next-check interval. Never print tokens, cookies, passwords, temporary certificates, or raw credential material.

Monitor resource-state policy: before choosing resource parameters for a new queued follow-up, refill, or authorized pending-job replacement, use the same live snapshot consumed by the macOS desktop widget/menu bar monitor. Prefer the latest `hpc_queue_summary.py --json` payload, and inspect `checked_at_local`, `cluster_resources.summary`, `cluster_resources.nodes`, `cluster_resources.excluded_reserved_nodes`, account summaries, pending reasons, and each job's native `resources`. If the widget/menu-bar payload is stale, missing `cluster_resources`, or has a resource query error, refresh with `cd "$SLURM_DIR" && "$PY" hpc_queue_summary.py --json` before setting CPU/GPU shape. Treat this snapshot as the candidate-generation source, not as final proof; `sbatch --test-only` and post-submit `scontrol` remain the authority. If free GPUs exist only on nodes whose same-node `cpu_free` is below the minimum floor for every allowed fallback (`1GPU/4CPU`, fallback `2GPU/8CPU`, or GPU-fill `2` CPUs per GPU), classify the situation as same-node CPU exhaustion. Lowering CPU, splitting to 1GPU singletons, or GPU-fill cannot claim those visible GPUs until same-node CPUs free up.

Sequential planner policy: because real experiment submissions are usually performed one at a time, do not submit an unchanged multi-job virtual packing plan. Run `cd "$SLURM_DIR" && "$PY" hpc_plan_from_snapshot.py --planner-json` before each new Slurm job; the default `--submit-mode sequential` uses each account's four-job cap as the fill target but emits only one `next_action` for the current submit. After submitting and verifying that job, refresh queue/resources and run the snapshot-backed planner again. The helper can also consume a saved snapshot with `--queue-json /tmp/bjtu_hpc_queue_summary_current.json`; if calling the planner directly, always pass the same snapshot through `hpc_resource_planner.py --queue-json /tmp/bjtu_hpc_queue_summary_current.json` rather than letting it start another live summary. Use `--gpu-first` when explicitly prioritizing GPU occupancy and `--wide-gpu-policy auto` to allow wide single-allocation candidates when same-node GPUs are abundant. Wide candidates above the normal two-child packed job are capped by `--available-children N` or `--child-manifest children.json`; when neither is supplied, the planner assumes two independent children. The default `--cpu-policy balanced` maximizes same-node GPU count first, then prefers the ordinary `1GPU/6CPU` ratio for that GPU count unless a CPU-rich shape has immediate-start evidence and does not strand GPUs. For example, `8G/48C` with eight independent children should prefer `--ntasks=8 --cpus-per-task=6 --gres=gpu:8`; `8G/40C` may use `--cpus-per-task=4` if 1:6 would wait, or `--cpus-per-task=5` only as an exact-fit intermediate supported by test-only evidence. With only a two-child pair, prefer `2GPU/12CPU`; use `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` only for explicit CPU-rich work or immediate-start proof. Use `--cpu-policy gpu-dense` only when deliberately preserving CPU for many follow-up placements or a CPU-poor fragment; use `--cpu-policy cpu-fill` only when explicitly trading schedulability for the largest integer CPU shape. With `--gpu-first`, a low-CPU `2GPU/4CPU` tail-fill candidate is allowed when a node has stranded GPUs but too little same-node CPU even for `1GPU/4CPU` ordinary fallback shapes. Add `--test-only-probe --probe-script ./candidate.template.sbatch --write-selected-script ./candidate.selected.sbatch` to rewrite that exact sbatch template for each candidate, run remote `bash -n` plus `sbatch --test-only` without submission, and write the selected shape locally for final submit. If `--probe-script` is omitted, the probe is resource-shape-only and the final exact script still must pass preflight. When no candidate fits current same-node resources, treat planner `queue_probe` output as backlog-only: plain `queue_probe` must carry `do_not_submit=true` and `totals.submissions_to_do_now=0`; use `--allow-queued-submit` only for intentional backlog after exact-script `sbatch --test-only` evidence. Do not submit high-CPU `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` queued jobs solely because they are ranked in a no-fit snapshot. Use `--submit-mode batch` only for capacity dry runs. Use `--summary-serial` only as the fallback when bounded parallel queue snapshots fail.

Planner diagnostics policy: if the planner returns no `next_action` but `cluster_resources.summary.gpu_free > 0`, inspect `cluster_diagnostics` and per-account `diagnostics` before reporting utilization. `slot_fragmentation` means running job slots are full but one or more slots are 1GPU singletons; do not submit beyond the configured account cap to compensate. Future refills should pack independent children into 2GPU, wide, or GPU-fill allocations. `dependency_held_followups` means queued follow-ups are blocked by explicit dependencies; use dependencies only for strict ordering, otherwise prefer plain queued follow-ups so Slurm/QOS can start the next eligible packed job as soon as a running slot opens.

Resource history ledger: keep recent CPU/GPU request and queue outcomes in `$SLURM_DIR/work/hpc_resource_history.jsonl`. The macOS monitor records changed queue/resource snapshots automatically through `hpc_queue_summary.py --history-log`; for manual updates run `cd "$SLURM_DIR" && "$PY" hpc_queue_summary.py --json --history-log work/hpc_resource_history.jsonl >/tmp/bjtu_hpc_queue_summary_current.json`. Backfill recent native Slurm evidence with `cd "$SLURM_DIR" && "$PY" hpc_resource_history.py --backfill-days 14 --summary`. Use this ledger before optimization or scheduling-policy changes to measure observed shapes, states, pending reasons, submit/start timing, and cluster node CPU/GPU availability. Keep it local and uncommitted; it may contain account aliases, job names, and cluster account ids, but must not contain portal tokens, cookies, passwords, temporary certificates, or local absolute paths.

Use the resource snapshot to generate candidates from allowed, non-reserved nodes only:

```text
for each node in cluster_resources.nodes not in excluded_reserved_nodes:
  free_gpu = node.gpu_free
  free_cpu = node.cpu_free
  2GPU candidates fit only if free_gpu >= 2 and free_cpu >= 2 * cpus_per_task
  1GPU candidates fit only if free_gpu >= 1 and free_cpu >= cpus_per_task
  GPU-fill fits only if explicit GPU-first mode and free_cpu >= 2 * requested_gpu
```

For queued jobs, do not blindly keep CPU-rich `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` when the current node snapshot shows no unreserved node can satisfy them or when `2GPU/12CPU` would occupy the same GPUs sooner. Preselect the highest-scoring same-node shape that currently fits, then let the pre-submit gate verify it: maximize GPU count first, then prefer `6` CPUs per GPU; raise CPU only for explicit CPU-rich intent or immediate-start proof, and lower to `4` only for resource-wait fallback. Try `2GPU/12CPU` first for ordinary packed pairs and `2GPU/8CPU` when 1:6 cannot start directly because of `Resources`, reservation, same-node CPU pressure, or GPU/GRES shape pressure. If a same node has many GPUs but limited CPUs, generate one wide allocation with `N=3..8`, `--ntasks=N`, `--gres=gpu:N`, and `--cpus-per-task=6` first, then `4` only for fallback; intermediate exact-fit values such as `5` are allowed when test-only proves immediate start. If only single GPUs fit, generate the 1GPU compatibility candidates. If GPU-first mode is active and a CPU-poor same-node fragment can be claimed only with `2` CPUs per GPU, generate the single GPU-fill allocation as the low-CPU exception. Score candidates by immediate GPUs occupied, fewer stranded GPUs on that node, higher CPU per child without delaying start, then preserving queue position. Record the snapshot timestamp, selected node/free resources, skipped larger or CPU-richer shapes, and final test-only result in launch notes.

Adaptive CPU policy: if an account has no `RUNNING` packed job and its non-terminal packed jobs are waiting mainly for Slurm `Priority` or `Resources`, later refill submissions for that account may proactively reduce the CPU request through the pre-submit gate to improve schedulability. Choose the largest GPU-count candidate that can plausibly start from the current same-node snapshot, but use ordinary `2GPU/12CPU` as the default packed backlog and prefer `2GPU/8CPU` when 1:6 would wait for `Resources`, reservation, or same-node CPU pressure. Use CPU-rich packed shapes such as native `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` only when the snapshot/test-only result or recent resource-history ledger suggests they are competitive, or when the user explicitly wants CPU-rich queued follow-ups. Keep `--gres=gpu:2 --gres-flags=disable-binding`, and lower `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `NUMEXPR_NUM_THREADS` accordingly. Do not cancel or resubmit historical higher-CPU jobs solely because they are pending for `Priority` or `Resources`; preserve queue position unless the user explicitly authorizes replacement. Treat `6` CPUs per child as the ordinary packed target for evidence-producing training.

Resource-wait 4-CPU policy: for any selected auth account, when the ordinary native `2GPU/12CPU` shape (`--ntasks=2 --cpus-per-task=6`) cannot start directly because of `Resources`, reservation constraints, node CPU availability, or another resource-shape allocation failure, the pre-submit or authorized replacement path may continue to native `2GPU/8CPU` (`--ntasks=2 --cpus-per-task=4`). This fallback shape is only for scheduler resource/reservation/CPU pressure with no dependency, node pin, feature constraint, or QOS running-job cap; do not use it for pure `Priority` or `QOSMaxJobsPerUserLimit`. For this shape set child thread limits (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `NUMEXPR_NUM_THREADS`) to `4`, record the reason, and do not go below `4` CPUs per child. If a pending run-slot job is already fallback `2GPU/8CPU` and no same-node `1GPU/4CPU` or GPU-fill candidate fits, preserve queue position and report same-node CPU exhaustion instead of canceling/replacing.

GPU-fill fragment policy: when the user explicitly prioritizes occupying available GPUs, and native Slurm evidence shows a same-node CPU-poor GPU fragment, use one native allocation instead of multiple singleton jobs. Generate a single multi-child script with `--nodes=1 --ntasks=<N> --cpus-per-task=2 --gres=gpu:<N> --gres-flags=disable-binding`, where `N = min(free_gpu, floor(free_cpu / 2), available independent single-GPU child experiments)`, provided the selected account can submit or run one additional allocation under current QOS limits. Prefer `N >= 3`, but allow `N = 2` as a tail-fill exception when that is the largest allowed same-node allocation and ordinary/fallback packed shapes would leave the fragment idle. Each task launches exactly one independent single-GPU child and sets all thread limits to `2`. This is a nonstandard low-CPU exception for CPU-poor fragments, not the default evidence shape. If the same GPU count can run with more CPU, such as `8G/40C` or `4G/16C`, prefer the higher-CPU wide shape under `--cpu-policy balanced`. Record `gpu-fill-fragment`, the node/free-resource snapshot, child labels, and the reason. Do not use it for true multi-GPU/DDP experiments, dependency-held jobs, reservations that exclude the account, or when fewer than two GPUs can be claimed in the same allocation. Prefer this single allocation over separate `1GPU/2CPU` jobs so the scheduler either grants the fragment or leaves queue order intact.

Low-memory GPU-sharing policy: BJTU V100 nodes are `Tesla V100-PCIE-32GB`. When each independent single-GPU child has observed or strongly bounded peak VRAM below `16GB`, a packed or wide native allocation may request `N` physical GPUs and launch up to `2N` child processes, mapping at most two child labels to each allocated GPU id. Use `--nodes=1 --ntasks=<2N> --cpus-per-task=<C> --gres=gpu:<N> --gres-flags=disable-binding` so Slurm accounts CPU for each code execution while GPU count remains physical. Do not use this for true multi-GPU/DDP code, unknown-memory runs, warm-up-unstable runs, or any child whose peak memory reaches or approaches `16GB`. If the planner or sbatch builder has no explicit GPU-sharing flag, use it only to evaluate physical node resources, then generate or manually review the exact sharing sbatch script and preflight that script with `sbatch --test-only`. Record `low-vram-gpu-share`, peak-VRAM evidence, requested physical GPUs, child count, CPU per child, and per-GPU child mapping. Child logs should include early `nvidia-smi` samples; any OOM or peak memory reaching `16GB` disables sharing for that experiment family.

Native exact-script helpers:

```bash
cd "$SLURM_DIR"
"$PY" hpc_native_sbatch_builder.py \
  --job-name JOB_NAME --gpus N --cpus-per-task C \
  --manifest children.json --output candidate.template.sbatch
"$PY" hpc_resource_planner.py --available-children N \
  --test-only-probe --probe-script candidate.template.sbatch \
  --write-selected-script candidate.selected.sbatch
"$PY" hpc_native_submit.py candidate.selected.sbatch --auth-account NAME \
  --expected-gpus N --expected-ntasks N --expected-cpus-per-task C
"$PY" hpc_native_submit.py candidate.selected.sbatch --auth-account NAME \
  --expected-gpus N --expected-ntasks N --expected-cpus-per-task C --submit
```

Use `hpc_native_sbatch_builder.py` for all wide/GPU-fill allocations so the requested GPU/task count equals the number of child commands. For low-memory GPU-sharing, the requested physical GPU count is lower than the child-command count by design; the script must explicitly map two child commands to each allocated GPU id and verify `NumTasks=2N`, `CPUs/Task=C`, and `gres/gpu=N`. Use `hpc_native_submit.py` for native GPU training preflight and submission; the command without `--submit` is read-only, and the command with `--submit` submits only after exact-script `bash -n` plus `sbatch --test-only` succeeds, then verifies native Slurm allocation through `scontrol`.

Single-GPU compatibility policy: keep packed `2GPU` jobs as the default, but allow native 1GPU singleton jobs when the packed 2GPU shape cannot be scheduled and a native 1GPU singleton can run. This is a 2GPU-to-1GPU scheduling fallback for pairs of independent single-GPU child experiments, not an automatic rewrite for true multi-GPU/DDP experiments that require two GPUs in one process. Acceptable evidence includes `sbatch --test-only`/`scontrol` messages that `gres/gpu:2` cannot be satisfied, `Requested node configuration is not available` for the 2GPU request, `sinfo`/`scontrol show node` evidence that only isolated single GPUs are usable on allowed unreserved nodes, reservation/node co-location pressure specific to `gres/gpu:2`, or a direct preflight result where `1GPU/6CPU` can run while all applicable packed 2GPU candidates cannot. In this case, split the same experiment pair into one or two singleton jobs, one child experiment per Slurm job, preserving the original child labels, parameters, account-local code path, output path, and environment. Test `--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding` first; if it cannot run directly because of CPU/resources, test fallback `--cpus-per-task=4`. Do not use this compatibility path for pure `Priority`, `QOSMaxJobsPerUserLimit`, dependency holds, a true multi-GPU job, or a 2GPU request that can run with the fallback `2GPU/8CPU` shape. Count each singleton as one non-terminal launch unit against the selected account's job cap, record the skipped 2GPU reason plus the singleton job ids, and treat it as slot-fragmenting until it completes. Future refills for that account should prefer packed/wide/GPU-fill jobs to restore GPU density per running slot.

Authorized replacement policy: when the user has explicitly authorized cancel-and-resubmit for this condition, and for normal reductions the selected account has no `RUNNING` packed job while same-project packed jobs are stuck mainly in `Resources` or reservation/node-CPU pressure, it is acceptable to replace only the relevant same-project `PENDING` packed job(s) with reduced native `2GPU/12CPU` submissions if they are currently CPU-richer. If the target job is already native `2GPU/12CPU` and the same resource/reservation/CPU blocker remains, it is also acceptable to replace the relevant same-project `PENDING` packed job(s) with fallback `2GPU/8CPU` submissions under the resource-wait 4-CPU policy above. If the target job is already fallback `2GPU/8CPU` and same-node CPU exhaustion prevents both `1GPU/4CPU` and GPU-fill candidates, replacement is not a repair; keep queue position and report Slurm timing. Under GPU-fill fragment mode, it is acceptable to replace one or more same-account same-project `PENDING` packed jobs, including lower-priority pending jobs, only when every canceled child is included in the same replacement allocation and the replacement claims more GPUs immediately than the original runnable candidate would. Under the single-GPU compatibility policy, if the pending 2GPU job cannot be scheduled as packed 2GPU but its child experiments are single-GPU capable and 1GPU preflight passes, it is acceptable to replace that exact same-project packed job with one or two `1GPU/6CPU` singleton jobs, falling back to `1GPU/4CPU` only when the 6-CPU singleton cannot run directly. Before `scancel`, verify with `scontrol show job` that the target job is still `PENDING`, belongs to the current project/experiment queue, and is the exact experiment pair or child set being replaced. Never cancel `RUNNING` jobs, terminal jobs, or unrelated jobs. Preserve child labels and parameters in the replacement, run the normal local/remote `bash -n`, `sbatch --test-only`, and post-submit `scontrol` checks, then record old job id, replacement job id(s), resource-shape change, reason, and any skipped candidates in the project experiment index. For pure `Priority` blockers without `Resources`, reservation pressure, GPU/GRES scarcity, or 2GPU resource-shape pressure, preserve queue position unless the user explicitly asks to trade queue priority for a lower-resource retry or GPU-fill fragment mode can include the exact pending children in a larger same-allocation fragment claim.

Run-slot diagnosis policy: when an account has fewer than two `RUNNING` packed jobs but already has four non-terminal packed jobs, do not assume that the submit pass failed. Use native Slurm state first:

```bash
cd "$SLURM_DIR" && "$PY" hpc_pending_reason.py --auth-account NAME
```

For pending jobs that are intended to occupy the first two run slots, inspect `scontrol show job -dd <job_id>` fields including `JobState`, `Reason`, `Dependency`, `ReqNodeList`, `ExcNodeList`, `Features`, `OverSubscribe`, `GresEnforceBind`, `NumCPUs`, `NumTasks`, `CPUs/Task`, `TRES`, `TresPerNode`, `SchedNodeList`, `StartTime`, and `LastSchedEval`. If the reason is `QOSMaxJobsPerUserLimit`, the account is already at the cluster running-job limit and queued follow-ups are behaving normally. If the reason is `Resources` or reservation/node-CPU pressure and the job is already native `2GPU/12CPU` (`NumTasks=2`, `CPUs/Task=6`, `TresPerNode=gpu:2`) with no dependency, node pin, or feature constraint, re-submitting the same packed shape is not a repair; authorized replacement should test fallback `2GPU/8CPU` (`CPUs/Task=4`) for that same account or preserve queue position. If the job is already fallback `2GPU/8CPU` (`NumTasks=2`, `CPUs/Task=4`, `gres/gpu=2`) and visible free GPUs are only on nodes with too few same-node CPUs even for `1GPU/4CPU` or GPU-fill, no allowed replacement can improve schedulability; preserve queue position and report `SchedNodeList`, `StartTime`, and `LastSchedEval`. If native evidence shows the packed `gpu:2` shape cannot be scheduled while `gpu:1` singleton preflight passes, authorized replacement may split the pair into `1GPU/6CPU` singletons with `1GPU/4CPU` as the fallback. If the reason is pure `Priority`, lowering CPU or splitting to singletons is unlikely to repair ordering; preserve queue position and report the Slurm-provided `SchedNodeList` and `StartTime` if present.

When free GPUs appear to exist but a packed job still waits for `Resources`, check CPU and reservations, not just GPU counts:

```bash
sinfo -N -p GPU -o '%N|%t|%C|%G'
scontrol show node=<node> -o
scontrol show reservation
```

An apparently free node is usable only if the same node has enough unallocated CPUs for the requested shape and the current user is allowed by any active reservation. A reserved node that does not include the current user must be treated as unavailable even when `sinfo`/`scontrol show node` show idle GPUs or CPUs. If the pending job is already at the ordinary `6` CPUs-per-child target and is blocked by `Resources`/reservation/node-CPU pressure, an authorized replacement may try the fallback `4` CPUs-per-child packed shape for the same account. If the user has requested GPU-first behavior and the node has a CPU-poor GPU fragment that can claim at least two GPUs with `2` CPUs per GPU, use the GPU-fill fragment policy before leaving GPUs stranded. If no allowed node can schedule the packed two-GPU shape but one GPU is schedulable, split the pair into native 1GPU singleton exceptions with 6 CPUs by default and 4 CPUs as the fallback. If the job is already fallback `2GPU/8CPU` and only pure `Priority` remains, prefer waiting, using another valid account, or asking the user before making an explicit single-GPU exception. If it is already fallback `2GPU/8CPU` and still waits on `Resources` because the only visible GPUs have `cpu_free < 2 * requested_gpu`, preserve queue position and report same-node CPU exhaustion; do not cancel/re-submit. Do not submit beyond the configured account cap to work around a scheduler-side `Resources` or `Priority` blocker.

Pre-submit runability gate:

1. Load or refresh the monitor resource snapshot (`hpc_queue_summary.py --json`) and generate the exact remote sbatch script for the highest-scoring same-node candidate that the snapshot says can fit for the selected auth account, account-local code path, output path, and environment.
2. For each CPU candidate, update both `#SBATCH --cpus-per-task` and child thread limits (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`) before testing.
3. Run `bash -n <script>` and `sbatch --test-only <script>` through the portal SSH proxy before any real `sbatch`.
4. For packed jobs, test only candidates that the monitor snapshot says can fit on at least one unreserved same node, starting with `--cpus-per-task=6` for ordinary jobs. Test CPU-rich `--cpus-per-task=8`, `12`, or `16` only when explicitly requested or when test-only evidence shows immediate start without reducing GPU occupancy. If the `--cpus-per-task=6` candidate cannot start directly because of `Resources`, reservation constraints, node CPU availability, or another resource-shape allocation failure, continue to fallback `--cpus-per-task=4` when the snapshot supports it. If GPU-fill fragment mode is active, inspect the target node's free CPU/GPU and test a single multi-child fragment script such as `--ntasks=4 --cpus-per-task=2 --gres=gpu:4` before falling back to separate singleton jobs. If the packed 2GPU path still cannot be scheduled and the children are single-GPU capable, generate single-GPU singleton scripts and test the largest fitting singleton shape from `--ntasks=1 --cpus-per-task=6 --gres=gpu:1`, then fallback `--cpus-per-task=4`.
5. Reject a candidate and test the next lower CPU shape if syntax fails, `sbatch --test-only` returns nonzero, or output indicates it cannot run directly: `BadConstraints`, `Requested node configuration is not available`, allocation failure, GRES/CPU binding failure, QOS submit/job limits, or a delayed start caused by the requested CPU/GPU shape.
6. Submit only the highest-scoring candidate that passes this gate, prioritizing immediate GPUs occupied, then fewer stranded GPUs, then higher CPU per child when it does not delay start. If no ordinary candidate can run directly, submit only the lowest valid fallback when it at least passes `sbatch --test-only`, and report the expected pending reason/start estimate. For packed jobs, fallback `2GPU/8CPU` is allowed when `2GPU/12CPU` remains blocked by `Resources`, reservation constraints, node CPU availability, or another resource-shape allocation failure. GPU-fill fragment jobs may use `2` CPUs per child only when they claim at least two GPUs in the same allocation and are explicitly recorded. When the packed 2GPU shape still cannot be scheduled but 1GPU can run, singletons may use `1GPU/6CPU` or fallback `1GPU/4CPU`. If the 4-CPU packed fallback, GPU-fill fragment shape, and the single-GPU compatibility shape all fail test-only, stop and report the blocker instead of submitting.

Checklist:

1. Request one batch allocation with the required GPU count and start the pre-submit gate from the largest GPU-count same-node shape that the monitor resource snapshot currently shows as feasible. For packed pairs, ordinary evidence jobs use `--gres=gpu:2`, `--ntasks=2`, `--cpus-per-task=6`, and `--gres-flags=disable-binding`. Use CPU-rich `--cpus-per-task=8`, `12`, or `16` only when explicitly requested or when one non-reserved node plus test-only evidence show immediate start without reducing GPU occupancy. If `2GPU/12CPU` is blocked by `Resources`, reservation constraints, node CPU availability, or another resource-shape allocation failure, retry fallback `--cpus-per-task=4`. If GPU-fill fragment mode is active and a node has a same-node fragment such as `4` free GPUs and `8` free CPUs, generate one allocation for all usable GPUs, for example `--ntasks=4 --cpus-per-task=2 --gres=gpu:4`, and launch four independent one-GPU children inside that allocation. If the packed 2GPU request still cannot be scheduled and the children are single-GPU capable, split the pair into native singleton jobs and test the largest fitting singleton shape from `--gres=gpu:1 --ntasks=1 --cpus-per-task=6`, then fallback `--cpus-per-task=4`. Do not go below `6` CPUs per child for ordinary packed/singleton jobs, below `4` CPUs per child for explicitly recorded resource-wait packed or 1GPU compatibility jobs, or below `2` CPUs per child for GPU-fill fragment jobs. If the selected account currently has no running packed jobs and is only waiting for `Priority`/`Resources`, it is acceptable for the gate to choose `--cpus-per-task=6`; use fallback `4`, GPU-fill `2`, or 1GPU split only for the documented resource/reservation/2GPU-shape or fragment exception while recording the reason.
2. In the batch script, read the allocation-provided `CUDA_VISIBLE_DEVICES` and split it into child lanes. Do not hardcode physical `0/1`; prior tests showed this can escape the allocated GPU ids.
3. For each child, set `CUDA_VISIBLE_DEVICES` to exactly one allocated id, run a lightweight `nvidia-smi` and `torch.cuda.device_count()` sanity check, then launch the experiment.
4. Save a batch stdout plus one child log per lane under the selected account's home, for example `/data/home/<cluster_user>/jobs/<job_name>_<job_id>_pair/`.
5. After submission, run native checks:

```bash
cd "$PROJECT_DIR"
"$PY" "$SLURM_DIR/hpc_pending_reason.py" <job_id> --no-sinfo
```

6. Verify the expected fields: `JobState=RUNNING`, `Reason=None`, `NumCPUs`, `NumTasks`, `CPUs/Task`, `TRES=...gres/gpu=<N>`, `TresPerNode=gpu:<N>`, and the node name.
7. Download or tail child logs and verify that each child reports one visible CUDA device and has entered real training before calling the launch successful.

After checking existing project jobs and the `per-user job-cap` QoS behavior, submit enough packed jobs to fill the selected account to the four packed-job cap when experiment pairs are available.

## Paths

- Portal SSH/SFTP must go through `hpc_winscp_info.py`; observed proxy is `<proxy_host>:<proxy_port>`, username shape `cluster2,<cluster_account>`.
- Portal SSH uses a temporary certificate token, not the local SSH key.
- Reusable code belongs under `/data/home/<cluster_account>/code/<project>`; portal path `home/code/<project>`.
- Portal job work/output dirs are under `/data/home/<cluster_account>/jobs/<job_name>_<timestamp>`.
- Trust job-side probes for runtime facts, not login-node inference.

For project experiment evidence:

- Portal snapshots: `$PROJECT_DIR/hpc_stdout/bjtu_jobs_YYYYMMDD_HHMM*.json`
- Native Slurm snapshots: `$PROJECT_DIR/hpc_stdout/bjtu_pending_reason_YYYYMMDD_HHMMSS*.json`
- Downloaded launch logs: `$PROJECT_DIR/hpc_stdout/bjtu_<jobid>_<shortname>_launch_YYYYMMDD.log`
- After a material launch/status change, update the project-local status files, experiment tracker, server-path notes, and narrative report when those files exist.

Download pattern:

```bash
"$PY" "$SLURM_DIR/hpc_download.py" "/data/home/<cluster_account>/path/to/remote.log" -o "$PROJECT_DIR/hpc_stdout/bjtu_<jobid>_<shortname>_launch_YYYYMMDD.log" --no-progress
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

- Choose a dataset name that encodes family, split/source, and version, for example `vision_subset_seed0_v1`. Do not reuse one `<dataset_name>` for different class splits or preprocessing variants.
- For project-aligned datasets, record the canonical BJTU root and any optional other-account symlink in the project notes, then point training configs at `/data/home/<cluster_account>/dataset/<dataset_name>`. Do not silently use a legacy dataset root for an aligned-split experiment.
- Before using a newly uploaded dataset in training, validate counts and write a manifest under `_manifests`. Training scripts should point to the canonical root, not `_uploads`, `_archives`, or a temporary extraction directory.
- Current archive task: `dataset-archive`; source-side screen: `bjtu-resume-archive`.
- Preferred command: `cd "$SLURM_DIR" && "$PY" hpc_transfer_app.py run dataset-archive --method parallel-chunk --parallel 4 --chunk-mib 8 --buffer-mib 4`.
- Never delete or reset `/data/home/<cluster_account>/dataset/data/_archives/<archive_name>.tar.gz.part` unless explicitly asked.
- Never run two upload workers writing the same archive `.part`; stop the old source-side `screen` first.
- When source-host SSH is slow or hangs, use cluster-side file size/progress as truth.

## Post-Submit Evidence Checklist

Before reporting a job as running:

- Portal row is present and the expected `jobId`, `ngpus`, `ncpus`, and node are recorded.
- Native Slurm reason was checked. If pending, report the exact `Reason`; if running, record `Reason=None`.
- CPU/GPU allocation matches the intended shape.
- Startup logs were downloaded or tailed locally.
- At least one real training/progress line was observed, not only environment setup.
- Evidence files were saved under the project `hpc_stdout/` when working from project.

## Safety

- For cross-account dataset sharing, inspect ACLs first; do not apply ACL/chmod changes without explicit confirmation.
- Keep tested limits, dataset layout, speed findings, and native Slurm fallback examples in project-local documentation.
