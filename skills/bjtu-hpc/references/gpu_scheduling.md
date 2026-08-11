# GPU Scheduling And Native Slurm Submission

Read this file before any evidence-producing GPU submission, running-only monitor action, resource-shape change, or pending-job replacement.

All local controller commands in this file use `HPC_PYTHON=<PYTHON3.12>`; do not use bare `python3`.

## Compliance Baseline

New evidence-producing training must let Slurm manage independent work units. The default launch forms are:

- Slurm Job Array with at most three seed task ids for one dataset/config/method profile.
- Independent native `1GPU` sbatch jobs, one seed per job.
- True DDP/multi-GPU single-experiment jobs only when the training code actually uses multiple GPUs as one process group.

Do not use these patterns for new evidence-producing jobs:

```bash
srun --exclusive ... &
run_child ... &
bash child_0.sh &
bash child_1.sh &
wait
```

Do not submit one SBATCH allocation that manually starts multiple independent seeds or child experiments. Do not use low-VRAM GPU-sharing, GPU-fill fragments, wide multi-child allocations, or packed/refill scripts as a default. If an old helper or planner suggests packed/wide/GPU-sharing, treat that as stale planner output for this compliance mode and generate a compliant array or single-job script instead.

## Seed Cap

One experiment family means one dataset/config/method combination. It may use
at most three unique random seeds total across all Slurm jobs, arrays, accounts,
HPO, and retries. Retries reuse the same seed. Do not create a large
`#SBATCH --array`, generated sbatch set, or launch manifest that expands one
family beyond three seeds.

If all three seeds are poor, failed, or inconclusive, report that the method's
effectiveness is doubtful instead of searching for a lucky seed.

## Data And Environment Prerequisites

Before submitting a new evidence-producing GPU job, verify:

- The script does not reference `pytorch1.7-python3.8` or `/data/apps/anaconda/anaconda3/envs/pytorch1.7-python3.8/bin/python`.
- The selected account uses its own `/data/home/<account>/envs/torch251-cu121-py310` environment, or a recorded `cu118`/platform-module fallback that passed a GPU-node smoke test.
- The script prints `python`, `torch.__version__`, `torch.version.cuda`, `torch.cuda.is_available()`, CUDA device count, and GPU name before training.
- The dataset uses `DATA_BACKEND=lmdb`, `hdf5`, or `tfrecord`, unless this is a local debug, single-account smoke test, or explicitly legacy run.
- The packed dataset has `manifest.json` and `validation_report.json`, with train/test split separation and a single-seed smoke test.
- The script does not make another account scan a raw small-file dataset tree such as `/data/home/<other_account>/dataset/...`.

## Anonymized Trace-Hash Queue Names

Before generating any candidate sbatch script, create a stable `plan_hash`, an anonymous per-submission `trace_hash`, and `trace_id` as described in `references/anonymization.md`. Hash-based queue names are allowed and preferred. Use only the trace id for Slurm-visible names:

```text
trace_id=hpc_<12-16 lowercase hex chars>
```

The trace id must drive:

- `#SBATCH --job-name`.
- `#SBATCH --output` and `#SBATCH --error` basenames when present.
- Remote `runs/`, `logs/`, and `outputs/` per-job basenames.
- Local Git-safe evidence filenames.
- Queue keywords used to find the submitted job.

Do not place saved auth-account aliases, cluster usernames, portal usernames, real person names, or email identifiers in job names, run/log basenames, evidence filenames, or public summaries. Store the reversible mapping from trace id to account, project, seed, script checksum, resource shape, Slurm job id, and minimal launch-identity fields only in `$PROJECT_DIR/hpc_evidence/private/hpc_trace_ledger.jsonl` with mode `0600`.

## Default Shapes

Default single-seed shape:

```bash
#SBATCH --partition=GPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=disable-binding
```

Fallback single-seed shape, only after exact-script `sbatch --test-only` shows the 6-CPU shape would wait or fail due to `Resources`, reservation, same-node CPU pressure, or GPU/GRES shape pressure:

```bash
#SBATCH --partition=GPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=disable-binding
```

Do not lower CPU for pure `Priority`, dependency holds, or `QOSMaxJobsPerUserLimit`. CPU-rich `1:8`, `1:12`, or `1:16` shapes are optional only when the user explicitly asks for CPU-rich work or a live snapshot plus `sbatch --test-only` proves immediate start without reducing GPU availability for approved jobs.

For true DDP/multi-GPU single-experiment jobs, request the GPU count the code actually uses and keep `NumTasks`, `CPUs/Task`, and launcher semantics consistent with that distributed training stack. This exception is not a way to run multiple seeds in one allocation.

