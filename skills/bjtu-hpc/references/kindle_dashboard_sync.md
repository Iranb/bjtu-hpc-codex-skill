# Kindle HPC Dashboard Sync

Read this file before creating, deploying, repairing, or validating a Kindle/e-ink mirror of the redacted BJTU HPC Widget snapshot.

## Contents

1. Authority and boundaries
2. Recommended architecture
3. Privacy contract
4. Implementation routing
5. Mac renderer and scheduler
6. SSH HTTPS edge
7. Kindle updater configuration
8. RTC validation
9. Verification matrix
10. Failure handling and rollback

## 1. Authority And Boundaries

- Treat the native Widget backend's redacted `snapshot.json` as the only input. Do not query the portal, saved account store, SSH proxy, cookies, CAS credentials, or tokens from the Kindle pipeline.
- Use the `bjtu-kindle-dashboard` repository implementation as the canonical renderer/updater source. Resolve a current local clone and verify that the required scripts exist before installation; do not recreate ad-hoc variants in user Library directories.
- Keep the native Widget read-only with respect to Slurm. This downstream mirror must never submit, cancel, reserve, chmod cluster data, or influence scheduler decisions.
- Start every edge-host session with read-only inspection. Check host identity, Python/OpenSSL/hash utilities, occupied ports, current listeners, sudo availability, user-service persistence, cron availability, and writable user roots before changing anything.
- Installing or changing the Mac LaunchAgent, edge service/cron, Kindle CA/config, or remote files requires an explicit user request. Do not infer authorization from a status or design question.
- Never use or depend on a Windows host. The normal data path is Mac -> SSH edge -> Kindle HTTPS.

## 2. Recommended Architecture

Keep the producer and consumer schedules decoupled:

```text
redacted native Widget snapshot.json
  -> Mac second-stage minimizer
  -> 1072 x 1448 grayscale PNG
  -> semantic SHA-256 change gate
  -> atomic SSH publication to an edge
  -> authenticated HTTPS with strong ETag
  -> Kindle charging screen-saver loop or battery RTC wake
  -> PNG/hash validation and atomic replacement
  -> unlock release or natural powerd deep re-suspend
```

Do not make Mac wait for Kindle SSH availability. Kindle sleeps, changes address, and may be offline; the edge absorbs that timing mismatch. If Mac, edge, or network is unavailable, Kindle retains its last known-good image.

## 3. Privacy Contract

The adapter must apply a second minimization boundary even though the Widget snapshot is already redacted:

- map accounts to fixed labels such as `ACCOUNT A` through `ACCOUNT F`;
- map node hostnames to fixed ordinals such as `GPU01` through `GPU04`;
- exclude source account names, hostnames, job ids, job names, portal identities, login details, guardian text, token fields, and raw errors;
- retain only GPU/CPU capacity, anonymous node capacity/state, anonymous account running/queued counts, and a coarse health/staleness indication;
- mark missing/invalid auth conservatively as `SIGN-IN` and old/error snapshots as `STALE`;
- publish only the PNG and, where needed, a content-only manifest containing dimensions, mode, size, and SHA-256.

Do not put addresses, SSH usernames, passwords, tokens, private keys, certificates containing environment-specific addresses, or raw logs in the repository, skill, documentation, PR, or final answer. Store local runtime state outside Git and keep private keys mode `0600`.

## 4. Implementation Routing

Expected repository components:

```text
scripts/sync_hpc_widget.py
scripts/run_hpc_kindle_sync.py
scripts/install_macos_hpc_sync.py
scripts/publish_kindle_live.py
scripts/publish_kindle_ssh.py
server/serve_kindle_panel.py
server/run_edge_server.sh
kindle/bjtu-dashboard-updater/
```

Choose publication only after a read-only reachability test:

1. If Kindle can reach GitHub HTTPS, use the GitHub Contents API raw media endpoint and a dedicated single-commit live branch.
2. If Kindle cannot reach GitHub but can reach an SSH-managed server, prefer the SSH HTTPS edge below.
3. Do not use direct Mac-to-Kindle SSH as the resident design.
4. Do not use plain HTTP in production, `curl --insecure`, a secret URL as access control, or a token embedded in a URL/plist.

