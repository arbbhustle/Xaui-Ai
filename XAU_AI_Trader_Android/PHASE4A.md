# Phase 4A — Android presentation layer

## Completion and immutable base

Completed 2026-09-18 against Phase 3F commit `dbb75f55bf37557a97c1c168913c4612bdccb5e9`. No backend source, deployment, provider configuration, trading logic, or Champion/Challenger behavior changed. No staging or commit performed.

Preserved package `com.arbnor.xauai`, version 2.0 (2), min SDK 26, target/compile SDK 35, Java 17, Kotlin 2.0.21, Gradle 8.7 and AGP 8.6.1. Default endpoint remains `https://xau-ai-trader-android.onrender.com/signal`.

## Screens

- Home: typography launch identity, DEMO/research status, decision, confidence, observed price, freshness/readiness, trade blueprint, council, market/hidden state, explanations, macro/news and provider provenance.
- Signals: paginated backend history, supplied setup levels, role labels and closed demo outcomes.
- Analytics: separate Champion and Challenger cohorts, evidence counts, performance and regime/session breakdowns when supplied.
- Settings: brand signature, version, disclaimer, connectivity, last sync, readiness and HTTPS endpoint configuration.

Dark navy/black cards, gold accents and readable wrapping text. No invented logo, model bundle, notifications or broker controls.

## API compatibility

| Endpoint / fields | Presentation behavior |
| --- | --- |
| `/signal`: decision/direction, confidence, entry/price, sl, tp1, tp2, risk/reward, timestamps | Legacy parsing retained; absent/invalid values unavailable. NO_TRADE explains reasons instead of fabricating levels. |
| Champion and Challenger objects | Primary decision uses Champion; Challenger is separately labelled research. |
| Council, hidden state, DGFE, resilience, liquidity, entropy, event absorption | Render supplied indices/details only; no computed trading signals. |
| Regime, session, timeframe alignment, macro/news, event risk | Optional fields degrade gracefully. |
| Provider health, provenance, data mode, readiness | LIVE display requires fresh matching symbols and authentication/entitlement/provenance evidence; successful HTTP alone is insufficient. TEST/FIXTURE remains research. |
| `/performance` or embedded performance | Legacy Champion metrics supported; separate cohort metrics never fall back across roles. Gross R is not net R. Missing metrics are not zero. |
| `/history` array or items/history envelope | Up to 200 records, displayed in pages of 30. Unknown role is explicitly unknown. |
| `/trades` | Closed outcomes joined by decision ID with role isolation, without mutating source records. |

Missing optional endpoints produce warnings and retain a valid signal. Missing calibrated evidence is labelled unavailable/unverified or a heuristic index; it is not presented as an empirically calibrated probability. The immutable backend may omit newer presentation fields; the app does not synthesize them or add server endpoints.

## Verification

- Android `assembleDebug testDebugUnitTest lintDebug`: successful.
- Android: **56 passed, 0 failures/errors** (38 presentation, 10 API, 8 layout tests).
- Complete backend regression suite: **770 passed, 0 failures**; one existing Starlette/AnyIO deprecation warning.
- Combined: **826 passed, 0 failures**.
- Lint: **0 errors, 18 warnings**: 15 retained unused resources, existing dependency update suggestion, missing approved application icon, and legacy fullBackupContent recommendation. `allowBackup=false` is set; Android 12+ extraction rules exclude app data. No automatic dependency upgrade or invented icon added.
- Native Robolectric screenshots/layout checks: API 26 and 35, 360dp and S23-class 393dp widths, 1.5 font scale, long explanations and all four tabs. Final Home and Settings screenshots visually checked. Physical Samsung device testing remains outstanding.
- Debug APK: `app/build/outputs/apk/debug/app-debug.apk`, **3,469,103 bytes (3.31 MiB)**. Debug build only, not release signed or installed on a physical device.

Build with the existing Java 17 / Android SDK environment:

```powershell
gradle --no-daemon assembleDebug testDebugUnitTest lintDebug
# From repository root, using the existing backend environment:
python -B -m pytest backend/tests -q -p no:cacheprovider --tb=short
```

## Forensic fixes and security

