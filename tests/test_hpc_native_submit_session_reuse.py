from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "skills" / "bjtu-hpc" / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hpc_native_submit
from hpc_core import native as native_core


class FakeClient:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def build_args(root: Path) -> argparse.Namespace:
    trace = "hpc_0123456789ab"
    script = root / "candidate.sbatch"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "#SBATCH --job-name=hpc_0123456789ab\n"
        "#SBATCH --ntasks=1\n"
        "#SBATCH --cpus-per-task=6\n"
        "#SBATCH --gres=gpu:1\n",
        encoding="utf-8",
    )
    script_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    intent = {
        "submit_intent": {
            "submit_attempt_id": "attempt-1",
            "backend_idempotency_key": hashlib.sha256(b"idempotency").hexdigest(),
            "anonymous_trace_id": trace,
            "launch_identity_hash": hashlib.sha256(b"launch").hexdigest(),
            "script_or_command_sha256": script_sha,
            "preflight_sha256": hashlib.sha256(b"preflight").hexdigest(),
            "pool_id": "pool-private",
            "execution_route": "bjtu_hpc",
            "trace_embedding": {"anonymous_trace_id": trace, "surface": "slurm_job_name"},
        }
    }
    intent_path = root / "intent.json"
    intent_path.write_text(json.dumps(intent), encoding="utf-8")
    return argparse.Namespace(
        script=script,
        remote_script=None,
        remote_path=None,
        submit=True,
        submit_intent=intent_path,
        receipt_out=root / "receipt.json",
        script_sha256=None,
        json=True,
        timeout=45,
        expected_total_cpus=6,
        expected_ntasks=1,
        expected_cpus_per_task=6,
        expected_gpus=1,
        no_verify=False,
        cluster="cluster2",
        account="private-os-account",
        portal_user="private-portal-user",
        auth_account="private-alias",
        token_file=root / "unused-token",
        refresh_token=False,
        refresh_browser="playwright",
        refresh_headless=False,
    )


def test_submit_and_verification_share_one_client() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = build_args(root)
        client = FakeClient()
        commands: list[str] = []

        def fake_remote(active_client, command: str, timeout: int = 45):
            assert active_client is client
            commands.append(command)
            if "sbatch --job-name" in command:
                return {"returncode": 0, "stdout": "Submitted batch job 12345", "stderr": "", "command": command}
            return {"returncode": 0, "stdout": "ok", "stderr": "", "command": command}

        verification = {
            "success": True,
            "observed": {"num_cpus": 6, "num_tasks": 1, "cpus_per_task": 6, "gpu_count": 1},
            "mismatches": [],
        }
        with (
            patch.object(hpc_native_submit, "load_connection", return_value={"cluster": "cluster2", "account": "x"}) as load,
            patch.object(hpc_native_submit, "connect_ssh", return_value=client) as connect,
            patch.object(hpc_native_submit, "run_remote", side_effect=fake_remote),
            patch.object(hpc_native_submit, "verify_slurm_allocation", return_value=verification) as verify,
        ):
            result = hpc_native_submit.run(args)

        assert result["success"] is True
        assert result["job_id"] == "12345"
        assert load.call_count == 1
        assert connect.call_count == 1
        assert verify.call_count == 1
        assert verify.call_args.kwargs["client"] is client
        assert verify.call_args.kwargs["expected_command"] == result["remote_path"]
        assert client.closed == 1
        assert len(commands) == 3
        assert sum("__BJTU_SCRIPT_BASE64__" in command for command in commands) == 1
        assert sum("bash -n" in command and "sbatch --test-only" in command for command in commands) == 1
        assert sum("sbatch --job-name" in command for command in commands) == 1
        receipt = json.loads(args.receipt_out.read_text())["submit_receipt"]
        assert receipt["script_or_command_sha256"] == receipt["remote_script_sha256"]
        assert receipt["submit_intent_sha256"] == hashlib.sha256(args.submit_intent.read_bytes()).hexdigest()
        assert os.stat(args.receipt_out).st_mode & 0o777 == 0o600