## Job Array Template

Use Job Array when validating multiple seeds for the same profile:

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
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export OPENBLAS_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export NUMEXPR_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"

echo "job_id=${SLURM_JOB_ID}"
echo "array_task_id=${SLURM_ARRAY_TASK_ID}"
echo "trace_id=hpc_<trace_hash>"
echo "cuda_visible_devices=${CUDA_VISIBLE_DEVICES:-unset}"
echo "data_backend=${DATA_BACKEND}"
echo "data_root=${DATA_ROOT:-unset}"

python - <<'PY'
import sys, torch
print("python", sys.executable)
print("torch", torch.__version__)
print("torch_cuda", torch.version.cuda)
print("cuda_available", torch.cuda.is_available())
print("device_count", torch.cuda.device_count())
if torch.cuda.is_available():
    print("device0", torch.cuda.get_device_name(0))
PY

python train.py --seed "${SLURM_ARRAY_TASK_ID}" --dataset_name <dataset>
```

Set array concurrency so every task is expected to start directly under the running-only policy. Do not use an array to create queued follow-up backlog. If Slurm leaves a current-project task `PENDING`, block further admission on that account; other accounts remain eligible only after a fresh snapshot unless live evidence shows a shared QOS/user limit.

## Independent Single-GPU Template

Use independent sbatch files when array semantics are not appropriate:

```bash
#!/usr/bin/env bash
#SBATCH --partition=GPU
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=disable-binding
#SBATCH --time=12:00:00
#SBATCH --job-name=hpc_<trace_hash>
#SBATCH --output=/data/home/<account>/projects/<project_slug>/logs/hpc_<trace_hash>_%j.out
#SBATCH --error=/data/home/<account>/projects/<project_slug>/logs/hpc_<trace_hash>_%j.err

set -euo pipefail

source /data/home/<account>/envs/torch251-cu121-py310/bin/activate
export DATA_BACKEND="${DATA_BACKEND:-lmdb}"
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export OPENBLAS_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
export NUMEXPR_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"

echo "trace_id=hpc_<trace_hash>"

python - <<'PY'
import sys, torch
print("python", sys.executable)
print("torch", torch.__version__)
print("torch_cuda", torch.version.cuda)
print("cuda_available", torch.cuda.is_available())
print("device_count", torch.cuda.device_count())
if torch.cuda.is_available():
    print("device0", torch.cuda.get_device_name(0))
PY

python train.py --seed <seed> --dataset_name <dataset>
```

## Pre-Submit Runability Gate

Before any real GPU training submission, run exact-script checks through the portal SSH proxy or `hpc_native_submit.py`:

```bash
"$HPC_PYTHON" hpc_native_submit.py ./candidate.sbatch --auth-account NAME \
  --expected-gpus 1 --expected-ntasks 1 --expected-cpus-per-task 6
"$HPC_PYTHON" hpc_native_submit.py ./candidate.sbatch --auth-account NAME \
  --expected-gpus 1 --expected-ntasks 1 --expected-cpus-per-task 6 --submit \
  --submit-intent <queue-bound-intent.json> --receipt-out <private-receipt.json>
```

The first command must run only syntax and `sbatch --test-only`. The second
requires a durable queue-bound intent created before the side effect, validates
the exact script hash, submits only after preflight succeeds, writes a mode-0600
native receipt immediately, and verifies the allocation with `scontrol`.

When generating a candidate:

1. Refresh or load a live snapshot with `hpc_queue_summary.py --json`.
2. Inspect `checked_at_local`, non-reserved nodes, free CPU/GPU, account summaries, and pending reasons.
3. Create the plan hash, launch identity hash, and anonymous trace id, then append a planned record to the local private trace ledger.
4. Generate one exact script for one job or one array using only the trace id for Slurm-visible names.
5. Run local/remote `bash -n` and `sbatch --test-only`.
6. If `1GPU/6CPU` cannot run due to resource/reservation/same-node CPU pressure, rewrite the same script to `1GPU/4CPU`, including all thread limits, and test again.
7. Persist the queue submit intent before `sbatch`, then submit only the exact
   candidate that passes. If no candidate passes, stop and report the blocker.
8. Write the native receipt immediately after backend acceptance, update the
   private trace ledger, reconcile Slurm by trace/job id, and only then mark the
   queue row running. Unknown intent outcome blocks retry.

Do not treat a resource-shape-only planner probe as final permission. The final exact script must pass preflight.

## Refresh-Gated Admission

Use one fresh snapshot-backed decision for exactly one physical Slurm submit. A controller cycle may admit several independent accounts, up to `max_hpc_admissions_per_cycle` (default `8`), but must refresh and replan after every material submit. Only the current `next_action` is submit-eligible; later `admission_frontier` entries are staleable candidates, never batch authorization.

For one-command single or multi-task execution, use `hpc_submit_cycle.py` and read `references/submit_cycle.md`. It implements the same rule with a private durable journal and rolling snapshots: N successful physical submissions normally use N+1 snapshots, because each post-submit refresh is also the next pre-decision snapshot. It reuses cycle-local portal/SSH setup and the same SSH client for upload, preflight, submit and verification, but it never reuses a planner permission or exact-script preflight across physical submissions.

If an AutoResearch global dispatcher selected this work, its schedule/assignment
hash proves only which project row may enter backend preflight. It never replaces
the BJTU snapshot or exact-script checks. After one Slurm submit and verification,
refresh both BJTU state and the shared cross-project resource view before another
global assignment can be claimed.

Preferred flow:

```bash
"$HPC_PYTHON" hpc_plan_from_snapshot.py --admission-mode direct-start \
  --max-admissions-per-cycle 8 --cap 2 --run-slots 2 \
  --workload single --no-queued --planner-json --summary-jobs 4
