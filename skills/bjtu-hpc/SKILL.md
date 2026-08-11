---
name: bjtu-hpc
description: "BJTU HPC portal workflow for the local `slurm` workspace: configure a new controller, migrate saved account metadata/secrets through passphrase-encrypted private JSON, refresh/save portal tokens, inspect native quota and queue state, plan content-addressed external HDF5 data supply, upload/download only with authorization, and run one or many anonymized native Slurm tasks through a durable refresh-gated submit cycle with manifest validation, exact-script preflight, cycle-local connection reuse, rolling snapshots, receipts, task-level Job Array verification, and crash-safe reconciliation; also maintain the redacted Windows, Apple-native, and Kindle widgets."
---

# BJTU HPC

Use the helper scripts in the local `slurm` workspace as the canonical interface to the BJTU HPC portal.

## Read First

Controller Python contract: all local BJTU helper commands must use `HPC_PYTHON=<PYTHON3.12>`, which has Paramiko installed. On macOS, do not invoke a helper with bare `python3`; the system interpreter is Python 3.9 and is unsupported. On Windows, use the Python 3.12 `python.exe` path and run the helpers directly in PowerShell; WSL is not required. The helpers use POSIX modes on macOS/Linux and restricted NTFS ACLs on Windows for private account, token, journal, intent, and receipt files. Any legacy `python3` command shown in a reference below means the configured `HPC_PYTHON` executable.

Policy authority and drift checks: treat live helper output (`hpc_doctor.py --json`, `hpc_accounts.py`, `hpc_queue_summary.py --json`, monitor/widget snapshots, and helper `--help` defaults) as authoritative for current auth, queue state, account caps, and resource snapshots. Compliance policy in this skill overrides stale queued, packed, wide, or GPU-sharing planner modes. New evidence-producing experiments use direct-start admission: `max_project_running_per_account` defaults to `2`, `max_hpc_admissions_per_cycle` defaults to `8`, and queued follow-up backlog remains disabled. A current-project `PENDING` job blocks only its BJTU account/resource pool; continue considering independent accounts and non-BJTU pools after a fresh snapshot. Propagate the block only when live evidence identifies a common `shared_limit_ref` or a shared QOS/user cap. `hpc_queue_summary.py` emits an anonymized per-user ref and marks it blocked only for exact per-user Slurm limit reasons; ordinary `Priority`, `Resources`, or account-local pending must not propagate. Every physical submit still requires a fresh snapshot, exact-script checks, submit, post-submit verification, and another refresh, so the cycle limit is not batch authorization. Before changing these defaults, scan both `bjtu-hpc` and `bjtu-hpc-submit` for `--cap`, `run-slots`, `admission-mode`, `max-admissions-per-cycle`, `queued follow-up`, `allow-queued`, and `QOSMaxJobsPerUserLimit`, then update the paired text and helper contract together.

For any live HPC or remote GPU work, start read-only unless the user explicitly asked to submit, cancel, delete, reserve, chmod, or otherwise mutate state.

For a transition-kernel AutoResearch run, require the caller's exact backend
action packet and Execution Authorization receipt. Before `sbatch`, the caller
must durably record a `submit_intent` through
`autoreskill-workflow goal.py transition record-native`, including the stable
action id and SHA-256 of the exact rendered sbatch script. Retain live resource
planning, syntax checks, `sbatch --test-only`, explicit submission authority,
and one post-submit refresh. Immediately return the native job id, script SHA,
account/pool, native status, observation id, and verified GPU/task/CPU shape so
the caller can record the matching native observation. A missing or ambiguous
response requires an exact Slurm lookup by trace/native identity; it never
authorizes a second submit.
The final Result Envelope must carry that native observation receipt and the
same script SHA. Do not mutate Queue, Runtime, finding, or paper state directly
from this skill.

For AutoResearch placement, keep stable compatibility separate from live Slurm
state. A project `RESOURCE_CAPABILITY_PASSPORT.json` may prove that this account
has the exact code/data/checkpoint/runtime/path components for one execution
profile; `hpc_queue_summary.py` and exact-script preflight still own volatile
capacity and launchability. A capability proof never authorizes submission, and
a failed path/hash/runtime preflight invalidates only the implicated profile
components.

