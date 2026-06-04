---
name: bjtu-hpc
description: "BJTU HPC portal workflow for the local `slurm` workspace: refresh/save portal tokens, run the local Web dashboard, upload/download files, reuse existing datasets across accounts, schedule two execution-slot experiments plus two queued follow-up experiments per saved account, submit and inspect CPU/GPU jobs, get SSH/SFTP proxy info, monitor resumable dataset uploads, and collect runtime GPU/CPU details from cluster nodes. Use when working with the BJTU HPC portal, `hpc_*.py` scripts, `hpc_transfer_web.py`, or SLURM jobs on the cluster."
---

# BJTU HPC

Use the scripts in this workspace as the canonical interface to the portal.

## Workflow

1. Refresh or save the portal token if `~/.bjtu_hpc_token` is missing or stale:
   ```bash
   python3 hpc_refresh_token.py --browser playwright --headless
   ```
   Use `--browser playwright` without `--headless` if a visible browser is needed. Set `HPC_LOGIN_PASSWORD` only when pre-filling CAS login fields for a one-off refresh.

   If the user explicitly asks for a "captcha/verification-code only" login flow, save CAS login credentials with the local helper below. This stores only on the controller machine in `~/.bjtu_hpc_credentials.json` with file mode `0600`; never place passwords in skill files, AGENTS files, Git-tracked files, logs, or final answers.
   ```bash
   python3 hpc_credentials.py set NAME --login-name PORTAL_USER
   python3 hpc_credentials.py list
   ```
   After this, `hpc_accounts.py refresh NAME --browser playwright` and `hpc_refresh_flow.py NAME --visible-only` will pre-fill the CAS username/password when the login form appears. The user should only enter the captcha/verification code, submit, wait for the HPC portal page to load, and close the Playwright window.

   Token refresh is not an experiment launch. If a user-requested BJTU status/progress check is blocked by `11009`, `11011`, `11012`, or HTTP `401`, immediately run the visible integrated refresh flow; do not ask whether to open Playwright. A "do not launch/start new experiments" request does not block token refresh. Only skip opening Playwright when the user explicitly says not to refresh the token, not to open a browser, or to rely on last-trusted status only.

   For multiple portal accounts, use `hpc_accounts.py` as the source of truth instead of the legacy token file:
   ```bash
   python3 hpc_accounts.py list
   python3 hpc_credentials.py list
   python3 hpc_accounts.py add NAME --refresh --browser playwright --fresh-page --timeout 600
   python3 hpc_accounts.py refresh NAME --browser playwright --headless --fresh-page
   python3 hpc_accounts.py use NAME
   ```
   Adding or refreshing an account auto-discovers `portal_user`, `cluster`, and cluster OS `account` from the portal token when possible, so do not copy defaults from another account unless the user explicitly provides them. Do not pass `--sync-legacy-token` while adding a secondary account unless the user intentionally wants `~/.bjtu_hpc_token` to point at that account. Use `hpc_accounts.py use NAME` for an intentional default/legacy switch.

   If a visible Playwright login window is opened, tell the user to finish CAS login, wait for the HPC portal page to load, and close the window. If the command appears stuck after the window is closed, exits with a visible-browser timeout, or the user says the browser windows were closed, first try reading the same profile headlessly:
   ```bash
   python3 hpc_accounts.py refresh NAME --browser playwright --headless --fresh-page --timeout 30 --sync-legacy-token
   python3 hpc_accounts.py validate NAME
   ```
   Do not start a second visible login before probing the account profile; the login may already have persisted a usable token even when the visible helper timed out.

   If token validation returns `11009`, `11011`, `11012`, HTTP `401`, or an auth transport error, the next action is always `hpc_refresh_flow.py NAME --visible-only` in a PTY. If the user says they can help refresh a token, do not ask again. Keep the command running while the user enters the captcha/verification code in the Playwright window. For multi-account checks, refresh each affected saved account unless the user scopes the request to one account.

2. For a local GUI, run the Web dashboard:
   ```bash
   python3 hpc_transfer_web.py
   ```
   Open `http://127.0.0.1:8765/`. It can get/save tokens, create upload tasks, launch resumable uploads, show upload progress, and list portal jobs with GPU counts. It polls `/api/state` every 10 seconds and avoids overlapping refresh requests.

