# Refresh-Gated Single And Batch Submit Cycles

Read this reference when preparing, validating, running, resuming, or reconciling one or more native BJTU Slurm submissions.

## Controller Contract

Use the workspace controller with Python 3.12:

```bash
HPC_PYTHON=<PYTHON3.12>
SLURM_DIR="<SLURM_DIR>"
"$HPC_PYTHON" "$SLURM_DIR/hpc_submit_cycle.py" validate --manifest <manifest.json>
"$HPC_PYTHON" "$SLURM_DIR/hpc_submit_cycle.py" run --manifest <manifest.json>
```

`run` is read-only with respect to Slurm unless `--submit` is explicit. It may make live queue/resource queries. A real cycle requires a durable submit intent for every candidate and uses:

```bash
"$HPC_PYTHON" "$SLURM_DIR/hpc_submit_cycle.py" run \
  --manifest <manifest.json> --submit
```

One command may manage several items, but a batch command is not batch authorization. For every physical `sbatch`, the controller consumes only a fresh planner `next_action`, runs the exact path audit and script preflight, records `submitting`, submits once, writes the receipt, verifies with `scontrol`, refreshes, and replans. Later `admission_frontier` entries are never submitted directly.

## Manifest

Validate manifests against `schemas/hpc_submit_batch.schema.json`. Use anonymous `cycle_<hex>` and `item_<hex>` ids. Each item declares:

- `kind`: `independent` or `array`.
- one experiment-family id and one to three seeds.
- one or more account-specific candidates.
- for each candidate, the exact local sbatch script, durable submit-intent path, expected 1GPU/1-task/6CPU or approved 4CPU shape, and every required remote path plus its registered real root.

Create a separate exact script and durable intent for every account candidate. Never rewrite `/data/home/<account>` paths after planning. The script SHA, intent SHA, trace id, launch identity, resource shape, and account are frozen when the cycle is initialized. Immediately before entering durable `submitting`, rehash both files and fail closed if either digest changed. The native helper must read each local file once, upload the frozen script bytes losslessly, verify the remote SHA before both `sbatch --test-only` and real `sbatch`, record the local/remote script SHA and intent SHA in the result/receipt, and require the submitted job's `scontrol Command` to equal that content-addressed remote path.

For `array`, list two or three explicit seed ids and set array concurrency equal to task count. Do not use ranges, `%1`, or another concurrency limit that creates queued follow-up work. The controller admits an array only when all elements fit the fresh account and cluster snapshot and verifies every task with `scontrol`.

## Rolling Snapshot Optimization

For N successful physical submissions, the normal cycle collects N+1 snapshots:

```text
S0 -> submit 1 -> S1 -> submit 2 -> S2 ... -> submit N -> SN
```

`Si` is both the post-submit refresh for item i and the fresh pre-decision snapshot for item i+1. Collect an extra snapshot after a pause, reconnect, failed preflight, stale evidence, or another exceptional transition, and record the reason. Never reduce the cycle to one stale snapshot.

The controller reuses cycle-local portal metadata, temporary certificate information, and one SSH client per account. It never persists tokens or certificates. It also uses the same SSH client for upload, `bash -n`, `sbatch --test-only`, `sbatch`, and immediate `scontrol` verification. Do not precede the automated `--submit` path with a separate dry preflight invocation; the submit path already performs exactly one preflight.

Stable `launch_identity` evidence may avoid repeating code/data/environment discovery for seed-only or approved hyperparameter variants. Never cache queue/resource state, exact-script checks, submit intent, receipt, or post-submit verification.

## Recovery

Cycle evidence is private and mode-restricted under:

```text
work/hpc_submit_cycles/private/<cycle_id>/
```

Inspect or resume with:

```bash
"$HPC_PYTHON" "$SLURM_DIR/hpc_submit_cycle.py" status --cycle-dir <cycle-dir>
"$HPC_PYTHON" "$SLURM_DIR/hpc_submit_cycle.py" resume --cycle-dir <cycle-dir> --submit
```

If the controller stopped after durable `submitting`, or the backend response was missing or ambiguous, the item becomes `needs_reconcile`. Do not rerun `sbatch`. Search native Slurm by anonymous trace and script/intent identity:

```bash
"$HPC_PYTHON" "$SLURM_DIR/hpc_submit_cycle.py" reconcile --cycle-dir <cycle-dir>
```

Only one exact Slurm match may repair a receipt. Zero or multiple matches require manual audit. After receipt recovery, resume performs shape verification and a fresh snapshot before any later admission.

## Limits And Blockers

- Default maximum: two current-project running jobs per account and eight physical admissions per cycle.
- A current-project `PENDING` job or array element blocks that account. Continue another account only from the refreshed plan.
- Propagate blocking only through an explicit shared-limit ref or exact shared QOS/user-cap evidence.
- Reaching the cycle cap leaves remaining items unsubmitted; it does not authorize another stale batch.
- A verification mismatch, changed manifest/script/intent hash, changed account identity, or ambiguous trace search is fail-closed and must never auto-retry.
