from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import toc_materializer as tm
import audit_toc_materialization as independent_toc_audit


def _add_outline(paragraph, level: int) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    outline = OxmlElement("w:outlineLvl")
    outline.set(qn("w:val"), str(level))
    ppr.append(outline)


def _add_toc_field(paragraph) -> None:
    for kind, text in (("begin", None), (None, ' TOC \\o "1-3" \\h \\z \\u '),
                       ("separate", None), (None, ""), ("end", None)):
        run = OxmlElement("w:r")
        if kind:
            marker = OxmlElement("w:fldChar")
            marker.set(qn("w:fldCharType"), kind)
            run.append(marker)
        else:
            node = OxmlElement("w:instrText" if "TOC" in text else "w:t")
            node.text = text
            run.append(node)
        paragraph._p.append(run)


def _make_fixture(root: Path, *, duplicate_output: bool = False,
                  page_restart: bool = False) -> tuple[Path, Path]:
    source = root / "source.docx"
    source_doc = Document()
    source_doc.add_paragraph("摘要", style="Heading 1")
    source_doc.add_paragraph("第1章 引言", style="Heading 1")
    source_doc.add_paragraph("1.1 背景", style="Heading 2")
    source_doc.add_paragraph("附录A 实验配置", style="Heading 1")
    source_doc.add_paragraph("后记", style="Heading 1")
    review = source_doc.styles.add_style("ThesisManualReview", WD_STYLE_TYPE.PARAGRAPH)
    marked = source_doc.add_paragraph("人工审查项目", style=review)
    _add_outline(marked, 0)
    toc_heading = source_doc.add_paragraph("目录", style="TOC Heading")
    _add_outline(toc_heading, 0)
    _add_toc_field(source_doc.add_paragraph())
    source_doc.save(source)

    baseline = root / "baseline.docx"
    output_doc = Document()
    for level in range(1, 4):
        output_doc.styles.add_style(f"TOC {level}", WD_STYLE_TYPE.PARAGRAPH)
    abstract_style = output_doc.styles.add_style("Abstract Title CN", WD_STYLE_TYPE.PARAGRAPH)
    output_doc.add_paragraph("摘要", style=abstract_style)
    toc_title = output_doc.add_paragraph("目 录", style="TOC Heading")
    field = output_doc.add_paragraph()
    _add_toc_field(field)
    output_doc.add_paragraph("第1章 引言", style="Heading 1")
    output_doc.add_paragraph("1.1 背景", style="Heading 2")
    output_doc.add_paragraph("附录 A 实验配置", style="Heading 1")
    custom = output_doc.styles.add_style("ThesisHeadingAcknowledgments", WD_STYLE_TYPE.PARAGRAPH)
    output_doc.add_paragraph("后记", style=custom)
    marker_style = output_doc.styles.add_style("ThesisManualReview", WD_STYLE_TYPE.PARAGRAPH)
    output_doc.add_paragraph("后记", style=marker_style)
    if duplicate_output:
        output_doc.add_paragraph("第1章 引言", style="Heading 1")
    if page_restart:
        section = output_doc.sections[0]
        pg_num = OxmlElement("w:pgNumType")
        pg_num.set(qn("w:start"), "1")
        section._sectPr.append(pg_num)
    output_doc.save(baseline)
    # Ensure page-number tab computation has explicit section geometry and
    # preserve the field position in the saved fixture.
    with ZipFile(baseline) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    return source, baseline


def _fake_pdf(path: Path, outline: list[list[object]], rows: list[tuple[str, int]] | None = None) -> None:
    import fitz

    doc = fitz.open()
    for _ in range(max((int(row[2]) for row in outline), default=1)):
        doc.new_page()
    doc.set_toc(outline)
    for index, (title, page_value) in enumerate(rows or []):
        page_index = index // 35
        while page_index >= len(doc):
            doc.new_page()
        y = 60 + (index % 35) * 18
        doc[page_index].insert_text((48, y), f"{title} ........ {page_value}", fontsize=10,
                                    fontname="china-s")
    doc.save(path)
    doc.close()