- Rejected stale, future, malformed or missing timestamps for live/setup presentation; re-evaluate freshness while foregrounded.
- Withheld actionable blueprints for NO_TRADE, vetoes, expired decisions and disconnected snapshots.
- Prevented fixture, wrong-symbol and incomplete provider evidence from displaying LIVE.
- Separated heuristic confidence from historical demo calibration, gross/net R, recorded/eligible trades and unknown/measured-zero metrics.
- Isolated Champion/Challenger analytics and outcome joins; no cross-role fallback.
- Prevented old asynchronous responses from overwriting newer endpoint/session state; cancellation, bounded responses, read timeouts and JSON-depth limits added.
- HTTPS only; redirects and credential-bearing endpoint URLs rejected. Sanitized network errors, no sensitive debug logging, and sensitive detail keys filtered.
- Fixed low-contrast button text, overly long timestamp formatting and redundant custom splash behavior.
- Production-source review found no hardcoded API keys, provider credentials, private keys or sensitive logging. Negative URL tests use an intentionally invalid placeholder, not a real credential.

## Exact source/documentation change manifest

Paths below are relative to this Android directory. Six existing files modified:

1. `app/build.gradle.kts`
2. `app/src/main/AndroidManifest.xml`
3. `app/src/main/java/com/arbnor/xauai/MainActivity.kt`
4. `app/src/main/res/layout/activity_main.xml`
5. `app/src/main/res/values/colors.xml`
6. `app/src/main/res/values/styles.xml`

Nine files added:

7. `.gitignore`
8. `PHASE4A.md`
9. `app/src/main/java/com/arbnor/xauai/Presentation.kt`
10. `app/src/main/java/com/arbnor/xauai/ApiClient.kt`
11. `app/src/main/res/values-v31/styles.xml`
12. `app/src/main/res/xml/data_extraction_rules.xml`
13. `app/src/test/java/com/arbnor/xauai/PresentationTest.kt`
14. `app/src/test/java/com/arbnor/xauai/ApiClientTest.kt`
15. `app/src/test/java/com/arbnor/xauai/LayoutTest.kt`

The Android directory was already untracked before this phase. Generated build/test/screenshot/cache outputs are ignored and are not source changes. Both original ZIPs remain untouched.

## Remaining limits

No implementation/build blocker remains for Phase 4A. Physical-device acceptance and an approved release icon/signing process remain release tasks. Complete intelligence cards require the backend to expose the optional fields; this phase does not deploy that backend or validate real provider subscriptions. Real collection remains disabled and readiness remains NOT_READY pending approved providers. No profitability or predictive-edge claim is established. No Phase 4B work was started.

## Galaxy S23 feedback — compact legacy presentation

Follow-up polish implements the user's physical-device feedback without backend changes:

- Council rows show only supplied advanced components, including individually supplied buy/sell sides. An empty advanced council has one concise status message.
- Legacy buy/sell scores, RSI (`rsi` or `rsi_state`), ATR, efficiency, edge, structure, pressure, volatility and breakout bias are displayed when supplied. Regime (`regime` or `mode`), session and original decision reasons remain visible.
- Missing hidden-state/DGFE and macro/news sections use the requested compact status messages. Empty backend-detail rows are omitted. Provider fallback shows connectivity, readiness and safe data status.
- Unverified backend LIVE declarations are no longer echoed in the unverified badge. Insufficient-forward-evidence analytics withhold win-rate and NO_TRADE percentages while retaining supplied observation counts. Unlabelled history uses `DEMO history`, without inventing Champion identity.
- Branding, navy/gold styling, DEMO safety, endpoint and launcher remain unchanged. No approved launcher asset was found; no icon was invented.

Exact files modified in this follow-up: `Presentation.kt`, `MainActivity.kt` under `app/src/main/java/com/arbnor/xauai/`; `PresentationTest.kt`, `LayoutTest.kt` under `app/src/test/java/com/arbnor/xauai/`; and this document. No new source files. Generated APK, test/lint reports and test screenshots are build artifacts only.

Regression coverage adds compact-section rendering, legacy metrics and RSI alias, partial council fields, safe unverified status, insufficient-evidence percentages and retention of genuinely qualified measured zero rates. A Kotlin syntax error and UTF-8 separator encoding regression introduced during editing were corrected before final verification. Device feedback came from the user; follow-up layout verification is automated, not a new physical-device test.

