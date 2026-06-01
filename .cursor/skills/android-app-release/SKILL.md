---
name: android-app-release
description: Build and verify Web Terminal ACP Android APKs. Use when the user asks to package, sign, release, verify, or troubleshoot the Android app/APK, including local release, signed release, unsigned release for external signing, or debug APK builds.
---

# Android App Release

## Workflow

Run all commands from the repository root. Prefer the Makefile targets; they use the project script and fall back to the `dev-sizao` container when the host lacks Java or Android SDK tools.

For a signed release APK:

```bash
make android-release
```

For a release-mode APK installable with the debug key:

```bash
make android-local-release
```

For a debug APK:

```bash
make android-debug
```

For an unsigned release APK intended for a separate signing pipeline:

```bash
make android-unsigned-release
```

## Release Signing

Signed release builds require `.release/android-release-signing.env` and `.release/web-terminal-acp-release.jks` in the repo root. These files are intentionally ignored by git.

Do not print release passwords in chat or logs. If you need to inspect the env file, redact values matching `PASSWORD=`.

If `.release/android-release-signing.env` is missing, stop and ask whether to restore the existing keystore or generate a new one. A new keystore changes the app signing identity and can break in-place upgrades for users who installed a previous release signed with a different key.

When running from a linked worktree but the local `.release/` directory exists only in the main checkout, set:

```bash
ANDROID_RELEASE_ROOT=/path/to/main/checkout make android-release
```

The script maps the host `develops/` directory to `/workspace` inside `dev-sizao`. Override `ANDROID_BUILD_HOST_WORKSPACE_ROOT` and `ANDROID_BUILD_CONTAINER_WORKSPACE_ROOT` only if the container mount changes.

## Expected Outputs

- Signed release: `frontend/android/app/build/outputs/apk/release/app-release.apk`
- Local release: `frontend/android/app/build/outputs/apk/localRelease/app-localRelease.apk`
- Debug: `frontend/android/app/build/outputs/apk/debug/app-debug.apk`
- Unsigned release: `frontend/android/app/build/outputs/apk/release/app-release-unsigned.apk`

For signed/debug/local builds, the script runs `apksigner verify --verbose` after packaging. For signed release builds, compare the APK certificate digest with `.release/web-terminal-acp-release.fingerprint.txt` when the signing key identity matters.

## Troubleshooting

- Missing `tsc`, `vite`, or Capacitor packages: the script installs frontend dependencies with `npm --prefix frontend ci --ignore-scripts` before packaging.
- Missing `cordova.variables.gradle`: run through the Makefile target or `npm run android:sync` before invoking Gradle directly.
- `assembleRelease requires release signing credentials`: source `.release/android-release-signing.env` or use `make android-local-release` for installable local validation.
- Host has no `java`, `apksigner`, or Android SDK: use the Makefile target; it executes inside `dev-sizao` if available.
