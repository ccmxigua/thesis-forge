"""Unrelated formatting must not invent a request to remove source pagination."""
from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from apply_format_spec import _content_instance_paragraphs, role_coverage
from page_field_ops import inspect_docx
from section_executor import all_story_paragraphs, execute_section_plan
from section_model import audit_plan_against_docx, compile_section_plan


def source_document(*, simple=False, variants=False):
    doc = Document()
    doc.add_paragraph("Unrelated body text")
    footer = doc.sections[0].footer
    paragraph = footer.paragraphs[0]
    paragraph.add_run("Page ")
    if simple:
        field = OxmlElement("w:fldSimple")
        field.set(qn("w:instr"), " PAGE ")
        run = OxmlElement("w:r")
        text = OxmlElement("w:t")
        text.text = "7"
        run.append(text)
        field.append(run)
        paragraph._p.append(field)
    else:
        for kind, value in (("begin", None), ("instrText", " PAGE "),
                            ("separate", None), ("t", "7"), ("end", None)):
            run = OxmlElement("w:r")
            if kind in {"begin", "separate", "end"}:
                child = OxmlElement("w:fldChar")
                child.set(qn("w:fldCharType"), kind)
            else:
                child = OxmlElement("w:" + kind)
                child.text = value
            run.append(child)
            paragraph._p.append(run)
    paragraph.add_run(" of the source")
    if variants:
        doc.settings.odd_and_even_pages_header_footer = True
        doc.sections[0].different_first_page_header_footer = True
        doc.sections[0].first_page_footer.paragraphs[0].text = "First footer"
        doc.sections[0].even_page_footer.paragraphs[0].text = "Even footer"
        doc.sections[0].header.paragraphs[0].text = "Source header"
        numbering = OxmlElement("w:pgNumType")
        numbering.set(qn("w:fmt"), "upperRoman")
        numbering.set(qn("w:start"), "7")
        doc.sections[0]._sectPr.append(numbering)
        doc.add_section(WD_SECTION.NEW_PAGE)
        doc.add_paragraph("Inherited stories")
    return doc


def story_parts(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()
                if name.startswith(("word/footer", "word/header"))}


@pytest.mark.parametrize("spec", [
    {}, {"roles": {"body_text": {"font": {"size_pt": 12}}}},
    {"page": {}}, {"page": {"margins_cm": {"top": 2.5}}},
    {"page": {"page_number": {}}},
    {"page": {"page_number": {"preserve_existing_locations": True}}},
])
@pytest.mark.parametrize("simple", [False, True])
@pytest.mark.parametrize("variants", [False, True])
def test_no_numbering_format_preserves_source_fields_and_section_contract(spec, simple, variants, tmp_path):
    doc = source_document(simple=simple, variants=variants)
    source, output = tmp_path / "source.docx", tmp_path / "output.docx"
    doc.save(source)
    before = inspect_docx(source)
    plan = compile_section_plan(spec, source)
    assert plan["valid"], plan["findings"]
    assert plan["page_properties_policy"] == "preserve_existing"
    result = execute_section_plan(doc, plan)
    doc.save(output)
    after = inspect_docx(output)
    assert story_parts(output) == story_parts(source)
    assert [s["page_number"] for s in after["sections"]] == [s["page_number"] for s in before["sections"]]
    assert [s["stories"] for s in after["sections"]] == [s["stories"] for s in before["sections"]]
    assert all(s["removed_page_fields"] == 0 for s in result["stories"])
    assert audit_plan_against_docx(plan, output) == []


@pytest.mark.parametrize("rule", [
    {"alignment": "center"}, {"body_start": 4}, {"front_matter_start": 2},
    {"body_start_selector": {"strategy": "section_index", "section_index": 999}},
    {"front_matter_start_selector": {"strategy": "section_index", "section_index": 1}},
    {"replace_existing_fields": True}, {"preserve_existing_locations": False},
    {"preserve_existing_locations": True, "alignment": "right"},
])
def test_formatless_numbering_directive_is_blocked_not_silently_ignored(rule, tmp_path):
    doc = source_document()
    source = tmp_path / "source.docx"
    doc.save(source)
    plan = compile_section_plan({"page": {"page_number": rule}}, source)
    assert not plan["valid"]
    assert "page_number_format_missing" in {item["code"] for item in plan["findings"]}
    with pytest.raises(ValueError, match="invalid SectionPlan"):
        execute_section_plan(doc, plan)


def test_preservation_executor_rejects_an_unapplied_alignment_override(tmp_path):
    doc = source_document()
    source = tmp_path / "source.docx"
    doc.save(source)
    plan = compile_section_plan({}, source)
    with pytest.raises(ValueError, match="alignment.*numbering format"):
        execute_section_plan(doc, plan, alignment="center")


def test_explicit_none_still_removes_source_page_field(tmp_path):
    doc = source_document()
    source, output = tmp_path / "source.docx", tmp_path / "output.docx"
    doc.save(source)
    plan = compile_section_plan({"page": {"page_number": {"body_format": "none"}}}, source)
    assert plan["page_properties_policy"] == "apply_plan"
    result = execute_section_plan(doc, plan)
    doc.save(output)
    assert sum(s["removed_page_fields"] for s in result["stories"]) == 1
    footer = inspect_docx(output)["sections"][0]["stories"]["footer"]["default"]
    assert footer["page_fields"] == []
    assert Document(output).sections[0].footer.paragraphs[0].text == "Page  of the source"
    assert audit_plan_against_docx(plan, output) == []


def test_paragraph_inventory_does_not_materialize_missing_stories(tmp_path):
    doc = source_document(variants=True)
    source, output = tmp_path / "source.docx", tmp_path / "output.docx"
    doc.save(source)
    paragraphs = _content_instance_paragraphs(doc)
    assert [p.text for p in paragraphs].count("Source header") == 1
    role_coverage(doc, {}, {})
    doc.save(output)
    assert story_parts(output) == story_parts(source)
    assert [s["stories"] for s in inspect_docx(output)["sections"]] == [
        s["stories"] for s in inspect_docx(source)["sections"]]


def test_story_reader_resolves_inheritance_without_creating_absent_story(tmp_path):
    doc = source_document(variants=True)
    source, output = tmp_path / "source.docx", tmp_path / "output.docx"
    doc.save(source)
    assert [p.text for p in all_story_paragraphs(doc.sections[1].header)] == ["Source header"]
    assert all_story_paragraphs(doc.sections[1].first_page_header) == []
    doc.save(output)
    assert story_parts(output) == story_parts(source)


def test_formatter_preserves_page_field_when_only_body_style_is_requested(tmp_path):
    source, output = tmp_path / "source.docx", tmp_path / "output.docx"
    source_document().save(source)
    spec = {"schema_version": "1.0", "source_document": "unit fixture",
            "status": "semantic_resolved", "analysis_mode": "known_template",
            "roles": {"body_text": {"font": {"size_pt": 12}}}, "requirements": []}
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    audit = tmp_path / "audit"
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/apply_format_spec.py"), str(source),
        str(spec_path), str(output), "--out-dir", str(audit),
        "--compliance-mode", "supported_subset",
    ], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert story_parts(output) == story_parts(source)
    receipt = json.loads((audit / "section-execution.json").read_text())
    assert sum(s["removed_page_fields"] for s in receipt["stories"]) == 0
    assert len(inspect_docx(output)["sections"][0]["stories"]["footer"]["default"]["page_fields"]) == 1
