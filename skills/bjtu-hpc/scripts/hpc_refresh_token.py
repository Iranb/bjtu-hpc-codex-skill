#!/usr/bin/env python3.12
import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from hpc_runtime import require_controller_python

require_controller_python()

from hpc_account_store import (
    AccountStoreError,
    credential_for_account,
    get_account,
    load_store,
    profile_dir_for,
    resolve_account_name,
    save_account_token,
    upsert_account,
)
from hpc_token_identity import verify_token_identity

PORTAL_URL = "https://hpc.bjtu.edu.cn/"
PORTAL_ORIGIN = "https://hpc.bjtu.edu.cn"
CAS_ORIGIN = "https://cas.bjtu.edu.cn"
DEFAULT_TOKEN_FILE = Path(os.getenv("HPC_PARA_ATOKEN_FILE", "~/.bjtu_hpc_token")).expanduser()
DEFAULT_PROFILE_DIR = Path(os.getenv("HPC_BROWSER_PROFILE", "~/.bjtu_hpc_browser")).expanduser()
DEFAULT_LOGIN_NAME = os.getenv("HPC_LOGIN_NAME") or os.getenv("HPC_PORTAL_USER", "")
DEFAULT_LOGIN_PASSWORD_ENV = "HPC_LOGIN_PASSWORD"
INVALID_TOKEN_CODES = {11009, 11011, 11012}


def redact_token_text(value):
    return re.sub(r"(atoken is invalid: )[^\s}]+", r"\1<redacted>", str(value))


def token_validation_failed(validation):
    http_status = validation.get("http_status")
    return validation.get("code") in INVALID_TOKEN_CODES or (
        isinstance(http_status, int) and http_status >= 400
    )


def validation_summary(validation):
    if not isinstance(validation, dict):
        return {"ok": False, "message": redact_token_text(validation)}
    return {
        "ok": not token_validation_failed(validation),
        "code": validation.get("code"),
        "http_status": validation.get("http_status"),
        "success": validation.get("success"),
        "message": redact_token_text(
            validation.get("msg") or validation.get("message") or validation.get("raw")
        ),
    }


def resolve_sync_account_name(requested_account=None, *, strict=False):
    try:
        return resolve_account_name(requested_account)
    except AccountStoreError:
        if strict:
            raise
        return None


def profile_dir_for_sync_account(requested_account=None):
    account = resolve_sync_account_name(requested_account)
    if not account:
        return DEFAULT_PROFILE_DIR
    try:
        _, entry = get_account(account)
    except AccountStoreError:
        return DEFAULT_PROFILE_DIR
    return profile_dir_for(account, entry)


def apply_verified_identity(name, entry, identity):
    """Fill absent identity metadata without ever rewriting an established binding."""
    updates = {
        key: identity.get(key)
        for key in ("portal_user", "cluster", "account")
        if not str(entry.get(key) or "").strip() and str(identity.get(key) or "").strip()
    }
    if not updates:
        return entry
    return upsert_account(name, **updates)


def sync_auth_account_token(token, validation, requested_account=None, *, strict=False):
    account = resolve_sync_account_name(requested_account, strict=strict)
    if not account:
        return None
    _, entry = get_account(account)
    identity = verify_token_identity(account, token, entry)
    saved = save_account_token(
        account,
        token,
        validation=validation_summary(validation) if validation else None,
    )
    apply_verified_identity(account, saved, identity)
    return account


def playwright_account_settings(
    requested_account,
    explicit_login_name,
    login_password_env,
    explicit_profile_dir,
):
    """Resolve Playwright settings from the selected alias, never another account's defaults."""
    account = resolve_sync_account_name(requested_account)
    if not account:
        return {
            "account": None,
            "profile_dir": explicit_profile_dir or DEFAULT_PROFILE_DIR,
            "login_name": explicit_login_name or DEFAULT_LOGIN_NAME,
            "login_password": os.getenv(login_password_env, ""),
        }

    _, entry = get_account(account)
    credential = credential_for_account(account)
    env_password = os.getenv(login_password_env)
    return {
        "account": account,
        "profile_dir": explicit_profile_dir or profile_dir_for(account, entry),
        "login_name": (
            explicit_login_name
            or str(credential.get("login_name") or "").strip()
            or str(entry.get("portal_user") or "").strip()
        ),
        "login_password": (
            env_password if env_password is not None else str(credential.get("login_password") or "")
        ),
    }


def should_write_token_file(requested_account, token_file, *, explicit_sync=False):
    """Keep the legacy global token pinned to the stored default account."""
    if explicit_sync:
        return True
    path = Path(token_file).expanduser()
    if path != DEFAULT_TOKEN_FILE.expanduser():
        return True
    account = resolve_sync_account_name(requested_account)
    if not account:
        return requested_account is None
    return load_store().get("default") == account