Final follow-up verification: `assembleDebug testDebugUnitTest lintDebug` succeeded; **65 Android tests passed, 0 failures/errors** (45 presentation, 10 API, 10 layout). Lint remains **0 errors / 18 existing warnings** in the categories documented above. APK is **3,469,103 bytes (3.31 MiB)**. The backend suite was not rerun for this Android-only follow-up; backend tracked files remain unchanged. No staging, commit, deployment or Phase 4B work.

## Final S23 system-bar layout correction

The activity now explicitly owns edge-to-edge layout through `WindowCompat`, handles system-bar and display-cutout insets with `WindowInsetsCompat`, and requests inset delivery on attachment. Safe-area padding is replaced on every dispatch, never accumulated. Bottom padding uses the larger of the navigation-bar and keyboard insets. This replaces deprecated raw system-window inset handling and applies to the shared shell used by every tab.

The weighted scroll viewport remains a sibling above the fixed 64dp app navigation. Explicit scroll clipping prevents content painting outside that viewport; system navigation has its own safe area below the app navigation. Existing 20dp content padding remains inside the safe area.

Changed files in this correction: `app/src/main/java/com/arbnor/xauai/MainActivity.kt`, `app/src/main/res/layout/activity_main.xml`, `app/src/test/java/com/arbnor/xauai/LayoutTest.kt`, and this document. No presentation model, API, backend, branding or launcher changes. Compact unavailable sections, legacy metrics and provenance/evidence safeguards remain covered by the Android regression suite.

Validation uses synthetic status-bar/cutout/navigation/keyboard insets across all four tabs, repeated dispatch, and scrolling to the last item. Physical S23 acceptance must still be checked on the device; automated layout tests are not a claim of a new hardware test.

## Resumed final acceptance (2026-09-19)

The final missing-data policy omits optional unavailable rows across all four tabs, including history and settings. Empty analytics detail cards are omitted. `INSUFFICIENT_FORWARD_DATA` now shows `Not enough data` instead of any performance totals or percentages, including backend-supplied zero counts. This supersedes the earlier follow-up policy that retained zero observation counts in performance. Operational forward-evaluation counts remain separately labelled when actually supplied.

Missing history roles now read `Legacy record`. Legacy LIVE declarations in history are also rendered as `UNVERIFIED · provider provenance not validated`; they cannot create a verified live badge. Existing legacy metric mappings and compact advanced-section messages remain intact.

The API 26 safe-area test was corrected to expect status-bar insets rather than display-cutout insets, which that Android version does not support. Modern Android tests exercise camera-cutout, gesture navigation, three-button navigation and keyboard insets across Home, Signals, Analytics and Settings.

Exact files changed across the resumed final correction: `app/src/main/java/com/arbnor/xauai/MainActivity.kt`, `app/src/main/java/com/arbnor/xauai/Presentation.kt`, `app/src/main/res/layout/activity_main.xml`, `app/src/test/java/com/arbnor/xauai/LayoutTest.kt`, `app/src/test/java/com/arbnor/xauai/PresentationTest.kt`, and `PHASE4A.md`. No API client, dependency, backend, Render or launcher changes. Approved D/X + Kosovo/Dardania artwork is absent: adaptive launcher branding remains a release task, not a Phase 4A blocker. No bull or substitute icon added.

Final validation: `assembleDebug testDebugUnitTest lintDebug` **BUILD SUCCESSFUL**. **72 Android tests passed, 0 failures/errors**: 47 presentation, 15 layout, 10 API. All four-tab safe-area scenarios passed, including API 26 status bars, API 35 camera cutout, navigation modes and keyboard. Lint: **0 errors, 18 existing warnings** (15 unused resources, one legacy backup-rules recommendation, one dependency update suggestion, one missing application icon). Debug APK: **3,469,103 bytes / 3.31 MiB**. No new hardware test claimed.

Production Android review found no API keys, provider credentials or sensitive logging. HTTPS validation, redirect rejection and cleartext prohibition remain intact. No Phase 4A implementation blocker remains; the approved icon and physical confirmation of the rebuilt APK remain release acceptance tasks. Backend regression tests were not repeated because this correction changes only Android presentation/tests/documentation; tracked backend files remain unchanged. Nothing staged, committed or deployed; no provider collection, broker trading or Phase 4B work enabled.