"$HPC_PYTHON" hpc_queue_summary.py --json --jobs 4 > /tmp/bjtu_hpc_queue_summary_current.json
"$HPC_PYTHON" hpc_resource_planner.py \
  --queue-json /tmp/bjtu_hpc_queue_summary_current.json \
  --admission-mode direct-start --max-admissions-per-cycle 8 \
  --cap 2 --run-slots 2 --workload single --no-queued --json
```

Follow only compliant `1GPU` or true DDP actions. Ignore stale packed/wide/GPU-sharing recommendations unless the user has explicitly supplied a current platform-approved exception.

## Running-Only Submission

Running-only cross-account submission is allowed for approved experiment backlog when the user asks to keep HPC busy or does not restrict submission to one account. Use all valid saved accounts, but submit only when live Slurm state plus exact-script preflight indicates direct start. `max_project_running_per_account` defaults to `2`; account/QOS caps are hard upper bounds, not fill targets:

```text
for each account/resource pool:
  blocked = current-project PENDING or account cap/auth/resource mismatch
  if live evidence records shared_limit_ref:
    propagate a blocked shared limit only to pools with that same ref
  eligible = not blocked and current-project RUNNING < max_project_running_per_account
approved_work = scientifically admitted, non-duplicate work within seed/compute budgets
admission_frontier = up to max_hpc_admissions_per_cycle eligible account/work pairs
submit_now = first freshly planned and exact-script-preflighted pair only
after submit: verify state, refresh, and recompute before another admission
```

`hpc_queue_summary.py` derives an anonymized `shared_limit_ref` from cluster plus
OS account and sets `shared_limit_blocked=true` only for exact per-user Slurm
limit reasons such as `QOSMaxJobsPerUserLimit`. Do not propagate ordinary
`Priority`, `Resources`, dependency, or account-local pending reasons.

Queued follow-up submissions remain disabled. `RUNNING`, `PENDING`, dependency-held, configuring, and other non-terminal records count against the affected scheduler/account limits. A current-project `PENDING` job stops new submissions to its account; it stops other accounts only when live evidence proves a shared QOS/user limit. `DONE`, `FAILED`, `CANCELLED`, `COMPLETED`, and `TIMEOUT` do not block once results/status have been synced.

Running-only cross-account submission must not:

- Create extra seed variants.
- Submit with invalid tokens.
- Skip packed dataset validation.
- Use another account's code, output, environment, or raw data path.
- Submit more than the account cap.
- Reintroduce packed child processes just to improve apparent utilization.
- Submit a speculative job expected to sit in `PENDING` for `Priority`, `Resources`, reservation pressure, or account caps.

If submission hits `QOSMaxSubmitJobPerUserLimit`, `QOSMaxJobsPerUserLimit`, or a similar cap, record the unsubmitted work in the local launch plan and retry only after a later live snapshot shows terminal jobs have reduced the account's non-terminal count.

## Pending Reason Diagnosis

When an account has fewer running jobs than expected, inspect native Slurm state before changing the plan:

```bash
"$HPC_PYTHON" hpc_pending_reason.py --auth-account NAME
```

For pending candidates, inspect `scontrol show job -dd <job_id>` fields including `JobState`, `Reason`, `Dependency`, `ReqNodeList`, `ExcNodeList`, `Features`, `OverSubscribe`, `GresEnforceBind`, `NumCPUs`, `NumTasks`, `CPUs/Task`, `TRES`, `TresPerNode`, `SchedNodeList`, `StartTime`, and `LastSchedEval`.

Under running-only policy, a current-project `PENDING` job or array task blocks its account/resource pool. Diagnose and preserve it unless the user explicitly authorizes replacement of that exact project/seed/parameters. After refreshing the snapshot, independent accounts may continue; do not continue pools sharing an explicitly blocked `shared_limit_ref`.

Interpretation:

```text
QOSMaxJobsPerUserLimit:
  The account is already at the running/non-terminal cap. Do not lower CPU or
  replace the job for this reason.

