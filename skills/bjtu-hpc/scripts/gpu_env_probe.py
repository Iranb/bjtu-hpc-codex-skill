#!/usr/bin/env python3
import json
import os
import platform
import re
import shutil
import subprocess
from pathlib import Path

RESULT_JSON = Path.home() / "gpu_env_probe_result.json"
RESULT_TXT = Path.home() / "gpu_env_probe_result.txt"


def run_cmd(cmd):
    try:
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            check=False,
        )
        return {
            "cmd": cmd,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
    except FileNotFoundError:
        return {
            "cmd": cmd,
            "error": f"command not found: {cmd[0]}",
        }


def parse_nvidia_smi(text):
    driver = None
    cuda = None
    match = re.search(r"Driver Version:\s*([0-9.]+)\s+CUDA Version:\s*([0-9.]+)", text)
    if match:
        driver = match.group(1)
        cuda = match.group(2)
    return driver, cuda


def parse_nvcc_version(text):
    match = re.search(r"release\s+([0-9.]+)", text)
    return match.group(1) if match else None


def parse_lscpu(text):
    cpu_count = None
    core_per_socket = None
    sockets = None
    threads_per_core = None
    for line in text.splitlines():
      if ":" not in line:
        continue
      key, value = line.split(":", 1)
      key = key.strip().lower()
      value = value.strip()
      if key == "cpu(s)":
        cpu_count = value
      elif key == "core(s) per socket":
        core_per_socket = value
      elif key == "socket(s)":
        sockets = value
      elif key == "thread(s) per core":
        threads_per_core = value
    return {
        "cpu(s)": cpu_count,
        "core(s)_per_socket": core_per_socket,
        "socket(s)": sockets,
        "thread(s)_per_core": threads_per_core,
    }


def parse_nvidia_smi_l(text):
    gpus = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("GPU "):
            continue
        match = re.match(r"GPU\s+(\d+):\s+(.+?)\s+\(UUID:\s+([^)]+)\)", line)
        if match:
            gpus.append(
                {
                    "index": int(match.group(1)),
                    "name": match.group(2),
                    "uuid": match.group(3),
                }
            )
    return gpus


def parse_compute_apps(text):
    occupied = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if parts:
            occupied.append(parts[0])
    return sorted(set(occupied))


def main():
    info = {
        "hostname": platform.node(),
        "fqdn": socket_getfqdn(),
        "cwd": os.getcwd(),
        "user": os.getenv("USER"),
        "slurm": {
            "job_id": os.getenv("SLURM_JOB_ID"),
            "node_name": os.getenv("SLURMD_NODENAME"),
            "nodelist": os.getenv("SLURM_NODELIST"),
            "cuda_visible_devices": os.getenv("CUDA_VISIBLE_DEVICES"),
        },
    }

    commands = {
        "nvidia_smi": run_cmd(["nvidia-smi"]),
        "nvidia_smi_L": run_cmd(["nvidia-smi", "-L"]),
        "nvidia_smi_query": run_cmd(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"]),
        "nvidia_compute_apps": run_cmd(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name", "--format=csv,noheader"]),
        "nvcc_version": run_cmd(["nvcc", "--version"]),
        "which_nvcc": run_cmd(["which", "nvcc"]),
        "which_nvidia_smi": run_cmd(["which", "nvidia-smi"]),
        "nproc_all": run_cmd(["nproc", "--all"]),
        "lscpu": run_cmd(["lscpu"]),
        "cuda_version_json": try_read_text("/usr/local/cuda/version.json"),
        "cuda_version_txt": try_read_text("/usr/local/cuda/version.txt"),
        "ls_cuda": run_cmd(["sh", "-lc", "ls -ld /usr/local/cuda* 2>/dev/null || true"]),
    }

    driver_version, cuda_version = parse_nvidia_smi(commands["nvidia_smi"].get("stdout", ""))
    nvcc_version = parse_nvcc_version(commands["nvcc_version"].get("stdout", ""))
    gpus = parse_nvidia_smi_l(commands["nvidia_smi_L"].get("stdout", ""))
    occupied = parse_compute_apps(commands["nvidia_compute_apps"].get("stdout", ""))
    free_gpus = [gpu for gpu in gpus if gpu["uuid"] not in occupied]

    info["versions"] = {
        "nvidia_driver_from_nvidia_smi": driver_version,
        "cuda_from_nvidia_smi": cuda_version,
        "cuda_toolkit_from_nvcc": nvcc_version,
    }
    info["inventory"] = {
        "gpu_total": len(gpus),
        "gpu_occupied": len(occupied),
        "gpu_free": len(free_gpus),
        "gpus": gpus,
        "occupied_gpu_uuids": occupied,
        "free_gpu_indices": [gpu["index"] for gpu in free_gpus],
        "cpu": parse_lscpu(commands["lscpu"].get("stdout", "")),
        "cpu_logical": commands["nproc_all"].get("stdout", "").strip() or None,
    }
    info["commands"] = commands

    summary_lines = [
        f"hostname: {info['hostname']}",
        f"node: {info['slurm']['node_name'] or '-'}",
        f"job_id: {info['slurm']['job_id'] or '-'}",
        f"gpu_total: {len(gpus)}",
        f"gpu_free: {len(free_gpus)}",
        f"nvidia_driver: {driver_version or '-'}",
        f"cuda_from_nvidia_smi: {cuda_version or '-'}",
        f"cuda_toolkit_from_nvcc: {nvcc_version or '-'}",
        f"cpu_logical: {info['inventory']['cpu_logical'] or '-'}",
    ]

    RESULT_JSON.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    RESULT_TXT.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    print("\n".join(summary_lines))
    print(f"[ok] wrote {RESULT_JSON}")
    print(f"[ok] wrote {RESULT_TXT}")
    return 0


def socket_getfqdn():
    try:
        import socket

        return socket.getfqdn()
    except Exception:
        return None


def try_read_text(path):
    p = Path(path)
    if not p.is_file():
        return {"path": path, "exists": False}
    try:
        return {
            "path": path,
            "exists": True,
            "text": p.read_text(encoding="utf-8", errors="replace").strip(),
        }
    except Exception as error:
        return {
            "path": path,
            "exists": True,
            "error": str(error),
        }


if __name__ == "__main__":
    raise SystemExit(main())
