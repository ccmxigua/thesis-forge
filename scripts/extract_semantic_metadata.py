#!/usr/bin/env python3
"""Extract normalized thesis semantics from LaTeX before preamble removal.

The output is valid Pandoc JSON metadata as well as an auditable semantic
contract.  Legacy aliases are normalized once here; downstream stages consume
``cn_title``/``en_title`` or the explicit ``title_zh``/``title_en`` fields.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from .format_spec_validation import load_and_validate
    from .preprocess_tex import first_braced_macro
except ImportError:  # direct script execution
    from format_spec_validation import load_and_validate
    from preprocess_tex import first_braced_macro

ROOT = Path(__file__).resolve().parents[1]


def _text_value(value: Any) -> str:
    """Return a trimmed scalar without turning structured data into PII-like text."""
    return value.strip() if isinstance(value, str) else ""


def _metadata_value(metadata: dict[str, Any], *names: str) -> str:
    for name in names:
        value = _text_value(metadata.get(name))
        if value:
            return value
    return ""


def _normalize_date(value: Any) -> str | None:
    """Normalize an explicit source date to the profile's ISO year-month form."""
    text = _text_value(value)
    if not text:
        return None
    text = re.sub(r"\s+", " ", text)
    chinese = re.fullmatch(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日?)?", text
    )
    if chinese:
        year, month, day = (int(item) if item else None for item in chinese.groups())
        try:
            if day is None:
                datetime(year, month, 1)
                return f"{year:04d}-{month:02d}"
            datetime(year, month, day)
            return f"{year:04d}-{month:02d}-{day:02d}"
        except ValueError:
            return None
    for pattern in (
        "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y/%m", "%Y.%m",
        "%B %d, %Y", "%b %d, %Y", "%B %Y", "%b %Y",
    ):
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if "%d" not in pattern:
            return parsed.strftime("%Y-%m")
        return parsed.strftime("%Y-%m-%d")
    return None


def _degree_level(metadata: dict[str, Any]) -> tuple[str | None, str]:
    values = [
        _metadata_value(metadata, "degree_level"),
        _metadata_value(metadata, "degree_display", "degree_type"),
        _metadata_value(metadata, "degree_display_en"),
    ]
    levels: set[str] = set()
    for value in values:
        lowered = value.lower()
        if re.search(r"博士|doctoral|\bph\.?d\.?\b|\bdoctor\b", lowered):
            levels.add("doctor")
        if re.search(r"硕士|master", lowered):
            levels.add("master")
    if len(levels) > 1:
        return None, "ambiguous_source_value"
    if not levels:
        return None, "missing_source_value"
    return next(iter(levels)), ""


def _degree_category(metadata: dict[str, Any]) -> tuple[str | None, str]:
    explicit = _metadata_value(metadata, "degree_category")
    values = [explicit, _metadata_value(metadata, "degree_display", "degree_type"),
              _metadata_value(metadata, "degree_display_en")]
    categories: set[str] = set()
    for value in values:
        lowered = value.lower()
        if re.search(r"专业学位|professional", lowered):
            categories.add("professional")
        if re.search(r"学术学位|academic", lowered):
            categories.add("academic")
    if len(categories) > 1:
        return None, "ambiguous_source_value"
    if not categories:
        return None, "missing_source_value"
    return next(iter(categories)), ""


def _writing_language(metadata: dict[str, Any]) -> tuple[str | None, str]:
    explicit = _metadata_value(metadata, "writing_language")
    if explicit in {"zh", "en", "other"}:
        return explicit, ""
    if (_metadata_value(metadata, "title_zh", "cn_title")
            or _metadata_value(metadata, "abstract_zh")):
        return "zh", ""
    if (_metadata_value(metadata, "title_en", "en_title")
            or _metadata_value(metadata, "abstract_en")):
        return "en", ""
    return None, "missing_source_value"