3. Get SSH/SFTP proxy details when you need interactive access:
   ```bash
   python3 hpc_winscp_info.py
   ```
   Use the returned proxy host/port and temporary certificate token. Do not expect local SSH keys to work through the portal proxy.

4. Upload or download files through the portal file manager:
   ```bash
   python3 hpc_upload.py ./path --remote-dir home
   python3 hpc_download.py /data/home/<cluster_account_main>/result.json -o .
   ```

5. Manage HPC datasets under explicit, stable paths.

   Keep dataset roots separate from code, logs, outputs, and temporary upload fragments. For BJTU `cluster2`, use these conventions:
   ```text
   main cluster account:  /data/home/<cluster_account_main>
   other cluster account: /data/home/<cluster_account_other>
   canonical datasets:    /data/home/<account>/dataset/<dataset_name>
   dataset manifests:     /data/home/<account>/dataset/_manifests/<dataset_name>_manifest.json
   upload staging:        /data/home/<account>/dataset/_uploads/<dataset_name>/
   archive staging:       /data/home/<account>/dataset/_archives/<dataset_name>/
   other-account links:   /data/home/<other_account>/dataset/<dataset_name>  (symlink after access is verified)
   code:                  /data/home/<account>/code/<project>
   outputs:               /data/home/<account>/<project_keyword>-experiments/... or /data/home/<account>/autoresearch_projs/<project>/outputs
   jobs/stdout:           /data/home/<account>/jobs
   ```

   For any new dataset upload, create a stable dataset name first, normally:
   ```text
   <dataset_family>_<split_or_source>_<version>
   ```
   Examples:
   ```text
   imagenet100_simgcd_seed0_v1
   cub_ssb_default_v1
   cars_ssb_default_v1
   ```
   Then use one canonical root and keep all temporary transfer artifacts outside that final root:
   ```text
   /data/home/<cluster_account_main>/dataset/<dataset_name>/          # final readable dataset root
   /data/home/<cluster_account_main>/dataset/_uploads/<dataset_name>/ # resumable chunks, partial extracts, scratch
   /data/home/<cluster_account_main>/dataset/_archives/<dataset_name>/# uploaded tar/zip archives and .part files
   /data/home/<cluster_account_main>/dataset/_manifests/<dataset_name>_manifest.json
   ```
   Do not upload new datasets directly into `/data/home/<account>/jobs`, code directories, experiment output directories, `/tmp`, or an existing dataset root. Do not mix two different splits under the same `<dataset_name>`.

   After extraction or sync, write a small manifest before using the dataset in training. At minimum include:
   ```json
   {
     "dataset_name": "<dataset_name>",
     "canonical_root": "/data/home/<cluster_account_main>/dataset/<dataset_name>",
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

   For <PROJECT> ImageNet-100 experiments, the current aligned dataset is:
   ```text
   canonical source on BJTU:
     /data/home/<cluster_account_main>/dataset/<dataset_name>/<dataset_subdir>

   optional target-account symlink:
     /data/home/<cluster_account_other>/dataset/<dataset_name>/<dataset_subdir>
     -> /data/home/<cluster_account_main>/dataset/<dataset_name>/<dataset_subdir>
   ```
   This is the SimGCD-aligned 100-class split. Expected validation counts are train `100` classes / `122115` files, val `100` classes / `5000` files, with `<split_metadata>.json` size `16747`.

   Do not use the legacy BJTU ImageNet root for aligned-split ImageNet-100 experiments:
   ```text
   /data/home/<cluster_account_main>/dataset/<legacy_dataset_name>/<dataset_subdir>
   ```
   That path has a different file count and lacks the SimGCD split metadata. Use it only for explicitly legacy jobs whose configs already document that choice.

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
     --data-root /data/home/<cluster_account_main>/dataset/<dataset_name>/<dataset_subdir> \
     --target-user <cluster_account_other>
   ```

   If the dataset subtree is already readable/executable by group or other users, and only the source home directory blocks traversal, grant the target user execute-only traversal on the source home directory. This does not grant directory listing of the source home:
   ```bash
   setfacl -m u:<cluster_account_other>:--x /data/home/<cluster_account_main>
   ```

   If the dataset subtree itself is not readable, use the `hpc_share_check.py` dry-run plan first and only add `--apply` after confirming the target user and data path. The apply mode grants read-only ACLs and can recurse through the dataset:
   ```bash
   <PYTHON3> hpc_share_check.py \
     --auth-account main \
     --data-root /path/to/source/dataset \
     --target-user u22xxxxxx \
     --apply
   ```

   Always verify as the target account before launching real training. A direct proxy SSH read test is sufficient for filesystem access; a small CPU job-side probe is better when queue time is acceptable. For the validated ImageNet-100 reuse case, `<cluster_account_other>` successfully read:
   ```text
   /data/home/<cluster_account_main>/dataset/<dataset_name>/<dataset_subdir>/<split_metadata>.json
   /data/home/<cluster_account_main>/dataset/<dataset_name>/<dataset_subdir>/train/n01644373/n01644373_9643.JPEG
   ```

   For convenience, optionally create a symlink in the target account home after access is verified:
   ```bash
   mkdir -p /data/home/<cluster_account_other>/dataset
   ln -sfn /data/home/<cluster_account_main>/dataset/<dataset_name> \
     /data/home/<cluster_account_other>/dataset/<dataset_name>
   ```
   Use the real source and target cluster OS account names; do not assume portal usernames are the same as cluster OS usernames.

