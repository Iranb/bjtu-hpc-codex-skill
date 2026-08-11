# Guardrails

Read this file before touching credentials, importing accounts, uploading data, changing permissions, or submitting/canceling jobs.

## Credentials And Migration

- Treat the saved multi-account store as auth authority and the single-token file as legacy compatibility only.
- Never print or commit tokens, cookies, CAS passwords, temporary certificates, browser storage, migration ciphertext, or decryption passphrases.
- Metadata-only migration may be plaintext. Any export containing token or CAS credentials must encrypt the complete payload with scrypt + AES-256-GCM.
- Require mode `0600`, a regular non-symlink input, authenticated decryption, inner schema/digest validation, and an explicit alias-conflict policy before import writes.
- Never migrate Playwright profiles or cookies. Validate imported tokens and refresh invalid aliases through isolated visible Playwright login.
- Keep migration file and passphrase in separate channels and delete all temporary copies after target validation.

## Hard Reject Before GPU Submit

Reject a new evidence-producing candidate that contains or implies:

- `srun --exclusive`, background child splitting, or multiple independent seeds inside one allocation.
- Hardcoded physical GPU ids.
- Persistent project-managed symlinks or paths escaping their registered real roots.
- Another account's environment interpreter.
- More than three unique seeds for one experiment family.
- Slurm-visible account aliases, portal users, cluster usernames, emails, or real names.
- A speculative queued follow-up whose expected initial state is `PENDING`.
- A submit based on a stale snapshot or later planner frontier entry.
- A script/intent/remote-byte digest mismatch.

True DDP is the only multi-GPU exception: one experiment must actually use all requested GPUs as one distributed group. A Job Array is allowed only for at most three explicit seeds with concurrency equal to task count and task-level verification.

## Data And Path Safety

- Keep code, datasets, checkpoints, environments, manifests, logs, outputs, and artifacts on direct real paths.
- Audit every declared root and required file with `lstat`, `readlink`, and `realpath` before transfer/preflight.
- Cross-account reuse requires a verified owner path plus minimal read/traverse ACLs or a verified physical copy. Do not create compatibility symlinks.
- Do not apply ACL/chmod changes, delete data, or restart a transfer worker without explicit authority.
- Never run two upload workers against the same destination fragment.

## Submission And Recovery

Every real submit path performs:

```bash
bash -n <frozen-script>
sbatch --test-only <frozen-script>
sbatch <frozen-script>
scontrol show job <job-id>
```

Bind those calls to the same frozen bytes and verify command path plus resource shape. Persist durable `submitting` state before the adapter call and a mode-`0600` receipt immediately after acceptance. Unknown outcomes require exact trace reconciliation and never authorize an automatic retry.

Never cancel unrelated jobs. Cancel-and-resubmit requires explicit authorization and must preserve the intended project/seed/parameters.
