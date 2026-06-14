#!/usr/bin/env bash
# pack-for-sigma.sh — build a library-only pageup-sigma tarball for SberOS
#
# Ships lib/pythonX.Y/site-packages/ (cp313 wheels when built with Python 3.13).  Sigma runs:
#   alias pageup='PYTHONPATH="$HOME/projects/pageup-sigma/lib/python3.13/site-packages" python3 -m pageup'
#   pageup ...
#
# Do not embed Python — fapolicy on Sigma blocks executing binaries/scripts from home.
# System python3 3.13+ (confirmed 3.13.2 on Sigma) is the interpreter.
#
# Run via pack-for-sigma-docker.sh on Fedora (Podman preferred; glibc 2.43).  This script alone
# requires glibc <= SIGMA_GLIBC_MAX (default 2.41).

set -euo pipefail

# Resolve repo root from scripts/ — all paths below are relative to pageup checkout.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

SIGMA_GLIBC_MAX="${SIGMA_GLIBC_MAX:-2.41}"
BUNDLE_NAME="pageup-sigma"
DIST_DIR="$REPO_ROOT/dist"
BUNDLE_ROOT="$DIST_DIR/$BUNDLE_NAME"

VERSION="$(python3 - <<'PY'
import tomllib
from pathlib import Path
print(tomllib.load(Path("pyproject.toml").open("rb"))["project"]["version"])
PY
)"

# ── glibc guard ───────────────────────────────────────────────────────────────
# Manylinux wheels embed native libs linked against the build host glibc.
# SberOS ships an older glibc (2.41); building on Fedora 44+ needs Docker/Podman.
host_glibc="$(ldd --version 2>&1 | head -1 | grep -oE '[0-9]+\.[0-9]+' | tail -1)"
if [[ -z "$host_glibc" ]]; then
    echo "ERROR: could not detect build-host glibc version (ldd --version)." >&2
    exit 1
fi
if awk -v host="$host_glibc" -v max="$SIGMA_GLIBC_MAX" 'BEGIN { exit !(host > max) }'; then
    echo "ERROR: build-host glibc $host_glibc exceeds SIGMA_GLIBC_MAX=$SIGMA_GLIBC_MAX." >&2
    echo "       Run: bash scripts/pack-for-sigma-docker.sh" >&2
    exit 1
fi
echo "Build host glibc: $host_glibc (max $SIGMA_GLIBC_MAX) — OK"

# ── Tooling preflight ─────────────────────────────────────────────────────────
if ! command -v uv &>/dev/null; then
    echo "ERROR: uv is not installed or not in PATH." >&2
    exit 1
fi

if [[ ! -f "$REPO_ROOT/uv.lock" ]]; then
    echo "ERROR: uv.lock not found — run 'uv lock' and commit the lockfile." >&2
    exit 1
fi

REQ_FILE="$(mktemp)"
trap 'rm -f "$REQ_FILE"' EXIT

echo "Building $BUNDLE_NAME $VERSION at $BUNDLE_ROOT"

# ── Build wheel and install deps into lib/pythonX.Y/site-packages ───────────
rm -rf "$BUNDLE_ROOT"
mkdir -p "$DIST_DIR"

echo "Installing build Python 3.13 (not shipped in bundle)..."
uv python install 3.13 --quiet
BUILD_PYTHON="$(uv python find 3.13)"

echo "Building pageup wheel..."
uv build --quiet
mapfile -t _wheels < <(ls -t "$DIST_DIR"/pageup-"$VERSION"-py3-none-any.whl 2>/dev/null || true)
if ((${#_wheels[@]} == 0)); then
    echo "ERROR: wheel not found in $DIST_DIR (pageup-${VERSION}-py3-none-any.whl)." >&2
    exit 1
fi
WHEEL="${_wheels[0]}"

echo "Exporting locked dependencies..."
uv export --frozen --no-dev --no-editable --no-emit-project -o "$REQ_FILE"

PY_MM="$("$BUILD_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
SITE_PACKAGES="$BUNDLE_ROOT/lib/python${PY_MM}/site-packages"
mkdir -p "$SITE_PACKAGES"

echo "Installing wheels into lib/python${PY_MM}/site-packages..."
uv pip install \
    --python "$BUILD_PYTHON" \
    --target "$SITE_PACKAGES" \
    --only-binary :all: \
    -r "$REQ_FILE" \
    "$WHEEL"

# ── Prune bundle for Sigma deploy ─────────────────────────────────────────────
# Sigma runs `python3 -m pageup` with PYTHONPATH — no console scripts or Selenium
# Manager binaries for macOS/Windows are needed on SberOS x86_64 Linux.
echo "Pruning bundle (Sigma runs python3 -m pageup, not console scripts)..."
rm -rf "$SITE_PACKAGES/bin"
find "$SITE_PACKAGES" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true
# pageup uses explicit sberdriver — Selenium Manager binaries for other OSes are unused.
rm -rf \
    "$SITE_PACKAGES/selenium/webdriver/common/macos" \
    "$SITE_PACKAGES/selenium/webdriver/common/windows"

cat > "$BUNDLE_ROOT/README-SIGMA.txt" <<EOF
pageup-sigma $VERSION — library bundle for SberOS (Python ${PY_MM}, x86_64).

Sigma cannot execute scripts or binaries from home (fapolicy).  Use system python3.

Add to ~/.bashrc (login shell only — do not run source ~/.bashrc on Sigma):

  alias pageup='PYTHONPATH="\$HOME/projects/pageup-sigma/lib/python${PY_MM}/site-packages" python3 -m pageup'

Re-login, then:

  tar xzf pageup-sigma-${VERSION}-linux-x86_64.tar.gz -C ~/projects
  pageup --version
  pageup --name "AI in Dev Community" \\
    --group-url "https://sberchat.sberbank.ru/#/chat/group796209083" \\
    --min-date 20200101
  # --write-dir ~/projects/pageup-results  # default
  # --sleep-time 60                        # default

Output: ~/projects/pageup-results/AI in Dev Community.json.

During --sleep-time countdown: scroll to the latest message and click inside
the chat for keyboard focus.  If the browser window is open but the client
certificate (.p12) picker never appeared, stop (Ctrl+C) and restart.

Transfer via oait-bucket object storage (upload from Fedora, download on Sigma).

Requires: system python3 >= 3.13, Sberbrowser + sberdriver, client .p12, OTP.
EOF

# ── Smoke test and archive ────────────────────────────────────────────────────
echo "Verifying bundle..."
PYTHONPATH="$SITE_PACKAGES" "$BUILD_PYTHON" -c "import lxml, pydantic, pageup; print('imports ok')"
PYTHONPATH="$SITE_PACKAGES" "$BUILD_PYTHON" -m pageup --version

ARCHIVE="$DIST_DIR/${BUNDLE_NAME}-${VERSION}-linux-x86_64.tar.gz"
tar -czf "$ARCHIVE" -C "$DIST_DIR" "$BUNDLE_NAME"

echo ""
echo "Done."
echo "  Bundle:  $BUNDLE_ROOT"
echo "  Archive: $ARCHIVE"
echo "  Deploy to Sigma, then:"
echo "    pageup --version"
