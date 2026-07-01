---
name: bjtu-hpc
description: "BJTU HPC portal workflow for the local `slurm` workspace: refresh/save portal tokens, run the local Web dashboard, upload/download files, reuse datasets across accounts, schedule native packed sbatch jobs only after pre-submit runability checks, use `hpc_queue_summary.py --json` or monitor snapshots to choose CPU/GPU shapes, default ordinary jobs to 1GPU/6CPU and fall back to 1GPU/4CPU only for direct-run/resource waits, use optional CPU-rich, wide, GPU-fill, low-VRAM GPU-sharing, or native 1GPU singleton fallbacks when allowed, fill selected accounts to their job cap with queued follow-ups, create/update scheduled queue-monitor heartbeats for status sync and timely refill, inspect CPU/GPU jobs, run native all-account queue summaries, get SSH/SFTP proxy info, monitor resumable uploads, and collect runtime GPU/CPU details."
---

# BJTU HPC

Use the helper scripts in the local `slurm` workspace as the canonical interface to the BJTU HPC portal.

## Read First

Policy authority and drift checks: treat live helper output (`hpc_doctor.py --json`, `hpc_accounts.py`, `hpc_queue_summary.py --json`, monitor/widget snapshots, and helper `--help` defaults) as authoritative over stale static prose when they conflict. Current validated scheduling policy is four non-terminal jobs per auth account: two run-slot jobs plus two queued follow-up jobs. Do not increase that cap, change queued-follow-up counts, or edit dashboard/planner/widget defaults without dated live verification. Before changing cap-related instructions or helper defaults, scan both `bjtu-hpc` and `bjtu-hpc-submit` for `--cap`, `HPC_MONITOR_ACCOUNT_CAP`, `run-slots`, `queued follow-up`, and `QOSMaxJobsPerUserLimit`, then update the paired skill text together.

For any live HPC or remote GPU work, start read-only unless the user explicitly asked to submit, cancel, delete, reserve, chmod, or otherwise mutate state.

## Reference Index

Load only the reference files needed for the user's task:

- `references/auth_dashboard.md`: token refresh, saved accounts, CAS credential prefill, visible Playwright login, Web dashboard, Token Guardian, macOS widget token actions, dashboard LaunchAgent service, and SSH/SFTP proxy discovery.
- `references/data_transfer.md`: portal upload/download, dataset root conventions, resumable archives, cross-account dataset reuse, ACL checks, account-local environments, and upload progress.
- `references/gpu_scheduling.md`: native Slurm GPU submissions, account fill-to-cap behavior, resource planner usage, CPU/GPU fallback order, low-memory GPU sharing, wide/GPU-fill allocations, single-GPU compatibility, pending replacement, and queue-monitor refill policy. Read this before any evidence-producing GPU submit or resource-shape change.
- `references/job_inspection.md`: portal API compatibility jobs, current queue summaries, pending reasons, native allocation checks, and runtime environment probes.
- `references/guardrails.md`: credential, submit, dataset-sharing, upload, and scheduling safety guardrails. Read when changing policy or when an operation can mutate cluster or local state.
- `references/hpc_workflow.md`: validated platform results and environment notes.

## Core Commands

```bash
python3 hpc_accounts.py list
python3 hpc_queue_summary.py --details
python3 hpc_queue_summary.py --json --jobs 4
python3 hpc_plan_from_snapshot.py --planner-json
python3 hpc_native_submit.py ./candidate.sbatch --auth-account NAME --expected-gpus N --expected-ntasks N --expected-cpus-per-task C
python3 hpc_native_submit.py ./candidate.sbatch --auth-account NAME --expected-gpus N --expected-ntasks N --expected-cpus-per-task C --submit
```

Use `--auth-account NAME` for multi-account work. Prefer `hpc_queue_summary.py` for queue/resource snapshots because it queries native Slurm state through the portal SSH proxy and catches pending jobs that portal rows may omit.

## Scheduling Essentials

