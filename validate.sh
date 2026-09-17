#!/usr/bin/env bash
# Validate one conversion against the source and the resolved configuration.
# This intentionally does not compare against reference.docx: a seed/template
# is not an independent expected thesis manifest.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON:-$(command -v python3 || true)}"
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "python3 not found; set PYTHON to an executable interpreter" >&2
  exit 2
fi

OVERLAY="${1:-}"
REPORT_PATH="$SCRIPT_DIR/reports/validation-report.json"
if [ -z "$OVERLAY" ]; then
  echo "usage: ./validate.sh <config-overlay.yaml> [--out report.json]" >&2
  exit 2
fi
shift
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out)
      if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "missing value for --out" >&2
        exit 2
      fi
      REPORT_PATH="$2"
      shift 2
      ;;
    *)
      echo "unknown validate.sh option: $1" >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$OVERLAY" ] || [ ! -r "$OVERLAY" ]; then
  echo "config overlay is missing or unreadable: $OVERLAY" >&2
  exit 2
fi
SAMPLE_TEX="$SCRIPT_DIR/tests/sample-thesis.tex"
if [ ! -f "$SAMPLE_TEX" ]; then
  echo "sample thesis not found: $SAMPLE_TEX" >&2
  exit 2
fi

REPORT_PARENT="$(dirname -- "$REPORT_PATH")"
mkdir -p "$REPORT_PARENT"
if [ ! -w "$REPORT_PARENT" ]; then
  echo "report directory is not writable: $REPORT_PARENT" >&2
  exit 2
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/thesis-forge-validate.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

TMP_DOCX="$TMP_DIR/candidate.docx"
EXTRACT_DIR="$TMP_DIR/extract"
COMPAT_REPORT="$TMP_DIR/ooxml.json"
VALIDATE_REPORT="$TMP_DIR/validation.json"
RESOLVED_CONFIG="$TMP_DIR/resolved-config.yaml"
EXPECTED_MANIFEST="$SCRIPT_DIR/tests/expected/sample-thesis.manifest.json"

echo "[1/4] converting sample thesis..."
"$PYTHON_BIN" "$SCRIPT_DIR/scripts/config_v2.py" "$SCRIPT_DIR/schema/config-schema-v2.yaml" "$OVERLAY" "$RESOLVED_CONFIG" --strict >/dev/null
"$SCRIPT_DIR/convert.sh" "$SAMPLE_TEX" "$TMP_DOCX" --config "$RESOLVED_CONFIG" >/dev/null
echo "[2/4] extracting serialized OOXML..."
"$PYTHON_BIN" "$SCRIPT_DIR/redteam/extract_docx.py" "$TMP_DOCX" --out "$EXTRACT_DIR"
echo "[3/4] running independent OOXML compatibility audit..."
set +e
"$PYTHON_BIN" "$SCRIPT_DIR/scripts/ooxml_compatibility.py" "$TMP_DOCX" --out "$COMPAT_REPORT" >/dev/null
COMPAT_STATUS=$?
set -e
echo "[4/4] checking source/output/config invariants..."
set +e
"$PYTHON_BIN" "$SCRIPT_DIR/scripts/validate_artifact.py" "$TMP_DOCX" --source "$SAMPLE_TEX" --config "$RESOLVED_CONFIG" --compatibility-report "$COMPAT_REPORT" --expected "$EXPECTED_MANIFEST" --out "$VALIDATE_REPORT" >/dev/null
VALIDATE_STATUS=$?
set -e

"$PYTHON_BIN" - "$VALIDATE_REPORT" "$REPORT_PATH" "$COMPAT_STATUS" "$OVERLAY" "$RESOLVED_CONFIG" <<'PY'
import json
import hashlib
import sys
from pathlib import Path

validation_path = Path(sys.argv[1])
report_path = Path(sys.argv[2])
compat_status = int(sys.argv[3])
input_config = Path(sys.argv[4]).resolve()
resolved_config = Path(sys.argv[5]).resolve()
data = json.loads(validation_path.read_text(encoding='utf-8'))
data['validation_entrypoint'] = 'validate.sh'
data['compatibility_exit_status'] = compat_status
data['valid'] = bool(data.get('valid')) and compat_status == 0
data['input_config'] = str(input_config)
data['resolved_config'] = str(resolved_config)
data.setdefault('hashes', {})['input_config_sha256'] = hashlib.sha256(input_config.read_bytes()).hexdigest()
if compat_status != 0:
    data.setdefault('errors', []).append('ooxml_compatibility.py exited non-zero')
report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(data, ensure_ascii=False))
PY

if [ "$VALIDATE_STATUS" -eq 0 ] && [ "$COMPAT_STATUS" -eq 0 ]; then
  echo "Validation PASSED: $REPORT_PATH"
  exit 0
fi
echo "Validation FAILED: $REPORT_PATH" >&2
exit 1
