# Refresh-Gated Submit Cycle

Use `hpc_submit_cycle.py` for one or more prepared native Slurm tasks. Its default mode plans without submitting; real work requires explicit `--submit`, a validated manifest, and one durable queue-bound intent per candidate.

## Exact-Script Contract

Create a separate account-specific script and intent for each candidate. Freeze script SHA, intent SHA, trace id, launch identity, resource shape, and account at cycle initialization. Immediately before durable `submitting`, rehash script and intent; fail closed if either changed.

The native helper must:

1. Read each local file exactly once.
2. Upload the frozen script bytes losslessly through a private temporary file and atomic replacement.
3. Verify remote SHA before `bash -n` and `sbatch --test-only`.
4. Verify remote SHA again immediately before real `sbatch`.
5. Record local/remote script SHA and intent SHA in the mode-`0600` receipt.
6. Verify the submitted job's `scontrol Command` path and expected GPU/task/CPU shape.

Any digest/command mismatch is a terminal verification failure and must not be retried automatically.

## Rolling Snapshots

For N successful physical submissions, collect N+1 normal snapshots:

```text
S0 -> submit 1 -> S1 -> submit 2 -> S2 ... -> submit N -> SN
```

Each post-submit snapshot is the next decision's fresh input. An account with current-project `PENDING` work is blocked, but independent accounts/pools remain eligible unless live evidence identifies a shared cap. A planner frontier is candidate ordering, not batch authorization.

Reuse cycle-local portal metadata, temporary certificates, and one SSH client per account, but never cache queue/resource state, exact-script checks, intent, receipt, or verification.

## Recovery

Persist private cycle state before invoking the submit adapter. If a process stops after durable `submitting`, a backend response is ambiguous, a receipt is missing, or multiple trace matches exist, mark the item for reconciliation. Search Slurm by anonymous trace and frozen identity; never issue a second `sbatch` until exactly one prior outcome has been resolved and verified.
