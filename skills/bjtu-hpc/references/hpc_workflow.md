# BJTU HPC Workflow Notes

## Connection

- Portal token file: `~/.bjtu_hpc_token`
- SSH/SFTP proxy: `<HPC_PROXY_HOST>:<HPC_PROXY_PORT>`
- Portal SSH identity: `cluster2,<cluster_account>` plus a temporary certificate token
- Direct SSH to `<HPC_PROXY_HOST>:22` did not accept the tested local key for this account

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

PyTorch/CUDA environment policy updated on 2026-07-02:

- Driver `525.105.17` corresponds to NVIDIA's CUDA Toolkit `12.0 Update 1` release family.
- NVIDIA CUDA minor-version compatibility allows CUDA `12.x` applications on drivers `>=525`, with documented feature limitations.
- PyTorch does not publish a `cu120` wheel. For custom account-local environments, default to Python `3.10` + PyTorch `2.5.1` + `cu121`, because PyTorch `2.5.1` is the pinned verified release in this skill that still publishes `cu121`, `cu118`, and `cu124` packages. Later PyTorch CUDA 12 wheels move away from `cu121` toward newer CUDA runtimes such as `cu124/cu126/cu128/cu130`, which are not the default choice for driver 525 without local validation.
- Use PyTorch `2.5.1+cu118` only as a fallback when a real GPU-node smoke test shows `cu121` fails. Do not default to newer `cu124/cu126/cu128/cu130` builds on driver 525 unless the exact BJTU GPU-node smoke test has passed.
- Prefer the account-local `/data/home/<account>/envs/torch251-cu121-py310` environment for new evidence-producing GPU training. Use `module purge && module load PyTorch-GPU` only as a recorded fallback when the account-local environment is unavailable or fails and the platform module itself passes a GPU-node smoke test.
- New evidence-producing templates must print `python`, `torch.__version__`, `torch.version.cuda`, `torch.cuda.is_available()`, device count, and GPU name before training.
- References: NVIDIA 525.105.17 release notes (`https://docs.nvidia.com/datacenter/tesla/tesla-release-notes-525-105-17/index.html`), NVIDIA CUDA Compatibility (`https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html`), and PyTorch previous-version install matrix (`https://pytorch.org/get-started/previous-versions/`).

Observed on `master1`/GPU partition from native SLURM `sbatch --test-only` on 2026-06-08:

- GPU nodes: `gpu01`-`gpu05`, each with `48` CPUs and `8` V100 GPUs.
- With `--nodes=1 --gres=gpu:1 --gres-flags=disable-binding`, a 1-GPU job is schedulable with up to `48` CPUs; `49` CPUs fails with `allocation failure: Requested node configuration is not available`.
- Without `--gres-flags=disable-binding`, 1-GPU CPU allocation can be constrained by GRES CPU binding and may fail above very small CPU counts.
- Historical technical maximum note: 1-GPU jobs can request many CPUs when `--gres-flags=disable-binding` is set, but this is no longer the default evidence-producing policy. Current compliant default is `--ntasks=1 --cpus-per-task=6 --gres=gpu:1 --gres-flags=disable-binding`, with `--cpus-per-task=4` only as a documented resource-wait fallback after exact-script `sbatch --test-only`. Use CPU-rich `8/12/16` CPUs only when the user explicitly asks for CPU-rich work or live snapshot plus test-only proves immediate start without reducing GPU availability.

Observed on job `<job_id>` on 2026-06-08:

- `hpc_submit_verified.py`/portal payload included `--cpus-per-task 8` and `--gres-flags disable-binding`, but the portal PyTorch-GPU app generated native `13.sh` without `#SBATCH --cpus-per-task=8` or `#SBATCH --gres-flags=disable-binding`.
- Native `scontrol show job <job_id>` reported `NumCPUs=1`, `NumTasks=1`, `CPUs/Task=1`, and `TRES=cpu=1,node=1,billing=1,gres/gpu=1`.
- Root cause: portal PyTorch-GPU app silently dropped CPU/GRES directives when rendering the Slurm script. For strict `16`-first, minimum-`8` CPU/task training, use native `sbatch` through the portal SSH proxy and verify with `scontrol`.
- Tool-side guard: verified submit wrappers must use the real Slurm job id from either the immediate `job` row or the `wait.job` row, then fail the launch if native allocation mismatches expected CPU/GPU shape.

## Dataset transfer

- Source SSH alias, host, username, and filesystem root are controller-private
  configuration. Set them through SSH config and environment variables; do not
  write their values into Git-tracked files.
- Cluster destination: `/data/home/<account>/dataset/data`
- Progress script: `python3 dataset_upload_progress.py`
- Watch mode: `python3 dataset_upload_progress.py --watch 30`
- The script compares source sizes with cluster SFTP sizes and treats `<target>.part` as active partial upload state.
- Cluster archive target: `/data/home/<account>/dataset/data/_archives/<archive>.tar.gz`
- Archive progress: `python3 dataset_upload_progress.py --archive <archive>.tar.gz`
- Source-side OpenSSH `scp` to the HPC proxy is not reliable: auth succeeded, then the session exited `255`. Source-side Paramiko/SFTP works through the same proxy.
- Source-side SSH can authenticate but hang after exec when reading transfer state/log files. Do not treat this as transfer failure by itself.
- For the compressed archive, use cluster-side SFTP stat of `<dest>.part` as the reliable progress source. Compare `.part` size twice, 10-30 seconds apart, before deciding whether the upload is stalled.
- Do not publish archive names, byte counts, throughput samples, source paths,
  or transfer logs; keep them in local runtime state.

## Local Web dashboard

- Start with `python3 hpc_transfer_web.py`, then open `http://127.0.0.1:8765/`.
- Token panel: requires an explicit saved auth account, verifies the token's real portal/bound-account identity, and saves it to that account. It mirrors the token to `~/.bjtu_hpc_token` only when the selected account is the stored default.
- The optional password field is passed only to the selected account's `hpc_accounts.py refresh` subprocess as `HPC_LOGIN_PASSWORD`; persistent credentials belong in the mode-`0600` credential store, never in workspace files or logs.
- Upload task panel: uses `hpc_transfer_tasks.json`; current task `dataset-archive` points at the missing-file archive and has `total_bytes=22425306462`.
- Progress panel: polls `/api/state` every 10 seconds and avoids overlapping refreshes. For tasks with `total_bytes`, progress is computed from cluster-side SFTP stat of `<dest_path>.part`/`<dest_path>`.
- Portal Jobs panel: display `ngpus` as the GPU count and page the browser table at 5 rows per page.

## Useful commands

```bash
python3 hpc_refresh_token.py --browser playwright --headless
python3 hpc_transfer_web.py
python3 hpc_winscp_info.py
python3 hpc_upload.py ./path --remote-dir home
python3 hpc_download.py /data/home/<account>/result.json -o .
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
