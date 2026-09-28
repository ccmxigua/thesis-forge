"""Shared protocol facts for source-obligation analysis and human work items.

The model proposes semantic dispositions.  This module owns the deterministic
workflow category derived from each disposition so producers and consumers do
not maintain subtly different hand-written mappings.
"""
from __future__ import annotations

from typing import Any


OBLIGATION_COVERAGE_PROTOCOL = "native_source_obligation_coverage_review_v8"
OBLIGATION_ANALYSIS_LEDGER_PROTOCOL = "obligation_analysis_ledger_v2"

SCOPE_DEPENDENCY_DIMENSIONS = {
    "abstract_target_metric_ambiguity": frozenset({"target", "metric", "condition"}),
    "quantitative_scope_unit_ambiguity": frozenset({"target", "metric", "condition", "strength"}),
    "source_correction_target_ambiguity": frozenset({"target"}),
}

WORK_TYPE_BY_DISPOSITION = {
    "represented": "format_execution",
    "unrepresented": "unrepresented_source_obligation",
    "ambiguous": "semantic_ambiguity",
    "external_action_pending": "external_action",
    "authoring_content_pending": "authoring_content",
    "backend_unsupported": "backend_capability",
    "source_content_verification_pending": "existing_content_verification",
    "scope_unresolved": "scope_clarification",
}


def work_type_for_disposition(disposition: Any) -> str:
    """Return the canonical work type or fail closed for an unknown disposition."""
    if not isinstance(disposition, str) or disposition not in WORK_TYPE_BY_DISPOSITION:
        raise ValueError(f"unknown source-obligation disposition: {disposition!r}")
    return WORK_TYPE_BY_DISPOSITION[disposition]
