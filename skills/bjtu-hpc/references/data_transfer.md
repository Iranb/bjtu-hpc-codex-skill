# Data, Transfer, And Runtime Layout

Read this file for portal uploads/downloads, dataset root conventions, account-local archives, packed dataset transfer, ACL checks, account-local environments, and resumable upload progress.

For new AutoResearch data, read `external_hdf5_data_supply.md` first. Raw data stays on the external SSH factory; HPC receives only deterministic HDF5 shards, one owner copy, core/attestation markers and verified read-only sharing. The archive/packed commands below are legacy-only and must never be selected automatically when a project is `external_hdf5_shadow` or `external_hdf5_enforced`. Real upload or ACL mutation still requires an exact plan, fresh native quota, expected registry revision and explicit authorization.

## Contents

- Portal upload/download commands
- Stable dataset roots, archives, packed outputs, and manifests
- Aligned ImageNet-100 path rules
- Safe cross-account archive/packed-data reuse, ACL checks, and persistent-symlink prohibition
- Account-local runtime environments
- Resumable upload progress and dashboard notes

4. Upload or download files through the portal file manager:
   ```bash
   python3 hpc_upload.py ./path --remote-dir home
   python3 hpc_download.py /data/home/<account>/result.json -o .
   ```

5. Manage HPC datasets under explicit, stable paths.

   Keep dataset roots separate from code, logs, outputs, and temporary upload fragments. For BJTU `cluster2`, use these conventions:
   ```text
   source cluster account: /data/home/<source_account>
   target cluster account: /data/home/<target_account>
   raw conversion source: /data/home/<account>/dataset_raw/<dataset_name>
   archive source:       /data/home/<account>/dataset_archives/<dataset_name>.tar
   archive manifest:     /data/home/<account>/dataset_archives/<dataset_name>.manifest.json
   packed training data: /data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/
   packed manifest:      /data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/manifest.json
   upload staging:       /data/home/<account>/dataset_uploads/<dataset_name>/
   legacy raw root:      /data/home/<account>/dataset/<dataset_name>  (legacy/debug only)
   shared owner data:    direct absolute owner path after read-only/traverse ACL verification; never a target-home symlink
   project root:          /data/home/<account>/projects/<project_slug>
   code:                  /data/home/<account>/projects/<project_slug>/code
   run directories:       /data/home/<account>/projects/<project_slug>/runs
   logs/stdout:           /data/home/<account>/projects/<project_slug>/logs
   outputs:               /data/home/<account>/projects/<project_slug>/outputs
   manifests:             /data/home/<account>/projects/<project_slug>/manifests
   ```

   For any new dataset upload, create a stable dataset name first, normally:
   ```text
   <dataset_family>_<split_or_source>_<version>
   ```
   Examples:
   ```text
   vision100_split_seed0_v1
   cub_ssb_default_v1
   cars_ssb_default_v1
   ```
   Then use archive and packed-data roots and keep all temporary transfer artifacts outside training outputs:
   ```text
   /data/home/<account>/dataset_archives/<dataset_name>.tar
   /data/home/<account>/dataset_archives/<dataset_name>.sha256
   /data/home/<account>/dataset_archives/<dataset_name>.manifest.json
   /data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/manifest.json
   /data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>/validation_report.json
   /data/home/<account>/dataset_uploads/<dataset_name>/ # resumable chunks, partial extracts, scratch
   ```
   Do not upload new datasets directly into project `runs/`, `logs/`, `outputs/`, `code/`, `/tmp`, or an existing dataset root. Do not mix two different splits under the same `<dataset_name>`. New training configs should point to `dataset_packed`, not raw image folders, unless the run is a local debug/smoke test or explicitly legacy.

   After archive upload or packed conversion, write a manifest before using the data in training. At minimum include:
   ```json
   {
     "dataset_name": "<dataset_name>",
     "archive_path": "/data/home/<account>/dataset_archives/<dataset_name>.tar",
     "packed_root": "/data/home/<account>/dataset_packed/<dataset_name>/<experiment_profile>",
     "backend": "lmdb",
     "source": "<source server/path or archive>",
     "created_at": "YYYY-MM-DD HH:MM CST",
     "class_count_train": 0,
     "file_count_train": 0,
     "class_count_val": 0,
     "file_count_val": 0,
     "split_metadata": "<path or none>",
     "notes": ""
   }
   ```
   Training configs should point to the packed root and set `DATA_BACKEND=lmdb`, `hdf5`, or `tfrecord`, not to upload staging, archives, temporary extraction directories, or another account's raw small-file tree. Once validation passes, upload staging may be cleaned only after confirming no transfer worker still needs it; never delete `.part` or chunk files for an active transfer.

   For aligned ImageNet-100 experiments, the current aligned raw conversion source is:
   ```text
   legacy/raw source on BJTU:
     /data/home/<source_account>/dataset/data_aligned_split_v1/ImageNet
   ```
   This is the project-aligned-aligned 100-class split. Expected validation counts are train `100` classes / `122115` files, val `100` classes / `5000` files, with `split_metadata.json` size `16747`. Treat it as a conversion source or single-account debug source. For new multi-account training, create account-local archives and packed outputs such as `/data/home/<account>/dataset_packed/imagenet100/aligned_seed0_v1/`.

   Do not use the legacy BJTU ImageNet root for aligned-split ImageNet-100 experiments:
   ```text
   /data/home/<source_account>/dataset/data/ImageNet
   ```
   That path has a different file count and lacks the project-aligned split metadata. Use it only for explicitly legacy jobs whose configs already document that choice.