class TocMaterializerTests(unittest.TestCase):
    def test_source_selection_excludes_toc_heading_and_manual_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = _make_fixture(root)
            headings = tm.extract_source_headings(source, 3)
            self.assertEqual([item["title"] for item in headings], [
                "摘要", "第1章 引言", "1.1 背景", "附录A 实验配置", "后记",
            ])

    def test_toc_materialization_binds_custom_postscript_and_keeps_dynamic_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root)
            headings = tm.extract_source_headings(source, 3)
            with ZipFile(baseline) as archive:
                document_root = etree.fromstring(archive.read("word/document.xml"))
                styles_root = etree.fromstring(archive.read("word/styles.xml"))
            seed_items = tm._prepare_output_headings(
                document_root, styles_root, headings,
                set(document_root.xpath(".//w:bookmarkStart/@w:name", namespaces=tm.NS)),
            )
            self.assertEqual(len(seed_items), 5)
            postscript = next(item for item in seed_items if item.title == "后记")
            self.assertEqual(postscript.level, 0)
            pages = {item.bookmark: index + 3 for index, item in enumerate(seed_items)}
            output = root / "materialized.docx"
            items = tm.materialize_docx(baseline, output, headings, pages)
            with ZipFile(output) as archive:
                out_root = etree.fromstring(archive.read("word/document.xml"))
            toc_instruction = tm._toc_field_paragraphs(out_root)[0][1]
            self.assertIn('TOC \\o "1-3"', toc_instruction)
            self.assertEqual(out_root.xpath("count(.//w:instrText[contains(., 'PAGEREF')] )", namespaces=tm.NS), 5)
            post_paragraph = next(p for p in out_root.xpath(".//w:body//w:p", namespaces=tm.NS)
                                 if tm.normalized_title(tm._paragraph_text(p)) == "后记"
                                 and tm._paragraph_style_id(p) != "ThesisManualReview")
            self.assertEqual(post_paragraph.find("w:pPr/w:outlineLvl", namespaces=tm.NS).get(qn("w:val")), "0")
            entries = [p for p in out_root.xpath(".//w:body/w:p", namespaces=tm.NS)
                       if tm._paragraph_style_id(p) in {"TOC1", "TOC2", "TOC3"}]
            self.assertEqual(len(entries), 5)

    def test_duplicate_output_heading_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root, duplicate_output=True)
            headings = tm.extract_source_headings(source, 3)
            with ZipFile(baseline) as archive:
                document_root = etree.fromstring(archive.read("word/document.xml"))
                styles_root = etree.fromstring(archive.read("word/styles.xml"))
            with self.assertRaisesRegex(ValueError, "exactly one output body heading"):
                tm._prepare_output_headings(document_root, styles_root, headings, set())

    def test_explicit_section_page_restart_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, baseline = _make_fixture(root, page_restart=True)
            with ZipFile(baseline) as archive:
                document_root = etree.fromstring(archive.read("word/document.xml"))
            with self.assertRaisesRegex(ValueError, "section restarts/non-decimal"):
                tm._effective_page_numbering(document_root)

    def test_pdf_outline_page_map_requires_unique_title_and_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root)
            headings = tm.extract_source_headings(source, 3)
            with ZipFile(baseline) as archive:
                document_root = etree.fromstring(archive.read("word/document.xml"))
                styles_root = etree.fromstring(archive.read("word/styles.xml"))
            items = tm._prepare_output_headings(document_root, styles_root, headings, set())
            pdf = root / "map.pdf"
            _fake_pdf(pdf, [[item.level + 1, item.title, index + 1]
                            for index, item in enumerate(items)])
            pages = tm.rendered_page_map(pdf, items)
            self.assertEqual(pages[items[-1].bookmark], 5)
            _fake_pdf(pdf, [[item.level + 1, item.title, index + 1]
                            for index, item in enumerate(items)] + [[1, items[-1].title, 5]])
            with self.assertRaisesRegex(ValueError, "exactly one outline target"):
                tm.rendered_page_map(pdf, items)

    def test_pdf_outline_depth_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root)
            headings = tm.extract_source_headings(source, 3)
            with ZipFile(baseline) as archive:
                document_root = etree.fromstring(archive.read("word/document.xml"))
                styles_root = etree.fromstring(archive.read("word/styles.xml"))
            items = tm._prepare_output_headings(document_root, styles_root, headings, set())
            pdf = root / "wrong-depth.pdf"
            outline = [[1, "Unrelated parent", 1], [2, items[0].title, 1]]
            outline.extend([item.level + 1, item.title, index + 2]
                           for index, item in enumerate(items[1:]))
            _fake_pdf(pdf, outline)
            with self.assertRaisesRegex(ValueError, "outline depth mismatch"):
                tm.rendered_page_map(pdf, items)

    def test_final_audit_rejects_page_cache_that_does_not_match_rendered_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root)
            headings = tm.extract_source_headings(source, 3)
            with ZipFile(baseline) as archive:
                document_root = etree.fromstring(archive.read("word/document.xml"))
                styles_root = etree.fromstring(archive.read("word/styles.xml"))
            seed_items = tm._prepare_output_headings(document_root, styles_root, headings, set())
            good = {item.bookmark: index + 4 for index, item in enumerate(seed_items)}
            output = root / "final.docx"
            items = tm.materialize_docx(baseline, output, headings, good)
            pdf = root / "final.pdf"
            _fake_pdf(pdf, [[item.level + 1, item.title, good[item.bookmark]] for item in items],
                      [(item.title, good[item.bookmark]) for item in items])
            self.assertEqual(tm.audit_materialized_toc(output, pdf, items, good)["status"], "passed")
            independent_report = independent_toc_audit.audit(source, output, pdf)
            self.assertEqual(independent_report["status"], "passed")
            self.assertEqual(independent_report["rows"][0]["page"], good[items[0].bookmark])
            self.assertEqual(independent_report["rows"][0]["toc_page"], 1)
            wrong = dict(good)
            wrong[items[0].bookmark] += 1
            with self.assertRaisesRegex(ValueError, "cached page values differ"):
                tm.audit_materialized_toc(output, pdf, items, wrong)

            tampered = root / "tampered.docx"
            with ZipFile(output) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            out_root = etree.fromstring(members["word/document.xml"])
            first_ref = next(node for node in out_root.xpath(".//w:instrText", namespaces=tm.NS)
                             if "PAGEREF" in (node.text or ""))
            ref_run = first_ref
            while ref_run is not None and ref_run.tag != tm.W + "r":
                ref_run = ref_run.getparent()
            row = ref_run.getparent()
            runs = row.xpath("./w:r", namespaces=tm.NS)
            ref_index = runs.index(ref_run)
            cache_run = next(run for run in runs[ref_index + 1:]
                             if run.xpath(".//w:t", namespaces=tm.NS)
                             and not run.xpath(".//w:fldChar[@w:fldCharType='end']", namespaces=tm.NS))
            cache_run.find("w:t", namespaces=tm.NS).text = "999"
            members["word/document.xml"] = etree.tostring(
                out_root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
            with ZipFile(tampered, "w", ZIP_DEFLATED) as archive:
                for name, payload in members.items():
                    archive.writestr(name, payload)
            with self.assertRaisesRegex(ValueError, "cached page/depth differs"):
                independent_toc_audit.audit(source, tampered, pdf)

    @unittest.skipUnless(shutil.which("soffice"), "LibreOffice is unavailable")
    def test_render_materialize_loop_converges_on_real_pdf_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root)
            output, work = root / "final.docx", root / "toc-work"
            report = tm.materialize(source, baseline, output, work, max_iterations=4, timeout=120)
            self.assertEqual(report["status"], "passed")
            self.assertFalse(report["field_update"]["actual_word_or_libreoffice_field_refresh"])
            self.assertGreaterEqual(len(report["iterations"]), 2)
            self.assertTrue(report["iterations"][-1]["cache_matches_render"])

    @unittest.skipUnless(shutil.which("soffice"), "LibreOffice is unavailable")
    def test_toc_insertion_repagination_is_recomputed_until_final_pages_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_doc = Document()
            baseline_doc = Document()
            for level in range(1, 4):
                baseline_doc.styles.add_style(f"TOC {level}", WD_STYLE_TYPE.PARAGRAPH)
            titles = [f"第{index:02d}章 验收章节标题" for index in range(1, 43)]
            for title in titles:
                source_doc.add_paragraph(title, style="Heading 1")
            source = root / "source-many.docx"
            source_doc.save(source)
            field = baseline_doc.add_paragraph()
            _add_toc_field(field)
            for title in titles:
                baseline_doc.add_paragraph(title, style="Heading 1")
            baseline = root / "baseline-many.docx"
            baseline_doc.save(baseline)
            report = tm.materialize(source, baseline, root / "final-many.docx",
                                   root / "toc-work", max_iterations=6, timeout=120)
            iterations = report["iterations"]
            self.assertGreaterEqual(len(iterations), 3)
            self.assertNotEqual(iterations[0]["rendered_pages"], iterations[-1]["rendered_pages"])
            self.assertTrue(iterations[-1]["cache_matches_render"])
            self.assertEqual(report["independent_toc_audit"]["entry_count"], len(titles))

    def test_non_converging_pagination_fails_without_accepting_a_final_docx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, baseline = _make_fixture(root)
            render_count = 0

            def fake_render(docx_path, output_pdf, log_path, profile_dir, *, timeout=900):
                nonlocal render_count
                render_count += 1
                pdf_path = output_pdf.parent / f"{docx_path.stem}.pdf"
                pdf_path.write_bytes(b"test pdf placeholder")
                log_path.write_text('{"exit_code":0,"timed_out":false}\n', encoding="utf-8")
                return {"command": ["soffice"], "exit_code": 0, "log": str(log_path),
                        "elapsed_seconds": 0.0, "pdf": str(pdf_path),
                        "pdf_sha256": tm.sha256(pdf_path), "pdf_bytes": pdf_path.stat().st_size,
                        "page_count": 1, "renderer_version": "test"}

            def moving_page_map(_pdf, items):
                return {item.bookmark: index + render_count
                        for index, item in enumerate(items, 1)}

            with mock.patch.object(tm, "render_pdf", side_effect=fake_render), \
                    mock.patch.object(tm, "rendered_page_map", side_effect=moving_page_map):
                with self.assertRaisesRegex(RuntimeError, "did not converge"):
                    tm.materialize(source, baseline, root / "never-accepted.docx",
                                   root / "non-converging-work", max_iterations=3)
            self.assertFalse((root / "never-accepted.docx").exists())


if __name__ == "__main__":
    unittest.main()
