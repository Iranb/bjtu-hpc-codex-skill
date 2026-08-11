# Guardrails

Read this file when changing policy, submitting jobs, touching credentials, sharing datasets, or diagnosing risky queue/resource behavior.

## Credential And Auth

- Use saved accounts in `~/.bjtu_hpc_accounts.json` as the auth source of truth. Treat `~/.bjtu_hpc_token` as legacy compatibility only.
- Never hardcode or print portal tokens, cookies, passwords, temporary certificate tokens, or raw credential material.
- If upload, status, pending-reason, or submit commands hit `11009`, `11011`, `11012`, HTTP `401`, or token-validation transport errors, refresh only the affected account through the integrated visible flow unless the user explicitly forbids browser/token refresh.

## Hard Reject Before GPU Submit

Reject a candidate sbatch script, generated manifest, or launch plan if it contains or implies any of the following for new evidence-producing training:

- `srun --exclusive`.
- Any background child splitting such as `run_child ... &`, `bash child_0.sh &`, `bash child_1.sh &`, followed by `wait`.
- Multiple independent seeds or child experiments inside one SBATCH allocation.
- `pytorch1.7-python3.8` or `/data/apps/anaconda/anaconda3/envs/pytorch1.7-python3.8/bin/python`.
- Another account's `/data/home/<other_account>/envs/...` Python or conda path.
- Any project-managed code, dataset, checkpoint, weight, environment, manifest, config, run, log, output, or artifact path that is a persistent symlink, traverses a project-managed symlink, or resolves outside its registered real root.
- Hardcoded physical GPU ids such as `CUDA_VISIBLE_DEVICES=0,1`; scripts must use Slurm-provided allocation values.
- A seed array/range, generated sbatch set, or manifest that expands one
  dataset/config/method experiment family beyond three unique random seeds.
- Another account's raw small-file dataset path such as `/data/home/<other_account>/dataset/...` without a packed `DATA_BACKEND` or explicit one-time staging/conversion plan.
- Portal PyTorch-GPU app submissions for CPU/GRES-sensitive evidence runs; use native sbatch through the SSH proxy.
- A launch plan that submits new evidence-producing experiments to an account with current-project `PENDING`/queued work, or to a pool sharing an explicitly blocked scheduler limit.
- A speculative queued follow-up submission whose expected initial state is `PENDING` rather than direct `RUNNING`, unless a later explicit user instruction overrides the running-only policy.
- Slurm-visible or Git-safe names that contain saved auth-account aliases, cluster usernames, portal usernames, email identifiers, real person names, or credential labels. This includes `#SBATCH --job-name`, Slurm output/error basenames, remote `runs/`, `logs/`, `outputs/` per-job basenames, local evidence filenames, and queue keywords.
- A candidate that lacks an anonymous trace id such as `hpc_<12-16hex>` for queue-facing identification.

Allowed exceptions are narrow:

- True DDP/multi-GPU single-experiment jobs may request multiple GPUs when the code uses them as one distributed process group.
- Slurm Job Array is allowed when each array task runs one seed and the array has
  at most three unique seed task ids for the experiment family.
- A project-managed symlink is allowed only for node-local disposable dataset/cache data when both the link entry and its resolved target are under `/dev/shm/bjtu_data_artifacts/` or `/dev/shm/bjtu_dataset_cache/`, and the allocation-side identity/readiness checks have passed. A persistent project path pointing into `/dev/shm` is not allowed.
- Read-only diagnostics may inspect existing noncompliant scripts or jobs.

## Data Safety

- New multi-account training must not high-frequency scan a single user's raw ImageFolder-style tree.
- Prefer account-local archives under `/data/home/<account>/dataset_archives/` and packed outputs under `/data/home/<account>/dataset_packed/`.
- Persistent dataset roots must use stable reusable dataset/profile names. Do not store source-of-truth datasets under per-job trace hashes, `runs/`, `logs/`, `outputs/`, `/tmp`, or `/dev/shm`.
- Use `DATA_BACKEND=lmdb`, `DATA_BACKEND=hdf5`, or `DATA_BACKEND=tfrecord` for evidence-producing runs unless the run is local debug, a single-account smoke test, or explicitly legacy.
- A packed dataset is ready only after `manifest.json`, `validation_report.json`, train/test split separation, and a single-seed smoke test exist.
- Runtime staging to a stable node-local cache such as `/dev/shm/bjtu_dataset_cache/<dataset_name>/` is allowed and preferred for validated packed inputs when capacity permits. `/dev/shm` is per-node: a cache on one GPU node is not evidence that another node has the same cache. The sbatch script must make the cache decision after Slurm allocation, log the actual node id with each staging event, first reuse an existing ready non-empty cache on that node, otherwise capacity-check and copy under a lock plus readiness marker, and keep the cache reusable by later jobs on the same node. The cache must not contain checkpoints, model weights, final outputs, raw logs, secrets, or source-of-truth dataset roots. For approved multi-account reuse, apply runtime ACLs through `SHM_SHARED_ACL_USERS` so selected cluster OS accounts can read/write shared cache directories without hardcoding private account ids in Git-tracked files.
- Cross-account sharing may use the owner data's direct absolute path plus verified read-only/traverse ACLs, or a verified physical copy when adjacent writes are unavoidable. Do not create target-account or compatibility symlinks. Do not use ACLs as a reason to keep multi-account raw small-file training.
- Before transfer, staging, capability admission, or submit preflight, inspect each declared project root and required file with `lstat`, `readlink`, and `realpath`. Treat any non-`/dev/shm` project-managed symlink or registered-root escape as an infrastructure blocker. Replace it with a direct real path or verified copy without deleting its target.
- Do not apply ACL/chmod changes without explicit confirmation.

