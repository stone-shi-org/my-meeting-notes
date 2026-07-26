#!/usr/bin/env bash
#
# Build and tag the container image.
#
#   ./build.sh            build and tag :latest and :<hash>
#   ./build.sh -p         also push to the registry
#   ./build.sh -t v1.2    add an extra tag
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REGISTRY="registry.example.com/homestack"
IMAGE="my-meeting-notes"
PUSH=0
EXTRA_TAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--push) PUSH=1; shift ;;
        -t|--tag)  EXTRA_TAG="$2"; shift 2 ;;
        -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

log() { echo "==> $*"; }

# version.txt is baked into the image and surfaced at /api/version, so a running
# container can always be traced back to a commit.
HASH="$(git rev-parse HEAD 2>/dev/null || echo dev)"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    HASH="${HASH}-dirty"
fi

{
    echo "hash=${HASH}"
    echo "timestamp=${TIMESTAMP}"
} > version.txt

log "Version: ${HASH} (${TIMESTAMP})"

SHORT="${HASH:0:12}"
log "Building ${REGISTRY}/${IMAGE}:latest"
docker build --target runtime \
    -t "${REGISTRY}/${IMAGE}:latest" \
    -t "${REGISTRY}/${IMAGE}:${SHORT}" \
    ${EXTRA_TAG:+-t "${REGISTRY}/${IMAGE}:${EXTRA_TAG}"} \
    .

log "Built:"
docker images "${REGISTRY}/${IMAGE}" --format '    {{.Repository}}:{{.Tag}}  {{.Size}}'

if [ "$PUSH" -eq 1 ]; then
    log "Pushing"
    docker push "${REGISTRY}/${IMAGE}:latest"
    docker push "${REGISTRY}/${IMAGE}:${SHORT}"
    [ -n "$EXTRA_TAG" ] && docker push "${REGISTRY}/${IMAGE}:${EXTRA_TAG}"
    log "Pushed"
else
    log "Not pushed. Re-run with -p to push."
fi
