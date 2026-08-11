"""Cross-platform locking and private local filesystem permissions."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


IS_WINDOWS = os.name == "nt"
SYSTEM_SID = "S-1-5-18"

if IS_WINDOWS:
    import msvcrt
    try:
        import ntsecuritycon
        import win32api
        import win32security

        HAS_WIN32_SECURITY = True
    except ImportError:
        HAS_WIN32_SECURITY = False
else:
    import fcntl
    HAS_WIN32_SECURITY = False


class PlatformSecurityError(RuntimeError):
    """Raised when a private path cannot be secured or verified."""


def _file_descriptor(target: int | IO[object]) -> int:
    return target if isinstance(target, int) else target.fileno()


@contextmanager
def exclusive_file_lock(
    target: int | IO[object], *, blocking: bool = True
) -> Iterator[None]:
    """Hold an exclusive process lock for one byte of a lock file."""

    descriptor = _file_descriptor(target)
    if hasattr(target, "flush"):
        target.flush()
    if IS_WINDOWS:
        if os.fstat(descriptor).st_size == 0:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(descriptor, mode, 1)
        except OSError as error:
            if not blocking:
                raise BlockingIOError(str(error)) from error
            raise
        try:
            yield
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    fcntl.flock(descriptor, operation)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def _current_windows_sid() -> str:
    if HAS_WIN32_SECURITY:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        return win32security.ConvertSidToStringSid(sid)
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    rows = list(csv.reader(io.StringIO(result.stdout)))
    if not rows or len(rows[0]) < 2 or not rows[0][1].startswith("S-"):
        raise PlatformSecurityError("could not determine the current Windows user SID")
    return rows[0][1]


def _run_acl_powershell(path: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["BJTU_HPC_ACL_PATH"] = str(path)
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def harden_private_path(path: str | Path, mode: int = 0o600) -> None:
    """Apply mode on POSIX or an exact current-user + SYSTEM DACL on Windows."""

    target = Path(path)
    if not IS_WINDOWS:
        os.chmod(target, mode)
        return
    if mode & 0o077:
        return
    if HAS_WIN32_SECURITY:
        try:
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
            )
            current_sid = win32security.GetTokenInformation(
                token, win32security.TokenUser
            )[0]
            system_sid = win32security.ConvertStringSidToSid(SYSTEM_SID)
            inheritance = 0
            if target.is_dir():
                inheritance = (
                    win32security.CONTAINER_INHERIT_ACE
                    | win32security.OBJECT_INHERIT_ACE
                )
            acl = win32security.ACL()
            for sid in (current_sid, system_sid):
                acl.AddAccessAllowedAceEx(
                    win32security.ACL_REVISION,
                    inheritance,
                    ntsecuritycon.FILE_ALL_ACCESS,
                    sid,
                )
            security_information = (
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION
            )
            win32security.SetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                security_information,
                current_sid,
                None,
                acl,
                None,
            )
            return
        except Exception as error:
            raise PlatformSecurityError(
                f"could not restrict NTFS ACL for private path: {target}"
            ) from error
    script = r"""
$ErrorActionPreference = 'Stop'
$path = $env:BJTU_HPC_ACL_PATH
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
if (Test-Path -LiteralPath $path -PathType Container) {
    $acl = New-Object System.Security.AccessControl.DirectorySecurity
    $inherit = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagate = [System.Security.AccessControl.PropagationFlags]::None
} else {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $inherit = [System.Security.AccessControl.InheritanceFlags]::None
    $propagate = [System.Security.AccessControl.PropagationFlags]::None
}
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($current)
$rights = [System.Security.AccessControl.FileSystemRights]::FullControl
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($current, $rights, $inherit, $propagate, $allow)))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($system, $rights, $inherit, $propagate, $allow)))
Set-Acl -LiteralPath $path -AclObject $acl
"""
    try:
        _run_acl_powershell(target, script)
    except (OSError, subprocess.SubprocessError) as error:
        raise PlatformSecurityError(f"could not restrict NTFS ACL for private path: {target}") from error


def harden_open_file(descriptor: int, path: str | Path, mode: int = 0o600) -> None:
    """Secure an already-created file before secret bytes are written."""

    if IS_WINDOWS:
        harden_private_path(path, mode)
    else:
        os.fchmod(descriptor, mode)


def private_acl_summary(path: str | Path) -> dict[str, object]:
    if not IS_WINDOWS:
        target = Path(path)
        return {"protected": True, "mode": target.stat().st_mode & 0o777}
    target = Path(path)
    if HAS_WIN32_SECURITY:
        try:
            security = win32security.GetNamedSecurityInfo(
                str(target),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.OWNER_SECURITY_INFORMATION,
            )
            acl = security.GetSecurityDescriptorDacl()
            allow_sids: list[str] = []
            deny_sids: list[str] = []
            if acl is not None:
                for index in range(acl.GetAceCount()):
                    header, _mask, sid = acl.GetAce(index)
                    sid_text = win32security.ConvertSidToStringSid(sid)
                    if header[0] == win32security.ACCESS_ALLOWED_ACE_TYPE:
                        allow_sids.append(sid_text)
                    elif header[0] == win32security.ACCESS_DENIED_ACE_TYPE:
                        deny_sids.append(sid_text)
            control, _revision = security.GetSecurityDescriptorControl()
            return {
                "protected": bool(control & win32security.SE_DACL_PROTECTED),
                "current_sid": _current_windows_sid(),
                "allow_sids": allow_sids,
                "deny_sids": deny_sids,
            }
        except Exception as error:
            raise PlatformSecurityError(
                f"could not inspect NTFS ACL for private path: {target}"
            ) from error
    script = r"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $env:BJTU_HPC_ACL_PATH
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
$allow = @($rules | Where-Object AccessControlType -eq Allow | ForEach-Object { $_.IdentityReference.Value })
$deny = @($rules | Where-Object AccessControlType -eq Deny | ForEach-Object { $_.IdentityReference.Value })
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
[pscustomobject]@{
    protected = $acl.AreAccessRulesProtected
    current_sid = $current
    allow_sids = $allow
    deny_sids = $deny
} | ConvertTo-Json -Compress
"""
    try:
        result = _run_acl_powershell(target, script)
        value = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise PlatformSecurityError(f"could not inspect NTFS ACL for private path: {target}") from error
    if not isinstance(value, dict):
        raise PlatformSecurityError(f"invalid NTFS ACL inspection result for: {target}")
    return value


def assert_private_path(path: str | Path, mode: int = 0o600) -> None:
    """Fail closed unless a path is private for the current controller user."""

    target = Path(path)
    if not IS_WINDOWS:
        observed = target.stat().st_mode & 0o777
        if observed & 0o077:
            raise PlatformSecurityError(
                f"private path is readable by group/others: {target} mode={observed:o}"
            )
        return
    acl = private_acl_summary(target)
    current_sid = str(acl.get("current_sid") or _current_windows_sid())
    allowed = {current_sid, SYSTEM_SID}
    actual = {str(value) for value in (acl.get("allow_sids") or [])}
    denied = {str(value) for value in (acl.get("deny_sids") or [])}
    if (
        not acl.get("protected")
        or not allowed.issubset(actual)
        or not actual.issubset(allowed)
        or bool(denied & allowed)
    ):
        raise PlatformSecurityError(
            f"private path has a loose NTFS ACL: {target}; allow only current user and SYSTEM"
        )


def ensure_private_directory(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    harden_private_path(target, 0o700)
    return target
