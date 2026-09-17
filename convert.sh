#!/usr/bin/env bash
# V2 通用论文 LaTeX → DOCX 转换管线
#
# Explicit interface:
#   convert.sh INPUT.tex OUTPUT.docx [--config OVERLAY.yaml]
#                         [--metadata METADATA.yaml] [--aux FILE.aux]
#                         [--bibliography FILE.bib] [pandoc arguments...]
#
# A single legacy positional YAML after the two required paths is still
# accepted as --config for migration, but missing files and ambiguous options
# are hard errors.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SCHEMA="$SCRIPT_DIR/schema/config-schema-v2.yaml"
CONFIG_LOADER="$SCRIPT_DIR/scripts/config_v2.py"
MAPPER="$SCRIPT_DIR/scripts/mapper-v2-to-tjufe.py"
PREPROCESS="$SCRIPT_DIR/scripts/preprocess_tex.py"
EXTRACT_SEMANTICS="$SCRIPT_DIR/scripts/extract_semantic_metadata.py"
POSTPROCESS="$SCRIPT_DIR/scripts/postprocess_docx.py"
MANIFEST_WRITER="$SCRIPT_DIR/scripts/write_conversion_manifest.py"
FILTER="$SCRIPT_DIR/filters/thesis-v2.lua"
BUILD_REF="$SCRIPT_DIR/scripts/build_reference_docx.py"

usage() {
  echo "usage: $(basename "$0") <input.tex> <output.docx> [--config FILE] [--metadata FILE] [--aux FILE] [--bibliography FILE] [--csl FILE] [--manifest FILE] [pandoc args...]" >&2
}

find_tool() {
  local requested="$1"
  local name="$2"
  if [ -n "$requested" ]; then
    printf '%s\n' "$requested"
  else
    command -v "$name" 2>/dev/null || true
  fi
}

PYTHON_BIN="$(find_tool "${PYTHON:-}" python3)"
PANDOC_BIN="$(find_tool "${PANDOC:-}" pandoc)"

if [ "$#" -lt 2 ]; then
  usage
  exit 2
fi
if [ -z "$PYTHON_BIN" ] || [ ! -x "$PYTHON_BIN" ]; then
  echo "[convert.sh] python3 not found; set PYTHON to an executable interpreter" >&2
  exit 2
fi
if [ -z "$PANDOC_BIN" ] || [ ! -x "$PANDOC_BIN" ]; then
  echo "[convert.sh] pandoc not found on PATH; set PANDOC to an executable" >&2
  exit 2
fi

INPUT="$1"
OUTPUT="$2"
shift 2

CONFIG=""
METADATA=""
AUX=""
BIBLIOGRAPHY=""
CSL=""
MANIFEST=""
EXTRA_ARGS=()

require_value() {
  if [ "$#" -lt 2 ] || [ -z "$2" ]; then
    echo "[convert.sh] missing value for $1" >&2
    usage
    exit 2
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --config)
      require_value "$1" "${2:-}"
      CONFIG="$2"
      shift 2
      ;;
    --config=*)
      CONFIG="${1#--config=}"
      [ -n "$CONFIG" ] || { echo "[convert.sh] empty --config" >&2; exit 2; }
      shift
      ;;
    --metadata)
      require_value "$1" "${2:-}"
      METADATA="$2"
      shift 2
      ;;
    --metadata=*)
      METADATA="${1#--metadata=}"
      [ -n "$METADATA" ] || { echo "[convert.sh] empty --metadata" >&2; exit 2; }
      shift
      ;;
    --aux)
      require_value "$1" "${2:-}"
      AUX="$2"
      shift 2
      ;;
    --aux=*)
      AUX="${1#--aux=}"
      [ -n "$AUX" ] || { echo "[convert.sh] empty --aux" >&2; exit 2; }
      shift
      ;;
    --bibliography)
      require_value "$1" "${2:-}"
      BIBLIOGRAPHY="$2"
      shift 2
      ;;
    --bibliography=*)
      BIBLIOGRAPHY="${1#--bibliography=}"
      [ -n "$BIBLIOGRAPHY" ] || { echo "[convert.sh] empty --bibliography" >&2; exit 2; }
      shift
      ;;
    --csl)
      require_value "$1" "${2:-}"
      CSL="$2"
      shift 2
      ;;
    --csl=*)
      CSL="${1#--csl=}"
      [ -n "$CSL" ] || { echo "[convert.sh] empty --csl" >&2; exit 2; }
      shift
      ;;
    --manifest)
      require_value "$1" "${2:-}"
      MANIFEST="$2"
      shift 2
      ;;
    --manifest=*)
      MANIFEST="${1#--manifest=}"
      [ -n "$MANIFEST" ] || { echo "[convert.sh] empty --manifest" >&2; exit 2; }
      shift
      ;;
    --)
      shift
      EXTRA_ARGS+=("$@")
      break
      ;;
    *.yaml|*.yml)
      if [ -n "$CONFIG" ]; then
        echo "[convert.sh] more than one config supplied: $1" >&2
        exit 2
      fi
      CONFIG="$1"
      echo "[convert.sh] warning: positional YAML is deprecated; use --config" >&2
      shift
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ ! -f "$INPUT" ] || [ ! -r "$INPUT" ]; then
  echo "[convert.sh] input TeX file is missing or unreadable: $INPUT" >&2
  exit 2
