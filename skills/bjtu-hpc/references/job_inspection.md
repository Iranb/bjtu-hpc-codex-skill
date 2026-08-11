# Job Inspection And Runtime Probes

Read this file for CPU jobs, portal API compatibility probes, current queue summaries, pending reasons, runtime GPU/CPU inventory probes, and post-submit status checks.

Use `HPC_PYTHON=<PYTHON3.12>` for every local controller command below.

9. Submit jobs through the portal API only for CPU jobs, lightweight probes, uploads, downloads, or other resource-shape-noncritical tasks. Do not use portal API submission for GPU training. GPU training must use the native `sbatch` path above:
   ```bash
   "$HPC_PYTHON" hpc_submit_verified.py ./script.py --auth-account NAME --app gpu --gpu 1 \
     --ntasks 1 --cpus-per-task 6 --gres-flags disable-binding \
     --submit --wait --job-name gpu-compat-probe
   ```
   Use `--auth-account NAME` for every multi-account run. Use `--app cpu` for CPU jobs and reserve `--app gpu` for compatibility probes only, not training. Do not trust portal payloads or portal rows alone for CPU/GRES correctness. `hpc_submit_verified.py` must perform native allocation verification against the Slurm job id, including the `wait.job` row when `--wait` is used. If a verified submit cannot find a Slurm job id or reports an allocation mismatch, mark the launch as failed for scheduling purposes even if the portal submit request returned success. A GPU training job observed as `NumCPUs=1`, `CPUs/Task=1`, `gres/gpu=1` is a wrong-shape launch and must be replaced with native `sbatch`; do not let it remain the primary evidence-producing run.

10. Inspect jobs:
   ```bash
   "$HPC_PYTHON" hpc_queue_summary.py --details
   "$HPC_PYTHON" hpc_queue_summary.py --accounts NAME1,NAME2 --json
   "$HPC_PYTHON" hpc_jobs.py list --auth-account NAME
   "$HPC_PYTHON" hpc_jobs.py wait <trace_id_or_job_id> --auth-account NAME
   "$HPC_PYTHON" hpc_jobs.py cancel <trace_id_or_job_id> --auth-account NAME
   ```
   For "current queue", "各账号队列", "running slots", or "pending reason" requests, run `hpc_queue_summary.py --details` first and summarize `RUN`, `PD`, `OTHER`, `TOTAL`, `run_open`, `cap_open`, and `pending_reasons` per account. Fall back to `hpc_pending_reason.py --auth-account NAME` only when deeper `scontrol` fields or partition/node details are needed. Portal job rows include `ngpus`; display it when presenting job tables or dashboards, but do not treat portal rows as the source of truth for native queue occupancy.

11. Probe runtime environment from inside the cluster:
   ```bash
   "$HPC_PYTHON" hpc_submit.py gpu_env_probe.py --auth-account NAME --app gpu --gpu 1 \
     --ntasks 1 --cpus-per-task 6 --gres-flags disable-binding \
     --submit --wait --job-name gpu-inventory
   ```
   Use this for lightweight `nvidia-smi`, driver/CUDA version, GPU count, and CPU topology checks only. If the probe result must validate CPU/GRES allocation shape, use a native `sbatch` probe and `scontrol show job`, because the portal PyTorch-GPU app may drop `--cpus-per-task` and `--gres-flags` from the generated Slurm script.