Runtime environment policy: for new evidence-producing GPU training, never use `pytorch1.7-python3.8` or `/data/apps/anaconda/anaconda3/envs/pytorch1.7-python3.8/bin/python`. Prefer an account-local Python `3.10` environment under `/data/home/<account>/envs/torch251-cu121-py310` with PyTorch `2.5.1` + CUDA `12.1`, because this is the closest validated PyTorch CUDA wheel to the platform's CUDA `12.0`/driver `525.105.17` stack. Use the `cu118` PyTorch `2.5.1` fallback only after a GPU-node smoke test shows `cu121` is unusable. Use `module purge && module load PyTorch-GPU` only as a recorded fallback when the account-local environment is unavailable or fails and the platform module itself passes a GPU-node smoke test. Every sbatch template must log `python`, `torch.__version__`, `torch.version.cuda`, `torch.cuda.is_available()`, and GPU name before training.

Seed cap policy: one experiment family means one dataset/config/method profile
and may use at most three unique random seeds total across jobs, arrays,
accounts, HPO, and retries. A retry reuses its original seed. Treat this as an
evidence hard limit, not only a scheduler default: if all three seeds fail to
support the method, mark effectiveness doubtful instead of searching for a lucky
seed. Use independent tracks, datasets, controls, or declared ablations for
additional concurrency.

Data I/O policy: new AutoResearch evidence-producing datasets default to `external_hdf5_shadow` and then `external_hdf5_enforced`. Build `image_bytes_indexed_v1` HDF5 shards on an SSH-accessible external data factory; BJTU HPC must not persist the source raw tree or a tar archive for that artifact. The project freezes artifact schema plus dataset-semantics, source-inventory, build-contract, converter, artifact-content, consumer and validator hashes, then binds a detached validation attestation/report produced by an exact consumer probe. Place one immutable owner copy under `/data/home/<owner>/autoreskill_data/artifacts/<artifact_content_sha256>/` and share it read-only only after native ACL/access verification. If an account cannot read that exact artifact, mark it non-fitting; do not silently create another copy. The older account-local archive/LMDB/HDF5/TFRecord path remains available only for `legacy` projects and must not be selected as an external-mode fallback. Read `references/external_hdf5_data_supply.md` before planning or consuming an external artifact.

Persistent symlink prohibition policy: do not create or consume project-managed symlinks on BJTU HPC for code, dataset, checkpoint, weight, environment, manifest, config, run, log, output, or artifact paths. Cross-account consumers must open the verified owner artifact by its direct absolute path plus minimal read-only/traverse ACLs, or use a verified physical copy when adjacent writes are unavoidable; never create a target-home alias symlink. The only project-managed exception is node-local disposable dataset/cache data when both the symlink entry and its resolved target are under `/dev/shm/bjtu_data_artifacts/` or `/dev/shm/bjtu_dataset_cache/`, after allocation and exact readiness/identity checks. Before transfer, capability admission, or submit preflight, use `lstat`, `readlink`, and `realpath` on every declared root and required file and fail closed on any persistent project-managed symlink or path escape. Existing noncompliant links are infrastructure blockers; inspect and replace them without deleting their targets. System-managed links outside project paths, such as the cluster CUDA installation, are not project artifacts and remain outside this rule.

Shared-memory staging policy: external mode uses `bjtu_stage_hdf5_artifact_to_shm` from `hpc_shm_cache.sh`, keyed by the full `artifact_content_sha256` plus exact READY-file SHA-256, never by dataset name. The nested attestation key lets distinct validated consumers coexist for one core. Staging copies only manifest-declared HDF5 members and the selected report/READY pair. Resolve global member/aggregate artifact receipts before scheduling duplicate multi-gigabyte verification, but treat them only as candidate locations; each account still needs current readability evidence and project capability import. Until the native storage provider supplies an audited generation fingerprint, cache reuse requires recomputing the manifest, attestation and report identities and every shard hash. A mismatch is fail-closed and is never overwritten or deleted. A capacity miss may fall back only to the already verified owner artifact root with the same complete identity; it may not fall back to raw, archive, another packed build, or a same-name cache. The legacy helper `bjtu_stage_packed_to_shm` retains relaxed behavior only for explicitly legacy runs. `/dev/shm` remains node-local disposable cache and never becomes artifact authority.

Node-local cache management policy: never treat a cache observed on one GPU node as available on another node. Pre-submit planning may estimate capacity but cannot depend on a cache hit because Slurm may choose a different node. Every sbatch script runs the cache decision after allocation and records the actual node. For external mode, the verified single-owner artifact is the persistent authority and `/dev/shm/bjtu_data_artifacts/<artifact_content_sha256>/<READY-file-sha256>/` is disposable. For legacy mode only, the account-local packed root remains authority. Optional pre-warm jobs require explicit approval and follow the same exact-identity, capacity, lock and `.ready` rules.

