import unittest

from scripts.profile_copilot import apply_response, build_request, serializable_profile


class ProfileCopilotTest(unittest.TestCase):
    def setUp(self):
        self.profile = {"profile_id": "test-profile", "structure": {"ordered_roles": [{
            "role": "body", "required": True,
            "selector": {"kind": "paragraph", "text": "1 引言", "match": "exact",
                         "accepted_texts": ["第一章 引言"],
                         "body_child_index": 10, "section_index": 2},
        }]}}
        self.audit = {"failures": [{
            "code": "template_required_role_selector_not_unique", "role": "body", "actual": 0,
            "selector": self.profile["structure"]["ordered_roles"][0]["selector"],
            "selector_diagnostics": {"top_score_margin": 0.4, "candidates": [{
                "candidate_selector": {"kind": "paragraph", "text": "1 引言", "match": "exact",
                                       "style_id": "Heading1", "body_child_index": 14, "section_index": 2},
            }]},
        }]}

    def test_request_forbids_invented_candidates_and_marks_index_volatile(self):
        request = build_request(self.profile, self.audit)
        self.assertEqual(request["task"], "repair_template_profile_selectors")
        self.assertEqual(request["tasks"][0]["role"], "body")
        self.assertNotIn("body_child_index", request["tasks"][0]["default_keep_fields"])

    def test_response_can_drop_volatile_body_index(self):
        response = {"contract_version": "1.0", "repairs": [{
            "role": "body", "unresolved": False, "candidate_index": 0,
            "keep_fields": ["kind", "text", "match", "section_index"],
            "reason": "absolute index changed after Word save", "confidence": 0.99,
        }]}
        staged, decisions = apply_response(self.profile, self.audit, response)
        selector = staged["structure"]["ordered_roles"][0]["selector"]
        self.assertNotIn("body_child_index", selector)
        self.assertEqual(selector["section_index"], 2)
        self.assertEqual(selector["accepted_texts"], ["第一章 引言"])
        self.assertTrue(decisions[0]["accepted"])

    def test_response_cannot_invent_candidate(self):
        response = {"contract_version": "1.0", "repairs": [{
            "role": "body", "unresolved": False, "candidate_index": 9,
            "keep_fields": ["kind", "text"], "reason": "invented",
        }]}
        _, decisions = apply_response(self.profile, self.audit, response)
        self.assertFalse(decisions[0]["accepted"])

    def test_runtime_resource_fields_are_not_serialized(self):
        profile = {"resources": [{"path": "resources/template.docx", "resolved_path": "/tmp/template.docx"}]}
        self.assertEqual(serializable_profile(profile)["resources"][0], {"path": "resources/template.docx"})
        self.assertEqual(serializable_profile(profile, absolute_resources=True)["resources"][0],
                         {"path": "/tmp/template.docx"})


if __name__ == "__main__":
    unittest.main()
