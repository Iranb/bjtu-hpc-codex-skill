# GPU Scheduling And Native Slurm Submission

Read this file before any evidence-producing GPU submission, queued refill, resource-shape change, or pending-job replacement.

## Contents

- Default packed jobs and account fill-to-cap policy
- Low-memory GPU-sharing on V100-32GB
- Monitor snapshots and resource planner usage
- Native pre-submit runability gate
- Wide, GPU-fill, and 1GPU compatibility exceptions
- Native sbatch script patterns and allocation verification
- Pending reason diagnosis and authorized replacement
- Scheduled queue-monitor refill policy

8. Schedule GPU experiments by filling the selected account's packed-job capacity.

   For evidence-producing GPU experiment batches, the default ordinary launch unit is a native packed Slurm job requesting `2GPU/12CPU` and running two independent child experiments, one child per allocated GPU:
   ```bash
   #SBATCH --partition=GPU
   #SBATCH --nodes=1
   #SBATCH --ntasks=2
   #SBATCH --cpus-per-task=6
   #SBATCH --gres=gpu:2
   #SBATCH --gres-flags=disable-binding
   ```
   Each child experiment must receive exactly one Slurm-allocated GPU id from the batch-level `CUDA_VISIBLE_DEVICES` and `6` CPU threads by default. Do not interpret a child experiment as `1CPU`; the default ordinary child shape is `1GPU/6CPU`. Use `1GPU/4CPU` only as a documented resource-wait fallback, and use higher CPU-rich shapes only when the snapshot plus `sbatch --test-only` proves they can start immediately without reducing GPU occupancy.

   Low-memory GPU-sharing exception: BJTU V100 nodes are `Tesla V100-PCIE-32GB`. If each child experiment has observed or strongly bounded peak VRAM below `16GB`, and the code is independent single-GPU code rather than true multi-GPU/DDP, one native packed or wide allocation may intentionally run two child processes on each allocated GPU. Request only the physical GPU count from Slurm, then map up to two child labels to each allocated GPU id inside the script. Prefer `--ntasks=<2N> --cpus-per-task=<C> --gres=gpu:<N> --gres-flags=disable-binding` for `N` physical GPUs and `2N` child processes so CPU accounting still reflects every code execution. Each physical GPU may host at most two code executions total. Do not use this mode when peak VRAM is unknown, close to `16GB`, or unstable during warm-up. If the planner or sbatch builder has no explicit GPU-sharing flag, use it only to evaluate physical node resources, then generate or manually review the exact sharing sbatch script and preflight that script with `sbatch --test-only`. Record `low-vram-gpu-share`, the peak-VRAM evidence, requested physical GPUs, child count, CPU per child, and per-GPU child mapping in launch notes.

   For experiment batches, treat each saved auth account as having two packed execution slots and two packed follow-up queue slots. Current Slurm `QOSMaxJobsPerUserLimit` behavior caps each account at four non-terminal jobs total. When the user selects an auth account, fill that account to this real cap in the same launch pass instead of stopping after the first successful packed job:
   ```text
   per account: 2 run-slot packed jobs + 2 queued follow-up packed jobs
   per packed job: 2 independent child experiments, normally 1GPU/6CPU each
   full account target: 4 packed jobs = 8 child experiments
   example saved accounts: account_a/<cluster_user_a>, account_b/<cluster_user_b>, account_c/<cluster_user_c>, account_d/<cluster_user_d>, account_e/<cluster_user_e>
   ```

   A run-slot packed job is one of the first two non-terminal packed jobs assigned to an account and intended to run as soon as resources permit. A queued follow-up packed job is the third or fourth non-terminal packed job for that same account, intentionally kept as backlog so the scheduler can start it after earlier work finishes. `RUNNING`, `PENDING`, dependency-held, configuring, and any other non-terminal packed jobs count against the cap. `DONE`, `FAIL`/`FAILED`, `CANCEL`/`CANCELLED`, `COMPLETED`, and `TIMEOUT` no longer count. Before launching a batch, list accounts and check current jobs for each candidate account:
   ```bash
   python3 hpc_accounts.py list
   python3 hpc_queue_summary.py --details
   python3 hpc_jobs.py list --auth-account account_a --scope current --size 50 --paths
   python3 hpc_jobs.py list --auth-account account_b --scope current --size 50 --paths
   python3 hpc_jobs.py list --auth-account account_c --scope current --size 50 --paths
   ```
   Prefer `hpc_queue_summary.py` for cross-account queue snapshots because it queries native `squeue` through the portal SSH proxy for every saved auth account and catches `PENDING` jobs that the portal `job/list` endpoint may omit. It does not open a browser by default; if an account reports no token or an auth error, refresh only the affected account before relying on the summary. Use `--accounts account_a,account_b`, `--all-partitions`, `--json`, or `--details` as needed.

   Treat the macOS desktop widget/menu bar monitor state as the live resource snapshot for queued-job parameter selection because those components consume the same `hpc_queue_summary.py --json` payload. Before setting CPU/GPU parameters for queued follow-ups, refills, or authorized pending replacements, inspect `checked_at_local`, `cluster_resources.summary`, `cluster_resources.nodes`, `cluster_resources.excluded_reserved_nodes`, account summaries, pending reasons, and each job's native `resources`. If the widget payload is stale, missing `cluster_resources`, or reports a resource query error, refresh with `python3 hpc_queue_summary.py --json` before deciding. Use this snapshot to generate same-node candidates: a `2GPU` shape fits only when one non-reserved node has `gpu_free >= 2` and `cpu_free >= 2 * cpus_per_task`; a `1GPU` shape fits only when one non-reserved node has `gpu_free >= 1` and `cpu_free >= cpus_per_task`; wide single-allocation candidates with `3-8GPU` fit when one non-reserved node has enough same-node GPUs and CPUs for one independent one-GPU child per task; GPU-fill fragment mode can go down to `2` CPUs per GPU when the same node has `free_gpu >= 2` and `free_cpu >= 2 * requested_gpu`. Do not blindly submit CPU-rich `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` queued jobs when the snapshot already shows no node can satisfy them or when `2GPU/12CPU` would occupy the same GPUs sooner. Preselect the largest GPU-count shape that fits; within that GPU count, first try the ordinary `1GPU/6CPU` ratio, raise CPU only for an explicit CPU-rich request or proven immediate fit, and lower to `1GPU/4CPU` only for resource-wait fallback. Let `sbatch --test-only` be the final authority. Record the snapshot timestamp, selected node/free resources, skipped larger or CPU-richer shapes, and final test-only result.

   If free GPUs exist only on nodes whose same-node `cpu_free` is below the minimum floor for every allowed fallback (`1GPU/4CPU`, resource-wait `2GPU/8CPU`, or GPU-fill `2` CPUs per GPU), record this as same-node CPU exhaustion, not simple GPU availability. In that state, lowering CPU, splitting to 1GPU singletons, or GPU-fill cannot claim the visible GPUs because all of them still require same-node CPUs. If a pending run-slot job is already fallback `2GPU/8CPU`, preserve queue position and report `SchedNodeList`, `StartTime`, and `LastSchedEval` instead of canceling/replacing.

   For one-by-one experiment submission, create or consume one queue snapshot and run the resource planner from that snapshot before each new Slurm job. Prefer `hpc_plan_from_snapshot.py`, which runs `hpc_queue_summary.py --json` once and then invokes `hpc_resource_planner.py --queue-json <snapshot>` so the planner does not perform a second live SSH/proxy sweep. Follow only its `next_action`:
   ```bash
   python3 hpc_plan_from_snapshot.py --accounts account_a,account_b,account_c,account_d,account_e --gpu-first --planner-json --summary-jobs 4
   python3 hpc_plan_from_snapshot.py --accounts account_a,account_b,account_c,account_d,account_e --gpu-first --cpu-policy balanced --available-children 8 --planner-json
   python3 hpc_plan_from_snapshot.py --available-children 8 --test-only-probe --wide-gpu-policy auto --planner-json
   python3 hpc_plan_from_snapshot.py --available-children 8 --test-only-probe \
     --probe-script ./candidate.template.sbatch --write-selected-script ./candidate.selected.sbatch --planner-json
   python3 hpc_queue_summary.py --accounts account_a,account_b,account_c,account_d,account_e --json --jobs 4 > /tmp/bjtu_hpc_queue_summary_current.json
   python3 hpc_resource_planner.py --queue-json /tmp/bjtu_hpc_queue_summary_current.json --json --gpu-first
   ```
   The default `--submit-mode sequential` treats each account's four-job cap as the target but recommends only one current submission. After that job is submitted and verified, do not reuse the old snapshot; rerun `hpc_plan_from_snapshot.py` or refresh `hpc_queue_summary.py --json --jobs 4` and pass the new file through `--queue-json` before submitting the next job. This avoids both stale virtual resources and duplicate live queue sweeps when submissions are performed one at a time. Use `--serial` or `--summary-serial` only when the portal proxy misbehaves under bounded parallel account queries. Use `--submit-mode batch` only for dry-run capacity planning or when a caller will submit a whole planned batch without interleaved queue changes. The planner is advisory and read-only, except that `--write-selected-script` may write the locally rewritten sbatch script. Wide/GPU-fill candidates are capped by `--available-children` or `--child-manifest`; when neither is supplied, the planner assumes only two independent children and will not recommend allocations wider than the normal two-child packed job. The default `--cpu-policy balanced` means maximize same-node GPU count first, then prefer the ordinary `1GPU/6CPU` ratio for that GPU count unless a CPU-rich shape has immediate-start evidence and does not strand GPUs. For example, a node with `8G/48C` and eight independent children should prefer `8GPU/48CPU` (`--ntasks=8 --cpus-per-task=6`) as the ordinary wide shape; a node with `8G/40C` may use `8GPU/32CPU` (`--cpus-per-task=4`) as a resource-wait fallback if 1:6 cannot run, or `8GPU/40CPU` (`--cpus-per-task=5`) only as an exact-fit intermediate shape supported by test-only evidence. With only two children, prefer `2GPU/12CPU`; use `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` only for explicit CPU-rich work or when snapshot plus test-only shows immediate start without reducing GPU occupancy. Use `--cpu-policy gpu-dense` only when deliberately preserving CPU for many follow-up placements or a CPU-poor fragment; use `--cpu-policy cpu-fill` only when explicitly trading schedulability for the largest integer CPU shape. With `--gpu-first`, the planner may still generate a low-CPU `2GPU/4CPU` tail-fill candidate when the node has stranded GPUs but too little same-node CPU even for `1GPU/4CPU` ordinary fallback shapes. With `--test-only-probe --probe-script ./candidate.template.sbatch`, the planner rewrites that exact local sbatch template for each candidate and runs remote `bash -n` plus `sbatch --test-only` without submitting. Add `--write-selected-script ./candidate.selected.sbatch` so the final selected resource shape is written locally for `hpc_native_submit.py`. Without `--probe-script`, the probe is resource-shape-only; treat it as scheduling evidence, not final submit permission. If one exact-script candidate can start within the immediate window, choose the largest immediate GPU-count candidate with the highest CPU count that does not delay or strand GPUs, normally 6 CPUs per GPU; otherwise choose the accepted candidate with the earliest Slurm start estimate. The final real experiment script must still pass post-submit `scontrol` verification.

   When no candidate fits current same-node resources, treat planner `queue_probe` recommendations as backlog-only. Plain `queue_probe` should report `do_not_submit=true` and `totals.submissions_to_do_now=0`; if it does not, stop and treat the planner output as stale/buggy. Do not submit a high-CPU `2GPU/16CPU`, `2GPU/24CPU`, or `2GPU/32CPU` queued job just because the planner ranked it during a no-fit snapshot. Under high contention, fill backlog with the ordinary `2GPU/12CPU` shape when it has any plausible start evidence; if exact-script test-only or same-node resources show that 1:6 would wait for `Resources`, reservation, or node CPU pressure, use the `2GPU/8CPU` fallback. Use CPU-rich queued follow-ups only when the user explicitly asks for them or recent resource-history evidence shows they are competitive. Use `--allow-queued-submit` only for an intentional backlog submission, and still run exact-script `sbatch --test-only` before real `sbatch`.

   If the planner returns no `next_action` while `cluster_resources.summary.gpu_free > 0`, do not conclude that the cluster is fully utilized. Inspect `cluster_diagnostics` and each account's `diagnostics` first. `slot_fragmentation` means the account's two running job slots are occupied but the running GPU total is lower than the expected packed target, usually because a `1GPU` singleton consumed an entire run slot. Do not submit beyond the configured account cap to work around this; future refills should pack independent children into `2GPU`, wider, or GPU-fill allocations so each running slot carries more GPUs. `dependency_held_followups` means queued follow-ups count toward the account cap but cannot start early until their dependency is satisfied; use dependencies only for strict ordering, not as the default refill mechanism. A node fragment such as `4G/24C` should prefer one `4GPU/24CPU` wide allocation when at least four independent children exist. A fragment such as `4G/16C` should prefer one `4GPU/16CPU` fallback allocation only when 1:6 cannot run directly. With only one available pair, use the planner's balanced packed recommendation; if there are enough near-term pairs/accounts to fill the fragment, `--cpu-policy gpu-dense` may choose lower-CPU paired submissions to preserve GPU occupancy.

   Maintain a local resource-history ledger for later optimization work. The helper file is `work/hpc_resource_history.jsonl`; the macOS monitors append changed snapshots through `hpc_queue_summary.py --history-log`, and manual refresh is `python3 hpc_queue_summary.py --json --history-log work/hpc_resource_history.jsonl >/tmp/bjtu_hpc_queue_summary_current.json`. Backfill saved native Slurm snapshots with `python3 hpc_resource_history.py --backfill-days 14 --summary`. Before designing or changing a global CPU/GPU allocation policy, summarize this ledger to compare requested shapes, running/pending outcomes, pending reasons, submit/start timing, and same-node free CPU/GPU context. Keep the ledger local and uncommitted; it may include account aliases/job names, but must not include tokens, cookies, passwords, temporary certificates, or local absolute paths.

   Use this fill-to-cap algorithm for each selected account:
   ```text
   current = count non-terminal packed jobs for that auth account
   open_slots = max(0, 4 - current)
   experiment_pairs = floor(number of unlaunched child experiments / 2)
   submit_now = min(open_slots, experiment_pairs)
   ```
   If `submit_now` is greater than zero, submit that many native packed jobs for the selected account before moving on. Do not hold queued follow-ups locally merely because two jobs already exist; jobs three and four are the account's intended queued backlog. If the account currently has 0/1/2/3 non-terminal packed jobs, submit up to 4/3/2/1 additional packed jobs respectively. If it already has 4, submit none unless the user explicitly overrides the cap. For multi-account batches with no specific account preference, apply the same fill-to-cap algorithm to each valid saved account in the requested or discovered order until the experiment queue is exhausted.

   Fill the account's two packed run slots first, then add up to two packed queued follow-ups for that same account. Any saved auth account, such as `account_a` through `account_e`, can hold two run-slot jobs plus two queued follow-ups when its token is valid and scheduler submit limits allow it. A 1GPU singleton also consumes one of these job slots, so an account with one `2GPU` packed job plus one `1GPU` singleton is job-slot full but only running three GPUs. Treat that as slot fragmentation and avoid creating more singleton run slots unless the packed/wide path is genuinely unschedulable. Do not submit a fifth non-terminal job under the same account unless the user explicitly overrides the cap and Slurm accepts it.

   For GPU training jobs, use native `sbatch` through the portal SSH proxy. Do not directly submit GPU training through the portal PyTorch-GPU app or any path that can produce a `NumCPUs=1`, `CPUs/Task=1`, `gres/gpu=1` allocation. On 2026-06-08, the portal PyTorch-GPU app accepted `--cpus-per-task 8` and `--gres-flags disable-binding` in the submitted payload but generated a native `13.sh` without those directives, resulting in a real Slurm allocation of `NumCPUs=1`, `CPUs/Task=1`, and `gres/gpu=1`. Treat any such allocation as wrong-shape, not as a valid GPU training run.

   Before every real GPU training submission, run a native pre-submit runability gate on the exact remote sbatch script and resource shape that would be submitted. Do not skip this gate for queued follow-ups:
   ```text
   packed candidates, in order:
     2GPU/12CPU: --ntasks=2 --cpus-per-task=6 --gres=gpu:2  (ordinary default)
     2GPU/8CPU:  --ntasks=2 --cpus-per-task=4 --gres=gpu:2  (Resources/reservation/same-node-CPU fallback)
     CPU-rich packed candidates: 2GPU/16CPU, 2GPU/24CPU, or 2GPU/32CPU only for explicit CPU-rich work or proven immediate fit without reducing GPU occupancy
     wide GPU allocation: --nodes=1 --ntasks=<N> --cpus-per-task=6 --gres=gpu:<N>, falling back to 4 only for resource waits, for N=3..8 independent one-GPU children
     low-VRAM GPU-sharing allocation: --nodes=1 --ntasks=<2N> --cpus-per-task=<C> --gres=gpu:<N>, for N=1..8 physical GPUs and at most two low-memory children per GPU
     GPU-fill fragment: --nodes=1 --ntasks=<N> --cpus-per-task=2 --gres=gpu:<N> (GPU-first / low-CPU fragment mode; N may be 2 for tail-fill or 3-8 for wider fills)
   single-GPU compatibility candidates, in order:
     1GPU/6CPU, then 1GPU/4CPU
   ```
   For each candidate, update both the `#SBATCH --cpus-per-task` directive and the child process thread limits (`OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS`) before testing. Use the monitor resource snapshot to skip shapes that clearly cannot fit on any same unreserved node, then run `bash -n <script>` and `sbatch --test-only <script>` through the portal SSH proxy. Reject the candidate and test the next lower CPU shape if the syntax check fails, `sbatch --test-only` returns nonzero, or its output indicates the shape cannot be allocated directly, including `BadConstraints`, `Requested node configuration is not available`, allocation failure, GRES/CPU binding failure, QOS submit/job limits, or a delayed start caused by the requested CPU/GPU shape. Submit only the first candidate that passes this gate. If no candidate can run directly, do not submit the larger shape anyway; submit the lowest valid fallback when it at least passes `sbatch --test-only`, then report the exact expected pending reason/start estimate. For ordinary packed jobs, the normal target is `2GPU/12CPU` (`6` CPUs per child). If `2GPU/12CPU` cannot start directly because of `Resources`, reservation constraints, same-node CPU availability, or another resource-shape allocation failure, continue to test `2GPU/8CPU` (`4` CPUs per child) for the selected account before stopping. Do not fall back to 1:4 for pure `Priority`, dependency holds, or QOS job-cap waits. If low-memory GPU-sharing is active, the preflight must verify the exact physical GPU count, child count, and CPU-per-child accounting; after launch, child logs must include an early `nvidia-smi` memory sample, and any OOM or peak VRAM reaching `16GB` disables sharing for that experiment family. If the packed 2GPU shape still cannot be scheduled but the child experiments are single-GPU capable, split the pair into native 1GPU singleton exceptions and test `1GPU/6CPU`, then `1GPU/4CPU`. Acceptable evidence includes GPU/GRES scarcity, no allowed node with two GPUs together, reservation or resource-shape pressure specific to `gres/gpu:2`, or `sbatch --test-only` showing 1GPU can run while the packed 2GPU candidates cannot. Do not use the 1GPU compatibility path for pure `Priority`, `QOSMaxJobsPerUserLimit`, dependency holds, a true multi-GPU/DDP experiment that requires two GPUs in one process, or CPU-only pressure where `2GPU/8CPU` can still run. If neither the 1:4 packed fallback nor the 1GPU compatibility shape passes, stop and report the blocker. When the user explicitly prioritizes occupying GPUs and native Slurm evidence shows a same-node CPU-poor GPU fragment, test a single GPU-fill fragment script instead of separate singleton jobs: `--nodes=1 --ntasks=<N> --cpus-per-task=2 --gres=gpu:<N> --gres-flags=disable-binding`, where `N = min(free_gpu, floor(free_cpu / 2), available independent single-GPU child experiments)`. Prefer `N >= 3`, but allow `N = 2` as a tail-fill exception when that is the largest allowed same-node allocation that avoids leaving the whole fragment idle. Each task launches exactly one independent single-GPU child, all thread limits are set to `2`, and the launch notes must record `gpu-fill-fragment`, the node/free-resource snapshot, child labels, and reason. Do not use GPU-fill fragments for true multi-GPU/DDP children, dependency-held jobs, reservations that exclude the account, or when fewer than two GPUs can be claimed in the same allocation.

   For native sbatch submission through the portal SSH proxy, use the exact-script helper rather than the portal PyTorch-GPU app:
   ```bash
   python3 hpc_native_submit.py ./candidate.sbatch --auth-account NAME \
     --expected-gpus N --expected-ntasks N --expected-cpus-per-task C
   python3 hpc_native_submit.py ./candidate.sbatch --auth-account NAME \
     --expected-gpus N --expected-ntasks N --expected-cpus-per-task C --submit
   ```
   The first command runs only `bash -n` and `sbatch --test-only`; the second submits only after that preflight succeeds and then verifies the native Slurm allocation with `scontrol`.

   Native ordinary packed training scripts should parse the allocated GPUs and launch exactly two child processes, not hardcode GPU ids:
   ```bash
   if [ -z "${CUDA_VISIBLE_DEVICES:-}" ]; then
     echo "CUDA_VISIBLE_DEVICES is empty; refusing to run packed GPU children" >&2
     exit 10
   fi
   IFS=',' read -r -a GPU_IDS <<< "$CUDA_VISIBLE_DEVICES"
   if [ "${#GPU_IDS[@]}" -lt 2 ]; then
     echo "Expected at least 2 allocated CUDA devices, got: $CUDA_VISIBLE_DEVICES" >&2
     exit 11
   fi

   run_child() {
     local label="$1"
     local gpu_id="$2"
     local run_dir="$3"
     (
       export CUDA_VISIBLE_DEVICES="$gpu_id"
       export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
       export MKL_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
       export OPENBLAS_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
       export NUMEXPR_NUM_THREADS="${SLURM_CPUS_PER_TASK:-6}"
       mkdir -p "$run_dir/logs"
       bash ./run_experiment.sh > "$run_dir/logs/packed_child_${label}.out" \
         2> "$run_dir/logs/packed_child_${label}.err"
     ) &
   }

   run_child child_a "${GPU_IDS[0]}" "$RUN_DIR_A"
   run_child child_b "${GPU_IDS[1]}" "$RUN_DIR_B"
   wait
   ```
   For wide/GPU-fill allocations where `N` can be 3-8, generate the exact N-child sbatch script from a manifest or explicit child commands. Without low-memory GPU-sharing, the child count must exactly equal the requested GPU/task count:
   ```bash
   python3 hpc_native_sbatch_builder.py \
     --job-name JOB_NAME --gpus N --cpus-per-task C \
     --manifest ./children.json --output ./candidate.template.sbatch
   python3 hpc_resource_planner.py --available-children N \
     --test-only-probe --probe-script ./candidate.template.sbatch \
     --write-selected-script ./candidate.selected.sbatch
   python3 hpc_native_submit.py ./candidate.selected.sbatch --auth-account NAME \
     --expected-gpus N --expected-ntasks N --expected-cpus-per-task C --submit
   ```
   Manifest items must include a child `label`/`name`/`id` and a shell `command`; optional `work_dir`/`cwd` is honored. The generated script launches one background child per Slurm-allocated GPU, assigns each child exactly one `CUDA_VISIBLE_DEVICES` entry, sets `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `NUMEXPR_NUM_THREADS` to `SLURM_CPUS_PER_TASK`, and writes one log directory per child. Do not request 3-8GPU unless the manifest contains enough independent single-GPU children and the code can run each child independently.
   For low-memory GPU-sharing, the manifest may contain up to `2N` child commands for `N` requested physical GPUs. The generated or manually reviewed script must map child pairs deterministically, for example GPU 0 -> child 0 and child 1, GPU 1 -> child 2 and child 3, while preserving one log directory per child. Set thread limits to `SLURM_CPUS_PER_TASK` for each child and verify post-submit that Slurm granted `NumTasks=2N`, `CPUs/Task=C`, and `gres/gpu=N`.
   For ordinary evidence-producing GPU training, use the monitor resource snapshot to start the pre-submit gate at the largest GPU-count shape that currently fits, with `--cpus-per-task=6` as the ordinary target for each independent one-GPU child. For packed pairs, that means `--ntasks=2 --cpus-per-task=6 --gres=gpu:2` unless the user explicitly asks for CPU-rich jobs and a higher CPU candidate can start immediately without reducing GPU occupancy. If an account has no `RUNNING` packed job and its non-terminal packed jobs are waiting mainly for Slurm `Priority` or `Resources`, later refill submissions for that account should be history-aware and same-node-fit-aware: use `2GPU/12CPU` as the default backlog shape when it has plausible start evidence, but under resource-wait, reservation, or same-node CPU pressure prefer `2GPU/8CPU` over high-CPU `2GPU/16CPU`/`2GPU/24CPU`/`2GPU/32CPU` queue probes. Use CPU-rich shapes only when the snapshot or recent ledger suggests they are competitive, or when the user explicitly wants CPU-rich queued follow-ups. If `2GPU/12CPU` is blocked by `Resources`, reservation constraints, node CPU availability, or another resource-shape allocation failure, the gate must continue to native `2GPU/8CPU` (`--ntasks=2 --cpus-per-task=4 --gres=gpu:2`) before giving up. In that case, each 1-GPU child uses the selected `SLURM_CPUS_PER_TASK`, and the script must lower `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, and `NUMEXPR_NUM_THREADS` consistently. Do not cancel or resubmit historical higher-CPU jobs solely because they are pending for `Priority` or `Resources`; preserve queue position unless the user explicitly authorizes replacement. If the user does authorize replacement for this exact condition, cancel only same-project `PENDING` packed jobs that are being re-submitted with the same child experiment labels/parameters and reduced CPU shape; verify state and ownership with `scontrol show job` before `scancel`, never cancel `RUNNING` or unrelated jobs, and record old/new job ids plus the replacement reason. If GPU-fill fragment mode is explicitly active, an authorized replacement may combine one or more same-account same-project pending packed jobs into one fragment allocation, including lower-priority pending jobs only when every canceled child is preserved in the replacement allocation and the replacement claims more GPUs immediately. Use native single-GPU singleton jobs when the user explicitly asks for one, a repair must preserve a singleton run, the packed 2GPU shape cannot be scheduled but 1GPU can run, or WorkflowGuard records why packed launch is unsafe. Single-GPU compatibility jobs must use native `sbatch`, preserve the child label/parameters from the packed pair, request `--ntasks=1 --gres=gpu:1 --gres-flags=disable-binding`, and test `--cpus-per-task=6` before fallback `4`; record why the packed pair was split and whether the reason was GPU/GRES scarcity, 2GPU resource-shape pressure, or reservation/node co-location pressure.

   When an account has fewer than two `RUNNING` packed jobs but already has up to four non-terminal packed jobs, diagnose the scheduler reason before changing the launch plan. Use native Slurm state, not portal rows:
   ```bash
   python3 hpc_pending_reason.py --auth-account NAME
   ```
   For each pending candidate in the first two intended run slots, inspect `scontrol show job -dd <job_id>` fields including `JobState`, `Reason`, `Dependency`, `ReqNodeList`, `ExcNodeList`, `Features`, `OverSubscribe`, `GresEnforceBind`, `NumCPUs`, `NumTasks`, `CPUs/Task`, `TRES`, `TresPerNode`, `SchedNodeList`, `StartTime`, and `LastSchedEval`. Also inspect node and reservation availability through the portal SSH proxy:
   ```bash
   sinfo -N -p GPU -o '%N|%t|%C|%G'
   scontrol show node=<node> -o
   scontrol show reservation
   ```
   Interpret the result this way:
   ```text
   QOSMaxJobsPerUserLimit:
     The account is already at the cluster's running-job limit. This is normal
     for the queued follow-up jobs; do not replace or lower CPU solely for this
     reason. Only add more queued follow-ups if the account is still below the
     configured cap and Slurm accepts the submit.

   Resources or reservation pressure with 2GPU/12CPU, no dependency, no node/feature pin:
     The job is at the ordinary 1:6 packed CPU shape. Replacing it with the
     same shape will not improve schedulability, but an authorized replacement
     may test 2GPU/8CPU (`--cpus-per-task=4`) before falling back to waiting.
     Preserve queue position unless the replacement is intentional.

   Priority with 2GPU/12CPU, no Resources/reservation blocker:
     Lowering CPU is unlikely to fix pure priority ordering. Preserve queue
     position and report SchedNodeList/StartTime when Slurm provides them.

   Visible free GPUs on a node:
     They are usable only when the same unreserved node also has enough free CPU
     for the requested shape and the current user/account is allowed by any
     active reservation. A node under an active reservation that does not include
     the current user is unavailable even if `sinfo` or `scontrol show node`
     appears to show free GPUs/CPUs.

   Same-node CPU exhaustion with fallback 2GPU/8CPU:
     If the pending job is already `NumTasks=2`, `CPUs/Task=4`, and `gres/gpu=2`,
     and visible free GPUs sit only on nodes with too few CPUs even for GPU-fill
     (`free_cpu < 2 * requested_gpu`), no allowed CPU reduction or 1GPU split can
     make those GPUs usable. Preserve queue position and report Slurm timing.
   ```
   Repair guidance: if pending run-slot jobs are still above the ordinary `1GPU/6CPU` target, future refill submissions may reduce CPU through the normal pre-submit gate, and authorized replacement may replace same-project pending jobs while preserving child labels and parameters. If the pending run-slot job is already `2GPU/12CPU` with `CPUs/Task=6` and is blocked by `Resources`, reservation constraints, or node CPU availability, an authorized replacement may test `2GPU/8CPU` with `CPUs/Task=4`; record the lower-CPU exception in the launch notes. If the pending run-slot job is already `2GPU/8CPU` and no same-node `1GPU/4CPU` or GPU-fill candidate fits, do not cancel/re-submit just to chase visible GPUs; report same-node CPU exhaustion, the job's queue order/timing, and any `SchedNodeList`/`StartTime` Slurm provides. If native Slurm evidence says the packed 2GPU shape cannot be scheduled but a native 1GPU singleton can run, an authorized replacement may split the same experiment pair into two `1GPU/6CPU` singleton jobs, falling back to `1GPU/4CPU` only when the 6-CPU singleton cannot run directly. If GPU-fill mode is active, an authorized replacement may instead consolidate same-account same-project pending children into one `--cpus-per-task=2` fragment allocation, but only if every canceled child label/parameter is preserved and the new job claims all usable GPUs in that same-node fragment. If the blocker is only `Priority`, do not cancel/re-submit just to chase a second running slot; use another valid account, wait for the reported start window, or ask the user before creating an explicit single-GPU exception. Never submit beyond the configured account cap as a workaround for a scheduler-side `Resources` or `Priority` blocker.

   Run `sbatch --test-only <script.sbatch>` before real submission, then `sbatch <script.sbatch>`, and verify with `scontrol show job <job_id>` that default packed jobs report `NumCPUs=12`, `NumTasks=2`, `CPUs/Task=6`, and `TRES`/`TresPerNode` containing `gres/gpu=2` or `gpu:2`. For fallback packed jobs, verify `NumCPUs=8`, `NumTasks=2`, `CPUs/Task=4`, plus the same GPU TRES fields. For explicitly chosen CPU-rich packed jobs, verify the requested CPU shape, such as `NumCPUs=16`, `24`, or `32` with `NumTasks=2` and matching `CPUs/Task=8`, `12`, or `16`. For 1GPU compatibility jobs, verify `NumCPUs=6`, `NumTasks=1`, `CPUs/Task=6`, and `gres/gpu=1` or the explicit fallback shape `NumCPUs=4`, `NumTasks=1`, `CPUs/Task=4`, and `gres/gpu=1`. If any other CPU fallback was used, verify `NumCPUs == NumTasks * CPUs/Task`, `CPUs/Task >= 6` for ordinary packed/singleton jobs, `CPUs/Task >= 4` only for explicitly recorded resource-wait packed or 1GPU compatibility fallbacks, and `CPUs/Task >= 2` only for explicitly recorded GPU-fill fragment jobs. Make the job name encode the experiment pair and slot; for split singletons include the child label and `1g`. If verification reports `NumCPUs=1` or `CPUs/Task=1` for a GPU training run, mark the launch failed/wrong-shape immediately; do not count it as a running experiment except while replacing or explicitly canceling it.

   If the cluster QOS naturally keeps the queued packed follow-ups pending until a slot frees, plain submission is sufficient. If strict "start only after this earlier packed job finishes" ordering is required, submit queued packed follow-ups as native Slurm jobs with dependencies, for example `--dependency=afterany:<job_id>` for automatic refill after completion. The portal submit wrapper may not expose dependencies; use the SSH proxy/native `sbatch` path when dependency semantics matter. Dependency-held follow-ups still count toward the selected account's four-job cap.

   Keep each account's launch script, code path, output path, and environment under that same cluster OS account's home. Shared datasets may use ACLs or target-home symlinks, but the Python path for `other` jobs should be `/data/home/<cluster_account>/envs/...`, not `/data/home/<cluster_account>/envs/...`.

   Group ordinary packed child experiments in pairs with similar expected runtimes. If the user requests an odd number of child experiments, pair the first even set into packed jobs and either hold the odd child in the local launch plan until another compatible child is available or launch it as an explicit single-GPU exception with the reason recorded. If queue-slot submissions are rejected by `QOSMaxSubmitJobPerUserLimit` or a similar submit cap, keep those packed jobs in the local launch plan and submit them when a slot clears; do not repeatedly retry rejected queue submissions. Never pack more than two child experiments into an ordinary Slurm job without explicit user approval or the explicitly recorded GPU-fill fragment or low-memory GPU-sharing exception.

   Scheduled queue-monitor/refill policy:
   - When the user asks to keep BJTU HPC queues full, keep an explicit scheduled monitor active until the user pauses it, the experiment backlog is exhausted, or the workflow reaches a terminal state. If a Codex automation or heartbeat tool is available, update the existing matching monitor instead of creating duplicates; otherwise record the next-check instruction in the project state/index for the next heartbeat.
   - On each monitor wake, first run a live native snapshot with `hpc_queue_summary.py --json` for the selected accounts. Do not trust the portal empty list alone. Sync lightweight terminal results before submitting replacements or follow-ups.
   - Refill in the same wake cycle whenever the snapshot shows an account below the configured non-terminal cap and prepared non-duplicate child experiments remain. Use the exact-script native pre-submit gate for every new job, and refresh the queue after material submissions before deciding the next one.
   - If `sbatch --test-only` fails with `QOSMaxSubmitJobPerUserLimit`, `QOSMaxJobsPerUserLimit`, or another submit cap, record the unsubmitted pairs in the local launch plan/status file and do not retry them in a loop. Retry only after a later live snapshot shows terminal jobs have reduced that account's non-terminal count or the user explicitly changes the cap/queue policy.
   - If jobs are pending for pure `Priority`, preserve queue position and monitor. If jobs are pending for `Resources`, reservation, or same-node CPU pressure, apply the replacement/fallback rules only when they are explicitly allowed and preserve child labels/parameters.
   - Choose the next monitor interval from live state: short, about 10-20 minutes, after recent terminal progress, failed preflight due transient auth, or jobs near expected start; medium, about 30-60 minutes, when jobs are running or mixed running/pending; long, about 60-120 minutes, when all accounts are full and only `Priority`, `Resources`, or submit-cap waits remain. Recompute the interval after every wake instead of using a fixed bucket.
   - Every monitor status artifact or user-facing update must include observed progress, submitted/not-submitted counts, exact blocker such as `QOSMaxSubmitJobPerUserLimit` or `Priority`, the selected next-check interval, and the reason the monitor remains active. Never print tokens, cookies, passwords, temporary certificates, or raw credential material.
