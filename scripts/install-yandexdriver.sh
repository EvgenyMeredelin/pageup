#!/usr/bin/env bash
# install-yandexdriver.sh — install YandexDriver for personal-device mode
#
# Yandex Browser is not compatible with stock ChromeDriver or Selenium Manager.
# Download the matching YandexDriver release from:
#   https://github.com/yandex/YandexDriver/releases
#
# The driver's first three version components must match Yandex Browser (e.g.
# browser 26.4.1.x → driver 26.4.1).  This script reads the installed browser
# version and installs the driver to ~/.local/bin/yandexdriver by default
# (override with INSTALL_PATH or YANDEX_DRIVER in src/pageup/config.py).
#
# GitHub API: unauthenticated curl is rate-limited (403).  This script prefers
# `gh api` when logged in, else curl with GITHUB_TOKEN or `gh auth token`.

set -euo pipefail

# ── Paths and temp workspace ──────────────────────────────────────────────────
# INSTALL_PATH must match YANDEX_DRIVER in src/pageup/config.py (default below).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
INSTALL_PATH="${INSTALL_PATH:-${HOME}/.local/bin/yandexdriver}"
TMP_DIR="$(mktemp -d)"

cleanup() {
    rm -rf "$TMP_DIR"
}
# Remove downloaded zip and extracted driver on exit (success or failure).
trap cleanup EXIT

# ── Preconditions ─────────────────────────────────────────────────────────────
if ! command -v yandex-browser &>/dev/null; then
    echo "ERROR: yandex-browser not found in PATH." >&2
    echo "Install Yandex Browser first, then re-run this script." >&2
    exit 1
fi

if ! command -v curl &>/dev/null; then
    echo "ERROR: curl is required." >&2
    exit 1
fi

if ! command -v unzip &>/dev/null; then
    echo "ERROR: unzip is required." >&2
    exit 1
fi

# ── Match browser version to YandexDriver release tag ───────────────────────
# e.g. "Yandex 26.4.1.1101 stable" → 26.4.1 (YandexDriver tags: v26.4.1-stable)
BROWSER_VERSION_LINE="$(yandex-browser --version 2>/dev/null | head -1)"
VERSION_PREFIX="$(echo "$BROWSER_VERSION_LINE" | sed -n 's/.* \([0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')"
if [[ -z "$VERSION_PREFIX" ]]; then
    echo "ERROR: could not parse Yandex Browser version from: $BROWSER_VERSION_LINE" >&2
    exit 1
fi

TAG="v${VERSION_PREFIX}-stable"
RELEASE_API="https://api.github.com/repos/yandex/YandexDriver/releases/tags/${TAG}"
RELEASES_API="https://api.github.com/repos/yandex/YandexDriver/releases"

echo "Yandex Browser: $BROWSER_VERSION_LINE"
echo "Looking for YandexDriver release tag: $TAG"

# ── GitHub API helpers ────────────────────────────────────────────────────────
# GitHub rejects anonymous API calls (403 / rate limit).  Prefer gh + auth token.
github_api_get() {
    local url="$1"
    local gh_path="${url#https://api.github.com/}"

    if command -v gh &>/dev/null && gh auth status &>/dev/null 2>&1; then
        if gh api "$gh_path" 2>/dev/null; then
            return 0
        fi
    fi

    local token="${GITHUB_TOKEN:-}"
    if [[ -z "$token" ]] && command -v gh &>/dev/null; then
        token="$(gh auth token 2>/dev/null || true)"
    fi

    local curl_args=(
        -fsSL
        -H "Accept: application/vnd.github+json"
        -H "User-Agent: pageup-install-yandexdriver"
    )
    if [[ -n "$token" ]]; then
        curl_args+=(-H "Authorization: Bearer ${token}")
    fi

    curl "${curl_args[@]}" "$url"
}

github_api_error_hint() {
    echo "ERROR: could not query GitHub API (rate limit or missing auth)." >&2
    echo "Run: gh auth login" >&2
    echo "Or set GITHUB_TOKEN, then re-run this script." >&2
    echo "Manual install: https://github.com/yandex/YandexDriver/releases/tag/${TAG}" >&2
}

# Parse a single-release JSON document and print the linux zip download URL.
parse_release_linux_asset_url() {
    python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
for asset in data.get('assets', []):
    name = asset.get('name', '')
    if name.endswith('-linux.zip'):
        print(asset['browser_download_url'])
        break
"
}

# Parse releases list JSON; pick first linux zip for matching major.minor prefix.
parse_releases_list_linux_asset_url() {
    python3 -c "
import json, sys
prefix = sys.argv[1].rsplit('.', 1)[0] + '.'
try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit(1)
for release in data:
    tag = release.get('tag_name', '')
    if tag.startswith('v' + prefix):
        for asset in release.get('assets', []):
            if asset.get('name', '').endswith('-linux.zip'):
                print(asset['browser_download_url'])
                raise SystemExit
sys.exit(1)
" "$VERSION_PREFIX"
}

ASSET_URL=""
if release_json="$(github_api_get "$RELEASE_API" 2>/dev/null)"; then
    ASSET_URL="$(printf '%s' "$release_json" | parse_release_linux_asset_url || true)"
fi

if [[ -z "$ASSET_URL" ]]; then
    # Exact tag missing (e.g. patch lag) — scan all releases for same major.minor.
    # Yandex may ship browser builds before the matching -stable driver tag exists.
    echo "Release $TAG not found; searching latest matching ${VERSION_PREFIX%.*}.* ..."
    if releases_json="$(github_api_get "$RELEASES_API" 2>/dev/null)"; then
        ASSET_URL="$(printf '%s' "$releases_json" | parse_releases_list_linux_asset_url || true)"
    fi
fi

if [[ -z "$ASSET_URL" ]]; then
    github_api_error_hint
    exit 1
fi

# ── Download, extract, and install the driver binary ────────────────────────
# Asset URL comes from GitHub releases API — browser_download_url is a direct zip link.
ZIP_PATH="$TMP_DIR/yandexdriver.zip"
echo "Downloading: $ASSET_URL"
curl -fsSL -o "$ZIP_PATH" "$ASSET_URL"
unzip -q -o "$ZIP_PATH" -d "$TMP_DIR"

# Zip layout varies by release; search shallow tree for the executable name.
DRIVER_BIN="$(find "$TMP_DIR" -maxdepth 2 -type f -name 'yandexdriver' | head -1)"
if [[ -z "$DRIVER_BIN" ]]; then
    echo "ERROR: yandexdriver binary not found inside zip." >&2
    exit 1
fi

chmod +x "$DRIVER_BIN"
mkdir -p "$(dirname "$INSTALL_PATH")"
echo "Installing to: $INSTALL_PATH"
# Prefer user-writable ~/.local/bin (no sudo); fall back for system paths.
if [[ -w "$(dirname "$INSTALL_PATH")" ]]; then
    install -m 755 "$DRIVER_BIN" "$INSTALL_PATH"
else
    sudo install -m 755 "$DRIVER_BIN" "$INSTALL_PATH"
fi

echo "Installed: $("$INSTALL_PATH" --version 2>/dev/null || echo "$INSTALL_PATH")"
echo "YANDEX_DRIVER in $REPO_ROOT/src/pageup/config.py should point to: $INSTALL_PATH"
