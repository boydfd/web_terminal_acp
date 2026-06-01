#!/usr/bin/env bash
set -euo pipefail

mode="${1:-release}"
repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
release_root="${ANDROID_RELEASE_ROOT:-$repo_root}"
signing_env="$release_root/.release/android-release-signing.env"
container="${ANDROID_BUILD_CONTAINER:-dev-sizao}"
host_workspace_root="${ANDROID_BUILD_HOST_WORKSPACE_ROOT:-$(cd "$repo_root/.." && pwd)}"
container_workspace_root="${ANDROID_BUILD_CONTAINER_WORKSPACE_ROOT:-/workspace}"
sdk_root="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/workspace/.cache/android-sdk}}"

to_container_path() {
  local host_path="$1"
  if [[ "$host_path" == "$host_workspace_root"/* ]]; then
    printf '%s/%s\n' "$container_workspace_root" "${host_path#"$host_workspace_root"/}"
  else
    printf '%s\n' "$host_path"
  fi
}

container_repo="${ANDROID_BUILD_CONTAINER_REPO:-$(to_container_path "$repo_root")}"
container_release_root="${ANDROID_BUILD_CONTAINER_RELEASE_ROOT:-$(to_container_path "$release_root")}"

case "$mode" in
  release|local-release|unsigned-release|debug) ;;
  *)
    echo "Usage: $0 {release|local-release|unsigned-release|debug}" >&2
    exit 2
    ;;
esac

if [[ "$mode" == "release" && ! -f "$signing_env" ]]; then
  echo "Missing $signing_env. Create or restore the local release keystore first." >&2
  exit 1
fi

run_local() {
  cd "$repo_root"
  if [[ ! -d frontend/node_modules ]]; then
    npm --prefix frontend ci --ignore-scripts
  fi
  if [[ "$mode" == "release" ]]; then
    export WEB_TERMINAL_ANDROID_RELEASE_STORE_FILE="$release_root/.release/web-terminal-acp-release.jks"
    # shellcheck disable=SC1090
    source "$signing_env"
  fi
  export ANDROID_HOME="${ANDROID_HOME:-$sdk_root}"
  export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$ANDROID_HOME}"
  export PATH="$ANDROID_SDK_ROOT/platform-tools:$ANDROID_SDK_ROOT/build-tools/36.0.0:$PATH"
  cd frontend
  case "$mode" in
    release) npm run android:build:release ;;
    local-release) npm run android:build:local-release ;;
    unsigned-release) npm run android:build:unsigned-release ;;
    debug) npm run android:build:debug ;;
  esac
}

run_container() {
  local container_cmd
  case "$mode" in
    release)
      container_cmd="export WEB_TERMINAL_ANDROID_RELEASE_STORE_FILE='$container_release_root/.release/web-terminal-acp-release.jks'; source '$container_release_root/.release/android-release-signing.env' && cd frontend && npm run android:build:release"
      ;;
    local-release)
      container_cmd='cd frontend && npm run android:build:local-release'
      ;;
    unsigned-release)
      container_cmd='cd frontend && npm run android:build:unsigned-release'
      ;;
    debug)
      container_cmd='cd frontend && npm run android:build:debug'
      ;;
  esac

  docker exec "$container" bash -lc \
    "set -euo pipefail; export ANDROID_HOME=/workspace/.cache/android-sdk ANDROID_SDK_ROOT=/workspace/.cache/android-sdk PATH=/workspace/.cache/android-sdk/platform-tools:/workspace/.cache/android-sdk/build-tools/36.0.0:\$PATH; cd '$container_repo'; if [[ ! -d frontend/node_modules ]]; then npm --prefix frontend ci --ignore-scripts; fi; $container_cmd"
}

verify_apk() {
  local apk_path="$1"
  if command -v apksigner >/dev/null 2>&1; then
    apksigner verify --verbose "$apk_path"
    return
  fi
  if docker inspect "$container" >/dev/null 2>&1; then
    docker exec "$container" bash -lc \
      "export PATH=/workspace/.cache/android-sdk/build-tools/36.0.0:\$PATH; cd '$container_repo'; apksigner verify --verbose '$apk_path'"
  else
    echo "apksigner not found; skipped APK signature verification." >&2
  fi
}

if command -v java >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 && [[ -d "$repo_root/frontend/node_modules" && -d "$sdk_root/build-tools" ]]; then
  run_local
else
  run_container
fi

case "$mode" in
  release)
    apk="frontend/android/app/build/outputs/apk/release/app-release.apk"
    ;;
  local-release)
    apk="frontend/android/app/build/outputs/apk/localRelease/app-localRelease.apk"
    ;;
  debug)
    apk="frontend/android/app/build/outputs/apk/debug/app-debug.apk"
    ;;
  unsigned-release)
    apk="frontend/android/app/build/outputs/apk/release/app-release-unsigned.apk"
    ;;
esac

cd "$repo_root"
if [[ -f "$apk" ]]; then
  ls -lh "$apk"
  if [[ "$mode" != "unsigned-release" ]]; then
    verify_apk "$apk"
  fi
else
  echo "Expected APK was not found: $apk" >&2
  exit 1
fi