The single-commit Git publisher must refuse credentials in remote URLs, refuse extra remote files or multi-commit history, validate the PNG before publishing, and use exact `--force-with-lease`. The SSH publisher must accept only a credential-free SSH Host alias and a safe path relative to remote HOME, verify byte count and SHA-256 remotely, then `mv` the incoming file atomically.

## 5. Mac Renderer And Scheduler

Render locally first with publication disabled. Verify the real snapshot produces a fresh anonymous image and that a second identical run does not rewrite it:

```bash
python3 scripts/run_hpc_kindle_sync.py \
  --runtime-dir "$HOME/Library/Application Support/BJTUKindleSync"
```

Required behavior:

- validate source structure and age before rendering;
- produce exactly 1072 x 1448, PNG, 8-bit grayscale (`L`);
- support `portrait` and clockwise physical `right` placement without rotating
  the Kindle framebuffer: compose `right` on a logical 1448 x 1072 canvas and
  pre-rotate it counter-clockwise into the native 1072 x 1448 PNG;
- include orientation in the semantic digest so a direction-only change forces
  a new render and publication;
- use a canonical semantic digest so timestamp-only snapshot changes do not rewrite the PNG;
- write JSON, image, and state through same-directory temporary files plus atomic replace;
- keep render success separate from publication success so a failed upload is retried even if the image does not change again;
- use a nonblocking file lock to prevent overlapping launchd runs.

Install the LaunchAgent only after endpoint selection. Use both `WatchPaths` on the snapshot and a 300-second `StartInterval` fallback. Copy the minimal runtime into Application Support with its own virtual environment so iCloud offloading of a development clone cannot break the service. Launch through `/usr/bin/env -i` and pass only a minimal `HOME`, `PATH`, locale, and Python setting; do not inherit unrelated GUI-session credentials.

For an SSH edge, use a credential-free alias from local SSH configuration:

```bash
python3 scripts/install_macos_hpc_sync.py --install \
  --ssh-target EDGE_ALIAS \
  --publish-both
```

The alias and environment-specific destination stay in the local plist/state only. Service stdout must emit safe result enums/hashes, not hostnames, addresses, account labels, source rows, or Git credentials.
In dual mode, one locked cycle must render and publish both native-size files:
`panel-base.png` for portrait and `panel-base-right.png` for clockwise placement.

## 6. SSH HTTPS Edge

When the edge has no root Web service or sudo, run the repository's narrow Python HTTPS server on a confirmed-free nonprivileged port under the SSH user's HOME. It must:

- serve only `/panel-base.png`, `/panel-base-right.png`, and `/healthz`;
- support GET/HEAD, TLS 1.2+, `Content-Type: image/png`, `Cache-Control: no-cache`, a strong SHA-256 ETag, and `If-None-Match` -> `304`;
- revalidate PNG signature, 1072 x 1448 dimensions, 8-bit grayscale color type, and maximum size on every panel open;
- avoid directory listings and omit client addresses/request headers from logs;
- read the image after atomic publication so clients see either the old complete file or the new complete file.

Use a dedicated private CA when no public DNS/certificate is available:

- keep the CA private key only on Mac, mode `0600`;
- generate the server private key on the edge, mode `0600`;
- sign an RSA/SHA-256 server certificate containing the exact endpoint identity in both CN and SAN;
- copy only the CA certificate to Kindle;
- never commit any key, environment-specific certificate, CSR, address, or generated plist.

If user-systemd lingering is unavailable but user cron is active, install one tagged cron watchdog that runs `run_edge_server.sh ensure` once per minute. Preserve unrelated crontab lines. Never stop or reuse an occupied port/process; select and verify a free user port. A validated deployment must survive SSH logout and be probed again after the watchdog has run.

Before touching Kindle, verify from Mac with the private CA:

- TLS certificate and endpoint identity validation succeeds without `--insecure`;
- first request is `200` with `image/png` and a nonempty ETag;
- a second request with `If-None-Match` is `304`;
- downloaded SHA-256 equals the Mac outbox image;
- edge health remains available after the initiating SSH session exits.

