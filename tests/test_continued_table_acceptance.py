import hashlib
import unittest
from io import BytesIO
from zipfile import ZipFile
from xml.sax.saxutils import escape

from PIL import Image, ImageDraw

from continued_table_acceptance import audit_continued_table

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

def paragraph(text):
    return f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"

def row(*cells, flag=None):
    prop = '' if flag is None else ('<w:trPr><w:tblHeader'+('' if flag=='' else f' w:val="{flag}"')+'/></w:trPr>')
    return "<w:tr>"+prop+"".join("<w:tc>"+paragraph(cell)+"</w:tc>" for cell in cells)+"</w:tr>"

def table(*rows):
    return "<w:tbl>"+"".join(rows)+"</w:tbl>"

def doc(*elements):
    memory = BytesIO()
    with ZipFile(memory, "w") as package:
        package.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{"".join(elements)}</w:body></w:document>')
    return memory.getvalue()

def digest(blob):
    return hashlib.sha256(blob).hexdigest()

def capture(color):
    buffer = BytesIO()
    image = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(image)
    for i in range(9):
        draw.line((60, 55 + i*22, 580, 55 + i*22), fill="black", width=2)
    for i in range(5):
        draw.line((60 + i*130, 55, 60 + i*130, 231), fill="black", width=2)
    image.save(buffer, format="PNG")
    return buffer.getvalue()

def native(blob, first, last, *, app="wps", before=True, after=True, table_index=1):
    return {"app": app, "candidate_sha256": digest(blob), "native_document_ref": "synthetic-app/reopen",
            "native_document_version": "synthetic-version", "table_ordinal": table_index,
            "review_ref": "synthetic:visual-review", "checks": {
                "same_logical_table": True, "header_repeated": True,
                "header_is_not_placeholder": True, "caption_on_continuation_page": True},
            "pages": [{"physical_page": 2, "capture_ref":"synthetic-page-2",
                       "capture_sha256": digest(first), "original_segment_visible": before},
                      {"physical_page": 3, "capture_ref":"synthetic-page-3",
                       "capture_sha256": digest(last), "continuation_segment_visible": after}]}

START = paragraph("表１－２  合成数据")
CONT = paragraph("续表1-2 合成数据")
HEADER = row("试验组", "数量")
DATA = row("甲组", "10")
RULES = doc(paragraph("合成学校规则：分开制作的续表必须逐页显示原表头。"))

def permission(candidate, table_index=2, *, rules=RULES, allowed=True):
    return {"allow_manual": allowed, "requirements_sha256": digest(rules),
            "candidate_sha256": digest(candidate), "table_ordinal": table_index,
            "clause_ref": "synthetic:C0066/E0081", "review_ref": "synthetic:school-rule-review"}

