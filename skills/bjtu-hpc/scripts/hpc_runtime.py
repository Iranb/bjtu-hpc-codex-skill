"""Local controller runtime contract for BJTU HPC helpers."""

from __future__ import annotations

import os
import sys


CONTROLLER_PYTHON = os.getenv("HPC_PYTHON", "python3.12")
REQUIRED_VERSION = (3, 12)


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def require_controller_python() -> None:
    """Fail early unless this controller runs the pinned Python 3.12 runtime."""
    if sys.version_info[:2] != REQUIRED_VERSION:
        current = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        _fail(
            "BJTU HPC controller helpers require Python 3.12; "
            f"received Python {current}. Re-run with {CONTROLLER_PYTHON}."
        )


def require_native_dependencies() -> None:
    """Validate the interpreter contract and SSH dependency used by native Slurm tools."""
    require_controller_python()
    try:
        import paramiko  # noqa: F401
    except ModuleNotFoundError:
        _fail(
            "Python 3.12 is missing Paramiko. Install it with "
            f"{CONTROLLER_PYTHON} -m pip install 'paramiko>=3.4'."
        )
