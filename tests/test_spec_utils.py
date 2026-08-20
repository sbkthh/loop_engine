"""Tests for spec_utils.py — spec hashing and normalization."""

import hashlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spec_utils import (compute_spec_hash, compute_spec_norm_hash,
                        normalize_spec,
                        audit_plan_existing_evidence,
                        count_plan_existing_claims)


class TestNormalizeSpec(unittest.TestCase):
    def test_html_comment_stripped(self):
        a = normalize_spec("# T\n\n## S\n\n<!-- note -->\n1. x\n")
        b = normalize_spec("# T\n\n## S\n\n1. x\n")
        self.assertEqual(a, b)

    def test_multiline_html_comment_stripped(self):
        a = normalize_spec("# T\n<!--\nmulti\nline\n-->\n## S\n")
        b = normalize_spec("# T\n## S\n")
        self.assertEqual(a, b)

    def test_crlf_and_trailing_whitespace_normalized(self):
        a = normalize_spec("# T\r\n\r\n## S\r\n1. x  \r\n")
        b = normalize_spec("# T\n\n## S\n1. x\n")
        self.assertEqual(a, b)

    def test_surrounding_blank_lines_trimmed(self):
        a = normalize_spec("\n\n# T\n\n## S\n\n\n")
        b = normalize_spec("# T\n\n## S\n")
        self.assertEqual(a, b)

    def test_prose_change_not_normalized_away(self):
        self.assertNotEqual(normalize_spec("# T\n\n1. create item\n"),
                            normalize_spec("# T\n\n1. create item quickly\n"))


class TestSpecHashes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "spec.md")
        self.base = "# Test\n\n## Scenarios\n\n1. Create item\n"
        with open(self.path, "w") as f:
            f.write(self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_hash_changes_norm_hash_stable_on_comment(self):
        raw_before = compute_spec_hash(self.path)
        with open(self.path, "a") as f:
            f.write("\n<!-- review note -->\n")
        self.assertNotEqual(compute_spec_hash(self.path), raw_before)
        expected = hashlib.md5(
            normalize_spec(self.base).encode("utf-8")).hexdigest()
        self.assertEqual(compute_spec_norm_hash(self.path), expected)

    def test_substantive_change_alters_norm_hash(self):
        with open(self.path, "a") as f:
            f.write("\n## New Scenario\n")
        expected = hashlib.md5(
            normalize_spec(self.base).encode("utf-8")).hexdigest()
        self.assertNotEqual(compute_spec_norm_hash(self.path), expected)

    def test_missing_file_returns_none(self):
        missing = os.path.join(self.tmp.name, "nope.md")
        self.assertIsNone(compute_spec_hash(missing))
        self.assertIsNone(compute_spec_norm_hash(missing))


class TestPlanEvidenceAudit(unittest.TestCase):
    """'已有/无需变更' claims in plans must cite code file:line evidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.java = os.path.join(self.root, "Foo.java")
        with open(self.java, "w") as f:
            f.write("line1\nline2\nline3\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _plan(self, text):
        path = os.path.join(self.root, "plan.md")
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_valid_evidence_passes(self):
        path = self._plan("- C1: 已有，见 Foo.java:2\n")
        self.assertEqual(audit_plan_existing_evidence(path, self.root), [])

    def test_bare_existing_without_evidence_rejected(self):
        path = self._plan("- C2: 已有实现，无需变更\n")
        errs = audit_plan_existing_evidence(path, self.root)
        self.assertEqual(len(errs), 1)
        self.assertIn("without code evidence", errs[0])

    def test_spec_md_reference_not_evidence(self):
        path = self._plan("- C1: 已有，见 spec.md:111\n")
        errs = audit_plan_existing_evidence(path, self.root)
        self.assertEqual(len(errs), 1)
        self.assertIn("without code evidence", errs[0])

    def test_missing_file_rejected(self):
        path = self._plan("- C1: 已有，见 Nope.java:2\n")
        errs = audit_plan_existing_evidence(path, self.root)
        self.assertEqual(len(errs), 1)
        self.assertIn("evidence file not found", errs[0])

    def test_line_out_of_range_rejected(self):
        path = self._plan("- C1: 已有，见 Foo.java:99\n")
        errs = audit_plan_existing_evidence(path, self.root)
        self.assertEqual(len(errs), 1)
        self.assertIn("out of range", errs[0])

    def test_chinese_colon_and_L_notation_supported(self):
        path = self._plan(
            "- C1: 已有，见 Foo.java：2\n"
            "- C2: 无需变更，见 Foo.java L3\n")
        self.assertEqual(audit_plan_existing_evidence(path, self.root), [])

    def test_absolute_path_evidence(self):
        path = self._plan(f"- C1: 已有，见 {self.java}:3\n")
        self.assertEqual(audit_plan_existing_evidence(path, None), [])

    def test_no_existing_markers_no_errors(self):
        path = self._plan("- C1: 新增 PurchasePlanService.save()\n")
        self.assertEqual(audit_plan_existing_evidence(path, self.root), [])

    def test_count_existing_claims(self):
        path = self._plan(
            "- C1: 已有，见 Foo.java:2\n"
            "- C2: 无需变更，见 Foo.java:3\n"
            "- C3: 新增方法\n")
        self.assertEqual(count_plan_existing_claims(path), 2)

    def test_missing_plan_file(self):
        missing = os.path.join(self.root, "missing.md")
        errs = audit_plan_existing_evidence(missing)
        self.assertEqual(len(errs), 1)
        self.assertIn("plan file missing", errs[0])
        self.assertEqual(count_plan_existing_claims(missing), 0)


if __name__ == "__main__":
    unittest.main()