## Approved launcher branding

The user supplied and approved `branding/dardania_xautrade_icon.png`. This resolves the previously missing-artwork blocker. Source SHA-256: `996EF4833F626038BB50D26A60C9893243F05DE7B9BDA0D2BCB41A64217CF6AF`. The source is unchanged; no artwork was redrawn, generated, recolored or cropped. The complete approved gold D/X, Kosovo identity and gold/blue artwork are uniformly scaled and centered with padding. A separate dark `#070A0F` background supports launcher masks. No bull or substitute icon is used.

Adaptive foreground: 108dp canvas with intact 48dp artwork. Standard/round bitmap fallbacks: 48dp canvas with 32dp artwork, matching the adaptive icon's central 72dp launcher viewport. Conservative padding preserves the artwork under circular masking. Android 12+ native splash uses the same adaptive icon. Screen layouts, logic, app label and Powered by ArbnorSylaj text are unchanged. No monochrome reinterpretation of the approved color artwork was invented.

Manifest references: `android:icon="@mipmap/ic_launcher"` and `android:roundIcon="@mipmap/ic_launcher_round"`. Launcher label remains `DardaniaXAUTRADE AI`.

Exact files added/modified for branding (relative to this Android project):

- Modified: `app/src/main/AndroidManifest.xml`, `app/src/main/res/values-v31/styles.xml`, `PHASE4A.md`.
- Added: `tools/Generate-LauncherIcons.ps1`, `app/src/test/java/com/arbnor/xauai/LauncherIconTest.kt`.
- Added: `app/src/main/res/drawable/ic_launcher_background.xml`.
- Added: `app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml`, `app/src/main/res/mipmap-anydpi-v26/ic_launcher_round.xml`.
- Added in **each** of `app/src/main/res/mipmap-mdpi`, `mipmap-hdpi`, `mipmap-xhdpi`, `mipmap-xxhdpi`, `mipmap-xxxhdpi`: `ic_launcher.png`, `ic_launcher_round.png`, `ic_launcher_foreground.png` (15 PNGs).

The reproducible PowerShell packaging script uses only the approved source and System.Drawing resampling/padding. Tests exercise adaptive resources on API 26/31/35, source-content bounds, app label, Android 12+ splash reference, and 144/192px launcher renders representative of S23-class sizing. Physical Samsung launcher acceptance still requires installing this APK; local renders do not claim a hardware installation.

Branding verification: **BUILD SUCCESSFUL; 76 Android tests passed, 0 failures/errors** (4 launcher, 15 layout, 47 presentation, 10 API). The package-manager test shadow's default drawable was replaced with direct loading of the manifest-selected resource in the test; compiled APK `aapt` inspection independently confirms both icon references and the unchanged label. Normal adaptive and round previews were visually checked. The package no longer relies on the default Android application icon.

APK: **3,656,471 bytes (3.49 MiB)**. Lint: **0 errors, 25 warnings**: 15 existing unused resources, one backup-rule recommendation, one dependency update suggestion, five square legacy bitmap shape recommendations, two optional monochrome-layer recommendations, one redundant v26 qualifier recommendation. The missing-application-icon warning is resolved. Monochrome artwork was not invented; the full-color approved identity remains authoritative. No warnings are hidden or suppressed.

Approved source hash remains unchanged. No screen implementation, backend, provider, notification, or trading changes; nothing staged, committed or deployed.

## Phase 4A acceptance and commit

The user has physically verified the approved launcher and current UI on Samsung Galaxy S23: the DardaniaXAUTRADE AI icon displays correctly and the default Android icon is gone. This supersedes the earlier pending-device-verification and missing-artwork notes. Phase 4A UI and launcher branding are approved.

The approved source PNG is intentionally included at repository path `branding/dardania_xautrade_icon.png` so the checked-in packaging script can reproduce the launcher resources. Only Android project source/resources/tests/build configuration/documentation and that approved asset are included. APKs, Gradle caches, local SDK paths, databases, credentials and generated test reports are excluded.