For evidence-producing GPU training, use native `sbatch` through the portal SSH proxy. Do not rely on the portal PyTorch-GPU app for CPU/GRES-sensitive training because it has produced wrong-shape `1CPU/1GPU` native allocations.

Before every real GPU training submission, read `references/gpu_scheduling.md`, generate or update the exact sbatch script, run local/remote syntax checks plus `sbatch --test-only`, submit only a passing candidate, then verify the real Slurm allocation with `scontrol`.

Default launch unit: one native packed Slurm job requesting `2GPU/12CPU` (`--ntasks=2 --cpus-per-task=6 --gres=gpu:2`) and running two independent one-GPU child experiments. Fill a selected auth account to four non-terminal jobs total when experiment pairs and submit limits allow it: two run-slot packed jobs plus two queued follow-up packed jobs.

One-by-one submission rule: use one fresh snapshot-backed planner decision for exactly one new Slurm job. After any material submit, refresh queue/resources and rerun the planner before the next job.

Low-memory GPU-sharing rule: BJTU V100 nodes are `Tesla V100-PCIE-32GB`. If each child experiment has observed or strongly bounded peak VRAM below `16GB`, and the code is independent single-GPU code rather than true multi-GPU/DDP, a native packed/wide job may intentionally run two child processes on the same allocated GPU to improve utilization. Slurm GPU count remains the number of physical GPUs requested; child capacity may be up to `2 * requested_gpus`. Each physical GPU may host at most two code executions total, meaning one extra co-runner per allocated GPU. Do not stack more than two processes on one GPU, do not use this mode when VRAM is unknown or close to 16GB, and record `low-vram-gpu-share`, peak-VRAM evidence, child labels, requested GPU count, and per-GPU child mapping in launch notes.

Use the monitor/widget resource snapshot and `hpc_resource_planner.py` to choose same-node CPU/GPU shapes. Ordinary evidence-producing jobs should first try `1GPU/6CPU` shapes (`2GPU/12CPU` packed pairs, `1GPU/6CPU` singletons, or wide `N GPU / 6N CPU`). Fall back to `1GPU/4CPU` shapes (`2GPU/8CPU`, `1GPU/4CPU`, or wide `--cpus-per-task=4`) only when exact-script `sbatch --test-only` cannot run directly or would wait because of `Resources`, reservation, same-node CPU pressure, or GPU/GRES shape pressure. CPU-rich `1:8`, `1:12`, or `1:16` shapes are optional only when the user asks for CPU-rich jobs or snapshot plus test-only proves immediate start without reducing GPU occupancy. Wider `3-8GPU` allocations, GPU-fill fragments down to `2` CPUs per GPU, 2GPU-to-1GPU compatibility splits, and low-memory GPU-sharing are explicit exceptions that require the conditions in `references/gpu_scheduling.md`.

## Auth Essentials

For saved accounts, use `hpc_accounts.py` as the source of truth instead of the legacy token file. If auth blocks a user-requested status/progress/upload/submit task with `11009`, `11011`, `11012`, HTTP `401`, or an auth transport error, run the integrated visible refresh flow for the affected account unless the user explicitly forbids browser/token refresh.

Use the Token Guardian panel only after an initial visible CAS login has created a usable account-local Playwright profile. It may validate saved accounts on a schedule, try headless profile refresh, warn on token age before expiry, sync the selected default account to the legacy token file, and mark accounts that need visible login. A token-age warning is maintenance signal, not proof that the token is invalid. Guardian logs and widget states must stay redacted.

Never place portal tokens, cookies, passwords, temporary certificate tokens, or raw credential material in skill files, AGENTS files, Git-tracked files, logs, or final answers.

## Status Essentials

For "current queue", "各账号队列", "running slots", or "pending reason" requests, run `hpc_queue_summary.py --details` first and summarize `RUN`, `PD`, `OTHER`, `TOTAL`, `run_open`, `cap_open`, and pending reasons per account. Use `hpc_pending_reason.py --auth-account NAME` only when deeper `scontrol` fields or node/reservation details are needed.
