#!/bin/bash
# V2 通用论文 LaTeX → DOCX 转换管线
# Usage: ./convert.sh <input.tex> <output.docx> [overlay.yaml] [extra pandoc args...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCHEMA="$SCRIPT_DIR/schema/config-schema-v2.yaml"
MAPPER="$SCRIPT_DIR/scripts/mapper-v2-to-tjufe.py"
PREPROCESS="$SCRIPT_DIR/scripts/preprocess_tex.py"
POSTPROCESS="$SCRIPT_DIR/scripts/postprocess_docx.py"
FILTER="$SCRIPT_DIR/filters/thesis-v2.lua"
REFDOC="$SCRIPT_DIR/reference.docx"
BUILD_REF="$SCRIPT_DIR/scripts/build_reference_docx.py"

if [ "$#" -lt 2 ]; then
  echo "usage: $(basename "$0") <input.tex> <output.docx> [overlay.yaml] [extra pandoc args...]" >&2
  exit 2
fi

INPUT="$1"
OUTPUT="$2"
shift 2

OVERLAY=""
METADATA=""
if [ "$#" -gt 0 ]; then
  case "$1" in
    *.yaml|*.yml)
      OVERLAY="$1"
      shift
      ;;
  esac
fi

EXTRA_ARGS=("$@")

# Generate metadata from overlay + schema
if [ -n "$OVERLAY" ] && [ -f "$OVERLAY" ]; then
  TMP_META="$(mktemp /tmp/v2-metadata.XXXXXX.yaml)"
  /opt/homebrew/bin/python3 "$MAPPER" "$OVERLAY" "$TMP_META"
  METADATA="$TMP_META"
  trap "rm -f '$TMP_META'" EXIT
elif [ "$#" -gt 0 ]; then
  # Check if first extra arg is a metadata file (direct metadata mode)
  case "$1" in
    *.yaml|*.yml)
      METADATA="$1"
      shift
      EXTRA_ARGS=("$@")
      ;;
  esac
fi

# Build reference docx if needed
if [ ! -f "$REFDOC" ] || [ "$BUILD_REF" -nt "$REFDOC" ]; then
  /opt/homebrew/bin/python3 "$BUILD_REF"
fi

# Preprocess TeX
TMP_DIR="$(mktemp -d)"
TMP_TEX="$TMP_DIR/preprocessed.tex"
TMP_DOCX="$TMP_DIR/raw.docx"

cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

mkdir -p "$(dirname "$OUTPUT")"

echo "[convert.sh] preprocessing $INPUT..." >&2
/opt/homebrew/bin/python3 "$PREPROCESS" "$INPUT" "$TMP_TEX"

PANDOC_ARGS=(
  "$TMP_TEX"
  --from=latex+raw_tex
  --to=docx
  --standalone
  --reference-doc="$REFDOC"
  --lua-filter="$FILTER"
  --resource-path="$(dirname "$(realpath "$INPUT")"):$PWD"
  -o "$TMP_DOCX"
)

if [ -n "$METADATA" ]; then
  echo "[convert.sh] using metadata from ${OVERLAY:-$METADATA}" >&2
  PANDOC_ARGS+=(--metadata-file="$METADATA")
fi

if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
  PANDOC_ARGS+=("${EXTRA_ARGS[@]}")
fi

echo "[convert.sh] running pandoc..." >&2
/opt/homebrew/bin/pandoc "${PANDOC_ARGS[@]}"

echo "[convert.sh] postprocessing..." >&2
/opt/homebrew/bin/python3 "$POSTPROCESS" "$TMP_DOCX" "$OUTPUT"

echo "$OUTPUT"
