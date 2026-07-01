# BJTU HPC Workflow Notes

## Connection

- Portal token file: `~/.bjtu_hpc_token`
- SSH/SFTP proxy: `<proxy_host>:<proxy_port>`
- Portal SSH identity: `cluster2,<cluster_account>` plus a temporary certificate token
- Direct SSH to `<proxy_host>:22` did not accept the tested local key for this account

## Validated environment

Observed on `gpu01` from SLURM jobs on 2026-05-07:

- NVIDIA driver: `525.105.17`
- `nvidia-smi` CUDA version: `12.0`
- CUDA toolkit symlink: `/usr/local/cuda -> /usr/local/cuda-11.7`
- `nvcc` is not in `PATH`
- Physical GPUs: `8`
- Free GPUs at sample time: `7`
- Occupied GPUs at sample time: `1`
- Occupied GPU index at sample time: `1`
- `CUDA_VISIBLE_DEVICES` for a 3-GPU job: `3,4,5`
- Logical CPUs: `48`
- CPU topology: `2` sockets × `24` cores/socket, `1` thread/core

Observed on `master1`/GPU partition from native SLURM `sbatch --test-only` on 2026-06-08:

- GPU nodes: `gpu01`-`gpu05`, each with `48` CPUs and `8` V100 GPUs.
- With `--nodes=1 --gres=gpu:1 --gres-flags=disable-binding`, a 1-GPU job is schedulable with up to `48` CPUs; `49` CPUs fails with `allocation failure: Requested node configuration is not available`.
- Without `--gres-flags=disable-binding`, 1-GPU CPU allocation can be constrained by GRES CPU binding and may fail above very small CPU counts.
- Operational default for ordinary training is not the technical maximum: force `--gres-flags disable-binding` and try `--ntasks 1 --cpus-per-task 6` first for single-GPU jobs. If that shape is rejected because of resource wait, fall back to `4`. Wider or CPU-rich shapes require snapshot plus `sbatch --test-only` evidence.

Observed on job `<job_id>` on 2026-06-08:

- `hpc_submit_verified.py`/portal payload included `--cpus-per-task 8` and `--gres-flags disable-binding`, but the portal PyTorch-GPU app generated native `13.sh` without `#SBATCH --cpus-per-task=8` or `#SBATCH --gres-flags=disable-binding`.
- Native `scontrol show job <job_id>` reported `NumCPUs=1`, `NumTasks=1`, `CPUs/Task=1`, and `TRES=cpu=1,node=1,billing=1,gres/gpu=1`.
- Root cause: portal PyTorch-GPU app silently dropped CPU/GRES directives when rendering the Slurm script. For strict `16`-first, minimum-`8` CPU/task training, use native `sbatch` through the portal SSH proxy and verify with `scontrol`.
- Tool-side guard: verified submit wrappers must use the real Slurm job id from either the immediate `job` row or the `wait.job` row, then fail the launch if native allocation mismatches expected CPU/GPU shape.

## Dataset transfer

- Source SSH alias: `<source_alias>`
- Source host: `<source_host>`
- Source user from SSH config: `<source_user>`
- Source directory: `<source_dataset_path>`
- Cluster destination: `/data/home/<cluster_account>/dataset/<dataset_name>`
- Progress script: `python3 dataset_upload_progress.py`
- Watch mode: `python3 dataset_upload_progress.py --watch 30`
- The script compares source sizes with cluster SFTP sizes and treats `<target>.part` as active partial upload state.
- Observed transfer snapshots should record completed, partial, and missing file counts plus transferred bytes in local notes, not in the public skill.
- Missing files archive: `<source_archive_path>/<archive_name>.tar.gz`
- Cluster archive target: `/data/home/<cluster_account>/dataset/<dataset_name>/_archives/<archive_name>.tar.gz`
- Archive progress: `python3 dataset_upload_progress.py --archive <archive_name>.tar.gz`
- Source-side OpenSSH `scp` to the HPC proxy is not reliable: auth succeeded, then the session exited `255`. Source-side Paramiko/SFTP works through the same proxy.
- Source-side SSH can authenticate but hang after exec when reading transfer state/log files. Do not treat this as transfer failure by itself.
- For the compressed archive, use cluster-side SFTP stat of `<dest>.part` as the reliable progress source. Compare `.part` size twice, 10-30 seconds apart, before deciding whether the upload is stalled.
- Archive total size is `<total_bytes>` bytes. Sampled on 2026-05-07 17:07-17:08 +0800, the `.part` grew from `<partial_bytes_a>` to `<partial_bytes_b>` bytes, about `12.43%` to `12.47%`, roughly `0.39 MiB/s`.
- Portal web upload API from the source host was observed to be much slower than resumable transfer. Retest before treating it as a faster dataset-transfer path.

## Local Web dashboard

- Start with `python3 hpc_transfer_web.py`, then open `http://127.0.0.1:8765/`.
- Token panel: saves tokens to `~/.bjtu_hpc_token` through Playwright/Chrome/Safari or a pasted `DESKTOP_PARA_ATOKEN`.
- Do not store portal passwords. The optional password field is only passed to `hpc_refresh_token.py` as `HPC_LOGIN_PASSWORD`.
- Upload task panel: uses `hpc_transfer_tasks.json`; current task `dataset-archive` points at the missing-file archive and has `total_bytes=<total_bytes>`.
- Progress panel: polls `/api/state` every 10 seconds and avoids overlapping refreshes. For tasks with `total_bytes`, progress is computed from cluster-side SFTP stat of `<dest_path>.part`/`<dest_path>`.
- Portal Jobs panel: display `ngpus` as the GPU count and page the browser table at 5 rows per page.

## Useful commands

```bash
python3 hpc_refresh_token.py --browser playwright --headless
python3 hpc_transfer_web.py
python3 hpc_winscp_info.py
python3 hpc_upload.py ./path --remote-dir home
python3 hpc_download.py /data/home/<cluster_account>/result.json -o .
python3 dataset_upload_progress.py
python3 hpc_submit.py gpu_env_probe.py --app gpu --gpu 1 \
  --ntasks 1 --cpus-per-task 6 --gres-flags disable-binding \
  --submit --wait --job-name gpu-inventory
python3 hpc_jobs.py list
python3 hpc_jobs.py wait <job_name>
```

## Probe outputs

`gpu_env_probe.py` writes:

- `~/gpu_env_probe_result.json`
- `~/gpu_env_probe_result.txt`

Use those files when you need to inspect the current node without rerunning the probe.
