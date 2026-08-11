# Packed Dataset Backends

Read this file before launching new evidence-producing training that reads image datasets on BJTU HPC.

## Default Policy

For new AutoResearch projects, the default is the external-factory contract in `external_hdf5_data_supply.md`, not the account-local archive pattern below. Build deterministic `image_bytes_indexed_v1` shards off-cluster, keep one content-addressed owner copy on HPC, and use the strict artifact-hash `/dev/shm` API. The remainder of this document describes the compatible `legacy` backend and converter guidance. It must not be used as fallback for `external_hdf5_enforced` rows.

New GPU training should read an account-local packed dataset, not a cross-account raw small-file tree. Persistent packed datasets must be named by dataset family, split/source, version, backend, and experiment profile so they can be reused by later jobs. Do not put persistent datasets under per-job trace directories, Slurm job hashes, `runs/`, `logs/`, `outputs/`, `/tmp`, or `/dev/shm`. Use one of:

```text
DATA_BACKEND=lmdb
DATA_BACKEND=hdf5
DATA_BACKEND=tfrecord
```

`DATA_BACKEND=raw` is allowed only for local debugging, a single-account smoke test, or an explicitly legacy run whose I/O risk is recorded. Do not use raw `ImageFolder`/per-image open loops for multi-account training.

In legacy mode, each running account may keep its own archive and packed output:

```text
/data/home/<account>/dataset_archives/<dataset_name>.tar
/data/home/<account>/dataset_archives/<dataset_name>.sha256
/data/home/<account>/dataset_archives/<dataset_name>.manifest.json

/data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/
  train.lmdb        # or train.h5 / train.tfrecord
  test.lmdb
  val.lmdb          # optional, only when materialized
  manifest.json
  validation_report.json
```

Archives may be copied from a shared source, but training jobs must not high-frequency scan another user's raw image directory. A shared packed dataset may be used only after read access is verified and the manifest shows it is immutable for the current experiment profile.

Per-job trace names such as `hpc_<hash>` are allowed for Slurm job names, run directories, and log basenames only. They must not contain account identifiers, are not dataset names, and should not be used for `/dev/shm` dataset cache paths.

## Conversion Contract

Convert by the experiment code's actual data contract, not only by the raw dataset name. Before conversion, identify and record:

- `dataset_name`, such as `cub`, `scars`, `aircraft`, or `imagenet_100`.
- The root config variable used by the code, such as `cub_root`, `car_root`, `aircraft_root`, or `imagenet_root`.
- The split policy, such as SSB split, ImageNet-100 seed-0 split, or explicit train/test metadata.
- The class subset, label mapping, `target_transform`, and `uq_idx` semantics.
- The expected `__getitem__` shape, normally `(image, target, uq_idx)`.

For ordinary training, convert the full target dataset/profile rather than a single seed's sampled subset. Seeds may change initialization, sampling, or label split, but they should not trigger a new data conversion unless the dataset/profile/backend changes.

Train and test splits must be physically separated:

```text
train.lmdb / train.h5 / train.tfrecord
test.lmdb  / test.h5  / test.tfrecord
```

If validation is derived from train, either materialize `val.*` or store `val_indices`, `train_indices_after_val_split`, and the split seed in `manifest.json`.

## Manifest Requirements

`manifest.json` must be enough for another agent to verify that the packed data matches the training code:

```json
{
  "dataset": "<dataset_name>",
  "experiment_profile": "<profile>",
  "backend": "lmdb",
  "archive_source": "/data/home/<account>/dataset_archives/<dataset_name>.tar",
  "code_contract": {
    "dataset_name": "<dataset_name>",
    "root_config": "<config_variable>",
    "split_policy": "<split_policy>",
    "getitem": ["image", "target", "uq_idx"]
  },
  "splits": {
    "train": {"num_samples": 0, "num_classes": 0},
    "val": {"num_samples": 0, "num_classes": 0},
    "test": {"num_samples": 0, "num_classes": 0}
  },
  "samples_schema": ["key", "target", "uq_idx", "relative_path", "split"]
}
```

## Validation Gate

Before using a packed dataset for multi-seed or multi-account training, create `validation_report.json` with:

- Raw-to-packed sample counts and class counts.
- Random sample checks for target/label consistency.
- Random image decode checks from the packed backend.
- Split integrity checks for train/test/val.
- A single-seed smoke test that imports the real training dataset code, builds the DataLoader, reads several batches, and records first-epoch or short-run throughput.
- GPU utilization and DataLoader throughput when the smoke test runs on a GPU node.

Example validation command shape for future helpers:

```bash
python3 validate_packed_dataset.py \
  --raw-root <raw_dataset_root> \
  --packed-root <packed_dataset_root> \
  --dataset <dataset_name> \
  --profile <experiment_profile> \
  --samples 200
```

Do not treat a packed dataset as ready if only archive checksums exist. The validation report must prove the packed backend preserves the code-visible dataset semantics.

## Runtime `/dev/shm` Staging

After the persistent packed dataset passes validation, evidence-producing training jobs should stage the packed root or the job's hot shard into a stable node-local `/dev/shm` RAM-disk cache when it fits safely, then point the training DataLoader at the staged copy. This reduces GPFS reads without changing the reusable dataset source of truth and allows later jobs on the same node to reuse the staged dataset.

`/dev/shm` is local to one compute node. A cache on `gpu03` is not visible from `gpu04`, and a cache observed through one running job must not be treated as a cluster-wide dataset copy. The path may stay the same on every node, for example `/dev/shm/bjtu_dataset_cache/<dataset_name>/`, because the namespace is already node-local. The management model is therefore per-node cache plus persistent account-local source of truth:

1. Pre-submit planning can use the monitor snapshot to estimate free RAM on candidate nodes, but it must assume the `/dev/shm` cache may be cold.
2. After Slurm starts the job, the sbatch script records the real node with `hostname -s`, checks that node's local cache, and reuses it only if that same node has a ready non-empty copy.
3. If the cache is cold on that node, the first job copies the packed dataset or hot shard from `/data/home/<account>/dataset_packed/...` into `/dev/shm` under the dataset lock, marks it ready, and leaves it in place for later jobs on the same node.
4. Later jobs that land on the same node skip copy work and train from the node-local cache. Jobs that land on another node repeat the same local check/copy/fallback flow for that node.
5. Cache-hit logs and optional monitor aggregation should be keyed by `(node, dataset_key)`. They are diagnostics only; the authoritative reusable dataset remains the account-local packed root.

Do not submit production training that relies on a cache being pre-existing on a particular node. If the user explicitly approves pre-warming, run a short stage-only Slurm job per intended node or let the first real job warm the cache. Pre-warm jobs must use the same source manifest, capacity limits, ACL repair, lock, and `.ready` marker as normal training jobs, and should not use private `mount` or `unshare` tricks.

Staging rules:

- Source path is the stable account-local packed dataset, for example `/data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/`.
- Destination path is stable by dataset name, for example `/dev/shm/bjtu_dataset_cache/<dataset_name>/`. Do not include a job token in the dataset cache path. If multiple incompatible packed contracts share a dataset family, encode the split/source/version/backend in `<dataset_name>` before staging.
- Log the real compute node and cache key before staging, for example `shm_node=$(hostname -s)` and `shm_dataset_cache=/dev/shm/bjtu_dataset_cache/<dataset_name>`.
- Before measuring capacity or copying, check whether the stable `/dev/shm` cache already exists and is ready for that dataset. Default ready criteria are intentionally relaxed for reuse: `.ready` exists and the directory is non-empty beyond marker files.
- If the ready cache is present, export the staged path and skip `du`, `df`, and copy work. Log optional manifest or validation-report presence/mismatch as a warning only; do not block reuse unless the user explicitly enables a strict cache check.
- If the cache is absent, empty, or currently being copied by another job, acquire the cache lock, re-check readiness under the lock, then measure the source with `du -sb` and `/dev/shm` capacity with `df -B1 /dev/shm` only if copying is still needed.
- Leave at least 20 GiB free by default and avoid using more than 70% of `/dev/shm` for one staged dataset unless the user explicitly overrides the threshold.
- Copy only packed inputs, manifests, and validation reports. Do not stage raw ImageFolder trees for multi-account evidence jobs.
- Export the staged path through the same config variables the DataLoader already reads, such as `PACKED_DATA_ROOT`, `DATA_ROOT`, or `DATA_PACKED_ROOT`.
- Use a lock plus `.ready` marker for first-copy creation so concurrent jobs do not read a partial cache. For multi-account reuse, apply ACLs to the cache root, `.locks` directory, and staged dataset directory so each selected cluster OS account has `rwx` and future subdirectories inherit the same default ACL. Pass this as a runtime-only account list such as `SHM_SHARED_ACL_USERS`; accept comma- or space-separated account lists, and do not hardcode private account ids in public scripts or skill files. Apply ACLs both before copying and before reusing an already-ready cache; otherwise the first account that created a private ready cache can keep blocking later accounts. Best-effort `chmod 1777` on the shared cache root and `.locks` directory is allowed to let selected accounts create lock/cache entries while the sticky bit protects unrelated entries. The helper workspace provides `hpc_shm_cache.sh`; source it or copy its logic instead of hand-rolling new staging snippets. If `setfacl` is unavailable or an account cannot change ACLs on an already-created cache, log a warning and continue with the best existing permissions instead of failing training. Write optional source manifest metadata before `.ready`, but keep it advisory by default. Never mark a cache ready before the copy finishes. Do not delete a ready shared cache on normal job exit. Treat `/dev/shm` as node-local disposable cache, not persistent storage. Never write checkpoints, model weights, final results, raw logs, secrets, or private ledgers there.
- If staging is too large, missing, or fails, log the reason and fall back to the account-local packed root.

## DataLoader Guidance

Each worker should lazy-open its own LMDB/HDF5/TFRecord handle. Do not open a write handle in the parent process and share it across forked workers.

Recommended starting points:

```text
raw debug only:
  num_workers=2..4
  persistent_workers=true when num_workers > 0
  prefetch_factor=2

packed dataset on GPFS:
  num_workers=4
  persistent_workers=true
  prefetch_factor=2

packed dataset staged to node-local /dev/shm:
  num_workers=6..8
  persistent_workers=true
  prefetch_factor=2
```

Increase `num_workers` only after measuring throughput; high worker counts can recreate the I/O pressure the packed backend is meant to avoid.

## Incremental Conversion Queue

Manage conversion as independent incremental tasks:

```text
/data/home/<account>/dataset_packed/_conversion_queue/
  queue.jsonl
  completed.jsonl
  failed.jsonl
```

Each record should include dataset, profile, backend, archive, output path, status, and validation report path. Convert the currently needed dataset/profile first, validate it, run one smoke seed, and then copy the packed artifact or repeat conversion under other accounts. Later seeds for the same profile must reuse the existing packed dataset.
