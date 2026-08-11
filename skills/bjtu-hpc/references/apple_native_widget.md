# Apple-Native BJTU HPC Widget

Read this file before changing the macOS WidgetKit component, its host app, or the snapshot-to-view contract.

## Mandatory Source Resolution

The public repository ships a sanitized component-lock template. Configure its
placeholder paths and bundle identifiers in the private installed skill before
running the resolver; do not commit the resolved controller paths.

Never infer the current UI source from a directory name, previous task history, source recency, or visual similarity. Before reading candidate UI implementation files or making changes, run:

```bash
python3 <BJTU_HPC_SKILL_DIR>/scripts/resolve_active_widget.py
```

Proceed only when the result contains `"status": "ok"`. Use its `selected_source_root` and `ui_source_files` as the only editable UI source for that task. Include the registered extension path/version and selected source root in the pre-edit audit. Retain this audit through build and deployment; source version plists may intentionally advance after editing, so do not rerun source selection and switch roots merely because edited source no longer matches the deployed version.

The resolver compares the live `pluginkit` registration, installed host and extension plists, source plists, required source markers, and `references/apple_native_widget_component_lock.json`. If any check fails:

- Do not edit or build a candidate UI.
- Inspect the live registration and installed bundle first.
- Map the installed binary/version to one source tree using bundle ids, versions, file layout, and source markers.
- Update the component lock only after that mapping is verified. A source tree must not become current merely because it can build.

The current source generation uses `HPC/HPCWidget.swift` and `HPC/HPCApp.swift` under the resolver-selected Apple-native widgets root. `<SLURM_DIR>/mac_hpc_monitor/native_widget/Sources/Widget/` is legacy UI and must not be selected for current UI changes. The same legacy tree may still be authoritative for shared backend files such as `hpc_native_widget_snapshot.py`; backend use does not imply UI ownership.

## Boundaries

- Keep the widget display-only for Slurm and GPU state. It may open the local dashboard or request the existing Token Guardian visible-login endpoint, but it must never submit, cancel, reserve, delete, or mutate HPC jobs.
- Decode only the redacted snapshot written by `<CONTROLLER_HOME>/Library/BJTUHPCNativeWidget/hpc_native_widget_snapshot.py` into the extension container. Never read account-store files, tokens, cookies, passwords, private trace ledgers, or raw portal responses from the extension.
- Preserve anonymous Slurm job names and redacted account aliases. Do not add usernames, portal identities, emails, or reversible trace mappings to the UI.
- Treat missing or malformed data as an informative empty/error state. Do not replace the last snapshot with fabricated live state.

## Runtime Contract

- Component lock: `<BJTU_HPC_SKILL_DIR>/references/apple_native_widget_component_lock.json`
- Host app: `<CONTROLLER_HOME>/Applications/BJTU HPC Native Widget.app`
- Widget extension bundle id: `com.example.bjtu-hpc-native-widget.widget`
- Snapshot writer: `<CONTROLLER_HOME>/Library/BJTUHPCNativeWidget/hpc_native_widget_snapshot.py`
- Local queue runtime: `<CONTROLLER_HOME>/Library/BJTUHPCNativeWidget/slurm_runtime`
- Snapshot: `~/Library/Containers/com.example.bjtu-hpc-native-widget.widget/Data/Library/Application Support/BJTUHPCNativeWidget/snapshot.json`
- Dashboard deep link: `bjtu-hpc-widget://dashboard`
- Token-login deep link: `bjtu-hpc-widget://token?account=<redacted-alias>`
- Refresh-all deep link: `bjtu-hpc-widget://refresh-all-tokens`
- Reload deep link: `bjtu-hpc-widget://reload`

Keep these identifiers stable so existing desktop placements, LaunchAgents, and deep links continue working.