def clear_playwright_auth_session(context):
    """Remove persisted CAS/portal cookies from one alias-specific browser profile."""
    context.clear_cookies()


def run_osascript(script):
    result = subprocess.run(
        ["osascript"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def read_token_from_chrome():
    return run_osascript(
        """
set foundToken to ""
tell application "Google Chrome"
    repeat with w in windows
        repeat with t in tabs of w
            if ((URL of t) as text) starts with "https://hpc.bjtu.edu.cn" then
                set foundToken to execute javascript "localStorage.getItem('DESKTOP_PARA_ATOKEN') || ''" in t
                return foundToken
            end if
        end repeat
    end repeat
end tell
return foundToken
"""
    )


def read_token_from_safari():
    return run_osascript(
        """
set foundToken to ""
tell application "Safari"
    repeat with w in windows
        repeat with t in tabs of w
            if ((URL of t) as text) starts with "https://hpc.bjtu.edu.cn" then
                set foundToken to do JavaScript "localStorage.getItem('DESKTOP_PARA_ATOKEN') || ''" in t
                return foundToken
            end if
        end repeat
    end repeat
end tell
return foundToken
"""
    )


def read_token(browser):
    if browser == "chrome":
        return read_token_from_chrome()
    if browser == "safari":
        return read_token_from_safari()
    raise ValueError(f"unsupported browser: {browser}")


def read_token_from_playwright(
    profile_dir,
    headless,
    timeout,
    login_name="",
    login_password="",
    fresh_page=False,
    clear_existing_token=False,
    clear_auth_session=False,
):
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Playwright is not installed. Run: python3 -m pip install playwright && "
            "python3 -m playwright install chromium"
        ) from error

    profile_dir = profile_dir.expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    deadline = started_at + timeout

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 900},
        )
        closed_by_user = False
        rejected_tokens = set()
        try:
            if clear_auth_session:
                clear_playwright_auth_session(context)
            page = open_portal_page(context, PlaywrightError, fresh_page=fresh_page)
            if clear_existing_token:
                try:
                    if is_portal_url(page.url):
                        page.evaluate("localStorage.removeItem('DESKTOP_PARA_ATOKEN')")
                        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
                except PlaywrightError:
                    pass
            login_filled = False
            visible_window_seen = False
            zero_visible_window_samples = 0
            visible_close_grace_seconds = 20
            visible_close_unseen_grace_seconds = 45
            visible_close_required_samples = 3
            while time.monotonic() < deadline:
                token = read_playwright_token_from_pages(context, PlaywrightError)
                if token and token not in rejected_tokens and token_is_usable(token):
                    return token
                if token:
                    rejected_tokens.add(token)

                if not headless:
                    open_window_count = profile_browser_open_window_count(profile_dir)
                    if open_window_count is not None:
                        if open_window_count > 0:
                            visible_window_seen = True
                            zero_visible_window_samples = 0
                        elif time.monotonic() - started_at >= (
                            visible_close_grace_seconds
                            if visible_window_seen
                            else visible_close_unseen_grace_seconds
                        ):
                            zero_visible_window_samples += 1
                            if zero_visible_window_samples >= visible_close_required_samples:
                                closed_by_user = True
                                break
                        else:
                            zero_visible_window_samples = 0

                active_pages = active_playwright_pages(context, PlaywrightError)
                if active_pages is None:
                    time.sleep(1)
                    continue
                if not headless and not active_pages:
                    closed_by_user = True
                    break

                if page.is_closed():
                    if headless:
                        page = open_portal_page(context, PlaywrightError, fresh_page=fresh_page)
                    else:
                        page = active_pages[0]

                if not login_filled:
                    login_page = find_playwright_page(context, is_cas_url)
                    if login_page:
                        login_filled = fill_playwright_login(
                            login_page,
                            login_name,
                            login_password,
                            PlaywrightError,
                        )

                time.sleep(1)

            if closed_by_user and not headless:
                close_playwright_context(context, PlaywrightError)
                token = read_token_from_closed_playwright_profile(playwright, profile_dir, PlaywrightError)
                if token and token_is_usable(token):
                    return token
                if token:
                    raise TimeoutError(
                        "visible browser was closed but the captured token is still invalid; "
                        "wait for the HPC portal page to finish loading before closing the window"
                    )
                raise TimeoutError(
                    "visible browser was closed before a valid token was captured; "
                    "finish CAS login, wait for the HPC portal page to load, then close the window"
                )

            mode = "headless" if headless else "visible browser"
            raise TimeoutError(f"timed out waiting for token in {mode}; finish login and retry")
        finally:
            close_playwright_context(context, PlaywrightError)


def token_is_usable(token):
    try:
        return not token_validation_failed(validate_token(token, timeout=5))
    except Exception:
        return False


