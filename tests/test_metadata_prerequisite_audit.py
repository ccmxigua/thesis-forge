from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_audit():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            "classify_metadata_prerequisites",
            ROOT / "scripts" / "classify_metadata_prerequisites.py",
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


class MetadataPrerequisiteAuditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.audit = load_audit()

    def test_standalone_label_is_metadata_safe_candidate(self):
        category, reason, labels, fields = self.audit.classify("学位论文题目")
        self.assertEqual(category, "metadata_safe_candidate")
        self.assertEqual(labels, ["学位论文题目"])
        self.assertIn("title_zh", fields)

    def test_compound_cover_labels_are_not_auto_satisfied_as_one_field(self):
        category, reason, labels, fields = self.audit.classify("论文作者 指导教师")
        self.assertEqual(category, "metadata_compound_candidate")
        self.assertEqual(labels, ["论文作者", "指导教师"])

    def test_tju_three_field_cover_row_is_an_exact_metadata_composite(self):
        category, reason, labels, fields = self.audit.classify("学号 中文论文题目 姓 名")
        self.assertEqual(category, "metadata_compound_candidate")
        self.assertEqual(reason, "exact_composite_all_fields_require_supplied_metadata")
        self.assertEqual(labels, ["学号", "中文论文题目", "姓名"])

        planner = importlib.import_module("capability_planner")
        clause = {"id": "C-tju-cover", "text": "学号 中文论文题目 姓 名"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C-tju-cover", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause], metadata={
                "student_id": "S1", "title_zh": "题目", "author": "作者",
            })
        item = next(row for row in result["clauses"] if row["clause_id"] == "C-tju-cover")
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["metadata_fields"], ["student_id", "title_zh", "author"])

    def test_tju_three_field_cover_row_requires_all_fields(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C-tju-cover", "text": "学号 中文论文题目 姓 名"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C-tju-cover", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause], metadata={"student_id": "S1", "author": "作者"})
        item = next(row for row in result["clauses"] if row["clause_id"] == "C-tju-cover")
        self.assertEqual(item["category"], "input_prerequisite")

    def test_school_name_in_prose_is_not_fixed_value(self):
        category, reason, labels, fields = self.audit.classify(
            "本人声明所呈交的学位论文不包含其他个人成果，也不包含为获得北京科技大学学位使用过的材料"
        )
        self.assertNotEqual(category, "school_fixed_value_candidate")

    def test_explicit_label_value_is_a_replacement_candidate_not_current_metadata(self):
        for text, label, field in (
            ("作者姓名：李化润", "作者姓名", "author"),
            ("导师姓名：姓名 职称", "导师姓名", "advisor"),
            ("答辩日期：2023年05月30日", "答辩日期", "defense_date"),
            ("分类号：TD235", "分类号", "classification_number"),
        ):
            with self.subTest(text=text):
                category, reason, labels, fields = self.audit.classify(text)
                self.assertEqual(category, "metadata_embedded_candidate")
                self.assertEqual(labels, [label])
                self.assertIn(field, fields)
                self.assertIn("supplied_metadata", reason)

    def test_embedded_example_value_does_not_become_metadata(self):
        category, reason, labels, fields = self.audit.classify("作者姓名：李化润")
        self.assertEqual(category, "metadata_embedded_candidate")
        self.assertNotIn("李化润", fields)

    def test_pure_layout_instructions_are_non_metadata(self):
        for text in (
            "表中参数应标明量和单位的符号",
            "篇幅以一页为限",
            "注意：请保证此页为奇数页",
            "自行确定是否使用三级标题",
            "数字和字母应为Times New Roman体",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.audit.classify(text)[0], "non_metadata_misclassification_candidate")

    def test_template_instruction_with_metadata_words_is_non_metadata(self):
        for text in (
            "论文题目即可",
            "注：学位论文作者、期刊名称加黑",
            "填写内容是论文的中文题目",
            "封面包括学科专业、指导教师等信息",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.audit.classify(text)[0], "non_metadata_misclassification_candidate")

    def test_composite_candidate_does_not_treat_template_values_as_metadata(self):
        category, reason, labels, fields = self.audit.classify("论文作者 指导教师")
        self.assertEqual(category, "metadata_compound_candidate")
        self.assertNotIn("测试学生", fields)

    def test_explicit_school_code_and_confidentiality_are_candidates(self):
        self.assertEqual(self.audit.classify("学校代码")[0], "school_fixed_value_candidate")
        self.assertEqual(self.audit.classify("密级：公开")[0], "school_fixed_value_candidate")

    def test_training_unit_address_label_is_not_an_institution_value(self):
        category, reason, labels, fields = self.audit.classify("培养单位地址")
        self.assertNotEqual(category, "school_fixed_value_candidate")

    def test_standalone_source_content_label_is_satisfied_by_explicit_metadata(self):
        planner = importlib.import_module("capability_planner")
        clause = {
            "id": "C-title",
            "text": "论文题目",
            "source_text_full": "论文题目：",
            "evidence_ids": ["E-title"],
        }
        spec = {
            "requirements": [],
            "clause_compliance": [{
                "clause_id": "C-title",
                "requirement_ids": [],
                "evidence_ids": ["E-title"],
                "scope": "docx",
                "status": "requires_source_content",
            }],
        }
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause],
            metadata={"title_zh": "测试论文题目"},
        )
        item = next(row for row in result["clauses"] if row["clause_id"] == "C-title")
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["disposition"], "supported")
        self.assertEqual(item["metadata_fields"], ["title_zh"])

    def test_pipeline_accepts_metadata_satisfied_source_content_clause(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            pipeline = importlib.import_module("thesis_format_pipeline")
        finally:
            sys.path.pop(0)
        blockers = pipeline.requirement_blockers(
            {"completeness": {"unresolved_clause_ids": ["C-title"]},
             "clause_compliance": [{"clause_id": "C-title", "status": "requires_source_content"}]},
            [], "supported_subset", {"C-title"})
        self.assertEqual(blockers, [])

    def test_exact_author_advisor_composite_is_satisfied(self):
        planner = importlib.import_module("capability_planner")
        clause = {
            "id": "C00023",
            "text": "论文作者 指导教师",
            "source_text_full": "论文作者 指导教师",
        }
        spec = {
            "requirements": [],
            "clause_compliance": [{
                "clause_id": "C00023",
                "requirement_ids": [],
                "evidence_ids": [],
                "scope": "docx",
                "status": "requires_metadata",
            }],
        }
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause],
            metadata={"author": "测试学生甲", "advisor": "测试导师乙 教授"},
        )
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00023")
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["disposition"], "supported")
        self.assertEqual(item["metadata_fields"], ["author", "advisor"])

    def test_author_advisor_composite_requires_both_values(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C00023", "text": "论文作者 指导教师"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C00023", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause], metadata={"author": "测试学生甲"})
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00023")
        self.assertEqual(item["category"], "input_prerequisite")
        self.assertEqual(item["disposition"], "unknown")

    def test_blank_or_whitespace_metadata_does_not_satisfy_composite(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C00023", "text": "论文作者 指导教师"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C00023", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause],
            metadata={"author": " ", "advisor": "测试导师乙 教授"})
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00023")
        self.assertEqual(item["category"], "input_prerequisite")

    def test_exact_degree_institution_composite_is_satisfied(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C00024", "text": "申请学位 培养单位"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C00024", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause], metadata={
                "degree_type": "工学硕士学位论文",
                "college": "智能交通与数据科学学院",
            })
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00024")
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["disposition"], "supported")
        self.assertEqual(item["metadata_fields"], ["degree_type", "college"])

    def test_degree_institution_composite_requires_both_field_groups(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C00024", "text": "申请学位 培养单位"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C00024", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause], metadata={"degree_type": "工学硕士学位论文"})
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00024")
        self.assertEqual(item["category"], "input_prerequisite")
        self.assertEqual(item["disposition"], "unknown")

    def test_exact_discipline_composite_is_satisfied_only_by_independent_fields(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C00025", "text": "一级学科 二级学科"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C00025", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause], metadata={
                "first_discipline": "交通运输工程",
                "second_discipline": "交通信息工程及控制",
            })
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00025")
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["metadata_fields"], ["first_discipline", "second_discipline"])

    def test_discipline_composite_does_not_fallback_to_major(self):
        planner = importlib.import_module("capability_planner")
        clause = {"id": "C00025", "text": "一级学科 二级学科"}
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": "C00025", "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_metadata",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[clause],
            metadata={"major": "交通信息工程及控制", "discipline": "交通信息工程及控制"})
        item = next(row for row in result["clauses"] if row["clause_id"] == "C00025")
        self.assertEqual(item["category"], "input_prerequisite")
        self.assertEqual(item["disposition"], "unknown")

    def _plan_clause(self, clause_id, text, metadata, template_fixed_values=None):
        planner = importlib.import_module("capability_planner")
        spec = {"requirements": [], "clause_compliance": [{
            "clause_id": clause_id, "requirement_ids": [], "evidence_ids": [],
            "scope": "docx", "status": "requires_source_content",
        }]}
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            spec, registry, extracted_clauses=[{"id": clause_id, "text": text}],
            metadata=metadata,
            template_fixed_values=template_fixed_values,
        )
        return next(row for row in result["clauses"] if row["clause_id"] == clause_id)

    def test_embedded_college_value_is_satisfied_by_source_metadata(self):
        item = self._plan_clause(
            "C00010", "所 在 学 院: 经济学院",
            {"college": "智能交通与数据科学学院"},
        )
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["metadata_fields"], ["college"])

    def test_embedded_training_unit_value_is_satisfied_by_source_metadata(self):
        item = self._plan_clause(
            "C00027", "培 养 单 位: 北京工商大学经济学院",
            {"school": "智能交通与数据科学学院"},
        )
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["metadata_fields"], ["school"])

    def test_training_unit_address_is_not_mistaken_for_embedded_unit_value(self):
        item = self._plan_clause(
            "C00329", "培养单位地址",
            {"school": "智能交通与数据科学学院"},
        )
        self.assertEqual(item["category"], "input_prerequisite")

    def test_explicit_confidentiality_value_requires_exact_metadata_match(self):
        supported = self._plan_clause(
            "C00012", "密 级： 公开", {"confidentiality_level": "公开"})
        blocked = self._plan_clause(
            "C00012", "密 级： 公开", {"confidentiality_level": "秘密"})
        self.assertEqual(supported["category"], "supported")
        self.assertEqual(supported["metadata_fields"], ["confidentiality_level"])
        self.assertEqual(blocked["category"], "input_prerequisite")

    def test_confidentiality_options_require_an_explicit_listed_choice(self):
        supported = self._plan_clause(
            "C00006", "密级：公开□ 内部1年□",
            {"confidentiality_level": "内部1年"},
        )
        blocked = self._plan_clause(
            "C00006", "密级：公开□ 内部1年□",
            {"confidentiality_level": "秘密"},
        )
        self.assertEqual(supported["category"], "supported")
        self.assertEqual(blocked["category"], "input_prerequisite")

    def test_school_code_is_satisfied_only_by_explicit_template_contract(self):
        item = self._plan_clause(
            "C00002", "学校代码", {}, {"school_code": "10043"})
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["template_fixed_fields"], ["template_fixed.school_code"])

    def test_school_code_is_satisfied_from_template_profile_fixed_values(self):
        planner = importlib.import_module("capability_planner")
        registry = json.loads((ROOT / "resources/backend-capabilities.default.json").read_text())
        result = planner.plan_capabilities(
            {"requirements": [], "clause_compliance": [{
                "clause_id": "C-school", "requirement_ids": [], "evidence_ids": [],
                "scope": "docx", "status": "requires_metadata"}]},
            registry, extracted_clauses=[{"id": "C-school", "text": "学校代码"}],
            template_profile={"fixed_values": {"school_code": "10224"}},
        )
        item = result["clauses"][0]
        self.assertEqual(item["category"], "supported")

    def test_school_code_without_selected_template_contract_remains_blocked(self):
        item = self._plan_clause("C00002", "学校代码", {})
        self.assertEqual(item["category"], "input_prerequisite")

    def test_embedded_school_code_must_match_selected_template_contract(self):
        supported = self._plan_clause(
            "C00001", "中图分类号 学校代码 10224", {}, {"school_code": "10224"})
        mismatched = self._plan_clause(
            "C00001", "中图分类号 学校代码 10224", {}, {"school_code": "10043"})
        self.assertEqual(supported["category"], "supported")
        self.assertEqual(mismatched["category"], "input_prerequisite")

    def test_non_school_code_clause_cannot_use_school_code_contract(self):
        item = self._plan_clause(
            "C00329", "培养单位地址", {}, {"school_code": "10008"})
        self.assertEqual(item["category"], "input_prerequisite")

    def test_conservative_alias_labels_use_existing_metadata_fields(self):
        metadata = {
            "title_zh": "题目", "student_id": "S1", "research_direction": "方向",
            "college": "学院", "major": "专业", "author": "作者",
            "advisor": "导师", "confidentiality_level": "公开",
            "classification_number": "U491.1",
        }
        for text in (
            "论文题目（中文）", "学位申请人学号", "研究方向（领域）",
            "学院（部、研究院）", "学科、专业", "作者姓名*", "学号*",
            "导师姓名*", "密级*", "中图分类号*", "培养单位名称*",
            "学科专业*", "研究方向*", "申请密级",
        ):
            with self.subTest(text=text):
                item = self._plan_clause("alias", text, metadata)
                self.assertEqual(item["category"], "supported")

    def test_standalone_college_alias_is_satisfied_by_existing_metadata(self):
        item = self._plan_clause(
            "C-college", "所在学院", {"college": "智能交通与数据科学学院"})
        self.assertEqual(item["category"], "supported")
        self.assertEqual(item["metadata_fields"], ["college"])

    def test_v10_reconciliation_uses_school_and_clause_context(self):
        cases = (
            ("btbu", "C00061", "首先我想感谢的是我的导师张三教授",
             "non_metadata_misclassification_candidate"),
            ("bsu", "C00176", "4 研究过程与分析（根据实际情况填写）",
             "requires_source_content"),
            ("szu", "C00034", "学科门类", "metadata_safe_candidate"),
        )
        for school, clause_id, text, expected in cases:
            with self.subTest(school=school, clause_id=clause_id):
                self.assertEqual(
                    self.audit.classify(text, school=school, clause_id=clause_id)[0], expected)

    def test_v10_reused_fields_satisfy_enterprise_unit_code_and_completion_date(self):
        cases = (
            ("企业导师", {"co_supervisors": [{"name": "企业导师", "kind": "enterprise"}]},
             "co_supervisors.enterprise"),
            ("培养单位代码*", {"unit_code": "10008"}, "unit_code"),
            ("填写论文定稿时间", {"completion_date": "2026-06"}, "completion_date"),
        )
        for text, metadata, expected_field in cases:
            with self.subTest(text=text):
                item = self._plan_clause("reuse", text, metadata)
                self.assertEqual(item["category"], "supported")
                self.assertEqual(item["metadata_fields"], [expected_field])

    def test_v10_formal_metadata_labels_require_nonempty_values(self):
        cases = (
            ("保密期限", {"confidentiality_period": {"start": "2026-01", "end": "2028-01"}}),
            ("学位论文作者毕业后去向", {"author_post_graduation_destination": "继续深造"}),
            ("专业技术职称", {"defense_committee": [{"name": "委员", "professional_title": "教授",
                                                     "institution": "某大学"}]}),
            ("学科门类", {"discipline_category": "工学"}),
            ("培养单位地址", {"unit_address": "权威配置地址"}),
        )
        for text, metadata in cases:
            with self.subTest(text=text):
                self.assertEqual(self._plan_clause("formal", text, metadata)["category"], "supported")
                self.assertEqual(self._plan_clause("formal", text, {})["category"], "input_prerequisite")


if __name__ == "__main__":
    unittest.main()