7. Keep runtime environments account-local even when datasets are shared.

   Do not launch a target account's jobs with another account's Python or conda environment path. For each cluster OS account, copy or rebuild the environment under that account's home, for example:
   ```text
   /data/home/<cluster_account_other>/envs/torch-cu118-py311
   ```

   When cloning an existing conda environment across accounts, run the clone as the target cluster OS user and force real file copies with `--copy`; plain `conda create --clone` may use hardlinks. Example validated on 2026-05-30:
   ```bash
   SRC=/data/home/<cluster_account_main>/envs/torch-cu118-py311
   DST=/data/home/<cluster_account_other>/envs/torch-cu118-py311
   CONDA=/data/home/<cluster_account_main>/software/miniconda3/bin/conda
   mkdir -p /data/home/<cluster_account_other>/envs
   "$CONDA" create --copy -y -p "$DST" --clone "$SRC"
   ```

   Verify the copied environment before using it:
   ```bash
   /data/home/<cluster_account_other>/envs/torch-cu118-py311/bin/python - <<'PY'
   import os, sys, torch
   print(sys.executable)
   print(sys.prefix)
   print(os.getuid())
   print(torch.__version__, torch.version.cuda, torch.cuda.is_available())
   PY
   stat -c '%U:%G %i %n' \
     /data/home/<cluster_account_main>/envs/torch-cu118-py311/bin/python3.11 \
     /data/home/<cluster_account_other>/envs/torch-cu118-py311/bin/python3.11
   ```
   It is acceptable for `torch.cuda.is_available()` to be `False` on the login node; use a GPU job-side probe when CUDA runtime availability matters.

