# Native GPU Scheduling

Read live native Slurm state before every decision. Historical monitor data and portal rows may suggest candidates, but only a fresh queue/resource snapshot plus exact-script preflight authorizes one submission.

## Direct-Start Admission

Use `hpc_queue_summary.py --json` and the resource planner with direct-start mode. Default to at most two current-project running/nonterminal jobs per independent account pool and no queued follow-up backlog. A planner pass authorizes only its first `next_action`; later frontier entries require another refresh.

A current-project `PENDING` job blocks its account/pool. Continue considering other accounts only when live evidence shows they are independent. Propagate a block only through an explicit shared QOS/user limit; ordinary `Priority`, `Resources`, or account-local pending does not prove a global block.

For N successful submissions, collect N+1 rolling snapshots:

```text
S0 -> submit 1 -> S1 -> submit 2 -> ... -> submit N -> SN
```

## Allowed Shapes

Default evidence-producing unit is one seed/process per Slurm allocation boundary:

```bash
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=6
#SBATCH --gres=gpu:1
#SBATCH --gres-flags=disable-binding
```

Use `--cpus-per-task=4` only after the exact 6-CPU script fails direct-start runability because of resource/reservation/same-node CPU pressure. Do not lower CPU for pure priority, dependency holds, or account/job caps.

True DDP may request multiple GPUs only when one experiment actually uses them as one distributed process group. Do not run independent seeds through background children, `srun --exclusive`, manual GPU id assignment, packed jobs, or low-VRAM GPU sharing.

A Slurm Job Array may contain at most three explicit seed ids for one experiment family. Set array concurrency equal to task count so the array does not create queued follow-up work; admit it only when every task fits the fresh snapshot, then verify every task with `scontrol`.

## Exact-Script Gate

For every candidate:

1. Freeze account-specific script and intent hashes.
2. Audit project paths for symlinks and registered-root escapes.
3. Run local/remote syntax checks and `sbatch --test-only` against the frozen remote bytes.
4. Recheck remote SHA immediately before real `sbatch`.
5. Write a private durable receipt immediately after acceptance.
6. Verify `scontrol Command`, tasks, CPUs/task, total CPUs, and GRES.
7. Refresh queue/resources before another decision.

An unknown backend result, missing receipt, or multiple trace matches is a reconciliation blocker, not permission to submit again.

## Pending Diagnosis

Treat `QOSMaxJobsPerUserLimit` or related account caps as scheduling limits, not code/data failures. Inspect the existing jobs and preserve queue position. Do not cancel or replace jobs without explicit authorization.

For `Resources` or reservation pressure, consider only the verified 4-CPU fallback. For pure `Priority`, keep the job unchanged. Never submit speculative work merely to fill visible GPUs.
