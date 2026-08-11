# External HDF5 Data Supply

Use this path for new AutoResearch datasets when BJTU storage should contain only training-ready HDF5 artifacts. Existing archive/packed workflows remain `legacy`; never switch a live project implicitly.

## Authority And Identity

Raw images and private source inventory remain on the SSH-accessible external factory. Portable identity keeps the following independent hashes explicit:

1. `dataset_semantics_sha256`: splits, class order, labels, roles, keys and sample identity.
2. `artifact_build_contract_sha256`: converter bundle, schema, sharding and deterministic HDF5 settings.
3. `source_inventory_sha256` and `converter_bundle_sha256`: exact portable sample inventory and converter implementation.
4. `artifact_content_sha256`: aggregate identity of exact HDF5 shard bytes.
5. `consumer_contract_sha256` and `validator_bundle_sha256`: loader/evaluator interpretation and the validator implementation.

Validation is detached. `READY.json` binds exact artifact content, consumer, validator bundle and validation report, and its ID is recomputed before use. The sibling report must be hash-matched, error-free and include a successful exact consumer probe. Changing only the consumer requires revalidation, not rebuilding the HDF5 core.

## Global Evidence Resolver

After the native provider verifies an immutable shard or aggregate artifact,
its authorized adapter should emit an `artifact` receipt to the AutoResearch
global evidence store. Bind the storage-domain id, exact persistent location,
native verification generation, member or aggregate content SHA-256,
verification-command hash, provider-bundle hash, registry revision, and native
verify-report ref. Import an already accepted native triple without rereading
shard bytes:

```bash
python3 <autoreskill-workflow>/scripts/goal.py evidence \
  ingest-hpc-hdf5-verify \
  --manifest <artifact-root>/MANIFEST.json \
  --verify-report <native-verify-report.json> \
  --registry <DATA_ARTIFACT_REGISTRY.json> \
  --storage-domain-id <bjtu-shared-domain>
```

The importer cross-checks all three documents and emits
`artifact_kind=external_hdf5_member` receipts plus one
`external_hdf5_aggregate` receipt. It records zero local shard-byte reread
because the native report is the source verification authority.

Resolve an existing content identity before scheduling another multi-gigabyte
hash:

```bash
python3 <autoreskill-workflow>/scripts/goal.py evidence find-artifacts \
  --content-sha256 <artifact-or-member-sha256>
```

The result is a candidate location, not data availability. Hash once per
physical storage generation, then run a cheap traversal/readability probe for
each scheduler account and import the proof through that project's capability
transaction. A path alias, account name, size/mtime tuple, or evidence-index row
alone cannot authorize reuse. The imported native verify event does not prove
the file generation is still current. Until the provider exposes an audited
current-generation fingerprint, `hpc_shm_cache.sh` must retain its full
shard-hash fallback.

## Local Control Plane

From the local `slurm` workspace:

```bash
python3 external_hdf5_artifact.py plan --contract <build.json> --inventory <private-inventory.json> --json
python3 external_hdf5_artifact.py build --contract <build.json> --inventory <private-inventory.json> --output-root <factory-artifact-root> --json
python3 external_hdf5_artifact.py validate --artifact-root <artifact-root> --inventory <private-inventory.json> --consumer-contract <consumer.json> --probe-argv-json '<exact-loader-argv-json>' --json
python3 hpc_storage_snapshot.py normalize --input <native-quota.json> --max-age-seconds 300 --json
python3 hpc_data_supply.py plan --manifest <DATA_ARTIFACT_MANIFEST.json> --ready <READY.json> --snapshot <native-quota.json> --share-accounts <aliases>
```

`plan`, `status`, `inspect`, `lint`, and `release-plan` are read-only. Local registry intent/verification records require explicit confirmation and expected revision CAS. A verify report cannot make an artifact available unless it matches a receipt already recorded by the authorized remote adapter. The current production remote adapter is deliberately disabled: `upload`, `share`, `commit`, and `delete` return `remote_adapter_not_enabled` until a native provider and exact transaction receive separate authorization.

Never derive quota from `df` or a historical chat report. The native snapshot must contain checked time, provider bundle hash, portal/cluster/account identity, hard quota, used bytes, artifact root and ACL capability. Stale or incomplete evidence is non-fitting.

## HPC Layout And Runtime

Keep one persistent owner copy:

```text
/data/home/<owner>/autoreskill_data/artifacts/<artifact_content_sha256>/
  DATA_ARTIFACT_MANIFEST.json
  ARTIFACT_COMPLETE.json
  shards/part-*.h5
  attestations/<validation_attestation_id>/READY.json
  attestations/<validation_attestation_id>/VALIDATION_REPORT.json
```

No raw tree, tar archive, extraction scratch, credential or external absolute source path is allowed inside the portable artifact. Other accounts receive read-only traversal/read access after native verification; lack of access removes that pool from fitting rather than creating an implicit duplicate.

Consumers must open the verified owner artifact by its direct absolute path. Do not create persistent target-account, compatibility, dataset, or project symlinks. Before capability admission and submit preflight, audit the declared artifact root, manifest, complete marker, shards, READY file, and validation report with `lstat`, `readlink`, and `realpath`; any project-managed persistent symlink or registered-root escape is non-fitting. A project-managed symlink is allowed only when both the link and resolved data/cache target are under the allocation-local `/dev/shm/bjtu_data_artifacts/` tree and the exact cache identity/readiness checks have passed.

After Slurm allocation, source `hpc_shm_cache.sh` and call:

```bash
bjtu_stage_hdf5_artifact_to_shm \
  "$HPC_DATA_ARTIFACT_ROOT" \
  "$ARTIFACT_CONTENT_SHA256" \
  "$CONSUMER_CONTRACT_SHA256" \
  "$DATA_ATTESTATION_READY" \
  DATA_ARTIFACT_ROOT
```

The cache path is `/dev/shm/bjtu_data_artifacts/<artifact_content_sha256>/<READY-file-sha256>/`. The second key prevents one validated consumer/attestation from blocking another for the same immutable core. Staging copies only the manifest, complete marker, manifest-declared HDF5 shards and the selected report/READY pair; unrelated JSON or factory metadata is not copied. It writes `.ready` last, recomputes manifest/attestation/report identities and full shard hashes before copy and reuse, never replaces an existing unverified path, and falls back only to the verified persistent artifact.

## Launch Gate

An enforced queue row, backend preflight and submit intent must agree on the project contract hash, artifact schema, semantics/inventory/build/converter/content/consumer/validator identities, attestation/report, receipt-bound registry revision, native verify report and data lease. The implementation conformance `exact_runtime_smoke` must consume the same artifact and consumer. Missing data supply is an infrastructure/protocol blocker, never a scientific negative.

Enable in order: local fixtures, project `external_hdf5_shadow`, a separately approved CUB pilot, DomainNet migration, then project `external_hdf5_enforced`. Cleanup is a separate exact-path approval after lease-aware `release-plan`; no implementation or pilot step authorizes deletion.
