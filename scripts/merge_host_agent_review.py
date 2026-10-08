#!/usr/bin/env python3
"""Validate and merge responses produced by the current host Agent.

The script is intentionally deterministic and offline.  It never chooses a
model, reads an API key, or contacts a provider.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from requirements_engine import merge_host_agent_review_packets  # noqa: E402
from host_agent_bridge import _materialize_fixed_declaration_source_text  # noqa: E402
from thesis_format_pipeline import runtime_code_fingerprint  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_dir", type=Path,
                        help="directory containing host-agent-review-manifest.json and chunk responses")
    parser.add_argument("--response-out", type=Path,
                        help="merged response output path (default: <artifact-dir>/host-agent-response.json)")
    parser.add_argument("--artifact-dir", type=Path,
                        help="write merge receipt, ledger, commit, and response here; requires --parent-receipt when separate from review_dir")
    parser.add_argument("--parent-receipt", type=Path,
                        help="accepted prior merge receipt for an append-only deterministic reprojection")
    args = parser.parse_args(argv)
    review_dir = args.review_dir.resolve()
    artifact_dir = (args.artifact_dir or review_dir).resolve()
    response_out = (args.response_out or (artifact_dir / "host-agent-response.json")).resolve()
    try:
        _response, metadata = merge_host_agent_review_packets(
            review_dir, response_out=response_out,
            response_projector=_materialize_fixed_declaration_source_text,
            artifact_dir=artifact_dir,
            parent_receipt_path=args.parent_receipt,
            derivation_code_fingerprint_sha256=(
                runtime_code_fingerprint()["sha256"] if args.parent_receipt else None
            ),
        )
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "merged", **metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
