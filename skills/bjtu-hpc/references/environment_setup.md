# New Controller Setup And Encrypted Account Migration

Use this checklist on a new macOS controller. Secret-bearing migration JSON files and their passphrases must travel through separate private channels.

## Runtime

```bash
export HPC_PYTHON="<PYTHON3.12>"
export BJTU_HPC_REPO="<PATH_TO_CLONED_REPOSITORY>"
export HPC_TOOLS="$BJTU_HPC_REPO/skills/bjtu-hpc/scripts"

"$HPC_PYTHON" --version
"$HPC_PYTHON" -m pip install -r "$HPC_TOOLS/requirements.txt"
"$HPC_PYTHON" -m playwright install chromium
"$HPC_PYTHON" -c 'import requests, paramiko, playwright, mcp, jsonschema, cryptography; print("dependencies ok")'
```

Require Python 3.12 and `cryptography>=42`. Confirm `ssh`, `screen`, and `tar` are on `PATH`. Do not use an older system Python.

## Export On The Source Computer

Metadata-only export contains no token or CAS password:

```bash
"$HPC_PYTHON" "$HPC_TOOLS/hpc_accounts.py" \
  export-json <private-migration.json>
```

To migrate authentication, explicitly include the required secrets:

```bash
"$HPC_PYTHON" "$HPC_TOOLS/hpc_accounts.py" \
  export-json <private-migration.json> \
  --include-tokens --include-credentials
chmod 600 <private-migration.json>
```

The command prompts twice for a passphrase of at least 12 characters. It derives a 256-bit key with scrypt and encrypts the complete payload with AES-256-GCM. Account aliases, metadata, tokens, CAS login names, and passwords do not appear in plaintext. The passphrase is neither a CLI option nor part of the JSON envelope.

Use repeated `--name NAME` options to export selected aliases. Use `--encrypt` to encrypt metadata-only output. Browser profiles, cookies, temporary certificates, and machine-specific profile paths are never exported.

## Import And Decrypt On The Target Computer

```bash
chmod 600 <private-migration.json>
"$HPC_PYTHON" "$HPC_TOOLS/hpc_accounts.py" \
  import-json <private-migration.json> \
  --use-exported-default --sync-legacy-token
```

`import-json` prompts for the source passphrase, authenticates and decrypts the payload in memory, validates the inner schema/SHA-256, then writes mode-`0600` account and credential stores. Do not produce a standalone plaintext decrypted file. A wrong passphrase, changed envelope field, corrupted ciphertext, loose permission, symlink input, unsupported field, or digest mismatch fails before any account write.

Default conflict policy is `error` and makes no changes. Inspect the target before choosing `--on-conflict skip`; use `--on-conflict replace` only when replacement is intentional. A metadata-only replacement preserves an existing target token and target-local browser profile path.

## Validate And Clean Up

```bash
"$HPC_PYTHON" "$HPC_TOOLS/hpc_accounts.py" list
"$HPC_PYTHON" "$HPC_TOOLS/hpc_credentials.py" list
"$HPC_PYTHON" "$HPC_TOOLS/hpc_accounts.py" validate --all --json
"$HPC_PYTHON" "$HPC_TOOLS/hpc_doctor.py" --json
```

Imported tokens may have expired. Refresh each invalid alias with:

```bash
"$HPC_PYTHON" "$HPC_TOOLS/hpc_refresh_flow.py" NAME --visible-only
```

Complete CAS/captcha in the isolated Playwright window and validate again. Run `hpc_queue_summary.py --details` only after account validation succeeds. The bundled doctor validates this portable controller set; a separate full private helper workspace is still required for upload and Slurm submission tooling. After success, delete all migration copies and temporary passphrase files from both computers and transfer services.
