"""Shared source-only boundaries for fixed declaration review packets.

These predicates identify where one exact source declaration may continue. They
do not classify clauses or authorize a declaration requirement on their own.
"""
from __future__ import annotations

import re
from typing import Any


_HEADING = re.compile(
    r"(?:原创性|独创性|诚信|使用授权|版权授权|公开授权).{0,12}(?:声明|说明|书)$"
    r"|^(?:非公开|不公开)学位论文标注说明$"
    r"|^(?:声明|授权书)$"
)
_SECTION_BOUNDARY = re.compile(
    r"^(?:摘要|ABSTRACT|目录|参考文献|第[一二三四五六七八九十\d]+章)$",
    re.IGNORECASE,
)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def is_fixed_declaration_heading(value: Any) -> bool:
    text = _compact(value)
    return len(text) <= 50 and bool(_HEADING.search(text))


def is_fixed_declaration_boundary(value: Any) -> bool:
    """Stop before a second declaration or an ordinary document section."""
    text = _compact(value)
    return is_fixed_declaration_heading(text) or bool(_SECTION_BOUNDARY.match(text))
