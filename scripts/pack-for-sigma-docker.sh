#!/usr/bin/env bash
# pack-for-sigma-docker.sh — build pageup-sigma inside debian:bookworm
#
# Primary workflow on Fedora (glibc newer than SberOS 2.41).  Requires Podman
# (preferred) or Docker (fallback) plus internet.  Writes dist/pageup-sigma-*.tar.gz
# on the host.
#
# Podman is tried first: Fedora ships it by default; it is daemonless and runs
# rootless without a background service.  Docker is only a compatibility fallback.
#
# Sigma never runs Podman or Docker — only extracts the tarball and uses python3.
# (Filename is historical; Podman is the primary container tool.)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if command -v podman &>/dev/null; then
    CONTAINER=podman
elif command -v docker &>/dev/null; then
    CONTAINER=docker
else
    echo "ERROR: neither Podman nor Docker found in PATH." >&2
    exit 1
fi

IMAGE="${PACK_SIGMA_IMAGE:-debian:bookworm}"

echo "Using $CONTAINER with image $IMAGE"
echo "Repository: $REPO_ROOT"
echo ""

# ── Container build ───────────────────────────────────────────────────────────
# Mount repo at /work, install uv inside Debian bookworm (glibc 2.36), then
# delegate to pack-for-sigma.sh which enforces SIGMA_GLIBC_MAX on the inner host.
$CONTAINER run --rm \
    -v "$REPO_ROOT:/work:Z" \
    -w /work \
    -e SIGMA_GLIBC_MAX="${SIGMA_GLIBC_MAX:-2.41}" \
    "$IMAGE" \
    bash -c '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates python3
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="/root/.local/bin:$PATH"
bash /work/scripts/pack-for-sigma.sh
'

echo ""
echo "Archive ready under $REPO_ROOT/dist/"
