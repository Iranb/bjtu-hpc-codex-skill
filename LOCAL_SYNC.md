# Local-First Synchronization Contract

This repository is the sanitized public mirror of the locally installed
`bjtu-hpc` and `bjtu-hpc-submit` skills plus the helper modules they invoke.
The local installed skills and local helper workspace are authoritative for
behavior and policy.

## Mirrored scope

- Both skill definitions and agent metadata.
- Every reference owned by `bjtu-hpc`.
- Helper entry points named by either skill and their complete local Python
  import closure.
- `hpc_core`, JSON schemas, `hpc_shm_cache.sh`, controller requirements, and
  local regression tests.
- The redacted Widget snapshot adapter. The Apple-native UI and Kindle client
  remain in their dedicated repositories and are not vendored here.

## Required public overlay

Local behavior is preserved except where publishing it would expose or depend
on controller-private state:

- Personal absolute paths become placeholders or environment variables.
- Real portal users, cluster accounts, source hosts/users, proxy addresses,
  job ids, bundle-owner identifiers, and research-specific transfer inventory
  are removed.
- Account, token, credential, browser-profile, transfer-task, receipt, intent,
  trace-ledger, snapshot, log, key, certificate, and migration files are never
  copied.
- Dataset/source selections that were private defaults require explicit CLI or
  environment configuration in the public bundle.
- Test identities use obviously synthetic values.
- Public-only portability and safety fixes may remain on top of the local
  source, including identity-bound refresh commands and dependency completion.

## Verification before publication

Run all checks from the repository root with a Python 3.12 controller:

```bash
export HPC_PYTHON="<PYTHON3.12>"
export PYTHONPATH="$PWD/skills/bjtu-hpc/scripts"

"$HPC_PYTHON" -m compileall -q skills/bjtu-hpc/scripts tests
bash -n skills/bjtu-hpc/scripts/hpc_shm_cache.sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  "$HPC_PYTHON" -m pytest -p no:capture -q
```

Validate every committed JSON file, check that every local import resolves
inside the bundle, and run high-confidence secret/identity scans over the full
staged diff before pushing.