fi
if [ -z "$CONFIG" ]; then
  CONFIG="$SCHEMA"
fi
if [ ! -f "$CONFIG" ] || [ ! -r "$CONFIG" ]; then
  echo "[convert.sh] config file is missing or unreadable: $CONFIG" >&2
  exit 2
fi
if [ -n "$METADATA" ] && { [ ! -f "$METADATA" ] || [ ! -r "$METADATA" ]; }; then
  echo "[convert.sh] metadata file is missing or unreadable: $METADATA" >&2
  exit 2
fi
if [ -n "$AUX" ] && { [ ! -f "$AUX" ] || [ ! -r "$AUX" ]; }; then
  echo "[convert.sh] AUX file is missing or unreadable: $AUX" >&2
  exit 2
fi
if [ -n "$BIBLIOGRAPHY" ] && { [ ! -f "$BIBLIOGRAPHY" ] || [ ! -r "$BIBLIOGRAPHY" ]; }; then
  echo "[convert.sh] bibliography is missing or unreadable: $BIBLIOGRAPHY" >&2
  exit 2
fi
if [ -n "$CSL" ] && [ -z "$BIBLIOGRAPHY" ]; then
  echo "[convert.sh] --csl requires --bibliography so citeproc has an input database" >&2
  exit 2
fi

OUTPUT_PARENT="$(dirname -- "$OUTPUT")"
mkdir -p "$OUTPUT_PARENT"
if [ ! -d "$OUTPUT_PARENT" ] || [ ! -w "$OUTPUT_PARENT" ]; then
  echo "[convert.sh] output directory is not writable: $OUTPUT_PARENT" >&2
  exit 2