def active_playwright_pages(context, playwright_error):
    try:
        return [candidate for candidate in list(context.pages) if not candidate.is_closed()]
    except playwright_error:
        return None


def has_relevant_playwright_pages(pages):
    return any(is_portal_url(candidate.url) or is_cas_url(candidate.url) for candidate in pages)


def close_playwright_context(context, playwright_error):
    try:
        context.close()
    except playwright_error:
        pass


def read_token_from_closed_playwright_profile(playwright, profile_dir, playwright_error):
    """After the visible login window is closed, reopen the profile briefly to read persisted localStorage."""
    context = playwright.chromium.launch_persistent_context(
        str(profile_dir),
        headless=True,
        viewport={"width": 1280, "height": 900},
    )
    try:
        open_portal_page(context, playwright_error, fresh_page=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            token = read_playwright_token_from_pages(context, playwright_error)
            if token:
                return token
            time.sleep(0.5)
        return ""
    finally:
        close_playwright_context(context, playwright_error)


def open_portal_page(context, playwright_error, fresh_page=False):
    if fresh_page:
        page = context.new_page()
    else:
        page = next((candidate for candidate in context.pages if not candidate.is_closed()), None)
        if page is None:
            page = context.new_page()
    try:
        page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=30000)
    except playwright_error:
        pass
    return page


def is_portal_url(url):
    parsed = urllib.parse.urlparse(url)
    portal = urllib.parse.urlparse(PORTAL_ORIGIN)
    return parsed.scheme == portal.scheme and parsed.netloc == portal.netloc


def is_cas_url(url):
    parsed = urllib.parse.urlparse(url)
    cas = urllib.parse.urlparse(CAS_ORIGIN)
    return parsed.scheme == cas.scheme and parsed.netloc == cas.netloc


def find_playwright_page(context, predicate):
    for candidate in list(context.pages):
        if candidate.is_closed():
            continue
        if predicate(candidate.url):
            return candidate
    return None


def fill_playwright_login(page, login_name, login_password, playwright_error):
    if not login_name and not login_password:
        return False

    try:
        if login_name:
            page.locator("#id_loginname").fill(login_name, timeout=3000)
        if login_password:
            page.locator("#id_password").fill(login_password, timeout=3000)
        page.locator("#id_captcha_1").focus(timeout=3000)
        return True
    except playwright_error:
        return False


def read_playwright_token_from_pages(context, playwright_error):
    for candidate in list(context.pages):
        if candidate.is_closed() or not is_portal_url(candidate.url):
            continue
        try:
            token = candidate.evaluate("localStorage.getItem('DESKTOP_PARA_ATOKEN') || ''")
        except playwright_error:
            token = ""
        if token:
            return token.strip()
    return ""


def profile_browser_process_exists(profile_dir):
    """Best-effort visible Chromium liveness check for macOS/Linux Playwright profiles."""
    pids = profile_browser_process_pids(profile_dir)
    if pids is None:
        return None
    return bool(pids)


def profile_browser_process_pids(profile_dir):
    profile_text = str(profile_dir.expanduser())
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    pids = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, command = parts
        lower = line.lower()
        if profile_text in command and ("chromium" in lower or "chrome" in lower):
            pids.append(pid)
    return pids