6. Reuse existing cluster data safely across accounts.

   The BJTU Web "file share" UI is not a reliable general-purpose way to expose an arbitrary existing dataset path to another cluster account. The observed frontend endpoints include:
   ```text
   GET  /pcp/clusters/{cluster}/file/share/list
   POST /pcp/clusters/{cluster}/file/share
   GET  /pcp/clusters/{cluster}/file/share/cancel?id=...
   ```
   In the 2026-05-30 test, both saved accounts returned an empty share list, and path-based share creation against an existing dataset returned 404, JSON decode, or backend DB errors. Treat this Web feature as portal-managed share metadata, not as the primary dataset reuse path.

   Prefer reusing immutable archives or already validated packed datasets. Do not launch new multi-account training that directly scans one source account's raw ImageFolder tree. First inspect the source account, archive or packed-data root, and target cluster OS user:
   ```bash
   <PYTHON3> hpc_share_check.py \
     --auth-account NAME \
     --data-root /data/home/<source_account>/dataset_packed/imagenet100/aligned_seed0_v1 \
     --target-user <target_account>
   ```

   If the archive or packed-data subtree is already readable/executable by group or other users, and only the source home directory blocks traversal, grant the target user execute-only traversal on the source home directory. This does not grant directory listing of the source home:
   ```bash
   setfacl -m u:<target_account>:--x /data/home/<source_account>
   ```

   If the archive or packed-data subtree itself is not readable, use the `hpc_share_check.py` dry-run plan first and only add `--apply` after confirming the target user and data path. The apply mode grants read-only ACLs and can recurse through the selected immutable data tree:
   ```bash
   <PYTHON3> hpc_share_check.py \
     --auth-account NAME \
     --data-root /path/to/source/archive-or-packed-dataset \
     --target-user u22xxxxxx \
     --apply
   ```

   Always verify as the target account before launching real training. A direct proxy SSH read test is sufficient for filesystem access; a small CPU job-side probe is better when queue time is acceptable. The old ImageNet-100 raw-path reuse test proved Unix access only; it is not approval for new multi-account raw small-file training:
   ```text
   /data/home/<source_account>/dataset/data_aligned_split_v1/ImageNet/split_metadata.json
   /data/home/<source_account>/dataset/data_aligned_split_v1/ImageNet/train/n01644373/n01644373_9643.JPEG
   ```

   Do not create a convenience or compatibility symlink in the target account. Configure the consumer with the verified owner's direct absolute path. Use the real source and target cluster OS account names; do not assume portal usernames are the same as cluster OS usernames. If training writes cache/index files next to the dataset, do not use a shared packed path; create a verified physical copy in the target account instead.

   Before transfer, staging, capability admission, or submit preflight, inspect every declared dataset/project root and required file with `lstat`, `readlink`, and `realpath`. Reject a persistent project-managed symlink, a path that traverses one, or a path that resolves outside its registered real root. The only project-managed exception is node-local disposable dataset/cache data when both the link entry and resolved target are under `/dev/shm/bjtu_data_artifacts/` or `/dev/shm/bjtu_dataset_cache/`, created or reused after allocation and exact identity/readiness checks. A persistent path outside `/dev/shm` that points into `/dev/shm` is still prohibited.

