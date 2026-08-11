from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hpc_platform import (
    PlatformSecurityError,
    assert_private_path,
    ensure_private_directory,
    exclusive_file_lock,
    harden_open_file,
    harden_private_path,
)


class PlatformCompatibilityTests(unittest.TestCase):
    def test_private_file_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "private"
            ensure_private_directory(root)
            assert_private_path(root, 0o700)
            path = root / "secret.txt"
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                harden_open_file(descriptor, path, 0o600)
                os.write(descriptor, b"private\n")
            finally:
                os.close(descriptor)
            assert_private_path(path, 0o600)

    def test_windows_loose_acl_is_rejected_and_repaired(self):
        if os.name != "nt":
            self.skipTest("Windows ACL test")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.txt"
            path.write_text("private\n", encoding="utf-8")
            harden_private_path(path, 0o600)
            subprocess.run(
                ["icacls.exe", str(path), "/grant", "*S-1-1-0:(R)"],
                check=True,
                capture_output=True,
            )
            with self.assertRaises(PlatformSecurityError):
                assert_private_path(path, 0o600)
            harden_private_path(path, 0o600)
            assert_private_path(path, 0o600)

    def test_nonblocking_lock_is_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process.lock"
            with path.open("a+b") as first, path.open("a+b") as second:
                with exclusive_file_lock(first, blocking=False):
                    with self.assertRaises(BlockingIOError):
                        with exclusive_file_lock(second, blocking=False):
                            pass


if __name__ == "__main__":
    unittest.main()