## 7. Kindle Updater Configuration

Keep the CA certificate and curl options root-owned, not USB-visible:

```text
/var/local/bjtu-dashboard/edge-ca.pem       # CA public certificate
/var/local/bjtu-dashboard/curl.conf        # mode 0600, contains cacert path
```

Keep only the non-secret HTTPS URL and bounded numeric policy in USB-visible `update.conf`. The privileged updater must parse it as strict whitelisted data; it must never `source` or `eval` it. Reject duplicates, unknown keys, shell syntax, embedded whitespace/quotes, noncanonical decimals, unsupported schemes, and out-of-range values.

Treat display orientation as strict data too. Accept only
`DISPLAY_ORIENTATION=portrait` or `DISPLAY_ORIENTATION=right`, atomically copy
the accepted value to root-private state, and require the published image to
match it. In `right` mode, the native sleep hook must skip its portrait-coordinate
time/date/battery overlay; the pre-rotated base image is authoritative. Do not
use `fbdepth` or rotate the framebuffer underneath the stock Kindle UI.
Keep separate `UPDATE_URL` and `UPDATE_URL_RIGHT` values. A mode switch must
briefly stop the updater to avoid a fetch-lock race, force one full fetch when
the stored orientation differs, validate and atomically replace the asset, then
restart scheduling. Repeated fetches in the same mode must use that mode's ETag.

Use this default timing profile for a permanently powered dashboard:

| Setting | Value | Purpose |
|---|---:|---|
| `CHARGING_INTERVAL_SECONDS` | 300 | ETag check while plugged in |
| `BATTERY_INTERVAL_SECONDS` | 3600 | RTC fallback after unplug |
| `CHARGING_KEEP_AWAKE` | 1 | keep locked screen-saver awake only while charging |
| `KEEP_AWAKE_GRACE_SECONDS` | 120 | bounded crash/unplug fail-safe |
| `KEEP_AWAKE_RENEW_SECONDS` | 30 | renew and detect unplug |
| `MIN_RTC_SECONDS` | 180 | shortest permitted real RTC test |
| `RTC_FINAL_DELAY_SECONDS` | 2 | allow other final-level listeners first |
| `WAKE_EARLY_TOLERANCE_SECONDS` | 60 | classify the observed early wake margin |
| `WIFI_CONNECT_TIMEOUT_SECONDS` | 45 | tolerate slow post-RTC association |
| `DOWNLOAD_TIMEOUT_SECONDS` | 30 | bounded HTTPS transfer |
| `NETWORK_WINDOW_TIMEOUT_SECONDS` | 60 | post-connect fetch deadline |

While charging and locked, renew `suspendGrace`; do not use `preventScreenSaver` or claim connected standby. On `outOfScreenSaver`, reset both `suspendGrace` and `deferSuspend` to zero immediately. When charging disappears, release on the next 30-second tick and let the one-hour RTC policy take over. If the daemon exits, the last 120-second grace must expire without a persistent override.

Preserve existing safe curl options when adding:

```text
cacert = "/var/local/bjtu-dashboard/edge-ca.pem"
```

Remove any `insecure` option. Leave `ALLOW_HTTP=0`. Restart the updater after changing its URL so the resident daemon reloads configuration.

Validate from Kindle before an RTC cycle:

1. TLS `200` using the root-owned curl config;
2. ETag extraction followed by `304`;
3. downloaded SHA-256 equals the Mac/edge image;
4. PNG is 1072 x 1448, bit depth 8, grayscale color type 0;
5. updater first fetch uses its `.incoming` path and atomic replacement;
6. second updater fetch reports not modified;
7. a failed download leaves the old asset hash unchanged.

Do not print the URL, address, SSH identity, curl config contents, certificate details, or updater raw logs in shared output.

## 8. Charging And RTC Validation

The permanently powered path is:

```text
screenSaver + isCharging=1
  -> renew suspendGrace=120 every 30 seconds
  -> keep Wi-Fi connected
  -> every 300 seconds perform HTTPS If-None-Match
  -> replace/render only if validated SHA-256 changes
  -> outOfScreenSaver releases immediately
  -> unplug releases within 30 seconds and restores RTC fallback
```

