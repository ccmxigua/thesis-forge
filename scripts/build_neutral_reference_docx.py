#!/usr/bin/env python3
"""Build the school-neutral reference DOCX used by synthetic validation."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from neutral_thesis_fixture import build_neutral_thesis  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path, nargs="?", default=ROOT / "build" / "neutral-reference.docx")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    build_neutral_thesis(output.resolve())
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