Project layout policy: use neutral project roots and slugs. On BJTU cluster accounts, place project files under `/data/home/<account>/projects/<project_slug>/` with subdirectories such as `code/`, `runs/`, `logs/`, `outputs/`, `manifests/`, and `tmp/`. Do not create or recommend legacy framework-specific folder names for new HPC work.

Experiment anonymization policy: every submitted experiment must receive an anonymous per-submission trace hash before generating the sbatch script. Slurm-visible job names, queue keywords, run/log/output basenames, and Git-safe evidence filenames must use the trace hash, not saved auth-account aliases, cluster usernames, portal usernames, emails, or real person names. Hash-based queue names are allowed and preferred, for example `hpc_<12hex>`. Use a separate stable `plan_hash` only for deduplicating planned work. Keep the reversible mapping in a local private ledger only, with file mode `0600`, and never commit or paste that ledger.

## Reference Index

Load only the reference files needed for the user's task:

- `references/anonymization.md`: anonymous trace-hash naming, private trace ledger, account redaction, and pre-submit lint for Slurm-visible names. Read this before any GPU submit, running-only monitor action, job-array generation, or public report about submitted experiments.
- `references/auth_dashboard.md`: token refresh, saved accounts, CAS credential prefill, visible Playwright login, Web dashboard, Token Guardian, macOS widget token actions, dashboard LaunchAgent service, and SSH/SFTP proxy discovery.
- `references/environment_setup.md`: new-controller Python 3.12 setup, dependency installation, scrypt/AES-256-GCM account JSON encryption/decryption, conflict handling, token validation, browser-profile reset, and migration cleanup. Read this before configuring another computer or moving saved BJTU accounts.
- `references/windows_setup.md`: native Windows Python 3.12 setup, PowerShell environment variables, NTFS secret ACLs, Task Scheduler dashboard service, diagnostics, and read-only verification. Read this before configuring or repairing a Windows controller.
- `references/windows_widget.md`: canonical Windows 10 desktop and Windows 11 Widgets Board sources, version-selection gate, redaction boundary, current 3.5/build 19 contract, and build/install workflow. Read before changing or installing the Windows widget.
- `references/windows_widget_component_lock.json`: machine-readable lock tying the canonical Windows source to the current cross-platform HPC widget contract.
- `references/apple_native_widget.md`: Apple-native WidgetKit source-selection gate, information hierarchy, UI rules, redaction boundary, runtime paths, build/deploy contract, and visual QA. Read before changing the BJTU HPC desktop component.
- `references/apple_native_widget_component_lock.json`: machine-readable lock for the currently deployed widget path, version, canonical UI source, and explicitly legacy UI roots. Do not hand-edit it before deployment; update it only after verifying the installed component.
- `references/kindle_dashboard_sync.md`: privacy-minimized HPC Widget snapshot rendering, macOS LaunchAgent publication, GitHub-versus-SSH edge selection, private-CA HTTPS deployment on a non-root server, Kindle trust/configuration, ETag and atomic-update checks, RTC wake validation, and rollback. Read before creating, deploying, repairing, or validating an HPC status lock screen for Kindle or another e-ink client.
- `references/data_transfer.md`: portal upload/download, dataset root conventions, per-account archives, safe packed-dataset copying, ACL checks, persistent-symlink prohibition, account-local environments, and upload progress.
- `references/data_backend.md`: LMDB/HDF5/TFRecord conversion requirements, reusable dataset naming, `/dev/shm` runtime staging, `DATA_BACKEND` contract, train/test split rules, validation reports, and single-seed smoke-test expectations. Read this before any new evidence-producing training that touches datasets.
- `references/external_hdf5_data_supply.md`: external SSH factory, complete data identity, probe-bound detached attestation, quota snapshot, one-owner placement, receipt-bound registry, strict cache, rollout, and fail-closed remote gates. This is the default reference for new AutoResearch datasets.
- `references/gpu_scheduling.md`: compliant native Slurm GPU submissions, Job Array or independent 1GPU job patterns, running-only submission behavior, resource planner usage, CPU fallback order, pending diagnosis, and queue-monitor policy. Read this before any evidence-producing GPU submit or resource-shape change.
- `references/submit_cycle.md`: manifest-driven single/multi-task controller, rolling-snapshot optimization, cycle-local SSH reuse, dry-run/submit commands, private journal, resume, and unknown-outcome reconciliation. Read this before one-command submission of one or more tasks.
- `references/job_inspection.md`: portal API compatibility jobs, current queue summaries, pending reasons, native allocation checks, and runtime environment probes.
- `references/guardrails.md`: credential, submit, dataset-sharing, upload, and scheduling safety guardrails. Read when changing policy or when an operation can mutate cluster or local state.
- `references/hpc_workflow.md`: validated platform results and environment notes.

