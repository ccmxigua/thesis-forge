#!/usr/bin/env python3
"""Audit input_prerequisite clauses without changing pipeline classifications.

The audit is deliberately conservative. It separates standalone/compound
metadata labels, school/template fixed-value candidates, and clauses that are
probably content/format/example/UI text rather than metadata fields.
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from capability_planner import METADATA_COMPOSITE_FIELDS, METADATA_LABEL_FIELDS

SCHOOLS = ("bsu", "btbu", "cauc", "dlut", "neau", "szu", "tju", "ujs", "ustb", "xzhmu")

FIXED_VALUE_PATTERNS = (
    # Only short standalone labels or explicit label/value fragments qualify.
    # A school name inside prose, a citation, or a legal declaration is not a
    # fixed metadata field.
    ("school_code", re.compile(r"^(?:中图分类号\s*)?(?:学校代码|单位代码|院校代码)(?:\s*[：:]?\s*[0-9A-Za-z_-]+)?$")),
    # Require an explicit label/value separator.  Without it, labels such as
    # ``培养单位地址`` can be mistaken for a training-unit value.
    ("institution_name", re.compile(r"^(?:所在学院|培养单位|学院(?:（部、研究院）)?|培养单位代码)[：:]\s*[一-龥A-Za-z0-9×X_-]{1,30}$")),
    ("fixed_confidentiality_option", re.compile(r"^密级[：:]?\s*(?:公开|内部\s*\d+年|秘密|机密)(?:[□☐].*)?$")),
)

# These are deliberately phrased as presentation/instruction signals rather than
# broad words such as ``格式``.  A requirement may mention formatting while still
# imposing a substantive/content obligation, so only unambiguously operational
# clauses are reclassified here.
NON_METADATA_PATTERNS = (
    ("placeholder", re.compile(r"X{2,}|×{2,}|\*{2,}|_{3,}|请根据|请填写|选填|按照实际情况|填写完毕|样例|示例|模板")),
    ("content_obligation", re.compile(r"本人声明|摘要|关键词|研究现状|研究方法|研究成果|结论|参考文献|致谢|附录|科研成果|学术成果")),
    ("format_or_ui", re.compile(
        r"居中|对齐|字号|字体|行距|页码|分页|另起一页|删除此框|保持原样式|插图清单|附表清单|"
        r"标明量和单位|篇幅以一页为限|保证此页为奇数页|换行不缩进|不编号|目录应列至|目录条目采用|"
        r"自行确定是否使用三级标题|数字和字母应为Times\s*New\s*Roman体|标题中阿拉伯数字换成汉字|"
        r"论文文字与格式基本要求|加黑|加粗|替换为本人|填写内容(?:为|是)|"
        r"正文中内容要求|正文中的每章|论文题目即可|目录条目|封面包括")),
    ("legal_or_external", re.compile(r"知识产权|授权|版权|数据库|复制|保存|汇编|声明")),
    ("example_or_prose", re.compile(r"[。；，].{8,}|例如|一般|应当|必须|不得|可以|不应|不少于|不超过|限一页")),
)

# Exact label/value fragments found in cover tables.  The value is intentionally
# not interpreted as the user's value: it is a template/example fragment that
# can only be replaced when the corresponding explicit input metadata exists.
# Keep this allow-list separate from generic prose matching.
EMBEDDED_METADATA_LABELS = {
    "作者姓名": ("author", "author_name"),
    "导师姓名": ("advisor", "supervisor_name"),
    "副导师": ("co_advisor",),
    "兼职导师": ("co_advisor",),
    "答辩日期": ("defense_date",),
    "分类号": ("classification_number", "class_no"),
    "中图分类号": ("classification_number", "class_no"),
}

# v10 reconciliation of the 67 v9 manual-review records.  These decisions are
# intentionally keyed by school and clause id: the same short text can mean a
# field label, a section heading, or template prose in another document.  Each
# entry was checked against source_text_full and adjacent template context.
RECONCILED_NON_METADATA = {
    "btbu": {"C00061", "C00187"},
    "cauc": {"C00090", "C01152", "C01224", "C01225", "C01231", "C01232"},
    "dlut": {"C00330", "C00895", "C00974"},
    "neau": {"C00013"},
    "tju": {"C00030", "C00031", "C00032", "C00050", "C00217", "C00236", "C00237",
            "C00308", "C00341", "C00374", "C00407", "C00433"},
    "ujs": {"C00058", "C00206"},
    "ustb": {"C00034", "C00041", "C00066", "C00286", "C00298", "C00299", "C00332",
             "C00378", "C00384"},
    "xzhmu": {"C00038", "C00166", "C00169"},
}

RECONCILED_SOURCE_CONTENT = {
    "bsu": {"C00157", "C00170", "C00176", "C00177", "C00178", "C00296", "C00300",
            "C00306", "C00397"},
    "cauc": {"C01162"},
    "dlut": {"C00999"},
    "szu": {"C00093", "C00286", "C00318"},
    "tju": {"C00036", "C00082", "C00353", "C00419"},
    "ustb": {"C00084", "C00242", "C00328"},
    "xzhmu": {"C00039", "C00365", "C00366"},
}

RECONCILED_METADATA_FIELDS = {
    ("bsu", "C00044"): ("confidentiality_period.start", "confidentiality_period.end"),
    ("btbu", "C00055"): ("author_post_graduation_destination", "employment_unit", "contact_phone",
                           "contact_address", "postal_code"),
    ("neau", "C00046"): ("defense_committee",),
    ("szu", "C00034"): ("discipline_category",),
    ("ustb", "C00329"): ("unit_address",),
}



def normalize(text: Any) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"\s+", "", text)
    return text.strip("：:;；,，。()（）[]【】")


def metadata_labels(text: str) -> list[str]:
    n = normalize(text)
    labels = []
    for label in sorted(METADATA_LABEL_FIELDS, key=len, reverse=True):
        if n == normalize(label):
            return [label]
    # Compound cover fragments such as “论文作者 指导教师” or
    # “申请学位 培养单位”: only report when the whole string is composed of
    # known labels, never infer arbitrary substrings from prose.
    remaining = n
    while remaining:
        matched = next((label for label in sorted(METADATA_LABEL_FIELDS, key=len, reverse=True)
                        if remaining.startswith(normalize(label))), None)
        if not matched:
            return []
        labels.append(matched)
        remaining = remaining[len(normalize(matched)):]
    return labels


def classify(text: str, *, school: str | None = None, clause_id: str | None = None,
             source_text_full: str | None = None) -> tuple[str, str, list[str], list[str]]:
    if school and clause_id:
        key = (school, clause_id)
        if clause_id in RECONCILED_NON_METADATA.get(school, set()):
            return "non_metadata_misclassification_candidate", "v10_context_reconciled_non_metadata", [], []
        if clause_id in RECONCILED_SOURCE_CONTENT.get(school, set()):
            return "requires_source_content", "v10_context_reconciled_source_content", [], []
        if key in RECONCILED_METADATA_FIELDS:
            return ("metadata_safe_candidate", "v10_context_reconciled_formal_metadata", [],
                    list(RECONCILED_METADATA_FIELDS[key]))
    normalized = normalize(text)
    # Exact composites are the only compound metadata rule.  Every component
    # is independently mapped by capability_planner and must be non-empty in
    # semantic metadata before the planner can satisfy the prerequisite.
    for composite, field_groups in METADATA_COMPOSITE_FIELDS.items():
        if normalized == normalize(composite):
            labels = metadata_labels(text)
            fields = sorted({field for group in field_groups for field in group})
            return "metadata_compound_candidate", "exact_composite_all_fields_require_supplied_metadata", labels, fields
    labels = metadata_labels(text)
    if labels:
        fields = sorted({field for label in labels for field in METADATA_LABEL_FIELDS[label]})
        if len(labels) > 1:
            return "metadata_compound_candidate", "compound_known_labels_requires_adapter", labels, fields
        return "metadata_safe_candidate", "standalone_known_label", labels, fields
    # An explicit label/value boundary is deterministic, but the value remains
    # untrusted.  This audit classification records a replacement candidate;
    # it does not promote the example value into current metadata.
    for label, fields in EMBEDDED_METADATA_LABELS.items():
        if re.fullmatch(rf"{re.escape(label)}[：:].+", normalized):
            return "metadata_embedded_candidate", "explicit_label_value_requires_supplied_metadata", [label], list(fields)
    for reason, pattern in FIXED_VALUE_PATTERNS:
        if pattern.fullmatch(normalized):
            return "school_fixed_value_candidate", reason, [], []
    reasons = [reason for reason, pattern in NON_METADATA_PATTERNS if pattern.search(text)]
    if reasons:
        return "non_metadata_misclassification_candidate", "+".join(reasons), [], []
    return "manual_review", "no_safe_generic_classification", [], []


def load_school(base: Path, school: str) -> list[dict[str, Any]]:
    work = base / school / "work"
    clauses = {x["id"]: x for x in json.loads((work / "requirements/requirement-clauses.json").read_text())}
    preflight = json.loads((work / "capability-preflight.json").read_text())
    rows = []
    for item in preflight.get("clauses", []):
        if item.get("category") != "input_prerequisite":
            continue
        clause_id = item.get("clause_id")
        source = clauses.get(clause_id, {})
        text = source.get("text") or source.get("source_text_full") or item.get("text") or ""
        category, reason, labels, fields = classify(
            text, school=school, clause_id=clause_id,
            source_text_full=source.get("source_text_full"),
        )
        rows.append({
            "school": school,
            "clause_id": clause_id,
            "text": text,
            "audit_category": category,
            "reason": reason,
            "matched_labels": labels,
            "candidate_metadata_fields": fields,
            "pipeline_category": item.get("category"),
            "source_status": item.get("source_status"),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [row for school in SCHOOLS for row in load_school(args.build_dir, school)]
    counts = Counter(row["audit_category"] for row in rows)
    per_school = defaultdict(Counter)
    for row in rows:
        per_school[row["school"]][row["audit_category"]] += 1
    report = {
        "schema_version": "metadata-prerequisite-audit-1",
        "build_dir": str(args.build_dir.resolve()),
        "total": len(rows),
        "counts": dict(sorted(counts.items())),
        "per_school": {school: dict(sorted(values.items())) for school, values in sorted(per_school.items())},
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "total": len(rows), "counts": dict(counts),
                      "per_school": report["per_school"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