class Regression(unittest.TestCase):
    def test_true_top_contiguous_tags_do_not_claim_actual_pages(self):
        f = doc(START, table(row("试验组", "数量", flag=""), row("类型", "数值", flag="on"),
                           row("甲组", "10", flag="off"), row("乙组", "20", flag="true")))
        r = audit_continued_table(f, 1, target_app="wps", strategy="automatic")
        self.assertEqual(r["structural"]["top_repeating_row_count"], 2)
        self.assertEqual(r["structural"]["ignored_noncontiguous_flag_rows"], [4])
        self.assertEqual(r["status"], "pending_native_evidence")

    def test_false_first_and_true_later_not_a_repeating_header(self):
        f = doc(START, table(row("试验组", "数量", flag="0"), row("甲组", "10", flag="1")))
        r = audit_continued_table(f, 1, target_app="wps", strategy="automatic")
        self.assertEqual(r["structural"]["top_repeating_row_count"], 0)
        self.assertEqual(r["status"], "hold_structure")

    def test_manual_copy_without_school_permission_not_accepted(self):
        f = doc(START, table(HEADER, DATA), CONT, table(HEADER, DATA))
        r = audit_continued_table(f, 2, target_app="wps", strategy="manual")
        self.assertEqual(r["structural"]["first_row_relationship"], "copied_from_preceding_table")
        self.assertEqual(r["status"], "hold_school_rule")

    def test_placeholder_identical_rows_are_not_verified_headers(self):
        f = doc(START, table(row("××", "××"), DATA), CONT, table(row("××", "××"), DATA))
        r = audit_continued_table(f, 2, target_app="wps", strategy="manual", manual_permission=permission(f), school_rules=RULES)
        self.assertEqual(r["structural"]["first_row_relationship"], "placeholder_or_non_distinct")
        self.assertEqual(r["status"], "hold_structure")

    def test_manual_permission_requires_exact_candidate_table_and_rules(self):
        f = doc(START, table(HEADER, DATA), CONT, table(HEADER, DATA))
        for grant in (permission(doc()), permission(f, 1), permission(f, allowed=False), permission(f, rules=doc())):
            with self.subTest(grant=grant):
                r = audit_continued_table(f, 2, target_app="wps", strategy="manual",
                                          manual_permission=grant, school_rules=RULES)
                self.assertEqual(r["status"], "hold_school_rule")

    def test_caption_must_belong_to_same_numbered_previous_table(self):
        f = doc(START, table(HEADER, DATA), paragraph("续表1-3 合成数据"), table(HEADER, DATA))
        r = audit_continued_table(f, 2, target_app="wps", strategy="manual", manual_permission=permission(f), school_rules=RULES)
        self.assertEqual(r["structural"]["first_row_relationship"], "unbound")
        self.assertEqual(r["status"], "hold_structure")

    def test_second_explicit_continuation_remains_bound_to_original(self):
        f = doc(START, table(HEADER, DATA), CONT, table(HEADER, DATA), CONT, table(HEADER, DATA))
        r = audit_continued_table(f, 3, target_app="wps", strategy="manual", manual_permission=permission(f, 3), school_rules=RULES)
        self.assertEqual(r["structural"]["predecessor_table_ordinal"], 2)
        self.assertEqual(r["status"], "pending_native_evidence")

    def test_manual_distinct_same_verified_header_does_not_require_tblHeader(self):
        f = doc(START, table(HEADER, DATA), CONT, table(HEADER, DATA))
        first, last = capture("white"), capture("lightgray")
        r = audit_continued_table(f, 2, target_app="wps", strategy="manual", manual_permission=permission(f), school_rules=RULES,
                                   evidence=native(f, first, last, table_index=2), page_captures=[first,last])
        self.assertEqual(r["status"], "host_evidence_bound_unverified")
        self.assertFalse(r["release_verified"])
        self.assertFalse(r["native_observation_verified"])
        self.assertFalse(r["host_provenance_authenticated"])
        self.assertEqual(r["structural"]["top_repeating_row_count"], 0)

    def test_wrong_target_or_other_candidate_or_missing_capture_never_passes(self):
        f = doc(START, table(row("试验组", "数量", flag=""), DATA))
        first, last = capture("white"), capture("lightgray")
        kw = dict(strategy="automatic", target_app="wps", page_captures=[first,last])
        for e in ({"verified": True}, native(f, first, last, app="word"), native(doc(), first, last),
                  native(f, first, b"unbound"), native(f, first, last, after=False)):
            with self.subTest(e=e):
                r = audit_continued_table(f, 1, evidence=e, **kw)
                self.assertNotEqual(r["status"], "host_evidence_bound_unverified")

    def test_solid_pngs_with_forged_claims_do_not_bind(self):
        f = doc(START, table(row("试验组", "数量", flag="true"), DATA))
        captures = []
        for color in ("white", "lightgray"):
            b = BytesIO()
            Image.new("RGB", (640,480), color).save(b, format="PNG")
            captures.append(b.getvalue())
        r = audit_continued_table(f, 1, strategy="automatic", target_app="wps",
                                  evidence=native(f, *captures), page_captures=captures)
        self.assertEqual(r["status"], "hold_native_evidence")

    def test_duplicate_capture_bytes_cannot_stand_for_adjacent_pages(self):
        f = doc(START, table(row("试验组", "数量", flag="true"), DATA))
        data = capture("white")
        e = native(f, data, data)
        r = audit_continued_table(f, 1, strategy="automatic", target_app="wps", evidence=e, page_captures=[data,data])
        self.assertNotEqual(r["status"], "host_evidence_bound_unverified")

    def test_missing_caption_cannot_be_inferred_from_xml_flag(self):
        f = doc(table(row("试验组", "数量", flag="true"), DATA))
        r = audit_continued_table(f, 1, target_app="wps", strategy="automatic")
        self.assertEqual(r["status"], "hold_structure")

if __name__ == "__main__":
    unittest.main()