8. Schedule experiments across accounts with a two-run-slot plus two-queue-slot policy.

   For experiment batches, treat each saved auth account as having two execution slots and two follow-up queue slots:
   ```text
   per account: 2 run-slot experiments + 2 queued follow-up experiments
   current accounts: main/<cluster_account_main> and other/<cluster_account_other>
   target total: 4 run-slot experiments + 4 queued follow-up experiments
   ```

   A run-slot experiment is one of the first two non-terminal experiments assigned to an account and intended to run as soon as resources permit. A queued follow-up experiment is an additional submitted experiment intentionally kept as backlog so the scheduler can start it after earlier work finishes. `DONE`, `FAILED`, and `CANCELLED` jobs no longer count toward either slot type. Before launching a batch, list accounts and check current jobs for each candidate account:
   ```bash
   python3 hpc_accounts.py list
   python3 hpc_jobs.py list --auth-account main --scope current --size 50 --paths
   python3 hpc_jobs.py list --auth-account other --scope current --size 50 --paths
   ```

   Fill each account's two run slots first, then allow up to two queued follow-ups for that same account. With the current validated accounts, `main`/`<cluster_account_main>` can hold two run-slot experiments plus two queued follow-ups, and `other`/`<cluster_account_other>` can hold the same. Do not submit a fifth non-terminal experiment under the same account unless the user explicitly overrides the cap.

   Submit each job with an explicit auth account, and make the job name encode the experiment clearly:
   ```bash
   python3 hpc_submit.py ./train_exp_a.py --auth-account main --app gpu --gpu 1 \
     --job-name exp-a-main-slot1 --submit
   python3 hpc_submit.py ./train_exp_b.py --auth-account main --app gpu --gpu 1 \
     --job-name exp-b-main-slot2 --submit
   python3 hpc_submit.py ./train_exp_c.py --auth-account main --app gpu --gpu 1 \
     --job-name exp-c-main-q1 --submit
   python3 hpc_submit.py ./train_exp_d.py --auth-account main --app gpu --gpu 1 \
     --job-name exp-d-main-q2 --submit
   ```

   If the cluster QOS naturally keeps the queued follow-ups pending until a slot frees, plain submission is sufficient. If strict "start only after this earlier experiment finishes" ordering is required, submit queued follow-ups as native Slurm jobs with dependencies, for example `--dependency=afterany:<job_id>` for automatic refill after completion. The portal submit wrapper may not expose dependencies; use the SSH proxy/native `sbatch` path when dependency semantics matter.

   Keep each account's launch script, code path, output path, and environment under that same cluster OS account's home. Shared datasets may use ACLs or target-home symlinks, but the Python path for `other` jobs should be `/data/home/<cluster_account_other>/envs/...`, not `/data/home/<cluster_account_main>/envs/...`.

   If the native Slurm reason is `QOSMaxJobsPerUserLimit` while the account should still run two single-GPU experiments, use one native packed job with `--gres=gpu:2` and two child launches as the fallback for the run slots. If queue-slot submissions are rejected by `QOSMaxSubmitJobPerUserLimit` or a similar submit cap, keep those experiments in the local launch plan and submit them when a run slot clears; do not repeatedly retry rejected queue submissions. In packed jobs, parse the batch-level `CUDA_VISIBLE_DEVICES` and pass the allocated physical GPU ids to child processes; never hardcode child GPUs as `0` and `1` unless Slurm allocated exactly those ids. Pack only two experiments per account and prefer similar expected runtimes.

9. Submit jobs through the portal API:
   ```bash
   python3 hpc_submit.py ./script.py --auth-account main --app gpu --gpu 1 --submit --wait
   ```
   Use `--auth-account NAME` for every multi-account run. Use `--app gpu` for GPU jobs, `--app cpu` for CPU jobs, and `--wait` when you need terminal status.

10. Inspect jobs:
   ```bash
   python3 hpc_jobs.py list --auth-account main
   python3 hpc_jobs.py wait <job_name> --auth-account main
   python3 hpc_jobs.py cancel <job_name> --auth-account main
   ```
   Portal job rows include `ngpus`; display it when presenting job tables or dashboards.

11. Probe runtime environment from inside the cluster:
   ```bash
   python3 hpc_submit.py gpu_env_probe.py --auth-account main --app gpu --gpu 1 --submit --wait --job-name gpu-inventory
   ```
   Use this for `nvidia-smi`, driver/CUDA version, GPU count, and CPU topology.

12. Check source-to-cluster dataset upload progress:
   ```bash
   python3 dataset_upload_progress.py
   ```
   Source is `<source_server_alias>` (`<source_host>`) at `~/dataset/data`; destination is `/data/home/<cluster_account_main>/dataset/data`. Use `--watch 30` for repeated checks.
   For the compressed missing-file archive, use:
   ```bash
   python3 dataset_upload_progress.py --archive <dataset_archive>.tar.gz
   ```

## Web Dashboard Notes

- `hpc_transfer_web.py` uses `hpc_transfer_tasks.json` as its task config.
- The `Portal Token` panel saves tokens to `~/.bjtu_hpc_token` from Playwright/Chrome/Safari, or from a manually pasted portal auth token.
- The optional password field is only passed to the current `hpc_refresh_token.py` subprocess as `HPC_LOGIN_PASSWORD`; never persist it.
- `Portal Jobs` is paged in the browser at 5 rows per page and should show the `GPU` column from `ngpus`.
- For tasks with `total_bytes`, progress should prefer cluster-side SFTP stat of `<dest_path>.part`/`<dest_path>` instead of source-side state JSON. This avoids blocking on `<source_server_alias>` SSH command execution when that server accepts auth but hangs after exec.
- The current archive task `dataset-archive` uses `total_bytes=<archive_size_bytes>` for `/data/home/<cluster_account_main>/dataset/data/_archives/<dataset_archive>.tar.gz`.