## Traceability And Anonymization

- Use hash-based queue names for every evidence-producing submit. Preferred form: `#SBATCH --job-name=hpc_<trace_hash>`.
- Keep account-to-trace mapping only in `$PROJECT_DIR/hpc_evidence/private/hpc_trace_ledger.jsonl` with mode `0600`.
- The helper argument `--auth-account NAME` is allowed for local controller auth selection, but that `NAME` must not be copied into job names, run directories, log basenames, output basenames, queue keywords, or Git-safe evidence filenames.
- Remote absolute paths may contain `/data/home/<account>/` because the cluster requires them, but per-job basenames under `runs/`, `logs/`, and `outputs/` must be trace ids.
- User-facing shared reports should identify jobs by trace id and resource shape. Mention saved account names only for private diagnosis when the user explicitly asks for account-level status.

## Runtime Environment

- Prefer account-local Python `3.10` at `/data/home/<account>/envs/torch251-cu121-py310` with PyTorch `2.5.1+cu121`.
- Use PyTorch `2.5.1+cu118` only as a recorded fallback after a GPU-node smoke test shows `cu121` fails.
- Use `module purge && module load PyTorch-GPU` only when the account-local environment is unavailable or fails and the platform module passes a GPU-node smoke test.
- Every GPU sbatch template must log Python executable, PyTorch version, CUDA runtime, CUDA availability, device count, and GPU name before training.

## Scheduling

- Default to one seed per Slurm allocation boundary: one array task or one independent `1GPU` sbatch job.
- Default resource shape is `--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding`.
- Fall back to `--cpus-per-task=4` only when exact-script `sbatch --test-only` cannot run the 6-CPU shape directly or reports/demonstrates `Resources`, reservation, same-node CPU pressure, or GPU/GRES shape pressure.
- Do not lower CPU for pure `Priority`, dependency holds, or `QOSMaxJobsPerUserLimit`.
- Do not use packed jobs, wide multi-child allocations, GPU-fill fragments, or low-VRAM GPU-sharing as default evidence-producing behavior.
- Running-only cross-account submission is allowed only for approved backlog. It may use selected/all valid accounts, but it must not create queued follow-up backlog. A current-project `PENDING` job blocks its account; other accounts are blocked only by explicit shared-limit evidence.
- Use refresh-gated admission: only the current snapshot's first action is submit-eligible. After submitting and verifying one job or array, refresh and replan before another admission, even within the same controller cycle.
- If no same-node candidate fits, do not submit a speculative high-CPU or stale planner candidate. Report the blocker and preserve queue position unless the user explicitly authorizes replacement.

## Submit Verification

Every real GPU submit path must run:

```bash
bash -n <script.sbatch>
sbatch --test-only <script.sbatch>
sbatch <script.sbatch>
scontrol show job <job_id>
```

Verify:

- Every experiment-bound project path passed the persistent-symlink and registered-root audit.
- Ordinary jobs have `NumTasks=1`, `CPUs/Task=6`, and `gres/gpu=1` unless a recorded 4-CPU fallback was used.
- Fallback jobs have `NumTasks=1`, `CPUs/Task=4`, and `gres/gpu=1`.
- Job array tasks keep the same one-task, one-GPU shape.
- True DDP/multi-GPU exceptions match the explicitly requested DDP shape.
- `Command=` points to the checked script and the script does not contain hard-reject patterns.

If native Slurm reports `NumCPUs=1` or `CPUs/Task=1` for a GPU training run, classify it as wrong-shape immediately. Do not count it as valid evidence.

## Queue And Replacement Safety

- For `QOSMaxJobsPerUserLimit`, inspect existing jobs before submitting or canceling anything. Treat it as a scheduling cap, not a data or code failure.
- Never cancel unrelated jobs.
- Cancel-and-resubmit requires explicit user authorization and must preserve the same project/seed/parameters.
- If a current-project job is `PENDING`, submit no additional experiment to that account or an explicitly shared blocked group. Continue independent accounts only from a refreshed direct-start plan.
- Do not replace jobs pending for pure `Priority` just to chase a different resource shape.
- If a job is pending for `Resources` or reservation/same-node CPU pressure, lower from `1GPU/6CPU` to `1GPU/4CPU` only after exact-script preflight supports the fallback.
- If even `1GPU/4CPU` cannot fit because the only visible free GPUs lack same-node CPUs or reservation access, preserve queue position and report the exact blocker.
- For manifest-driven single/multi-task work, use `hpc_submit_cycle.py`. Keep its journal and receipts private and mode-restricted. A process that stopped after durable `submitting`, an ambiguous backend response, a missing receipt, or multiple native trace matches is a reconciliation blocker; never rerun `sbatch` automatically.

## Transfer Safety

- Use `dataset_upload_progress.py` before restarting dataset transfers; it detects completed files and active `.part` files by size.
- For resumable archive uploads, treat cluster-side `.part` size as the progress source of truth when source-side state/log SSH reads hang.
- Do not restart a resumable upload screen just because source-side state reads fail. First compare cluster-side `.part` size twice, 10-30 seconds apart.
- Do not run two upload workers writing the same archive `.part`.
- If upload or query APIs return auth errors, refresh the token once and retry.

Read `references/hpc_workflow.md` when you need validated platform results and current environment notes.