def _security_level(metadata: dict[str, Any]) -> tuple[str | None, str]:
    raw = _metadata_value(metadata, "security_level", "confidentiality_level", "confidentiality")
    if not raw:
        return None, "missing_source_value"
    lowered = raw.lower()
    if raw in {"公开", "开放", "公开发表"} or lowered in {"public", "open"}:
        return "public", ""
    if raw in {"内部", "限制公开", "限公开"} or lowered in {"restricted", "internal"}:
        return "restricted", ""
    if raw in {"保密", "涉密", "机密", "秘密"} or lowered in {"classified", "secret", "confidential"}:
        return "classified", ""
    return None, "invalid_source_value"


def _degree_discipline(metadata: dict[str, Any]) -> tuple[str, str] | None:
    for key in ("discipline_category", "degree_discipline", "first_discipline"):
        value = _metadata_value(metadata, key)
        if value:
            return value, key
    display = _metadata_value(metadata, "degree_display", "degree_type")
    for name in ("哲学", "经济学", "法学", "教育学", "文学", "历史学", "理学",
                 "工学", "农学", "医学", "管理学", "艺术学"):
        if name in display:
            return name, "degree_display"
    return None


def normalize_semantic_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Convert extracted semantics into one validated, fail-closed thesis profile.

    Every populated value is copied from an explicit semantic field.  Missing
    user-controlled values remain absent/null and are represented by pending
    records; this function never supplies a name, identifier, or date.
    """
    if not isinstance(metadata, dict):
        raise TypeError("semantic metadata must be an object")

    pending: list[dict[str, str]] = []
    pending_fields: set[str] = set()
    field_sources: dict[str, str] = {}

    def add_pending(field: str, reason: str) -> None:
        if field in pending_fields:
            return
        pending_fields.add(field)
        pending.append({
            "field": field,
            "reason": reason,
            "resolution": "supply_user_confirmed_value",
        })

    profile: dict[str, Any] = {"schema_version": "1.0"}

    degree_level, degree_reason = _degree_level(metadata)
    profile["degree_level"] = degree_level
    if degree_level:
        field_sources["degree_level"] = "degree_level/degree_display/degree_display_en"
    else:
        add_pending("degree_level", degree_reason)

    degree_category, category_reason = _degree_category(metadata)
    if degree_category:
        profile["degree_category"] = degree_category
        field_sources["degree_category"] = "degree_category/degree_display/degree_display_en"
    else:
        add_pending("degree_category", category_reason)

    writing_language, language_reason = _writing_language(metadata)
    profile["writing_language"] = writing_language
    if writing_language:
        field_sources["writing_language"] = "writing_language/title_zh/title_en/abstract"
    else:
        add_pending("writing_language", language_reason)

    security_level, security_reason = _security_level(metadata)
    if security_level:
        profile["security_level"] = security_level
        field_sources["security_level"] = "security_level/confidentiality_level"
    else:
        add_pending("security_level", security_reason)

    for key in ("has_appendices", "has_figure_list", "has_table_list", "has_symbol_list"):
        if isinstance(metadata.get(key), bool):
            profile[key] = metadata[key]
            field_sources[key] = key

    raw_co_supervisors = metadata.get("co_supervisors")
    co_supervisors: list[dict[str, str]] = []
    co_supervisors_valid = isinstance(raw_co_supervisors, list)
    if isinstance(raw_co_supervisors, list):
        for item in raw_co_supervisors:
            if not isinstance(item, dict):
                co_supervisors_valid = False
                continue
            name = _text_value(item.get("name"))
            kind = _text_value(item.get("kind")) or "other"
            if not name or kind not in {"academic", "enterprise", "other"}:
                co_supervisors_valid = False
                continue
            co_supervisors.append({"name": name, "kind": kind})
    if co_supervisors_valid and co_supervisors and len(co_supervisors) <= 2:
        profile["co_supervisor_count"] = len(co_supervisors)
        field_sources["co_supervisor_count"] = "co_supervisors"
    elif not co_supervisors:
        add_pending("co_supervisor_count", "missing_source_value")
    else:
        add_pending("co_supervisor_count", "invalid_source_value")

    student_id = _metadata_value(metadata, "student_id")
    if student_id:
        profile["student_id"] = student_id
        field_sources["student_id"] = "student_id"

    direct_completion = _metadata_value(metadata, "completion_date")
    fallback_completion = _metadata_value(metadata, "submit_date", "submit_date_cn")
    completion_raw = direct_completion or fallback_completion
    completion_date = _normalize_date(completion_raw)
    if completion_date:
        profile["completion_date"] = completion_date
        field_sources["completion_date"] = "completion_date" if direct_completion else "submit_date"

    source_values: dict[str, tuple[str, tuple[str, ...]]] = {
        "classification_number": ("classification_number", ("classification_number", "class_no")),
        "unit_code": ("unit_code", ("unit_code",)),
    }
    for canonical, (source_key, names) in source_values.items():
        value = _metadata_value(metadata, *names)
        if value:
            field_sources[canonical] = source_key

    cover_values: dict[str, Any] = {
        "title_zh": _metadata_value(metadata, "title_zh", "cn_title"),
        "title_en": _metadata_value(metadata, "title_en", "en_title"),
        "author_name": _metadata_value(metadata, "author"),
        "student_id": student_id,
        "supervisor_name": _metadata_value(metadata, "advisor", "supervisor"),
        "completion_date": completion_date or "",
    }
    cover_sources = {
        "title_zh": "title_zh/cn_title", "title_en": "title_en/en_title",
        "author_name": "author", "student_id": "student_id",
        "supervisor_name": "advisor/supervisor",
        "completion_date": "completion_date" if direct_completion else "submit_date",
    }
    for field, value in cover_values.items():
        if not value:
            reason = "invalid_source_value" if field == "completion_date" and completion_raw else "missing_source_value"
            add_pending(f"cover_metadata.{field}", reason)

    discipline = _degree_discipline(metadata)
    optional_cover: dict[str, Any] = {
        "classification_number": _metadata_value(metadata, "classification_number", "class_no"),
        "unit_code": _metadata_value(metadata, "unit_code"),
        "subtitle_zh": _metadata_value(metadata, "subtitle", "cn_subtitle"),
        "subtitle_en": _metadata_value(metadata, "subtitle_en", "en_subtitle"),
        "college_name": _metadata_value(metadata, "school", "college"),
        "program_name": _metadata_value(metadata, "major", "discipline"),
        "field_name": _metadata_value(metadata, "second_discipline"),
        "research_direction": _metadata_value(metadata, "research_direction"),
    }
    if discipline:
        optional_cover["degree_discipline"] = discipline[0]
        field_sources["cover_metadata.degree_discipline"] = discipline[1]
    if co_supervisors:
        optional_cover["co_supervisors"] = co_supervisors
    security_marking = _metadata_value(metadata, "confidentiality_level", "confidentiality")
    if security_level in {"restricted", "classified"}:
        if security_marking:
            optional_cover["security_marking"] = security_marking
        optional_cover["administrative_verification"] = "external_required"

    if not any(field.startswith("cover_metadata.") for field in pending_fields):
        cover: dict[str, Any] = {
            "trust": {
                "source": "source_document",
                "confirmed": True,
                "note": "Copied from explicit source metadata; no personal data was inferred.",
            }
        }
        cover.update({key: value for key, value in cover_values.items() if value})
        cover.update({key: value for key, value in optional_cover.items() if value})
        profile["cover_metadata"] = cover
        for field in cover:
            if field != "trust" and field not in field_sources:
                field_sources[f"cover_metadata.{field}"] = field
        for field, source in cover_sources.items():
            field_sources[f"cover_metadata.{field}"] = source

    source_document = _metadata_value(metadata, "source_document")
    provenance: dict[str, Any] = {
        "source_kind": "semantic_metadata",
        "source_document": source_document,
        "normalizer": "extract_semantic_metadata.normalize_semantic_metadata",
        "trust": {
            "source": "source_document",
            "confirmed": True,
            "note": "Only explicit source values are copied; missing user fields remain pending.",
        },
        "field_sources": field_sources,
    }
    source_sha256 = _metadata_value(metadata, "source_sha256")
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        provenance["source_sha256"] = source_sha256
    profile["provenance"] = provenance
    profile["pending_fields"] = sorted(pending_fields)
    profile["pending_metadata"] = sorted(pending, key=lambda item: item["field"])
    profile["metadata_status"] = "complete" if not pending else "pending"
    return profile


# Public aliases keep the contract discoverable to callers that name the
# operation as a conversion rather than normalization.
semantic_metadata_to_thesis_profile = normalize_semantic_metadata
build_thesis_profile = normalize_semantic_metadata


def _value(text: str, names: tuple[str, ...]) -> str:
    match = first_braced_macro(text, names)
    return match[1].strip() if match else ""


def _keywords(text: str, english: bool) -> list[str]:
    if english:
        pattern = re.compile(r"\\keywords\[(?:english|en)\]\s*")
    else:
        pattern = re.compile(r"\\keywords\s*(?!\[)")
    match = pattern.search(text)
    if not match:
        return []
    try:
        from .preprocess_tex import extract_balanced
    except ImportError:
        from preprocess_tex import extract_balanced
    start = match.end()
    while start < len(text) and text[start].isspace():
        start += 1
    if start >= len(text) or text[start] != "{":
        return []
    try:
        group, _ = extract_balanced(text, start, "{", "}")
    except ValueError:
        return []
    return [item.strip() for item in re.split(r"[,，;；]", group[1:-1]) if item.strip()]


def _environment(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        match = re.search(rf"\\begin\{{{re.escape(name)}\}}(?:\[[^]]*\])?(.*?)\\end\{{{re.escape(name)}\}}", text, re.S)
        if match:
            return match.group(1).strip()
    return ""


def _co_supervisors(text: str) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    for names, kind in (
        (("coadvisor", "cosupervisor"), "academic"),
        (("enterpriseadvisor", "industryadvisor", "enterprisesupervisor"), "enterprise"),
    ):
        value = _value(text, names)
        if value:
            values.append({"name": value, "kind": kind})
    return values


def _defense_committee(text: str) -> list[dict[str, str]]:
    """Extract explicit repeated committee-member macros; never infer titles from names."""
    pattern = re.compile(r"\\defensecommitteemember\s*\{([^{}]*)\}\s*\{([^{}]*)\}\s*\{([^{}]*)\}(?:\s*\{([^{}]*)\})?")
    return [
        {key: value.strip() for key, value in zip(
            ("name", "professional_title", "institution", "role"), match.groups(default="")) if value.strip()}
        for match in pattern.finditer(text)
    ]


def _abstract_english(text: str) -> str:
    """Extract English abstracts using both named and optional-argument forms."""
    named = _environment(text, ("englishabstract", "abstracten"))
    if named:
        return named
    match = re.search(r"\\begin\{abstract\}\[(?:english|en)\](.*?)\\end\{abstract\}", text, re.S | re.I)
    return match.group(1).strip() if match else ""


def extract(path: Path, encoding: str = "utf-8") -> dict[str, Any]:
    raw = path.read_bytes()
    text = raw.decode(encoding)
    title_zh = _value(text, ("title", "ctitle", "zh_title"))
    title_en = _value(text, ("englishtitle", "entitle", "titleen", "en_title"))
    subtitle = _value(text, ("subtitle", "cnsubtitle", "zh_subtitle"))
    subtitle_en = _value(text, ("englishsubtitle", "ensubtitle", "subtitleen", "en_subtitle"))
    author = _value(text, ("author", "studentname"))
    author_en = _value(text, ("authoren", "englishauthor", "studentnameen"))
    advisor = _value(text, ("advisor", "supervisor"))
    advisor_en = _value(text, ("advisoren", "supervisoren"))
    co_advisor = _value(text, ("coadvisor", "cosupervisor"))
    co_advisor_en = _value(text, ("coadvisoren", "cosupervisoren"))
    school = _value(text, ("school", "college", "affiliation"))
    school_en = _value(text, ("schoolen", "collegeen", "affiliationen"))
    major = _value(text, ("major", "discipline", "program"))
    major_en = _value(text, ("majoren", "disciplineen", "programen"))
    first_discipline = _value(text, (
        "firstdiscipline", "firstleveldiscipline", "disciplinefirst",
        "一级学科", "first_discipline",
    ))
    second_discipline = _value(text, (
        "seconddiscipline", "secondleveldiscipline", "disciplinesecond",
        "二级学科", "second_discipline",
    ))
    first_discipline_en = _value(text, (
        "firstdisciplineen", "firstleveldisciplineen", "disciplinefirsten",
        "first_discipline_en",
    ))
    second_discipline_en = _value(text, (
        "seconddisciplineen", "secondleveldisciplineen", "disciplineseconden",
        "second_discipline_en",
    ))
    research_direction = _value(text, ("researchdirection", "direction"))
    research_direction_en = _value(text, ("researchdirectionen", "directionen"))
    student_id = _value(text, ("studentid", "studentnumber"))
    classification_number = _value(text, ("classificationnumber", "classno"))
    udc = _value(text, ("udc",))
    confidentiality_level = _value(text, ("confidentialitylevel", "confidentiality", "securitylevel"))
    confidentiality_level_en = _value(text, ("confidentialitylevelen", "confidentialityen"))
    confidentiality_period_start = _value(text, ("confidentialityperiodstart", "securityperiodstart"))
    confidentiality_period_end = _value(text, ("confidentialityperiodend", "securityperiodend"))
    author_post_graduation_destination = _value(text, (
        "authorpostgraduationdestination", "postgraduationdestination", "graduationdestination"))
    employment_unit = _value(text, ("employmentunit", "workunit"))
    contact_phone = _value(text, ("contactphone", "phonenumber"))
    contact_address = _value(text, ("contactaddress", "mailingaddress"))
    postal_code = _value(text, ("postalcode", "postcode"))
    discipline_category = _value(text, ("disciplinecategory", "disciplineclass"))
    unit_code = _value(text, ("unitcode", "institutioncode"))
    unit_address = _value(text, ("unitaddress", "institutionaddress"))
    completion_date = _value(text, ("completiondate", "finalizationdate", "thesisfinalizationdate"))
    submit_date = _value(text, ("submitdate", "submissiondate"))
    submit_date_en = _value(text, ("submitdateen", "submissiondateen"))
    defense_date = _value(text, ("defensedate",))
    defense_date_en = _value(text, ("defensedateen",))
    degree_conferral_date = _value(text, ("degreeconferraldate", "degreedate"))
    degree_conferral_date_en = _value(text, ("degreeconferraldateen", "degreedateen"))
    degree_display = _value(text, ("degreedisplay", "degreetype"))
    degree_display_en = _value(text, ("degreedisplayen", "degreetypeen"))
    figures = len(re.findall(r"\\includegraphics(?:\[[^]]*\])?\s*\{", text))
    # Count semantic table objects, not nested tabular layout environments.
    tables = len(re.findall(r"\\begin\{(?:table|longtable)\*?\}", text))
    equations = len(re.findall(r"\\begin\{(?:equation|align|gather|multline|eqnarray)\*?\}", text))
    return {
        "schema_version": "1.0",
        "source_document": str(path.resolve()),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "title_zh": title_zh,
        "title_en": title_en,
        "cn_title": title_zh,
        "en_title": title_en,
        "subtitle": subtitle,
        "subtitle_en": subtitle_en,
        "author": author,
        "author_en": author_en,
        "advisor": advisor,
        "advisor_en": advisor_en,
        "co_advisor": co_advisor,
        "co_advisor_en": co_advisor_en,
        "co_supervisors": _co_supervisors(text),
        "school": school,
        "school_en": school_en,
        "major": major,
        "major_en": major_en,
        "first_discipline": first_discipline,
        "second_discipline": second_discipline,
        "first_discipline_en": first_discipline_en,
        "second_discipline_en": second_discipline_en,
        "research_direction": research_direction,
        "research_direction_en": research_direction_en,
        "student_id": student_id,
        "classification_number": classification_number,
        "udc": udc,
        "confidentiality_level": confidentiality_level,
        "confidentiality_level_en": confidentiality_level_en,
        "confidentiality_period": {
            "start": confidentiality_period_start,
            "end": confidentiality_period_end,
        } if confidentiality_period_start or confidentiality_period_end else {},
        "author_post_graduation_destination": author_post_graduation_destination,
        "employment_unit": employment_unit,
        "contact_phone": contact_phone,
        "contact_address": contact_address,
        "postal_code": postal_code,
        "defense_committee": _defense_committee(text),
        "discipline_category": discipline_category,
        "unit_code": unit_code,
        "unit_address": unit_address,
        "completion_date": completion_date,
        "submit_date": submit_date,
        "submit_date_en": submit_date_en,
        "defense_date": defense_date,
        "defense_date_en": defense_date_en,
        "degree_conferral_date": degree_conferral_date,
        "degree_conferral_date_en": degree_conferral_date_en,
        "degree_display": degree_display,
        "degree_display_en": degree_display_en,
        "cn_subtitle": subtitle,
        "en_subtitle": subtitle_en,
        "college": school,
        "discipline": major,
        "class_no": classification_number,
        "confidentiality": confidentiality_level,
        "submit_date_cn": submit_date,
        "degree_type": degree_display,
        "abstract_zh": _environment(text, ("abstract",)),
        "abstract_en": _abstract_english(text),
        "keywords_zh": _keywords(text, False),
        "keywords_en": _keywords(text, True),
        "has_appendices": bool(re.search(r"\\appendix\b|\\begin\{appendices\}", text, re.I)),
        "has_figure_list": bool(re.search(r"\\listoffigures\b", text, re.I)),
        "has_table_list": bool(re.search(r"\\listoftables\b", text, re.I)),
        "has_symbol_list": bool(re.search(r"\\listofsymbols\b|\\printsymbols\b", text, re.I)),
        "inventory": {"figures": figures, "tables": tables, "display_equations": equations},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--schema", type=Path, default=ROOT / "schema" / "semantic-metadata.schema.json")
    parser.add_argument("--thesis-profile-out", "--profile-out", dest="profile_out", type=Path)
    parser.add_argument("--profile-schema", type=Path, default=ROOT / "schema" / "thesis-profile.schema.json")
    args = parser.parse_args(argv)
    try:
        result = extract(args.input, encoding=args.encoding)
    except (OSError, LookupError, UnicodeDecodeError) as exc:
        raise SystemExit(f"cannot decode input TeX as {args.encoding}: {args.input}: {exc}")
    errors = load_and_validate(result, args.schema)
    if errors:
        raise SystemExit("invalid semantic metadata:\n" + "\n".join(errors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    profile = normalize_semantic_metadata(result)
    profile_errors = load_and_validate(profile, args.profile_schema)
    if profile_errors:
        raise SystemExit("invalid thesis profile:\n" + "\n".join(profile_errors))
    if args.profile_out:
        args.profile_out.parent.mkdir(parents=True, exist_ok=True)
        args.profile_out.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "profile_output": str(args.profile_out) if args.profile_out else None,
                      "metadata_status": profile["metadata_status"],
                      "pending_fields": profile["pending_fields"],
                      "title_zh": bool(result["title_zh"]),
                      "title_en": bool(result["title_en"]), "inventory": result["inventory"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