7. Keep runtime environments account-local even when datasets are shared.

   Do not launch a target account's jobs with another account's Python or conda environment path. For new GPU training, do not use `pytorch1.7-python3.8` or `/data/apps/anaconda/anaconda3/envs/pytorch1.7-python3.8/bin/python`. Prefer an account-local Python `3.10` environment. The default custom target for the validated CUDA 12.0 / driver 525 platform is:

   ```text
   /data/home/<account>/envs/torch251-cu121-py310
   ```

   Conda creation:
   ```bash
   conda create -y -p /data/home/<account>/envs/torch251-cu121-py310 python=3.10
   conda activate /data/home/<account>/envs/torch251-cu121-py310
   conda install -y \
     pytorch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
     pytorch-cuda=12.1 \
     -c pytorch -c nvidia
   ```

   Pip/venv creation:
   ```bash
   python3.10 -m venv /data/home/<account>/envs/torch251-cu121-py310
   source /data/home/<account>/envs/torch251-cu121-py310/bin/activate
   python -m pip install --upgrade pip
   python -m pip install \
     torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
     --index-url https://download.pytorch.org/whl/cu121
   ```

   Use the PyTorch `2.5.1+cu118` fallback only after a real GPU-node smoke test shows `cu121` fails on BJTU:
   ```bash
   python -m pip install \
     torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
     --index-url https://download.pytorch.org/whl/cu118
   ```

   Use the platform module only as a recorded fallback when the account-local environment is unavailable or fails and the module itself passes a GPU-node smoke test:
   ```bash
   module purge
   module load PyTorch-GPU
   ```

   Verify the environment before using it. Login-node `torch.cuda.is_available() == False` is not enough to reject an environment; CUDA availability must be checked from a GPU allocation:
   ```bash
   /data/home/<account>/envs/torch251-cu121-py310/bin/python - <<'PY'
   import os, sys, torch
   print("python", sys.executable)
   print("prefix", sys.prefix)
   print("uid", os.getuid())
   print("torch", torch.__version__)
   print("torch_cuda", torch.version.cuda)
   print("cuda_available", torch.cuda.is_available())
   print("device_count", torch.cuda.device_count())
   if torch.cuda.is_available():
       print("device0", torch.cuda.get_device_name(0))
       x = torch.randn(1024, 1024, device="cuda")
       y = x @ x
       torch.cuda.synchronize()
       print("matmul_mean", float(y.mean().item()))
   PY
   ```

   When cloning an existing conda environment across accounts, run the clone as the target cluster OS user and force real file copies with `--copy`; plain `conda create --clone` may use hardlinks. After cloning, verify owner, executable path, `torch.__version__`, `torch.version.cuda`, and a GPU-node smoke test before training.

12. Check source-to-cluster dataset upload progress:
   ```bash
   python3 dataset_upload_progress.py
   ```
   Source is `<SOURCE_SSH_ALIAS>` at `~/dataset/data`; destination is `/data/home/<account>/dataset/data`. Use `--watch 30` for repeated checks.
   For the compressed missing-file archive, use:
   ```bash
   python3 dataset_upload_progress.py --archive <archive>.tar.gz
   ```

## Web Dashboard Notes

- `hpc_transfer_web.py` uses `hpc_transfer_tasks.json` as its task config.
- The `Portal Token` panel saves tokens to `~/.bjtu_hpc_token` from Playwright/Chrome/Safari, or from a manually pasted `DESKTOP_PARA_ATOKEN`.
- The optional password field is only passed to the current `hpc_refresh_token.py` subprocess as `HPC_LOGIN_PASSWORD`; never persist it.
- `Portal Jobs` is paged in the browser at 5 rows per page and should show the `GPU` column from `ngpus`.
- For tasks with `total_bytes`, progress should prefer cluster-side SFTP stat of `<dest_path>.part`/`<dest_path>` instead of source-side state JSON. This avoids blocking on `<SOURCE_SSH_ALIAS>` SSH command execution when that server accepts auth but hangs after exec.
- The current archive task `dataset-archive` uses `total_bytes=22425306462` for `/data/home/<account>/dataset/data/_archives/<archive>.tar.gz`.