Run the persistent snapshot LaunchAgent only from the local runtime. Do not execute its per-minute queue helper from the iCloud-backed `slurm` source tree: File Provider may expose source modules as `dataless`, which can yield an empty Python program and overwrite the widget with a zero-valued snapshot. Keep local copies of `hpc_queue_summary.py`, `hpc_winscp_info.py`, `hpc_upload.py`, and `hpc_account_store.py` byte-identical to their verified source versions. Disable resource-history recording for the widget poller by default; it is independent of the live redacted snapshot and its large iCloud history file must not block display updates.

Retry malformed or empty queue-helper output before publishing. If all retries fail, preserve the previous valid redacted payload, mark it stale/error, and never replace it with fabricated zeros.

## Information Hierarchy

Prioritize what can be understood in one glance:

1. Free GPU count and total GPU capacity.
2. Per-node GPU availability.
3. Free/total CPU capacity and connected-account count.
4. Account RUN/PD state and token attention.
5. Detailed pending reasons only in the dashboard; do not crowd the widget with scheduler jargon.

Use family-specific layouts instead of scaling one canvas:

- Small: status header, GPU availability ring, compact CPU capacity.
- Medium: GPU availability plus four node rows.
- Large: GPU/CPU summary, node rows, and compact account queue rows.

## Apple-Native Visual Rules

- Use San Francisco through SwiftUI semantic text styles and use SF Symbols for servers, CPU, people, clocks, keys, warnings, and success.
- Use `primary`, `secondary`, and semantic system colors. Reserve green for available/running/healthy, orange for pending or near-capacity, purple for token attention, and red for errors.
- Never rely on color alone; pair every status color with a count, label, icon, or state text.
- Use `containerBackground(for: .widget)` so WidgetKit can adapt the surface for full-color and vibrant macOS desktop appearances. Avoid baked-in wallpaper colors, glossy gradients, and custom glass simulations.
- Prefer spacing and typographic weight over boxes and borders. Use a low-emphasis `Divider` only between major groups.
- Use rounded, monospaced digits for changing counts so resource updates do not shift nearby content.
- Keep content calm and dense. Timeline refreshes should not animate; WidgetKit already manages state replacement. Honor system reduced-motion, transparency, contrast, and tint behavior by using semantic SwiftUI primitives.
- Make every label legible when the system removes or alters the widget background. Test light, dark, increased-contrast, reduced-transparency, full-color, and vibrant contexts when available.

## Interaction Rules

- A tap on the widget may open the local dashboard.
- A token-attention control may open the existing visible-login flow only for the selected redacted alias.
- The labeled `刷新 Token` control may explicitly request that existing visible-login flow for every saved account. It must not read token or account-store data from the extension.
- Use SF Symbol hit targets and direct labels; do not invent hidden gestures, hover-only actions, or looping attention animations.
- Keep feedback immediate and native. Avoid decorative motion because the widget is checked frequently and should feel quiet.

## Build And Deploy Checklist

1. Require a successful pre-edit source-selection audit for this change.
2. Compile only that audit's resolver-selected host app and extension for the current Mac architecture with the installed macOS SDK.
3. Keep bundle ids, URL schemes, snapshot paths, and widget kind stable.
4. Ad-hoc sign the extension, then deep-sign and verify the host app.
5. Render deterministic small/medium/large previews from both representative and current redacted snapshots.
6. Inspect medium and large previews in light and dark appearances for truncation, alignment, contrast, and tabular-number stability.
7. Decode the live snapshot without contacting BJTU HPC.
8. Replace the app atomically, register the extension with `pluginkit`, invoke the reload URL, and confirm `pluginkit -m -v -i com.example.bjtu-hpc-native-widget.widget` reports exactly one enabled entry at the intended version and installed path.
9. Update the component lock to the verified deployed version/source and rerun the resolver.
10. Unregister temporary same-bundle-id build products and confirm no duplicate registration remains.
11. Do not restart, submit, cancel, or modify any HPC job while deploying UI-only changes.