## Core Commands

```bash
HPC_PYTHON=<PYTHON3.12>
"$HPC_PYTHON" hpc_accounts.py list
"$HPC_PYTHON" hpc_accounts.py export-json <private.json> --include-tokens
"$HPC_PYTHON" hpc_accounts.py import-json <private.json> --use-exported-default --sync-legacy-token
"$HPC_PYTHON" hpc_queue_summary.py --details
"$HPC_PYTHON" hpc_queue_summary.py --json --jobs 4
"$HPC_PYTHON" hpc_plan_from_snapshot.py --admission-mode direct-start --max-admissions-per-cycle 8 --cap 2 --run-slots 2 --workload single --no-queued --planner-json
"$HPC_PYTHON" hpc_submit_cycle.py validate --manifest <manifest.json>
"$HPC_PYTHON" hpc_submit_cycle.py run --manifest <manifest.json>
"$HPC_PYTHON" hpc_submit_cycle.py run --manifest <manifest.json> --submit
"$HPC_PYTHON" hpc_native_submit.py ./candidate.sbatch --auth-account NAME --expected-gpus N --expected-ntasks N --expected-cpus-per-task C
"$HPC_PYTHON" hpc_native_submit.py ./candidate.sbatch --auth-account NAME --expected-gpus N --expected-ntasks N --expected-cpus-per-task C --submit --submit-intent <intent.json> --receipt-out <receipt.json>
```

Use `--auth-account NAME` for multi-account work. Prefer `hpc_queue_summary.py` for queue/resource snapshots because it queries native Slurm state through the portal SSH proxy and catches pending jobs that portal rows may omit.

## Scheduling Essentials

For evidence-producing GPU training, use native `sbatch` through the portal SSH proxy. Do not rely on the portal PyTorch-GPU app for CPU/GRES-sensitive training because it has produced wrong-shape `1CPU/1GPU` native allocations.

For a prepared single task or a prepared list of tasks, prefer `hpc_submit_cycle.py` over manually chaining snapshot, planner, and native-submit commands. Its default `run` is no-submit planning; require explicit `--submit` plus one durable intent per candidate for real side effects. One batch command is not batch authorization: the controller consumes only the current fresh `next_action`, submits one physical job or atomic array, verifies it, refreshes, and replans. Read `references/submit_cycle.md` before using this path.

Before every real GPU training submission, read `references/gpu_scheduling.md`, generate or update the exact sbatch script, run local/remote syntax checks plus `sbatch --test-only`, submit only a passing candidate, then verify the real Slurm allocation with `scontrol`.

Before `sbatch --test-only`, audit every experiment-bound project path with `lstat`, `readlink`, and `realpath`. Reject persistent project-managed symlinks and registered-root escapes. Permit a symlink only when both the link and resolved data/cache target are under the approved node-local `/dev/shm` cache roots and the allocation-side identity/readiness gate has passed.

Repeated-variant launch optimization: when an existing manifest, run record, or
private trace ledger for the same project/account identifies the same code
export, dataset/profile manifest, runtime environment, launcher template, and
resource shape, treat data/env/code validation as reusable evidence for a
seed or hyperparameter-only follow-up. Do not rerun full dataset conversion,
environment discovery, code upload audit, broad queue sync, or human wiki/status
rewrite just to submit that repeated variant.

For this optimization, prefer an explicit `launch_identity` object or equivalent
private trace fields: code export ref/hash, dataset profile plus manifest
ref/hash, runtime environment ref/probe, launcher template hash, resource shape,
method profile, and data backend. The stable identity hash must exclude the
seed and hyperparameters being intentionally varied. Do not rely on `plan_hash`
alone because the anonymization plan hash may include seed or array identity.

Never cache volatile submit safety. Every real GPU submit, including a
repeated-variant launch, still requires fresh queue/resource evidence for the
selected account, a new anonymous trace id, the exact generated sbatch script,
local/remote syntax checks, `sbatch --test-only`, real submission only after a
passing preflight, and post-submit `scontrol` allocation verification. If any
stable element changed or the manifest evidence is missing, fall back to
first-use checks.

