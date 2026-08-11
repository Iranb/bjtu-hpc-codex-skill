# Auth, Dashboard, And Proxy

Read this file for token refresh, saved accounts, CAS credential handling, the local Web dashboard, Token Guardian, macOS widget token actions, LaunchAgent service management, and portal SSH/SFTP proxy setup.

1. Refresh or save the portal token if `~/.bjtu_hpc_token` is missing or stale:
   ```bash
   python3 hpc_refresh_token.py --browser playwright --headless
   ```
   Use `--browser playwright` without `--headless` if a visible browser is needed. Set `HPC_LOGIN_PASSWORD` only when pre-filling CAS login fields for a one-off refresh.

   If the user explicitly asks for a "captcha/verification-code only" login flow, save CAS login credentials with the local helper below. This stores only on the controller machine in `~/.bjtu_hpc_credentials.json` with file mode `0600`; never place passwords in skill files, AGENTS files, Git-tracked files, logs, or final answers.
   ```bash
   python3 hpc_credentials.py set NAME --login-name PORTAL_USER
   python3 hpc_credentials.py list
   ```
   After this, `hpc_accounts.py refresh NAME --browser playwright` and `hpc_refresh_flow.py NAME --visible-only` will pre-fill the CAS username/password when the login form appears. The user should only enter the captcha/verification code, submit, wait for the HPC portal page to load, and close the Playwright window.

   Token refresh is not an experiment launch. If a user-requested BJTU status/progress check is blocked by `11009`, `11011`, `11012`, or HTTP `401`, immediately run the visible integrated refresh flow; do not ask whether to open Playwright. A "do not launch/start new experiments" request does not block token refresh. Only skip opening Playwright when the user explicitly says not to refresh the token, not to open a browser, or to rely on last-trusted status only.

   For multiple portal accounts, use `hpc_accounts.py` as the source of truth instead of the legacy token file:
   ```bash
   python3 hpc_accounts.py list
   python3 hpc_credentials.py list
   python3 hpc_accounts.py add NAME --refresh --browser playwright --fresh-page --timeout 600
   python3 hpc_accounts.py refresh NAME --browser playwright --headless --fresh-page
   python3 hpc_accounts.py refresh NAME --browser playwright --headless --fresh-page --clear-existing-token
   python3 hpc_accounts.py refresh NAME --browser playwright --fresh-page --clear-existing-token --clear-auth-session
   python3 hpc_accounts.py use NAME
   ```
   To move aliases to another computer, read `environment_setup.md` and use `hpc_accounts.py export-json` / `import-json`. Metadata-only export is the default. Tokens and CAS passwords require explicit `--include-tokens` / `--include-credentials`; secret-bearing exports are automatically wrapped in a passphrase-protected scrypt + AES-256-GCM JSON envelope and remain mode `0600`. Enter the passphrase interactively during import, transfer the file and passphrase separately, and delete the file after import. Browser profiles and cookies are never exported.
   Every named refresh must verify both the actual portal user and the bound `cluster:account` before saving a token. An identity mismatch must leave the saved token, alias metadata, and legacy token untouched. Refresh may fill missing identity fields after verification, but it must not rewrite established `portal_user`, `cluster`, or cluster OS `account` fields. Use `--clear-auth-session` together with a visible account-switch/recovery login so the alias-specific persistent profile cannot silently reuse another CAS identity. Headless Guardian warm-up may retain the session, but the same fail-closed identity check still applies.

   Adding or refreshing an account auto-discovers missing `portal_user`, `cluster`, and cluster OS `account` from the verified portal token when possible, so do not copy defaults from another account unless the user explicitly provides them. Normal refresh of a secondary account must not write `~/.bjtu_hpc_token`. Only the stored default account is automatically mirrored to the legacy cache by the integrated refresh flow/Guardian. Use `hpc_accounts.py use NAME` for an intentional default and legacy-cache switch; reserve `--sync-legacy-token` for an explicit compatibility override.

   For actual token renewal, prefer `--clear-existing-token` on Playwright refreshes. Without it, a saved profile may simply return the old usable `DESKTOP_PARA_ATOKEN` from portal localStorage and re-save it, which validates the token but does not prove a new OAuth/CAS token was issued. `--clear-existing-token` removes only the portal localStorage token from that account profile before waiting for a new token; it does not remove saved account tokens, CAS credentials, or account-store entries. `--clear-auth-session` clears cookies only inside the selected alias profile and is appropriate for visible login, not routine headless warm-up.

   If a visible Playwright login window is opened, tell the user to finish CAS login, wait for the HPC portal page to load, and close the window. If the command appears stuck after the window is closed, exits with a visible-browser timeout, or the user says the browser windows were closed, first try reading the same profile headlessly:
   ```bash
   python3 hpc_accounts.py refresh NAME --browser playwright --headless --fresh-page --timeout 30
   python3 hpc_accounts.py validate NAME
   ```
   Do not start a second visible login before probing the account profile; the login may already have persisted a usable token even when the visible helper timed out.

   If token validation returns `11009`, `11011`, `11012`, HTTP `401`, or an auth transport error, the next action is always `hpc_refresh_flow.py NAME --visible-only` in a PTY. If the user says they can help refresh a token, do not ask again. Keep the command running while the user enters the captcha/verification code in the Playwright window. For multi-account checks, refresh each affected saved account unless the user scopes the request to one account.

