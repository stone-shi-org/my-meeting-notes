#!/usr/bin/env bash
#
# Test runner. Emits JUnit XML into test-reports/ for the Atlassian Bamboo
# JUnit Parser task (result pattern: **/test-reports/*.xml).
#
#   ./test.sh                       backend + frontend, natively
#   ./test.sh --backend-only
#   ./test.sh --frontend-only
#   ./test.sh --docker              run both suites inside containers
#   ./test.sh -- -k test_config     extra args after -- go to pytest
#
# Deliberately NOT `set -e`: a failing test run must still leave its XML on disk,
# or Bamboo reports "no test results found" instead of showing which test failed.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REPORT_DIR="$SCRIPT_DIR/test-reports"
VENV_DIR="$SCRIPT_DIR/venv"

RUN_BACKEND=1
RUN_FRONTEND=1
USE_DOCKER=0
PYTEST_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --backend-only)  RUN_FRONTEND=0; shift ;;
        --frontend-only) RUN_BACKEND=0;  shift ;;
        --docker)        USE_DOCKER=1;   shift ;;
        --)              shift; PYTEST_ARGS=("$@"); break ;;
        -h|--help)       sed -n '2,14p' "$0"; exit 0 ;;
        *)               PYTEST_ARGS+=("$1"); shift ;;
    esac
done

# Bamboo fails a build when result files predate the build start, so a stale XML
# from a previous run is worse than no XML at all. Always start clean.
rm -rf "$REPORT_DIR"
mkdir -p "$REPORT_DIR"

BACKEND_RC=0
FRONTEND_RC=0

banner() {
    echo ""
    echo "================================================================"
    echo "  $1"
    echo "================================================================"
}

# --------------------------------------------------------------------------- #
# Native
# --------------------------------------------------------------------------- #

setup_venv() {
    if [ ! -d "$VENV_DIR" ]; then
        echo "--> Creating virtualenv at $VENV_DIR"
        python3 -m venv "$VENV_DIR" || { echo "ERROR: could not create venv" >&2; return 1; }
    fi
    VENV_PYTHON="$VENV_DIR/bin/python"
    [ -x "$VENV_PYTHON" ] || VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
    if [ ! -x "$VENV_PYTHON" ]; then
        echo "ERROR: no python binary inside $VENV_DIR" >&2
        return 1
    fi
    echo "--> Installing dependencies"
    "$VENV_PYTHON" -m pip install --upgrade pip --quiet
    "$VENV_PYTHON" -m pip install -r requirements.txt --quiet
}

run_backend_native() {
    banner "Backend (pytest)"
    setup_venv || return 1
    "$VENV_PYTHON" -m pytest tests/ -v \
        -m "not integration" \
        -n auto \
        --junitxml="$REPORT_DIR/backend-results.xml" \
        --junit-prefix=backend \
        "${PYTEST_ARGS[@]}"
}

run_frontend_native() {
    banner "Frontend (vitest)"
    if [ ! -d "$SCRIPT_DIR/web/node_modules" ]; then
        echo "--> Installing web dependencies"
        ( cd web && npm ci --silent ) || return 1
    fi
    ( cd web && npx vitest run \
        --reporter=verbose \
        --reporter=junit \
        --outputFile="$REPORT_DIR/frontend-results.xml" )
}

# --------------------------------------------------------------------------- #
# Docker -- for CI agents without a Python/Node toolchain. Needs a Docker socket.
# --------------------------------------------------------------------------- #

run_backend_docker() {
    banner "Backend (pytest, in Docker)"
    docker build --target test-backend -t my-meeting-notes-test-backend . || return 1
    docker run --rm \
        -v "$REPORT_DIR:/app/test-reports" \
        my-meeting-notes-test-backend
}

run_frontend_docker() {
    banner "Frontend (vitest, in Docker)"
    docker build --target test-frontend -t my-meeting-notes-test-frontend . || return 1
    docker run --rm \
        -v "$REPORT_DIR:/app/test-reports" \
        my-meeting-notes-test-frontend
}

# --------------------------------------------------------------------------- #

if [ "$RUN_BACKEND" -eq 1 ]; then
    if [ "$USE_DOCKER" -eq 1 ]; then run_backend_docker; else run_backend_native; fi
    BACKEND_RC=$?
fi

if [ "$RUN_FRONTEND" -eq 1 ]; then
    if [ -f "$SCRIPT_DIR/web/package.json" ]; then
        if [ "$USE_DOCKER" -eq 1 ]; then run_frontend_docker; else run_frontend_native; fi
        FRONTEND_RC=$?
    else
        echo "--> Skipping frontend: web/package.json not present yet"
    fi
fi

banner "Summary"
echo "  backend  exit=$BACKEND_RC"
echo "  frontend exit=$FRONTEND_RC"
echo ""
echo "  Reports:"
ls -1 "$REPORT_DIR" 2>/dev/null | sed 's/^/    /' || echo "    (none)"
echo ""
echo "  Bamboo: add a JUnit Parser final task with pattern **/test-reports/*.xml"
echo ""

# Report the backend failure in preference to the frontend one.
if [ "$BACKEND_RC" -ne 0 ]; then exit "$BACKEND_RC"; fi
exit "$FRONTEND_RC"