The repository-root legacy GitHub APK workflow still builds the original ZIP; it is unchanged by this Android source-only commit. Build this checked-in Phase 4A project directly with Gradle as documented above for the approved dashboard and branding. The nested original workflow is retained as project configuration, not an active repository-root workflow.

No backend or Render configuration changes, provider activation, broker trading or Phase 4B implementation are part of Phase 4A.

### Exact Phase 4A commit file manifest (48 files)

```text
XAU_AI_Trader_Android/.github/workflows/build-apk.yml
XAU_AI_Trader_Android/.gitignore
XAU_AI_Trader_Android/BUILD_APK_ANDROID.md
XAU_AI_Trader_Android/PHASE4A.md
XAU_AI_Trader_Android/README_ALBANIAN.md
XAU_AI_Trader_Android/UPDATE_V2_ALBANIAN.md
XAU_AI_Trader_Android/app/build.gradle.kts
XAU_AI_Trader_Android/app/src/main/AndroidManifest.xml
XAU_AI_Trader_Android/app/src/main/java/com/arbnor/xauai/ApiClient.kt
XAU_AI_Trader_Android/app/src/main/java/com/arbnor/xauai/MainActivity.kt
XAU_AI_Trader_Android/app/src/main/java/com/arbnor/xauai/Presentation.kt
XAU_AI_Trader_Android/app/src/main/res/drawable/button_bg.xml
XAU_AI_Trader_Android/app/src/main/res/drawable/card_bg.xml
XAU_AI_Trader_Android/app/src/main/res/drawable/card_glow.xml
XAU_AI_Trader_Android/app/src/main/res/drawable/chip_bg.xml
XAU_AI_Trader_Android/app/src/main/res/drawable/ic_launcher_background.xml
XAU_AI_Trader_Android/app/src/main/res/drawable/input_bg.xml
XAU_AI_Trader_Android/app/src/main/res/layout/activity_main.xml
XAU_AI_Trader_Android/app/src/main/res/mipmap-anydpi-v26/ic_launcher.xml
XAU_AI_Trader_Android/app/src/main/res/mipmap-anydpi-v26/ic_launcher_round.xml
XAU_AI_Trader_Android/app/src/main/res/mipmap-hdpi/ic_launcher.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-hdpi/ic_launcher_foreground.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-hdpi/ic_launcher_round.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-mdpi/ic_launcher.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-mdpi/ic_launcher_foreground.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-mdpi/ic_launcher_round.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xhdpi/ic_launcher.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xhdpi/ic_launcher_foreground.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xhdpi/ic_launcher_round.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xxhdpi/ic_launcher.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xxhdpi/ic_launcher_foreground.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xxhdpi/ic_launcher_round.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xxxhdpi/ic_launcher.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xxxhdpi/ic_launcher_foreground.png
XAU_AI_Trader_Android/app/src/main/res/mipmap-xxxhdpi/ic_launcher_round.png
XAU_AI_Trader_Android/app/src/main/res/values-v31/styles.xml
XAU_AI_Trader_Android/app/src/main/res/values/colors.xml
XAU_AI_Trader_Android/app/src/main/res/values/styles.xml
XAU_AI_Trader_Android/app/src/main/res/xml/data_extraction_rules.xml
XAU_AI_Trader_Android/app/src/test/java/com/arbnor/xauai/ApiClientTest.kt
XAU_AI_Trader_Android/app/src/test/java/com/arbnor/xauai/LauncherIconTest.kt
XAU_AI_Trader_Android/app/src/test/java/com/arbnor/xauai/LayoutTest.kt
XAU_AI_Trader_Android/app/src/test/java/com/arbnor/xauai/PresentationTest.kt
XAU_AI_Trader_Android/build.gradle.kts
XAU_AI_Trader_Android/gradle.properties
XAU_AI_Trader_Android/settings.gradle.kts
XAU_AI_Trader_Android/tools/Generate-LauncherIcons.ps1
branding/dardania_xautrade_icon.png
```


Pre-commit final verification: full Gradle rerun (54 tasks executed), debug build successful; 76 tests passed, 0 failed/errors; lint 0 errors and 25 documented warnings. User-confirmed physical S23 approval stands. Secret and artifact staging audit passed. Backend/Render unchanged; no Phase 4B work.