2. For a local GUI, run the Web dashboard:
   ```bash
   python3 hpc_transfer_web.py
   ```
   Open `http://127.0.0.1:8765/`. Token refresh and manual token save require choosing an explicit saved auth account. The dashboard uses that account's isolated Playwright profile and saved CAS credential; visible refresh clears that profile's CAS cookies, then verifies the returned portal identity before saving. A secondary account never overwrites the legacy token cache. It polls `/api/state` every 10 seconds and avoids overlapping refresh requests.

   Use the `Saved CAS Login` panel to save or delete local login credentials for captcha/verification-code-only refreshes. The panel writes through the same `hpc_account_store` helper used by `hpc_credentials.py`, stores credentials only on the controller machine in `~/.bjtu_hpc_credentials.json`, and keeps the file mode at `0600`. The dashboard must never display saved passwords; it only shows whether a password is present.

   Use the `Token Guardian` panel to keep saved account tokens usable after an initial visible CAS login. The guardian's default policy is validate-only: it checks token health and verifies the token's real portal/bound-account identity on every cycle. It does not force a new token while the current token still validates and matches the selected alias. The current token-age warning default is 5 days, so `token age` is a pre-expiry maintenance signal rather than a 24-hour freshness check. Headless Playwright without `--clear-existing-token` is used only as a low-frequency warm-up when the token age reaches the warning threshold or when explicitly forced; if that warm-up cannot complete because CAS needs captcha, but the saved token still validates, this is not a visible-login failure and should not be treated as a strong alert. Headless recovery with `--clear-existing-token` is reserved for genuinely invalid tokens. The guardian syncs only the stored default account back to the legacy token file. Its optional visible fallback clears the selected alias profile's cookies before CAS login. The guardian must not display token values, passwords, or certificate tokens; it only records status summaries and redacted errors in `hpc_token_guardian.jsonl`.

   In the macOS desktop widget, a purple key/account treatment means token attention is needed. Activate that account to open a visible Playwright login window for that account only. The `刷新 Token` header control explicitly starts the same visible-login refresh flow for every saved account; each alias keeps its isolated Playwright profile and must pass identity verification before its token is saved. Keep this treatment aligned with `references/apple_native_widget.md`.

   To keep the dashboard and Token Guardian running across terminal exits and user login sessions on macOS, install the per-user LaunchAgent:
   ```bash
   <PYTHON3> hpc_dashboard_service.py install
   <PYTHON3> hpc_dashboard_service.py status
   ```
   The default service label is `com.example.bjtu-hpc-dashboard`, the plist is `~/Library/LaunchAgents/com.example.bjtu-hpc-dashboard.plist`, stdout is `/tmp/bjtu_hpc_transfer_web.out.log`, stderr is `/tmp/bjtu_hpc_transfer_web.err.log`, and the service starts `hpc_transfer_web.py --token-guardian --guardian-accounts all` on `127.0.0.1:8765`. Use `hpc_dashboard_service.py stop`, `start`, `restart`, or `uninstall` to manage it. Prefer a LaunchAgent over a system LaunchDaemon so Playwright profiles, CAS cookies, and `~/.bjtu_hpc_*` files are accessed as the same macOS user.

3. Get SSH/SFTP proxy details when you need interactive access:
   ```bash
   python3 hpc_winscp_info.py
   ```
   Use the returned proxy host/port and temporary certificate token. Do not expect local SSH keys to work through the portal proxy.
