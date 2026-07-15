#!/bin/sh
# Build the AppSail bundle for the SplitLLM FastAPI backend.
#
# AppSail's Python managed runtime does NOT pip-install requirements.txt at
# deploy time — dependencies must be vendored into the bundle. Wheels are
# resolved for manylinux/x86_64 (the AppSail runtime), not the local machine.
#
# Run from anywhere; paths are resolved relative to this script.
set -e

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CATALYST_DIR=$(dirname "$SCRIPT_DIR")
REPO_DIR=$(dirname "$CATALYST_DIR")
BUILD_DIR="$CATALYST_DIR/appsail/build"

echo "==> Building AppSail bundle in $BUILD_DIR"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

# App source (no tests, venv, caches, or local .env — secrets live in
# app-config.json env_variables / the Catalyst console)
cp -R "$REPO_DIR/app" "$BUILD_DIR/app"
find "$BUILD_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
cp "$REPO_DIR/requirements.txt" "$BUILD_DIR/requirements.txt"

# Vendor dependencies for the AppSail runtime (linux x86_64, Python 3.12).
# --only-binary requires manylinux wheels; all current deps provide them.
python3 -m pip install \
    -r "$BUILD_DIR/requirements.txt" \
    -t "$BUILD_DIR" \
    --platform manylinux2014_x86_64 \
    --python-version 3.12 \
    --implementation cp \
    --only-binary=:all: \
    --upgrade \
    --quiet

# pip drops dist-info/bin clutter we don't need at runtime
rm -rf "$BUILD_DIR/bin" 2>/dev/null || true

echo "==> AppSail bundle ready ($(du -sh "$BUILD_DIR" | cut -f1))"