Before generating the script, read `references/anonymization.md`, create a
stable plan hash, launch identity hash, and per-submission anonymous trace hash.
Use `#SBATCH --job-name=hpc_<trace_hash>` or another neutral trace-only name.
Store account-to-trace mapping only in the local private ledger.

Default launch unit: one Slurm-managed seed per allocation boundary. Use a Slurm Job Array with at most three task ids for multi-seed validation, or submit independent native `1GPU/6CPU` sbatch jobs. Each array task or independent job runs one seed and one training command. Do not use `srun --exclusive`, background `child_*.sh` processes, or one SBATCH allocation that manually launches multiple independent seed/child experiments.

Running-only cross-account submission is allowed when the user asks to keep HPC busy or does not restrict the launch to one account. Use all valid saved accounts, but submit only scientifically approved work that the live snapshot plus exact-script preflight indicates can start directly. An account with current-project `PENDING` work is blocked; another account with fewer than `max_project_running_per_account` current-project `RUNNING` jobs remains eligible unless live evidence ties both to the same blocked limit. Do not create queued follow-up backlog, add seeds, switch to packed jobs, or raise scientific budgets merely to fill cluster resources.

For a single experiment family, cap unique seed variants at three across all
jobs and accounts. A Job Array may contain at most three seed task ids, and
independent sbatch files may cover at most the same three seeds. When they are
all poor or inconclusive, report the method as likely ineffective or unstable;
do not add a fourth seed.

Refresh-gated admission rule: one planner pass authorizes at most its first `next_action`; later `admission_frontier` entries are candidates only. A controller cycle may complete up to `max_hpc_admissions_per_cycle` admissions, but each admission independently performs fresh snapshot -> replan -> exact-script lint/`bash -n`/`sbatch --test-only` -> one submit -> `scontrol`/state verification -> refresh. If the new job is `PENDING`, block that account or evidenced shared-limit group and continue only with independent pools from the refreshed plan.

When AutoResearch uses `admission_scope=global`, a current hashed first global
assignment is upstream project-admission evidence only. It does not authorize an
Slurm submit. The same controller must still run this BJTU fresh-snapshot,
exact-script, test-only, submit, verification, and refresh sequence for exactly
one physical job; the next global assignment requires a newly computed shared
schedule.

For every AutoResearch submit, the project queue must already be `submitting`
with a durable intent bound to the exact script hash, launch identity, trace id,
allocation, and queue revision. Pass that intent to `hpc_native_submit.py` and a
private mode-0600 `--receipt-out` path. The helper embeds the trace in Slurm
identity, writes the native job receipt immediately after `sbatch`, and verifies
shape. The controller then records the receipt as `needs_sync` and changes to
`running` only after authoritative Slurm observation. If the controller crashes,
search Slurm by trace/script/launch identity before any retry.

Use the monitor/widget resource snapshot and `hpc_resource_planner.py` only as candidate-generation evidence. Ordinary evidence-producing jobs should first try `1GPU/6CPU` (`--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding`). Fall back to `1GPU/4CPU` only when exact-script `sbatch --test-only` cannot run directly or would wait because of `Resources`, reservation, same-node CPU pressure, or GPU/GRES shape pressure. CPU-rich `1:8`, `1:12`, or `1:16` shapes are optional only when the user asks for CPU-rich jobs or snapshot plus test-only proves immediate start without reducing GPU occupancy. True DDP/multi-GPU single-experiment jobs may request more GPUs, but they must be one experiment process group, not seed packing or manual child splitting.

## Auth Essentials

For saved accounts, use `hpc_accounts.py` as the source of truth instead of the legacy token file. If auth blocks a user-requested status/progress/upload/submit task with `11009`, `11011`, `11012`, HTTP `401`, or an auth transport error, run the integrated visible refresh flow for the affected account unless the user explicitly forbids browser/token refresh.

Never place portal tokens, cookies, passwords, temporary certificate tokens, or raw credential material in skill files, AGENTS files, Git-tracked files, logs, or final answers.

## Status Essentials

For "current queue", "各账号队列", "running slots", or "pending reason" requests, run `hpc_queue_summary.py --details` first and summarize `RUN`, `PD`, `OTHER`, `TOTAL`, `run_open`, `cap_open`, and pending reasons per account. Use `hpc_pending_reason.py --auth-account NAME` only when deeper `scontrol` fields or node/reservation details are needed.