fi
if [ -n "$MANIFEST" ]; then
  MANIFEST_PARENT="$(dirname -- "$MANIFEST")"
  mkdir -p "$MANIFEST_PARENT"
  if [ ! -d "$MANIFEST_PARENT" ] || [ ! -w "$MANIFEST_PARENT" ]; then
    echo "[convert.sh] manifest directory is not writable: $MANIFEST_PARENT" >&2
    exit 2
  fi
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/thesis-forge.XXXXXXXX")"
cleanup() {
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

RESOLVED_CONFIG="$TMP_DIR/resolved-config.yaml"
CONFIG_REPORT="$TMP_DIR/config-report.json"
METADATA_OUT="$TMP_DIR/metadata.yaml"
TMP_TEX="$TMP_DIR/preprocessed.tex"
TMP_DOCX="$TMP_DIR/raw.docx"
REFERENCE_DOCX="$TMP_DIR/reference.docx"
SEMANTIC_METADATA="$TMP_DIR/semantic-metadata.json"
DEPENDENCY_MANIFEST="$TMP_DIR/source-dependencies.json"

echo "[convert.sh] resolving configuration $CONFIG..." >&2
"$PYTHON_BIN" "$CONFIG_LOADER" "$SCHEMA" "$CONFIG" "$RESOLVED_CONFIG" --strict --report "$CONFIG_REPORT"
"$PYTHON_BIN" "$MAPPER" "$RESOLVED_CONFIG" "$METADATA_OUT"

if [ -n "$BIBLIOGRAPHY" ]; then
  if [ -z "$CSL" ]; then
    CSL="$($PYTHON_BIN -c 'import sys, yaml; config = yaml.safe_load(open(sys.argv[1], encoding="utf-8")); print((config.get("bibliography") or {}).get("csl_file") or "")' "$RESOLVED_CONFIG")"
    if [ -z "$CSL" ]; then
      echo "[convert.sh] resolved config has no bibliography.csl_file; pass --csl explicitly" >&2
      exit 2
    fi
    case "$CSL" in
      /*) ;;
      *) CSL="$SCRIPT_DIR/$CSL" ;;
    esac
  else
    case "$CSL" in
      /*) ;;
      *) CSL="$(CDPATH= cd -- "$(dirname -- "$CSL")" && pwd)/$(basename -- "$CSL")" ;;
    esac
  fi
  if [ ! -f "$CSL" ] || [ ! -r "$CSL" ]; then
    echo "[convert.sh] CSL file is missing or unreadable: $CSL" >&2
    exit 2
  fi
elif [ -n "$MANIFEST" ]; then
  CSL=""
fi

echo "[convert.sh] building configured reference DOCX..." >&2
"$PYTHON_BIN" "$BUILD_REF" --config "$RESOLVED_CONFIG" --output "$REFERENCE_DOCX" >/dev/null

echo "[convert.sh] preprocessing $INPUT..." >&2
PREPROCESS_ARGS=("$INPUT" "$TMP_TEX" --encoding utf-8 --dependency-manifest "$DEPENDENCY_MANIFEST")
if [ -n "$AUX" ]; then
  PREPROCESS_ARGS+=(--aux "$AUX")
fi
if [ -n "$BIBLIOGRAPHY" ]; then
  PREPROCESS_ARGS+=(--preserve-citations)
fi
"$PYTHON_BIN" "$PREPROCESS" "${PREPROCESS_ARGS[@]}"

echo "[convert.sh] extracting semantic metadata..." >&2
"$PYTHON_BIN" "$EXTRACT_SEMANTICS" "$INPUT" "$SEMANTIC_METADATA" >/dev/null

INPUT_DIR="$(CDPATH= cd -- "$(dirname -- "$INPUT")" && pwd)"
PANDOC_ARGS=(
  "$TMP_TEX"
  --from=latex+raw_tex
  --to=docx
  --standalone
  --reference-doc="$REFERENCE_DOCX"
  --lua-filter="$FILTER"
  --metadata-file="$SEMANTIC_METADATA"
  --metadata-file="$METADATA_OUT"
  --resource-path="$INPUT_DIR:$SCRIPT_DIR"
  -o "$TMP_DOCX"
)

if [ -n "$METADATA" ]; then
  echo "[convert.sh] using explicit metadata $METADATA" >&2
  PANDOC_ARGS+=(--metadata-file="$METADATA")
fi

if [ -n "$BIBLIOGRAPHY" ]; then
  PANDOC_ARGS+=(
    --citeproc
    --bibliography="$BIBLIOGRAPHY"
    --csl="$CSL"
    --metadata=link-citations:true
    --metadata=reference-section-title:参考文献
  )
fi
if [ "${#EXTRA_ARGS[@]}" -gt 0 ]; then
  PANDOC_ARGS+=("${EXTRA_ARGS[@]}")
fi

echo "[convert.sh] running pandoc..." >&2
"$PANDOC_BIN" "${PANDOC_ARGS[@]}"

echo "[convert.sh] postprocessing..." >&2
"$PYTHON_BIN" "$POSTPROCESS" "$TMP_DOCX" "$OUTPUT" --config "$RESOLVED_CONFIG"

if [ -n "$MANIFEST" ]; then
  MANIFEST_ARGS=(
    "$MANIFEST" --input "$INPUT" --output "$OUTPUT" --config "$RESOLVED_CONFIG" --input-config "$CONFIG" --dependencies "$DEPENDENCY_MANIFEST"
    --pandoc "$PANDOC_BIN" --python "$PYTHON_BIN"
  )
  if [ -n "$BIBLIOGRAPHY" ]; then
    MANIFEST_ARGS+=(--bibliography "$BIBLIOGRAPHY" --csl "$CSL")
  fi
  "$PYTHON_BIN" "$MANIFEST_WRITER" "${MANIFEST_ARGS[@]}"
fi

echo "$OUTPUT"
