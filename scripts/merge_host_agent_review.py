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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review_dir", type=Path,
                        help="directory containing host-agent-review-manifest.json and chunk responses")
    parser.add_argument("--response-out", type=Path,
                        help="merged contract-2.1 response path (default: <review_dir>/llm-response.json)")
    args = parser.parse_args(argv)
    review_dir = args.review_dir.resolve()
    response_out = (args.response_out or (review_dir / "llm-response.json")).resolve()
    try:
        _response, metadata = merge_host_agent_review_packets(
            review_dir, response_out=response_out,
        )
    except (OSError, ValueError, json.JSONDecodeError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "merged", **metadata}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