Priority:
  Lowering CPU is unlikely to repair pure priority ordering. Preserve queue
  position and report StartTime/SchedNodeList when Slurm provides them.

Resources or reservation pressure:
  Check same-node CPU and allowed reservations. If the job is 1GPU/6CPU and the
  blocker is resource/reservation/same-node CPU pressure, an authorized
  replacement or next submit may test 1GPU/4CPU. Do not cancel a pending job
  without explicit user authorization.

Same-node CPU exhaustion:
  Free GPUs are usable only when the same non-reserved node also has enough
  free CPUs for the requested shape. If no 1GPU/4CPU candidate fits, preserve
  queue position and report the exact node/resource blocker.
```

Useful native checks:

```bash
sinfo -N -p GPU -o '%N|%t|%C|%G'
scontrol show node=<node> -o
scontrol show reservation
```

## Post-Submit Verification

After `sbatch`, verify with `scontrol show job <job_id>`:

- Ordinary single-GPU job: `NumTasks=1`, `CPUs/Task=6`, and GPU TRES/TresPerNode contains `gres/gpu=1` or `gpu:1`.
- Resource-wait fallback: `NumTasks=1`, `CPUs/Task=4`, and GPU TRES/TresPerNode contains `gres/gpu=1` or `gpu:1`.
- Job Array: each task still has one task, one GPU, and the expected CPU shape.
- True DDP/multi-GPU exception: allocation matches the explicitly requested DDP shape.
- `Command=` points to the exact checked script and the script does not contain forbidden child-splitting patterns.

If verification reports `NumCPUs=1` or `CPUs/Task=1` for a GPU training run, mark it as wrong-shape immediately and do not count it as a valid evidence run.

Startup logs should show the environment probe, `DATA_BACKEND`, dataset manifest path, and at least one real training/progress line before reporting the launch as successful.

## Scheduled Queue Monitor

When the user asks to keep BJTU HPC busy under running-only rules, keep an explicit scheduled monitor active until the user pauses it, the approved backlog is exhausted, or the workflow reaches a terminal state. On each wake/controller cycle:

- Run a live native snapshot with `hpc_queue_summary.py --json` for selected accounts.
- Sync lightweight terminal results before submitting follow-ups.
- Mark each account with current-project `PENDING`/queued work blocked, and propagate only explicit shared-limit blocks.
- Admit approved compliant work on independent eligible accounts, bounded by `max_hpc_admissions_per_cycle=8` and `max_project_running_per_account=2` unless explicit project policy lowers them.
- For every admission, refresh/replan, run exact-script preflight, submit exactly one job or array, verify, and refresh again. If it becomes `PENDING`, stop that account/shared group and continue only from the refreshed plan.
- Preserve queue position for pure `Priority`.
- Apply 1GPU/4CPU fallback only for resource/reservation/same-node CPU pressure and only after exact-script test-only, and only when it avoids speculative queueing.
- Record submitted/not-submitted counts, exact blockers, next-check interval, and why the monitor remains active.

Never print tokens, cookies, passwords, temporary certificates, or raw credential material in monitor artifacts or user-facing updates.

## Resource History

Maintain a local resource-history ledger for later optimization work. The helper file is `work/hpc_resource_history.jsonl`; the macOS monitors append changed snapshots through `hpc_queue_summary.py --history-log`, and manual refresh is:

```bash
"$HPC_PYTHON" hpc_queue_summary.py --json --history-log work/hpc_resource_history.jsonl >/tmp/bjtu_hpc_queue_summary_current.json
```

Backfill saved native Slurm snapshots with:

```bash
"$HPC_PYTHON" hpc_resource_history.py --backfill-days 14 --summary
```

Keep the ledger local and uncommitted. It must not include portal tokens, cookies, passwords, temporary certificates, or private credentials. Prefer trace ids and redacted account tags in this optimization ledger; keep reversible account aliases and cluster user ids only in the private trace ledger described in `references/anonymization.md`.