## Desktop Widget Essentials

For Windows widget work, read `references/windows_widget.md` and run `scripts/resolve_windows_widget.py` before editing, building, or installing. Proceed only when it returns `status: ok`, and use only its `selected_source_root`. Treat separately copied Windows projects as stale candidates, not installation sources. Keep both Windows hosts read-only with respect to Slurm and credential stores: they may read only the redacted local snapshot and may request visible token refresh only through the explicit loopback dashboard API. Preserve both the single-account `{ "account": alias }` action and the deliberate all-account `{ "accounts": "all" }` action. On Windows 10 install the WPF desktop host only; build but do not install the Windows 11 Widget Board package.

Keep the macOS widget read-only with respect to Slurm. Render only the redacted local snapshot produced by `hpc_native_widget_snapshot.py`; do not query the portal, SSH proxy, or tokens from the WidgetKit extension. Preserve the existing dashboard and per-account visible-login deep links.

Before inspecting candidate UI implementation files or making widget changes:

1. Read `references/apple_native_widget.md`.
2. Run `"$HPC_PYTHON" scripts/resolve_active_widget.py`.
3. Proceed only when it returns `status: ok`; use only its `selected_source_root` and `ui_source_files`.
4. Treat `slurm/mac_hpc_monitor/native_widget/Sources/Widget/` as legacy UI unless the resolver lock is deliberately updated after a verified deployment. Its snapshot writer and installer may remain live backend dependencies; that does not make its UI source current.
5. Record that successful pre-edit selection audit and build only the selected root. Source version plists may intentionally diverge from the deployed lock after editing; that expected divergence does not authorize switching roots.
6. Do not build any same-bundle-id candidate when the pre-edit source resolution was mismatched, because macOS may auto-register the build and replace the visible widget.

After deployment, verify small, medium, and large families in light and dark appearances, confirm `pluginkit` reports exactly one enabled extension at the intended installed path/version, then update `references/apple_native_widget_component_lock.json` to that verified state.

## Kindle Mirror Essentials

Treat the Kindle mirror as a read-only downstream consumer of the already redacted native Widget snapshot. Never query the portal, SSH proxy, account store, cookies, or tokens from the renderer, publisher, edge server, or Kindle. Read `references/kindle_dashboard_sync.md` before changing this pipeline.

Keep the Mac and Kindle schedules independent: the Mac renders and publishes only when visible semantics change; the Kindle conditionally downloads with ETag. On a permanently powered device, prefer the bounded charging profile: while `screenSaver` and charging, renew 120 seconds of `suspendGrace` every 30 seconds and check ETag every 300 seconds; release immediately on `outOfScreenSaver`, within 30 seconds of unplug, and naturally within 120 seconds if the daemon dies. On battery, fall back to a 3600-second RTC cycle. This is an awake screen-saver state, not Wi-Fi surviving true suspend. Prefer authenticated HTTPS. When Kindle cannot reach GitHub but can reach an SSH-managed edge, publish the PNG atomically over SSH and serve it from a non-root TLS endpoint with a dedicated private CA. Install only the CA certificate on Kindle; keep the CA private key on Mac and the server private key on the edge.

Never publish raw `snapshot.json`, source account/node names, job ids/names, guardian details, credentials, addresses, or raw request logs. Publish one validated 1072 x 1448 8-bit grayscale PNG per supported orientation. For clockwise physical placement, compose a true 1448 x 1072 landscape layout, pre-rotate it counter-clockwise into the native PNG, include `right` in the semantic digest, and propagate only the strict `portrait|right` enum to device-private state. For an SSH edge, prefer one dual LaunchAgent cycle that atomically publishes portrait and right files to separate HTTPS paths with independent ETags. Never rotate the stock framebuffer. In right mode, suppress the native hook's portrait-coordinate time/date/battery overlay. Preserve the last known-good image across snapshot, render, upload, TLS, or download failure.

For a real battery sleep-cycle test, arm a short `next-due`, ask the user to lock the Kindle, and observe the normal `readyToSuspend` path. Do not use a helper path that simulates `powerButton`. Accept the cycle only when RTC wake, `abortSuspend`, Wi-Fi recovery, HTTPS `200` or `304`, user-unlock cancellation, and natural deep re-suspend match the reference contract. Separately validate the charging profile by observing `screenSaver`, `suspend_grace:120`, continuously connected Wi-Fi, two 300-second ETag cycles, and same-second release on user unlock. Do not claim unplug fallback or long-run stability until each is tested.