Validate at least two complete 300-second `304` cycles, confirm no image write or render, then ask the user to unlock and require a same-second release event plus zero grace. Do not simulate a power key. Unplug fallback and 12–24 hour behavior remain separate acceptance tests.

The resident service owns the normal sequence:

```text
final readyToSuspend level 1
  -> set one-shot rtcWakeup
  -> deep suspend
  -> wakeupFromSuspend near scheduled epoch
  -> next readyToSuspend network level
  -> abortSuspend while screensaver remains active
  -> wait for normal Wi-Fi recovery
  -> HTTPS fetch and optional render
  -> bounded window ends
  -> powerd naturally deep-suspends again
```

For a short real-device test:

1. restart the updater so current config is loaded;
2. write a short future `next-due` that still respects `MIN_RTC_SECONDS`;
3. ask the user to lock the device manually and keep it locked;
4. observe state/events with sanitized summaries;
5. verify the RTC request is armed only after the last level-1 listener phase;
6. verify planned wake classification, `abortSuspend`, Wi-Fi recovery, HTTPS result, and natural re-suspend;
7. ask the user to unlock only after the deep-suspend result is recorded.

The production package has no `control.sh test` action because it previously simulated `powerButton`. Never restore or replace it with another synthetic power-key path.

If the user unlocks during the background window, `outOfScreenSaver`/active state must cancel pending work. The fetcher must check active state again before atomic replacement/render, and the service must not force the device back to sleep.

## 9. Verification Matrix

Run and record only sanitized outcomes:

| Layer | Required evidence |
|---|---|
| Source | current redacted snapshot; no portal/token access from mirror |
| Adapter | account/node identifiers absent; stale/auth states conservative |
| Image | PNG, 1072 x 1448, 8-bit grayscale, bounded bytes, SHA-256 |
| Orientation | portrait/right enum; right preview upright at 1448 x 1072; native file remains 1072 x 1448; no portrait overlay |
| Idempotence | unchanged semantics do not rewrite; failed publish retries |
| SSH upload | byte count and SHA-256 checked before atomic remote move |
| Edge TLS | trusted CA, TLS 1.2+, identity verified, no insecure mode |
| HTTP cache | both routes return `200`, distinct strong ETags, then per-mode `304` |
| Kindle fetch | PNG contract, SHA-256, atomic replace, unchanged path |
| Failure | bad TLS/network/content keeps previous image |
| User activity | unlock cancels replacement/render and is never overridden |
| Charging power | screenSaver held with bounded grace, Wi-Fi connected, two ETag cycles, unlock release |
| Battery power | RTC wake, abortSuspend, Wi-Fi restore, no power-key simulation, deep re-suspend |
| Static QA | all Python tests, `py_compile`, every device/edge POSIX `sh -n`, diff check, secret/address scan |

## 10. Failure Handling And Rollback

- Missing or malformed snapshot: fail the current Mac cycle and retain outbox/edge/Kindle old image.
- Stale but structurally valid snapshot: publish a clearly marked `STALE` image if policy permits.
- SSH upload failure: do not record publication success; retry the same image next cycle.
- Edge TLS/service failure: leave the current edge image untouched and let Kindle back off.
- Kindle download/content failure: keep the existing asset and ETag state consistent.
- Mac sleep/offline: resume on the next WatchPaths or interval event; do not wake Kindle from Mac.
- Edge restart: rely on the tagged watchdog or approved system service; do not alter unrelated cron/services.
- Disable Mac publication by reinstalling the LaunchAgent without a destination or uninstalling it while preserving runtime state.
- Disable Kindle scheduling through its updater control without removing the last valid screensaver asset.
- Remove edge cron/service/files only with explicit deletion authorization. Preserve keys and last image by default for recovery/audit.

After any repair, repeat the smallest affected verification layer and then one complete RTC cycle. Treat a change of Kindle firmware, network, edge identity/port, CA, sleep hook, or updater script as requiring renewed end-to-end validation.
