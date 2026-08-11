import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import hpc_dashboard_service as service


class WindowsDashboardServiceTests(unittest.TestCase):
    def make_args(self, command: str):
        return service.build_parser().parse_args(
            [
                command,
                "--label",
                "bjtu-hpc-dashboard-test",
                "--python",
                r"C:\Program Files\Python312\python.exe",
            ]
        )

    def result(self, stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(["powershell.exe"], 0, stdout=stdout, stderr="")

    def test_install_registers_per_user_task_with_exact_command(self):
        args = self.make_args("install")
        with patch.object(service, "IS_WINDOWS", True), patch.object(
            service, "run_powershell", return_value=self.result()
        ) as run:
            self.assertEqual(service.install(args), 0)

        script, environment = run.call_args.args
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn", script)
        self.assertEqual(environment["BJTU_HPC_TASK_NAME"], args.label)
        self.assertEqual(environment["BJTU_HPC_TASK_EXECUTE"], str(args.python))
        self.assertIn(str(Path(service.ROOT) / "hpc_transfer_web.py"), environment["BJTU_HPC_TASK_ARGUMENTS"])

    def test_start_stop_and_uninstall_use_task_name_environment(self):
        for action, expected in (
            ("start", "Start-ScheduledTask"),
            ("stop", "Stop-ScheduledTask"),
            ("uninstall", "Unregister-ScheduledTask"),
        ):
            with self.subTest(action=action):
                args = self.make_args(action)
                with patch.object(service, "IS_WINDOWS", True), patch.object(
                    service, "run_powershell", return_value=self.result()
                ) as run:
                    self.assertEqual(getattr(service, action)(args), 0)
                script, environment = run.call_args.args
                self.assertIn(expected, script)
                self.assertEqual(environment, {"BJTU_HPC_TASK_NAME": args.label})

    def test_status_redacts_output_and_probes_dashboard(self):
        args = self.make_args("status")
        raw = '{"task":"bjtu-hpc-dashboard-test","state":"Running"}'
        with patch.object(service, "IS_WINDOWS", True), patch.object(
            service, "run_powershell", return_value=self.result(raw)
        ), patch.object(service, "dashboard_status_probe") as probe:
            self.assertEqual(service.print_status(args), 0)
        probe.assert_called_once_with(args)


if __name__ == "__main__":
    unittest.main()