## Guardrails

- Use the token file `~/.bjtu_hpc_token`; never hardcode tokens or certificate values.
- For multi-account work, treat `~/.bjtu_hpc_accounts.json` as the auth source of truth and `~/.bjtu_hpc_token` only as a legacy compatibility cache. Prefer `--auth-account NAME` on common commands.
- For experiment batches, cap each saved auth account at two run-slot experiments plus two queued follow-up experiments. Check current jobs per account before submitting, fill the two run slots first, then add at most two queued follow-ups, and do not submit a fifth non-terminal experiment under the same account unless the user explicitly overrides the cap.
- Current validated scheduling policy: `main`/`<cluster_account_main>` can carry two run-slot experiments plus two queued follow-ups, and `other`/`<cluster_account_other>` can carry two run-slot experiments plus two queued follow-ups, assuming resources and scheduler submit limits allow them.
- If strict refill ordering is required, queue follow-up experiments with native Slurm dependencies such as `--dependency=afterany:<job_id>`; plain portal submissions may become runnable immediately if scheduler/QOS limits allow them.
- If a per-account two-experiment launch hits `QOSMaxJobsPerUserLimit`, use a native two-experiment packed job with `--gres=gpu:2` as the fallback for run slots; never pack more than two experiments per account without explicit user approval.
- If queued follow-up submissions hit `QOSMaxSubmitJobPerUserLimit` or a similar submit cap, record them in the local launch plan and submit later when a slot clears rather than retrying in a loop.
- Multi-account launches must keep account-local code, outputs, and environments under the corresponding cluster OS home. Shared datasets can cross accounts by ACL, but runtime paths should not cross accounts.
- Prefer the portal proxy from `hpc_winscp_info.py` for SSH/SFTP.
- Prefer a job-side probe over trying to infer GPU/CPU details from the login machine.
- Before re-uploading a dataset for another account, first test whether the target cluster OS user can reuse the existing cluster path via Unix permissions or ACLs. Portal tokens and the Web file-share UI do not by themselves grant Slurm jobs read access to another account's files.
- For cross-account dataset reuse, use the minimum permission that works: execute-only ACL on the source home directory when the dataset subtree is already readable, recursive read-only ACLs only when the subtree blocks reads.
- After ACL changes, verify reads as the target cluster OS user and pass the shared absolute path or a target-home symlink into training configs; do not duplicate large datasets unless access cannot be made safe.
- Datasets may be shared by ACL, but Python/conda runtime environments must live under the account that runs the job. Do not point `<cluster_account_other>` jobs at `/data/home/<cluster_account_main>/envs/...`; copy or rebuild the environment under `/data/home/<cluster_account_other>/envs/...`.
- For conda environment copies, use `conda create --copy --clone` as the target cluster OS user and verify owner plus inode samples. Avoid default clone hardlinks when the intent is an account-local copy.
- Use `dataset_upload_progress.py` before restarting dataset transfers; it detects completed files and active `.part` files by size.
- For resumable archive uploads, treat cluster-side `.part` size as the progress source of truth when source-side state/log SSH reads hang.
- Do not restart a resumable upload screen just because source-side state reads fail. First compare cluster-side `.part` size twice, 10-30 seconds apart, to determine whether bytes are still increasing.
- For dataset transfer, do not assume OpenSSH `scp` works from the source server. It authenticated to `<proxy_host>:<proxy_port>` but exited with status `255`; source-side Paramiko/SFTP works through the same proxy.
- Portal web upload API from `<source_server_alias>` was tested with `hpc_upload.py`: `128MiB` took `512.874s`, about `0.25 MiB/s`. Do not treat the web upload API as a faster dataset-transfer path unless retested.
- If upload or query APIs return auth errors, refresh the token once and retry.
- Read `references/hpc_workflow.md` when you need validated platform results and current environment notes.
