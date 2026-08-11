# Validated Workflow Notes

Treat these as workflow patterns, not permanent site facts. Re-run local helpers because proxy endpoints, scheduler limits, partitions, nodes, and runtime modules may change.

## Connectivity

- Portal SSH/SFTP uses a temporary certificate token returned by the portal helper, not a local SSH key.
- Resolve proxy host, port, cluster, and account identity at runtime; do not hardcode observed endpoints.
- Reuse one cycle-local SSH client for upload, preflight, submit, and immediate verification, but never persist temporary certificate material.

## Controller

- Use Python 3.12 with the workspace requirements and Playwright Chromium.
- Run `hpc_doctor.py --json` after setup/account import.
- Treat the multi-account store as authority; refresh only affected aliases.
- Keep account migration packages, credentials, browser profiles, queue history, and private trace ledgers outside Git.

## Scheduling

- Native Slurm state is authoritative over portal compatibility rows.
- Use direct-start admission, one frozen candidate per fresh planner decision, post-submit verification, and a refresh before the next decision.
- Default to one process/seed with one GPU and six CPUs. Use four CPUs only after resource-shape preflight evidence.
- Treat scheduler caps as scheduling blockers, not experimental failures.

## Runtime Evidence

Trust job-side probes over login-node inference. Record Python executable, framework version, CUDA runtime, CUDA availability, device count, GPU name, Slurm allocation fields, and exact script identity for every evidence-producing run.
