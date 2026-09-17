from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from postprocess_docx import (  # noqa: E402
    W_NS,
    caption_xref_display_map,
    convert_citation_hyperlinks_to_fields_for_wps,
    ensure_page_break_before,
    extract_figure_table_paragraphs,
    insert_section_break_before,
    is_page_break_only_paragraph,
    normalize_bibliography_label_spacing,
    normalize_citation_bookmarks_for_wps,
    normalize_body,
    qn,
    replace_caption_text_preserving_math,
    replace_xref_placeholders_in_paragraph,
    requires_page_break_before,
    unique_bookmark_name,
)


class PostprocessFigureLayoutTests(unittest.TestCase):
    def test_extracted_figure_drawings_remain_inline(self):
        tbl = ET.Element(qn("w", "tbl"))
        tr = ET.SubElement(tbl, qn("w", "tr"))
        tc = ET.SubElement(tr, qn("w", "tc"))
        p = ET.SubElement(tc, qn("w", "p"))
        drawing = ET.SubElement(ET.SubElement(p, qn("w", "r")), qn("w", "drawing"))
        inline = ET.SubElement(drawing, qn("wp", "inline"))
        ET.SubElement(inline, qn("wp", "extent"), {"cx": "100", "cy": "200"})

        extracted = extract_figure_table_paragraphs(tbl)

        self.assertEqual(len(extracted), 1)
        self.assertEqual(len(extracted[0].findall(".//" + qn("wp", "inline"))), 1)
        self.assertEqual(len(extracted[0].findall(".//" + qn("wp", "anchor"))), 0)

    def test_figure_table_expansion_preserves_text_only_subcaption(self):
        tbl = ET.Element(qn("w", "tbl"))
        ET.SubElement(ET.SubElement(tbl, qn("w", "tblPr")), qn("w", "tblStyle"),
                      {qn("w", "val"): "FigureTable"})
        tr = ET.SubElement(tbl, qn("w", "tr"))
        tc = ET.SubElement(tr, qn("w", "tc"))
        drawing_para = ET.SubElement(tc, qn("w", "p"))
        drawing = ET.SubElement(ET.SubElement(drawing_para, qn("w", "r")), qn("w", "drawing"))
        ET.SubElement(drawing, qn("wp", "inline"))
        text_para = ET.SubElement(tc, qn("w", "p"))
        ET.SubElement(ET.SubElement(text_para, qn("w", "r")), qn("w", "t")).text = "(a) 子图说明"

        extracted = extract_figure_table_paragraphs(tbl)

        self.assertEqual(len(extracted), 2)
        self.assertEqual(
            "".join(node.text or "" for node in extracted[1].findall(".//" + qn("w", "t"))),
            "(a) 子图说明",
        )

    def test_caption_numbering_preserves_inline_math_position(self):
        p = ET.Element(qn("w", "p"))
        ET.SubElement(ET.SubElement(p, qn("w", "r")), qn("w", "t")).text = "右侧显示不同 "
        math = ET.SubElement(p, qn("m", "oMath"))
        ET.SubElement(ET.SubElement(math, qn("m", "r")), qn("m", "t")).text = "H"
        ET.SubElement(ET.SubElement(p, qn("w", "r")), qn("w", "t")).text = " 值下的期权价值。"

        replace_caption_text_preserving_math(p, "图4-10", "unused")

        children = list(p)
        self.assertEqual(children[0].find(qn("w", "t")).text, "图4-10    右侧显示不同 ")
        self.assertEqual(children[1].find(".//" + qn("m", "t")).text, "H")
        self.assertEqual(children[2].find(qn("w", "t")).text, " 值下的期权价值。")


