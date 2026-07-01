# Data, Transfer, And Runtime Layout

Read this file for portal uploads/downloads, dataset root conventions, cross-account dataset reuse, ACL checks, account-local environments, and resumable upload progress.

## Contents

- Portal upload/download commands
- Stable dataset roots and manifests
- project-specific dataset path rules
- Cross-account dataset reuse and ACL checks
- Account-local runtime environments
- Resumable upload progress and dashboard notes

4. Upload or download files through the portal file manager:
   ```bash
   python3 hpc_upload.py ./path --remote-dir home
   python3 hpc_download.py /data/home/<cluster_account>/result.json -o .
   ```

5. Manage HPC datasets under explicit, stable paths.

   Keep dataset roots separate from code, logs, outputs, and temporary upload fragments. For BJTU `cluster2`, use these conventions:
   ```text
   main cluster account:  /data/home/<cluster_account>
   other cluster account: /data/home/<cluster_account>
   canonical datasets:    /data/home/<account>/dataset/<dataset_name>
   dataset manifests:     /data/home/<account>/dataset/_manifests/<dataset_name>_manifest.json
   upload staging:        /data/home/<account>/dataset/_uploads/<dataset_name>/
   archive staging:       /data/home/<account>/dataset/_archives/<dataset_name>/
   other-account links:   /data/home/<other_account>/dataset/<dataset_name>  (symlink after access is verified)
   code:                  /data/home/<account>/code/<project>
   outputs:               /data/home/<account>/<keyword>-experiments/... or /data/home/<account>/autoresearch_projs/<project>/outputs
   jobs/stdout:           /data/home/<account>/jobs
   ```

   For any new dataset upload, create a stable dataset name first, normally:
   ```text
   <dataset_family>_<split_or_source>_<version>
   ```
   Examples:
   ```text
   vision_subset_seed0_v1
   benchmark_default_v1
   project_split_v1
   ```
   Then use one canonical root and keep all temporary transfer artifacts outside that final root:
   ```text
   /data/home/<cluster_account>/dataset/<dataset_name>/          # final readable dataset root
   /data/home/<cluster_account>/dataset/_uploads/<dataset_name>/ # resumable chunks, partial extracts, scratch
   /data/home/<cluster_account>/dataset/_archives/<dataset_name>/# uploaded tar/zip archives and .part files
   /data/home/<cluster_account>/dataset/_manifests/<dataset_name>_manifest.json
   ```
   Do not upload new datasets directly into `/data/home/<account>/jobs`, code directories, experiment output directories, `/tmp`, or an existing dataset root. Do not mix two different splits under the same `<dataset_name>`.

   After extraction or sync, write a small manifest before using the dataset in training. At minimum include:
   ```json
   {
     "dataset_name": "<dataset_name>",
     "canonical_root": "/data/home/<cluster_account>/dataset/<dataset_name>",
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
   Training configs should point to the canonical root, not `_uploads`, `_archives`, or a temporary extraction directory. Once validation passes, `_uploads/<dataset_name>` may be cleaned only after confirming no transfer worker still needs it; never delete `.part` or chunk files for an active transfer.

   For project-specific aligned datasets, record the canonical source and any
   target-account symlink explicitly:
   ```text
   canonical source on BJTU:
     /data/home/<source_account>/dataset/<dataset_name>

   optional target-account symlink:
     /data/home/<target_account>/dataset/<dataset_name>
     -> /data/home/<source_account>/dataset/<dataset_name>
   ```
   Store expected class/file counts and split metadata checksum in the dataset
   manifest rather than in the skill text.

   Do not silently use a legacy dataset root for an aligned-split experiment:
   ```text
   /data/home/<cluster_account>/dataset/<legacy_dataset_name>
   ```
   Use a legacy path only for explicitly legacy jobs whose configs already
   document that choice.

6. Reuse existing cluster datasets across accounts instead of uploading another copy when possible.

   The BJTU Web "file share" UI is not a reliable general-purpose way to expose an arbitrary existing dataset path to another cluster account. The observed frontend endpoints include:
   ```text
   GET  /pcp/clusters/{cluster}/file/share/list
   POST /pcp/clusters/{cluster}/file/share
   GET  /pcp/clusters/{cluster}/file/share/cancel?id=...
   ```
   In the 2026-05-30 test, both saved accounts returned an empty share list, and path-based share creation against an existing dataset returned 404, JSON decode, or backend DB errors. Treat this Web feature as portal-managed share metadata, not as the primary dataset reuse path.

   Prefer cluster filesystem permissions. First inspect the source account, data root, and target cluster OS user:
   ```bash
   <PYTHON3> hpc_share_check.py \
     --auth-account main \
     --data-root /data/home/<source_account>/dataset/<dataset_name> \
     --target-user <cluster_account>
   ```

   If the dataset subtree is already readable/executable by group or other users, and only the source home directory blocks traversal, grant the target user execute-only traversal on the source home directory. This does not grant directory listing of the source home:
   ```bash
   setfacl -m u:<cluster_account>:--x /data/home/<cluster_account>
   ```

   If the dataset subtree itself is not readable, use the `hpc_share_check.py` dry-run plan first and only add `--apply` after confirming the target user and data path. The apply mode grants read-only ACLs and can recurse through the dataset:
   ```bash
   <PYTHON3> hpc_share_check.py \
     --auth-account main \
     --data-root /path/to/source/dataset \
     --target-user u22xxxxxx \
     --apply
   ```

   Always verify as the target account before launching real training. A direct proxy SSH read test is sufficient for filesystem access; a small CPU job-side probe is better when queue time is acceptable. For a validated reuse case, the target account should be able to read representative manifest and data files:
   ```text
   /data/home/<source_account>/dataset/<dataset_name>/<split_metadata>.json
   /data/home/<source_account>/dataset/<dataset_name>/<split>/<class>/<sample_file>
   ```

   For convenience, optionally create a symlink in the target account home after access is verified:
   ```bash
   mkdir -p /data/home/<cluster_account>/dataset
   ln -sfn /data/home/<source_account>/dataset/<dataset_name> \
     /data/home/<target_account>/dataset/<dataset_name>
   ```
   Use the real source and target cluster OS account names; do not assume portal usernames are the same as cluster OS usernames.

7. Keep runtime environments account-local even when datasets are shared.

   Do not launch a target account's jobs with another account's Python or conda environment path. For each cluster OS account, copy or rebuild the environment under that account's home, for example:
   ```text
   /data/home/<cluster_account>/envs/torch-cu118-py311
   ```

   When cloning an existing conda environment across accounts, run the clone as the target cluster OS user and force real file copies with `--copy`; plain `conda create --clone` may use hardlinks. Example validated on 2026-05-30:
   ```bash
   SRC=/data/home/<cluster_account>/envs/torch-cu118-py311
   DST=/data/home/<cluster_account>/envs/torch-cu118-py311
   CONDA=/data/home/<cluster_account>/software/miniconda3/bin/conda
   mkdir -p /data/home/<cluster_account>/envs
   "$CONDA" create --copy -y -p "$DST" --clone "$SRC"
   ```

   Verify the copied environment before using it:
   ```bash
   /data/home/<cluster_account>/envs/torch-cu118-py311/bin/python - <<'PY'
   import os, sys, torch
   print(sys.executable)
   print(sys.prefix)
   print(os.getuid())
   print(torch.__version__, torch.version.cuda, torch.cuda.is_available())
   PY
   stat -c '%U:%G %i %n' \
     /data/home/<cluster_account>/envs/torch-cu118-py311/bin/python3.11 \
     /data/home/<cluster_account>/envs/torch-cu118-py311/bin/python3.11
   ```
   It is acceptable for `torch.cuda.is_available()` to be `False` on the login node; use a GPU job-side probe when CUDA runtime availability matters.

12. Check source-to-cluster dataset upload progress:
   ```bash
   python3 dataset_upload_progress.py
   ```
   Source is `<source_alias>` (`<source_host>`) at `<source_path>`; destination is `/data/home/<cluster_account>/dataset/<dataset_name>`. Use `--watch 30` for repeated checks.
   For the compressed missing-file archive, use:
   ```bash
   python3 dataset_upload_progress.py --archive <archive_name>.tar.gz
   ```

## Web Dashboard Notes

- `hpc_transfer_web.py` uses `hpc_transfer_tasks.json` as its task config.
- The `Portal Token` panel saves tokens to `~/.bjtu_hpc_token` from Playwright/Chrome/Safari, or from a manually pasted `DESKTOP_PARA_ATOKEN`.
- The optional password field is only passed to the current `hpc_refresh_token.py` subprocess as `HPC_LOGIN_PASSWORD`; never persist it.
- `Portal Jobs` is paged in the browser at 5 rows per page and should show the `GPU` column from `ngpus`.
- For tasks with `total_bytes`, progress should prefer cluster-side SFTP stat of `<dest_path>.part`/`<dest_path>` instead of source-side state JSON. This avoids blocking when the source SSH server accepts auth but hangs after exec.
- The current archive task `dataset-archive` uses `total_bytes=<total_bytes>` for `/data/home/<cluster_account>/dataset/data/_archives/<archive_name>.tar.gz`.
