# Experiment Anonymization And Traceability

Read this file before any GPU submit, running-only monitor action, job-array generation, pending-job replacement, or public report about submitted experiments.

## Policy

Every submitted experiment must be anonymous in Slurm-visible and Git-safe surfaces while remaining privately traceable.

Use a plan hash, launch identity hash, and trace hash before writing an sbatch
script:

```text
plan_hash = first 12-16 lowercase hex chars of SHA-256(
  project_slug | dataset_profile | method_profile | seed_or_array | normalized_resource_shape | normalized_sbatch_template
)
trace_hash = first 12-16 lowercase hex chars of SHA-256(
  plan_hash | submit_time_nonce | random_nonce_or_local_secret
)
trace_id = hpc_<trace_hash>
```

`plan_hash` may be stable and is useful for deduplicating planned work. `trace_hash` must identify one concrete submission. Random nonces are allowed and preferred when multiple accounts may submit the same plan. Do not build queue-facing hashes from raw account aliases, cluster usernames, portal usernames, emails, or real names; a short unsalted hash of a small account set can still be guessed.

For repeated-variant launch reuse, also record a stable launch identity that
excludes the seed and hyperparameters intentionally being varied:

```text
launch_identity_hash = first 12-16 lowercase hex chars of SHA-256(
  code_export_ref_or_sha | dataset_profile | dataset_manifest_ref_or_sha |
  runtime_env_ref_or_probe_sha | launcher_template_sha |
  resource_shape | method_profile | data_backend
)
```

Do not use `plan_hash` alone to prove repeated-variant identity because it may
include `seed_or_array`.

The following surfaces must use `trace_id` or another neutral hash-only identifier:

- `#SBATCH --job-name`.
- Slurm output/error filenames when the script controls them.
- Remote `runs/`, `logs/`, `outputs/`, and per-job workdir basenames.
- Local evidence filenames that may be committed or summarized.
- Queue keywords used for job lookup after submission.
- User-facing summaries intended for sharing outside the private machine.

Do not include saved auth-account aliases, cluster usernames, portal usernames, email addresses, real person names, or credential labels in those surfaces. Paths such as `/data/home/<account>/...` are unavoidable for cluster access, but never copy the account segment into a job name, run basename, log basename, or public evidence filename.

## Private Trace Ledger

Keep the reversible mapping only in a local private ledger:

```text
$PROJECT_DIR/hpc_evidence/private/hpc_trace_ledger.jsonl
```

Create the directory with mode `0700` and the ledger with mode `0600`. Keep it local and uncommitted. Add it to the project's ignore rules if the project has a VCS ignore file.

Each JSONL record should include:

```json
{
  "trace_id": "hpc_0123456789ab",
  "trace_hash": "0123456789ab",
  "plan_hash": "89abcdef0123",
  "project_slug": "neutral_project",
  "dataset_profile": "packed_dataset_profile",
  "method_profile": "method_profile",
  "seed": 13,
  "array_task_ids": [13, 14, 15],
  "auth_account_alias_private": "saved-account-name",
  "cluster_user_private": "cluster-user-id",
  "remote_project_root": "/data/home/<account>/projects/<project_slug>",
  "remote_run_dir": "/data/home/<account>/projects/<project_slug>/runs/hpc_0123456789ab",
  "resource_shape": "1GPU/6CPU",
  "sbatch_sha256": "full_script_sha256",
  "launch_identity": {
    "identity_version": 1,
    "identity_hash": "abcdef012345",
    "code_export_ref": "git:<commit-or-archive-sha>",
    "dataset_manifest_ref": "/data/home/<account>/dataset/_manifests/<dataset>_manifest.json",
    "runtime_env_ref": "/data/home/<account>/envs/<env-name>",
    "launcher_template_sha256": "template_sha256_without_trace_or_seed",
    "data_backend": "packed_hdf5"
  },
  "submit_time_local": "YYYY-MM-DDTHH:MM:SS+08:00",
  "slurm_job_id": "pending-until-submit",
  "status": "planned"
}
```

The private fields may contain account aliases or cluster user ids because this ledger is the audit mapping. Do not mirror those fields into public reports, Slurm job names, or Git-tracked evidence.

## Pre-Submit Lint

Reject a candidate launch before `sbatch --test-only` if any Slurm-visible or Git-safe name contains:

- A saved auth-account alias.
- A cluster username or portal username.
- An email local part or full email address.
- A real person name or credential label.
- A raw `/data/home/<account>` segment copied into a basename.

Allowed exceptions:

- The helper invocation may use `--auth-account NAME` or `HPC_AUTH_ACCOUNT=NAME`; this is controller-side auth selection, not an experiment name.
- Remote absolute paths may contain `/data/home/<account>/` because the cluster requires that location.
- The private trace ledger may store account mappings for audit.

When in doubt, use a hash-only Slurm name:

```bash
#SBATCH --job-name=hpc_<trace_hash>
#SBATCH --output=/data/home/<account>/projects/<project_slug>/logs/hpc_<trace_hash>_%j.out
#SBATCH --error=/data/home/<account>/projects/<project_slug>/logs/hpc_<trace_hash>_%j.err
```

## Reporting

For ordinary user updates, report anonymous queue rows as `trace_id`, `state`, `resources`, `seed`, and `pending reason`. If the user asks for account-level diagnosis on the private machine, summarize per saved account in the conversation, but do not copy those account names into sbatch scripts, queue keywords, or Git-safe artifacts.
