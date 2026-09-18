#!/bin/bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
BUILD_DIR="${BUILD_DIR:-build/fresh-batch-$(date -u +%Y%m%d-%H%M%S)}"

exec "$PYTHON_BIN" scripts/batch_rerun_ten_schools.py \
  --build-dir "$BUILD_DIR" \
  --auto-host-agent \
  "$@"