class PostprocessSectionPaginationTests(unittest.TestCase):
    def _paragraph(self, *, text: str | None = None, page_break: bool = False,
                   page_break_before: bool = False) -> ET.Element:
        p = ET.Element(qn("w", "p"))
        if page_break_before:
            ppr = ET.SubElement(p, qn("w", "pPr"))
            ET.SubElement(ppr, qn("w", "pageBreakBefore"))
        if text is not None:
            ET.SubElement(ET.SubElement(p, qn("w", "r")), qn("w", "t")).text = text
        if page_break:
            ET.SubElement(ET.SubElement(p, qn("w", "r")), qn("w", "br"),
                          {qn("w", "type"): "page"})
        return p

    def _section_paragraph(self) -> ET.Element:
        p = ET.Element(qn("w", "p"))
        sectpr = ET.SubElement(ET.SubElement(p, qn("w", "pPr")), qn("w", "sectPr"))
        ET.SubElement(sectpr, qn("w", "headerReference"),
                      {qn("w", "type"): "default", qn("r", "id"): "rIdHeader"})
        ET.SubElement(sectpr, qn("w", "pgNumType"), {qn("w", "fmt"): "decimal"})
        return p

    def test_only_adjacent_page_break_paragraph_is_removed(self):
        body = ET.Element(qn("w", "body"))
        body.append(self._paragraph(text="正文结尾"))
        page_break = self._paragraph(page_break=True)
        body.append(page_break)
        section = self._section_paragraph()

        removed = insert_section_break_before(body, 2, section)

        self.assertTrue(removed)
        self.assertEqual(len(body), 2)
        self.assertIs(body[1], section)
        self.assertEqual(len(body.findall(".//" + qn("w", "br"))), 0)
        self.assertIsNotNone(section.find(".//" + qn("w", "headerReference")))
        self.assertIsNotNone(section.find(".//" + qn("w", "pgNumType")))

    def test_normal_body_page_break_is_not_removed(self):
        body = ET.Element(qn("w", "body"))
        body.append(self._paragraph(text="正文结尾"))
        page_break = self._paragraph(page_break=True, text="不可删除的分页内容")
        body.append(page_break)
        section = self._section_paragraph()

        removed = insert_section_break_before(body, 2, section)

        self.assertFalse(removed)
        self.assertEqual(len(body), 3)
        self.assertIs(body[1], page_break)
        self.assertEqual(len(body.findall(".//" + qn("w", "br"))), 1)

    def test_page_break_before_property_is_not_treated_as_explicit_break(self):
        paragraph = self._paragraph(page_break_before=True)
        self.assertFalse(is_page_break_only_paragraph(paragraph))

    def test_chapter_and_reference_heading_require_explicit_page_break(self):
        for text in ("第1章  绪论", "附录A  补充证明", "参考文献"):
            with self.subTest(text=text):
                paragraph = self._paragraph(text=text)
                self.assertTrue(requires_page_break_before("Heading1", text))
                self.assertTrue(ensure_page_break_before(paragraph))
                self.assertIsNotNone(
                    paragraph.find("./" + qn("w", "pPr") + "/" + qn("w", "pageBreakBefore"))
                )
                self.assertFalse(ensure_page_break_before(paragraph))

        self.assertFalse(requires_page_break_before("Heading2", "第1章  绪论"))
        self.assertFalse(requires_page_break_before("Heading1", "普通一级标题"))

    def test_disabled_page_break_before_is_normalized_to_enabled(self):
        paragraph = self._paragraph(text="第2章  文献综述")
        ppr = paragraph.find(qn("w", "pPr"))
        if ppr is None:
            ppr = ET.Element(qn("w", "pPr"))
            paragraph.insert(0, ppr)
        disabled = ET.SubElement(ppr, qn("w", "pageBreakBefore"), {qn("w", "val"): "0"})

        self.assertTrue(ensure_page_break_before(paragraph))
        self.assertNotIn(qn("w", "val"), disabled.attrib)

    def test_multiple_breaks_are_not_silently_rewritten(self):
        paragraph = self._paragraph(page_break=True)
        ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "br"),
                      {qn("w", "type"): "page"})
        self.assertTrue(is_page_break_only_paragraph(paragraph))

        body = ET.Element(qn("w", "body"))
        body.append(self._paragraph(text="正文"))
        body.append(paragraph)
        section = self._section_paragraph()
        self.assertTrue(insert_section_break_before(body, 2, section))
        self.assertEqual(len(body.findall(".//" + qn("w", "br"))), 0)

    def test_break_before_existing_section_property_is_removed_without_replacing_section(self):
        body = ET.Element(qn("w", "body"))
        body.append(self._paragraph(text="正文"))
        page_break = self._paragraph(page_break=True)
        body.append(page_break)
        existing_section = self._section_paragraph()
        body.append(existing_section)
        inserted_section = self._section_paragraph()

        self.assertTrue(insert_section_break_before(body, 3, inserted_section))
        self.assertNotIn(page_break, list(body))
        self.assertIn(existing_section, list(body))
        self.assertIn(inserted_section, list(body))
        self.assertEqual(len(body.findall(".//" + qn("w", "br"))), 0)

    def test_xref_replacement_accepts_fallback_text_containing_bracket(self):
        paragraph = self._paragraph(
            text="540 (700 in [[[TJUFE_XREF:fig:mc_std|Reference [fig:mc_std)]]])"
        )

        replace_xref_placeholders_in_paragraph(paragraph, {}, {})

        rendered = "".join(
            node.text or "" for node in paragraph.findall(".//" + qn("w", "t"))
        )
        self.assertNotIn("TJUFE_XREF", rendered)
        self.assertEqual(rendered, "540 (700 in fig:mc_std)")

    def test_unique_bookmark_name_suffixes_collisions(self):
        used = {"TJUFE_duplicate"}
        self.assertEqual(unique_bookmark_name("duplicate", used), "TJUFE_duplicate_2")
        self.assertEqual(unique_bookmark_name("duplicate", used), "TJUFE_duplicate_3")

    def test_bookmark_names_respect_word_40_character_limit(self):
        label = "fig_std_European_lookback_option_different_TC_plot"
        first = unique_bookmark_name(label, set())
        self.assertLessEqual(len(first), 40)
        self.assertTrue(first.startswith("TJUFE_"))

        used = {first}
        second = unique_bookmark_name(label, used)
        self.assertLessEqual(len(second), 40)
        self.assertNotEqual(first, second)

    def test_xref_replacement_preserves_inline_math(self):
        paragraph = ET.Element(qn("w", "p"))
        ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "t")).text = "变量"
        math = ET.SubElement(paragraph, qn("m", "oMath"))
        ET.SubElement(ET.SubElement(math, qn("m", "r")), qn("m", "t")).text = "x"
        ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "t")).text = (
            "见[[[TJUFE_XREF:eq:a|(3.1)]]]。"
        )

        replace_xref_placeholders_in_paragraph(
            paragraph, {"eq:a": "eq_a"}, {"eq:a": "式（3.1）"}
        )

        self.assertIsNotNone(paragraph.find(".//" + qn("m", "oMath")))
        self.assertEqual(paragraph.find(".//" + qn("m", "t")).text, "x")
        rendered = "".join(node.text or "" for node in paragraph.findall(".//" + qn("w", "t")))
        self.assertEqual(rendered, "变量见式（3.1）。")
        hyperlink = paragraph.find(".//" + qn("w", "hyperlink"))
        self.assertIsNotNone(hyperlink)
        self.assertEqual(hyperlink.get(qn("w", "anchor")), "eq_a")

    def test_unnumbered_equation_label_gets_bookmark_without_number(self):
        body = ET.Element(qn("w", "body"))
        heading = ET.SubElement(body, qn("w", "p"))
        ET.SubElement(ET.SubElement(heading, qn("w", "r")), qn("w", "t")).text = "第1章  理论"
        ET.SubElement(heading, qn("w", "pPr"))
        heading.find(qn("w", "pPr")).append(
            ET.Element(qn("w", "pStyle"), {qn("w", "val"): "Heading1"})
        )
        control = ET.SubElement(body, qn("w", "p"))
        ET.SubElement(ET.SubElement(control, qn("w", "r")), qn("w", "t")).text = (
            "[[[TJUFE_EQCONTROL:unnumbered:]]]"
        )
        label = ET.SubElement(body, qn("w", "p"))
        ET.SubElement(ET.SubElement(label, qn("w", "r")), qn("w", "t")).text = (
            "[[[TJUFE_EQLABEL:eq:star]]]"
        )
        equation = ET.SubElement(body, qn("w", "p"))
        math_para = ET.SubElement(equation, qn("m", "oMathPara"))
        math = ET.SubElement(math_para, qn("m", "oMath"))
        ET.SubElement(ET.SubElement(math, qn("m", "r")), qn("m", "t")).text = "x"
        reference = ET.SubElement(body, qn("w", "p"))
        ET.SubElement(ET.SubElement(reference, qn("w", "r")), qn("w", "t")).text = (
            "见 [[[TJUFE_XREF:eq:star|[eq:star)]]]"
        )

        maps = normalize_body(body)

        self.assertIn("eq:star", maps["bookmark_map"])
        self.assertEqual("".join(node.text or "" for node in equation.findall(".//" + qn("w", "t"))), "")
        link = reference.find(".//" + qn("w", "hyperlink"))
        self.assertIsNotNone(link)
        self.assertNotIn("TJUFE_XREF", "".join(node.text or "" for node in reference.findall(".//" + qn("w", "t"))))

    def test_equation_numbering_is_idempotent(self):
        body = ET.Element(qn("w", "body"))
        heading = ET.SubElement(body, qn("w", "p"))
        ppr = ET.SubElement(heading, qn("w", "pPr"))
        ET.SubElement(ppr, qn("w", "pStyle"), {qn("w", "val"): "Heading1"})
        ET.SubElement(ET.SubElement(heading, qn("w", "r")), qn("w", "t")).text = "第1章  理论"
        equation = ET.SubElement(body, qn("w", "p"))
        math_para = ET.SubElement(equation, qn("m", "oMathPara"))
        math = ET.SubElement(math_para, qn("m", "oMath"))
        ET.SubElement(ET.SubElement(math, qn("m", "r")), qn("m", "t")).text = "x"

        normalize_body(body)
        normalize_body(body)

        self.assertEqual(
            "".join(node.text or "" for node in equation.findall(".//" + qn("w", "t"))),
            "（1.1）",
        )
        self.assertEqual(len(equation.findall(".//" + qn("w", "rStyle"))), 1)

    def test_caption_display_map_uses_final_continued_figure_number(self):
        body = ET.Element(qn("w", "body"))
        caption = self._paragraph(text="续图3-5  多面板结果")
        start = ET.Element(qn("w", "bookmarkStart"), {
            qn("w", "id"): "1", qn("w", "name"): "TJUFE_fig_panel",
        })
        caption.insert(0, start)
        body.append(caption)

        displays = caption_xref_display_map(body, {"fig:panel": "TJUFE_fig_panel"})

        self.assertEqual(displays, {"fig:panel": "图3.5"})

    def test_citation_bookmarks_are_normalized_for_word_and_wps(self):
        document = ET.Element(qn("w", "document"))
        body = ET.SubElement(document, qn("w", "body"))
        citation = ET.SubElement(body, qn("w", "p"))
        link = ET.SubElement(citation, qn("w", "hyperlink"), {
            qn("w", "anchor"): "ref-alpha-key",
        })
        ET.SubElement(ET.SubElement(link, qn("w", "r")), qn("w", "t")).text = "1"
        bibliography = ET.SubElement(body, qn("w", "p"))
        ET.SubElement(bibliography, qn("w", "bookmarkStart"), {
            qn("w", "id"): "42",
            qn("w", "name"): "ref-alpha-key",
        })
        field = ET.SubElement(ET.SubElement(body, qn("w", "p")), qn("w", "r"))
        ET.SubElement(field, qn("w", "instrText")).text = (
            ' HYPERLINK \\l "ref-alpha-key" '
        )

        mapping = normalize_citation_bookmarks_for_wps(document)

        self.assertEqual(mapping, {"ref-alpha-key": "REF0001"})
        self.assertEqual(link.get(qn("w", "anchor")), "REF0001")
        bookmark = bibliography.find(qn("w", "bookmarkStart"))
        self.assertEqual(bookmark.get(qn("w", "name")), "REF0001")
        self.assertEqual(field.find(qn("w", "instrText")).text, ' HYPERLINK \\l "REF0001" ')

    def test_wps_citation_links_are_converted_to_internal_hyperlink_fields(self):
        document = ET.Element(qn("w", "document"))
        body = ET.SubElement(document, qn("w", "body"))
        paragraph = ET.SubElement(body, qn("w", "p"))
        citation = ET.SubElement(paragraph, qn("w", "hyperlink"), {
            qn("w", "anchor"): "REF0001",
        })
        run = ET.SubElement(citation, qn("w", "r"))
        props = ET.SubElement(run, qn("w", "rPr"))
        ET.SubElement(props, qn("w", "color"), {qn("w", "val"): "FF00FF"})
        ET.SubElement(run, qn("w", "t")).text = "2"
        other = ET.SubElement(paragraph, qn("w", "hyperlink"), {
            qn("w", "anchor"): "figure-one",
        })
        ET.SubElement(ET.SubElement(other, qn("w", "r")), qn("w", "t")).text = "图1"

        self.assertEqual(convert_citation_hyperlinks_to_fields_for_wps(document), 1)

        instructions = [node.text for node in paragraph.findall(".//" + qn("w", "instrText"))]
        self.assertEqual(instructions, [' HYPERLINK \\l "REF0001" '])
        field_types = [
            node.get(qn("w", "fldCharType"))
            for node in paragraph.findall(".//" + qn("w", "fldChar"))
        ]
        self.assertEqual(field_types, ["begin", "separate", "end"])
        self.assertEqual("".join(node.text or "" for node in paragraph.findall(".//" + qn("w", "t"))), "2图1")
        self.assertIsNotNone(paragraph.find(".//" + qn("w", "color")))
        remaining = paragraph.findall(".//" + qn("w", "hyperlink"))
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].get(qn("w", "anchor")), "figure-one")

    def test_citation_normalization_avoids_existing_names_and_leaves_other_links(self):
        document = ET.Element(qn("w", "document"))
        body = ET.SubElement(document, qn("w", "body"))
        ET.SubElement(body, qn("w", "bookmarkStart"), {
            qn("w", "id"): "1", qn("w", "name"): "REF0001",
        })
        target = ET.SubElement(body, qn("w", "bookmarkStart"), {
            qn("w", "id"): "2", qn("w", "name"): "ref-beta-key",
        })
        citation_link = ET.SubElement(body, qn("w", "hyperlink"), {
            qn("w", "anchor"): "ref-beta-key",
        })
        other_link = ET.SubElement(body, qn("w", "hyperlink"), {
            qn("w", "anchor"): "figure-one",
        })

        mapping = normalize_citation_bookmarks_for_wps(document)

        self.assertEqual(mapping, {"ref-beta-key": "REF0002"})
        self.assertEqual(target.get(qn("w", "name")), "REF0002")
        self.assertEqual(citation_link.get(qn("w", "anchor")), "REF0002")
        self.assertEqual(other_link.get(qn("w", "anchor")), "figure-one")

    def test_bibliography_label_separator_is_one_space_without_tab(self):
        body = ET.Element(qn("w", "body"))
        paragraph = ET.SubElement(body, qn("w", "p"))
        ppr = ET.SubElement(paragraph, qn("w", "pPr"))
        ET.SubElement(ppr, qn("w", "pStyle"), {qn("w", "val"): "Bibliography"})
        label = ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "t"))
        label.text = "[43]"
        whitespace = ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "t"))
        whitespace.text = " \t"
        tab_run = ET.SubElement(paragraph, qn("w", "r"))
        ET.SubElement(tab_run, qn("w", "tab"))
        content = ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "t"))
        content.text = "MARQUARDT D W. Article"

        self.assertEqual(normalize_bibliography_label_spacing(body), 1)

        self.assertEqual(label.text, "[43] ")
        self.assertEqual(whitespace.text, "")
        self.assertEqual(content.text, "MARQUARDT D W. Article")
        self.assertEqual(paragraph.findall(".//" + qn("w", "tab")), [])

    def test_bibliography_spacing_repair_does_not_touch_non_bibliography_tabs(self):
        body = ET.Element(qn("w", "body"))
        paragraph = self._paragraph(text="[1]")
        ET.SubElement(ET.SubElement(paragraph, qn("w", "r")), qn("w", "tab"))
        body.append(paragraph)

        self.assertEqual(normalize_bibliography_label_spacing(body), 0)
        self.assertEqual(len(paragraph.findall(".//" + qn("w", "tab"))), 1)


if __name__ == "__main__":
    unittest.main()
