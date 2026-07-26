#!/usr/bin/env bash
#
# Build and tag the container image.
#
#   ./build.sh                              build my-meeting-notes:latest locally, no registry
#   ./build.sh -p                           build and push (requires -r or MMN_REGISTRY)
#   ./build.sh -t v1.2                      add an extra tag
#   ./build.sh -r registry.example.com/x    tag for a specific registry
#   MMN_REGISTRY=registry.example.com/x ./build.sh   same, via environment
#
# Registry precedence: -r flag > MMN_REGISTRY env var > unset (local image only).
# There is deliberately no hardcoded registry default here -- this script is
# checked into a public repo, and baking in a real host would leak it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REGISTRY="${MMN_REGISTRY:-}"
IMAGE="my-meeting-notes"
PUSH=0
EXTRA_TAG=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--push)     PUSH=1; shift ;;
        -t|--tag)      EXTRA_TAG="$2"; shift 2 ;;
        -r|--registry) REGISTRY="$2"; shift 2 ;;
        -h|--help)     sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

log() { echo "==> $*"; }

if [ "$PUSH" -eq 1 ] && [ -z "$REGISTRY" ]; then
    echo "ERROR: --push needs a registry. Pass -r <registry> or set MMN_REGISTRY." >&2
    exit 1
fi

# REPO is the full tag prefix: "my-meeting-notes" locally, or
# "registry.example.com/homestack/my-meeting-notes" once a registry is set.
REPO="${REGISTRY:+${REGISTRY}/}${IMAGE}"

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
log "Building ${REPO}:latest"
docker build --target runtime \
    -t "${REPO}:latest" \
    -t "${REPO}:${SHORT}" \
    ${EXTRA_TAG:+-t "${REPO}:${EXTRA_TAG}"} \
    .

log "Built:"
docker images "${REPO}" --format '    {{.Repository}}:{{.Tag}}  {{.Size}}'

if [ "$PUSH" -eq 1 ]; then
    log "Pushing"
    docker push "${REPO}:latest"
    docker push "${REPO}:${SHORT}"
    [ -n "$EXTRA_TAG" ] && docker push "${REPO}:${EXTRA_TAG}"
    log "Pushed"
elif [ -z "$REGISTRY" ]; then
    log "Built locally as ${REPO}:latest. Set -r/MMN_REGISTRY and pass -p to push."
else
    log "Not pushed. Re-run with -p to push to ${REGISTRY}."
fi