def profile_browser_open_window_count(profile_dir):
    """Return visible macOS window count for the Playwright browser, or None if unavailable."""
    if sys.platform != "darwin":
        return None
    pids = profile_browser_process_pids(profile_dir)
    if not pids:
        return 0 if pids == [] else None
    script = """
on run argv
    set totalWindows to 0
    tell application "System Events"
        repeat with proc in application processes
            try
                set procPid to (unix id of proc as integer) as string
                if procPid is in argv then
                    set totalWindows to totalWindows + (count of windows of proc)
                end if
            end try
        end repeat
    end tell
    return totalWindows as string
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script, *pids],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    output = result.stdout.strip()
    if not output.isdigit():
        return None
    return int(output)


def write_token(path, token):
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(token.strip() + "\n")
    os.chmod(path, 0o600)


def validate_token(token, timeout):
    params = urllib.parse.urlencode({"atoken": token})
    url = f"https://hpc.bjtu.edu.cn/as/as/user/self?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"http_status": error.code, "raw": body}


def main():
    parser = argparse.ArgumentParser(
        description="Refresh BJTU HPC DESKTOP_PARA_ATOKEN from a logged-in browser tab."
    )
    parser.add_argument("--browser", choices=["playwright", "chrome", "safari"], default="playwright")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        help="Playwright profile directory. Defaults to the selected auth account profile when available.",
    )
    parser.add_argument("--headless", action="store_true", help="Use Playwright headless mode; requires an existing login session")
    parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for browser login/token")
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument(
        "--auth-account",
        default=os.getenv("HPC_AUTH_ACCOUNT"),
        help="Also sync the refreshed token into this saved hpc_accounts.py account; defaults to HPC_AUTH_ACCOUNT or the stored default account.",
    )
    parser.add_argument(
        "--no-sync-auth-account",
        action="store_true",
        help="Only update the legacy token file and do not update any saved auth account.",
    )
    parser.add_argument("--manual", action="store_true", help="Paste token manually without using browser automation")
    parser.add_argument("--fresh-page", action="store_true", help="Playwright mode: open a new portal page instead of reusing a restored tab")
    parser.add_argument(
        "--clear-existing-token",
        action="store_true",
        help="Playwright mode: remove DESKTOP_PARA_ATOKEN from the profile before waiting for a fresh portal token",
    )
    parser.add_argument(
        "--clear-auth-session",
        action="store_true",
        help="Playwright mode: clear cookies from the selected account profile before CAS login",
    )
    parser.add_argument(
        "--sync-legacy-token",
        action="store_true",
        help="Explicitly allow this refresh to overwrite the global legacy token file",
    )
    parser.add_argument("--no-open", action="store_true", help="Do not open the HPC portal before reading token")
    parser.add_argument("--skip-wait", action="store_true", help="Do not wait for Enter before reading token")
    parser.add_argument("--no-validate", action="store_true", help="Save token without calling the self-check endpoint")
    parser.add_argument(
        "--login-name",
        help="Pre-fill the CAS login name; defaults to the selected account's saved credential",
    )
    parser.add_argument(
        "--login-password-env",
        default=DEFAULT_LOGIN_PASSWORD_ENV,
        help="Environment variable containing the CAS password for Playwright pre-fill",
    )
    args = parser.parse_args()

    if args.manual:
        token = getpass.getpass("Paste DESKTOP_PARA_ATOKEN: ").strip()
    elif args.browser == "playwright":
        settings = playwright_account_settings(
            args.auth_account,
            args.login_name,
            args.login_password_env,
            args.profile_dir,
        )
        if args.headless:
            print("Running Playwright headless. This only works if the saved browser profile is already logged in.")
        else:
            print(
                "A Playwright Chromium window will open. Finish CAS login there, wait for "
                "the HPC portal page to load, then close the window; this script will "
                "continue automatically."
            )
            if settings["login_name"] or settings["login_password"]:
                print("If CAS login appears, the script will pre-fill configured fields; enter captcha and submit.")
        try:
            token = read_token_from_playwright(
                settings["profile_dir"],
                args.headless,
                args.timeout,
                settings["login_name"],
                settings["login_password"],
                fresh_page=args.fresh_page,
                clear_existing_token=args.clear_existing_token,
                clear_auth_session=args.clear_auth_session,
            )
        except Exception as error:
            print(f"[error] could not refresh token with Playwright: {error}", file=sys.stderr)
            print("If Chromium is missing, run: python3 -m playwright install chromium", file=sys.stderr)
            return 1
    else:
        if not args.no_open:
            webbrowser.open(PORTAL_URL)
        if not args.skip_wait:
            print("If the browser asks you to log in, finish login first, then press Enter here.")
            input()
        try:
            token = read_token(args.browser).strip()
        except Exception as error:
            print(f"[error] could not read token from {args.browser}: {error}", file=sys.stderr)
            print("Fallback: run this script with --manual and paste the token locally.", file=sys.stderr)
            return 1

    if not token:
        print("[error] token is empty. Make sure hpc.bjtu.edu.cn is open and logged in.", file=sys.stderr)
        return 1

    validation = None
    if not args.no_validate:
        validation = validate_token(token, timeout=10)
        if token_validation_failed(validation):
            print(f"[error] token self-check did not pass: {redact_token_text(validation)}", file=sys.stderr)
            return 1

    synced_account = None
    if not args.no_sync_auth_account:
        try:
            synced_account = sync_auth_account_token(
                token,
                validation,
                args.auth_account,
                strict=bool(args.auth_account),
            )
        except AccountStoreError as error:
            print(f"[error] could not sync auth account token: {error}", file=sys.stderr)
            return 1
        if synced_account:
            print(f"[ok] synced token to auth account {synced_account}")
    else:
        try:
            selected = resolve_sync_account_name(
                args.auth_account,
                strict=bool(args.auth_account),
            )
            if selected:
                _, entry = get_account(selected)
                verify_token_identity(selected, token, entry)
        except AccountStoreError as error:
            print(f"[error] token identity check failed: {error}", file=sys.stderr)
            return 1

    if should_write_token_file(
        args.auth_account,
        args.token_file,
        explicit_sync=args.sync_legacy_token,
    ):
        write_token(args.token_file, token)
        print(f"[ok] token saved to {args.token_file.expanduser()}")
    else:
        print(
            "[ok] skipped global legacy token sync because the refreshed auth account "
            "is not the stored default"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