def test_upload_command_preserves_exact_bytes() -> None:
    with tempfile.TemporaryDirectory() as directory:
        remote_path = Path(directory) / "candidate exact.sbatch"
        script_bytes = (
            b"#!/usr/bin/env bash\n"
            b"echo __BJTU_NATIVE_SBATCH__\n"
            b"printf 'trailing spaces preserved'  \n\n"
        )
        digest = hashlib.sha256(script_bytes).hexdigest()
        command = hpc_native_submit.upload_command(script_bytes, str(remote_path), digest)
        completed = subprocess.run(["bash", "-lc", command], text=True, capture_output=True, check=False)
        assert completed.returncode == 0, completed.stderr
        assert remote_path.read_bytes() == script_bytes
        assert f"remote_script_sha256={digest}" in completed.stdout


def test_remote_drift_guard_blocks_preflight_and_submit() -> None:
    with tempfile.TemporaryDirectory() as directory:
        remote_path = Path(directory) / "candidate.sbatch"
        frozen_bytes = b"#!/usr/bin/env bash\necho frozen\n"
        frozen_digest = hashlib.sha256(frozen_bytes).hexdigest()
        remote_path.write_bytes(b"#!/usr/bin/env bash\necho drifted\n")

        preflight = subprocess.run(
            ["bash", "-lc", hpc_native_submit.preflight_command(str(remote_path), frozen_digest)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert preflight.returncode == 74
        assert "remote script SHA mismatch" in preflight.stderr

        submit = subprocess.run(
            [
                "bash",
                "-lc",
                hpc_native_submit.submit_command(
                    str(remote_path),
                    {"anonymous_trace_id": "hpc_0123456789ab"},
                    frozen_digest,
                ),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert submit.returncode == 74
        assert "remote script SHA mismatch" in submit.stderr


def test_frozen_script_and_intent_mutation_fail_before_connection() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        args = build_args(root)
        args.frozen_script_sha256 = hashlib.sha256(args.script.read_bytes()).hexdigest()
        args.frozen_intent_sha256 = hashlib.sha256(args.submit_intent.read_bytes()).hexdigest()
        args.script.write_bytes(args.script.read_bytes() + b"echo changed\n")
        with patch.object(hpc_native_submit, "load_connection") as load:
            try:
                hpc_native_submit.run(args)
            except ValueError as error:
                assert "changed after cycle validation" in str(error)
            else:
                raise AssertionError("mutated script must fail closed")
        assert load.call_count == 0

        args = build_args(root)
        args.frozen_script_sha256 = hashlib.sha256(args.script.read_bytes()).hexdigest()
        args.frozen_intent_sha256 = hashlib.sha256(args.submit_intent.read_bytes()).hexdigest()
        payload = json.loads(args.submit_intent.read_text())
        payload["submit_intent"]["pool_id"] = "mutated-pool"
        args.submit_intent.write_text(json.dumps(payload), encoding="utf-8")
        with patch.object(hpc_native_submit, "load_connection") as load:
            try:
                hpc_native_submit.run(args)
            except ValueError as error:
                assert "intent changed after cycle validation" in str(error)
            else:
                raise AssertionError("mutated intent must fail closed")
        assert load.call_count == 0


def test_scontrol_command_path_is_part_of_verification() -> None:
    client = FakeClient()
    info = {"cluster": "cluster2", "account": "private-os-account"}
    stdout = (
        "JobId=12345 JobState=RUNNING NumCPUs=6 NumTasks=1 CPUs/Task=6 "
        "TRES=cpu=6,gres/gpu=1 Command=/tmp/wrong.sbatch"
    )
    with patch.object(
        native_core,
        "run_remote",
        return_value={"returncode": 0, "stdout": stdout, "stderr": ""},
    ):
        result = native_core.verify_slurm_allocation(
            "12345",
            expected_total_cpus=6,
            expected_ntasks=1,
            expected_cpus_per_task=6,
            expected_gpus=1,
            expected_command="/tmp/frozen.sbatch",
            connection_info=info,
            client=client,
        )
    assert result["success"] is False
    assert any(item["field"] == "command_path" for item in result["mismatches"])


def main() -> None:
    test_submit_and_verification_share_one_client()
    test_upload_command_preserves_exact_bytes()
    test_remote_drift_guard_blocks_preflight_and_submit()
    test_frozen_script_and_intent_mutation_fail_before_connection()
    test_scontrol_command_path_is_part_of_verification()
    print("PASS native submit session reuse fixtures")


if __name__ == "__main__":
    main()
