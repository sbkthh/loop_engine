"""Tests for parser.py — all output format parsing."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parser import (
    parse_maker_output, parse_checker_output,
    parse_score, parse_classify_change, parse_code_review, parse,
)


class TestParseMakerOutputStep0(unittest.TestCase):
    def test_step0_success(self):
        text = """Some preamble text.

---MAKER_OUTPUT---
STATUS: SUCCESS
PLAN_PATH: openspec/changes/cross-dock-v2/plans/dashboard-plan.md
---END_MAKER_OUTPUT---
"""
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['plan_path'], 'openspec/changes/cross-dock-v2/plans/dashboard-plan.md')
        self.assertEqual(result['mode'], 'step0')

    def test_step0_failed(self):
        text = "---MAKER_OUTPUT---\nSTATUS: FAILED\n---END_MAKER_OUTPUT---"
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIsNone(result['plan_path'])


class TestParseMakerOutputStep1Red(unittest.TestCase):
    def test_step1_red_confirmed(self):
        text = """---MAKER_OUTPUT---
STATUS: SUCCESS
TDD_RED_EVIDENCE:
  test_files_written:
    - /src/test/FooTest.java
    - /src/test/BarTest.java
  red_test_output: |
    Tests run: 2, Failures: 2, Errors: 0, Skipped: 0
    [ERROR] FooTest.testCreate()
  red_confirmed: true
---END_MAKER_OUTPUT---
"""
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'step1_red')
        self.assertEqual(result['status'], 'SUCCESS')
        evidence = result['tdd_red_evidence']
        self.assertEqual(len(evidence['test_files_written']), 2)
        self.assertTrue(evidence['red_confirmed'])
        self.assertIsNotNone(evidence['red_test_output'])

    def test_step1_red_not_confirmed(self):
        text = """---MAKER_OUTPUT---
STATUS: FAILED
TDD_RED_EVIDENCE:
  test_files_written:
    - /src/test/FooTest.java
  red_test_output: |
    Tests run: 1, Failures: 0, Errors: 1
  red_confirmed: false
---END_MAKER_OUTPUT---
"""
        result = parse_maker_output(text)
        self.assertFalse(result['tdd_red_evidence']['red_confirmed'])


class TestParseMakerOutputStep2Green(unittest.TestCase):
    def test_step2_success(self):
        text = """---MAKER_OUTPUT---
STATUS: SUCCESS
FILES_CREATED:
  - /src/main/Foo.java
  - /src/main/Bar.java
FILES_MODIFIED:
  - /src/main/Baz.java
PLAN_PATH: /abs/plan.md
TEST_RESULTS:
  class: FooTest
  total: 5
  passed: 5
  failed: 0
BLOCKERS: none
HUMAN_DECISIONS: 0
---END_MAKER_OUTPUT---
"""
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'step2_green')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(len(result['files_created']), 2)
        self.assertEqual(len(result['files_modified']), 1)
        self.assertEqual(result['test_results']['total'], 5)
        self.assertEqual(result['test_results']['passed'], 5)
        self.assertIsNone(result['blockers'])
        self.assertEqual(result['human_decisions'], 0)

    def test_step2_partial(self):
        text = """---MAKER_OUTPUT---
STATUS: PARTIAL
FILES_CREATED:
  - /src/main/Foo.java
FILES_MODIFIED:
PLAN_PATH: /abs/plan.md
TEST_RESULTS:
  class: FooTest
  total: 5
  passed: 3
  failed: 2
BLOCKERS: some dependency issue
HUMAN_DECISIONS: 2
---END_MAKER_OUTPUT---
"""
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['blockers'], 'some dependency issue')
        self.assertEqual(result['human_decisions'], 2)


class TestParseMakerOutputFixMode(unittest.TestCase):
    def test_fix_mode(self):
        text = """---MAKER_OUTPUT---
STATUS: SUCCESS
FIXED_ITEMS:
  - Missing field 'skuCode' in Entity
  - Method signature mismatch
REMAINING_ITEMS:
TEST_RESULTS:
  class: FooTest
  total: 5
  passed: 5
  failed: 0
---END_MAKER_OUTPUT---
"""
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'fix')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(len(result['fixed_items']), 2)
        self.assertEqual(len(result['remaining_items']), 0)


class TestParseCheckerOutput(unittest.TestCase):
    def test_consistent(self):
        text = """---CHECKER_OUTPUT---
STATUS: CONSISTENT
DISCREPANCY_COUNT: 0
HARD_ERROR_COUNT: 0
SOFT_WARNING_COUNT: 0
INFO_COUNT: 0
DISCREPANCIES:
TEST_RESULTS:
  class: FooTest
  total: 5
  passed: 5
  failed: 0
  errors: 0
COVERAGE: 3/3 Scenarios have test methods
---END_CHECKER_OUTPUT---
"""
        result = parse_checker_output(text)
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertEqual(result['hard_error_count'], 0)
        self.assertEqual(result['coverage']['tested'], 3)
        self.assertEqual(result['coverage']['total'], 3)

    def test_inconsistent(self):
        text = """---CHECKER_OUTPUT---
STATUS: INCONSISTENT
DISCREPANCY_COUNT: 2
HARD_ERROR_COUNT: 1
SOFT_WARNING_COUNT: 1
INFO_COUNT: 0
DISCREPANCIES:
  1. [HARD_ERROR] [A] Entity missing field 'skuCode' (spec L42) — Foo.java:88
  2. [SOFT_WARNING] [B] Method signature mismatch — Bar.java:15
TEST_RESULTS:
  class: FooTest
  total: 5
  passed: 3
  failed: 2
  errors: 0
COVERAGE: 2/3 Scenarios have test methods
---END_CHECKER_OUTPUT---
"""
        result = parse_checker_output(text)
        self.assertEqual(result['status'], 'INCONSISTENT')
        self.assertEqual(result['hard_error_count'], 1)
        self.assertEqual(result['soft_warning_count'], 1)
        self.assertEqual(len(result['discrepancies']), 2)
        self.assertEqual(result['discrepancies'][0]['severity'], 'HARD_ERROR')
        self.assertEqual(result['discrepancies'][0]['type'], 'A')
        self.assertEqual(result['coverage']['tested'], 2)


class TestParseScore(unittest.TestCase):
    def test_pass(self):
        text = "SCORE: 92/100\ncross-consistency: PASS"
        result = parse_score(text)
        self.assertEqual(result['score'], 92)
        self.assertEqual(result['cross_consistency'], 'PASS')

    def test_fail(self):
        text = "SCORE: 75/100\ncross-consistency: FAIL: orphan field in Scenario"
        result = parse_score(text)
        self.assertEqual(result['score'], 75)
        self.assertEqual(result['cross_consistency'], 'FAIL')


class TestParseClassifyChange(unittest.TestCase):
    def test_lightweight(self):
        result = parse_classify_change("CHANGE_MAGNITUDE: 轻量\nreason: typo fix")
        self.assertEqual(result['magnitude'], '轻量')

    def test_heavy(self):
        result = parse_classify_change("CHANGE_MAGNITUDE: 重量")
        self.assertEqual(result['magnitude'], '重量')


class TestParseCodeReview(unittest.TestCase):
    def test_with_critical(self):
        text = "Review findings:\nCritical: null pointer risk in line 42\nImportant: missing test for edge case\nMinor: naming convention"
        result = parse_code_review(text)
        self.assertGreater(result['critical'], 0)
        self.assertGreater(result['important'], 0)
        self.assertGreater(result['minor'], 0)

    def test_no_issues(self):
        text = "Code looks good. Minor style suggestions only."
        result = parse_code_review(text)
        self.assertEqual(result['critical'], 0)

    def test_markdown_bold_format(self):
        text = (
            "## CODE_REVIEW\n\n"
            "### 1. Logic Bugs\n\n"
            "**Important** — `Foo.java:12` — exception swallowed\n\n"
            "**Important** — `Foo.java:20` — tx still commits\n\n"
            "### 3. Architecture\n\n"
            "**Minor** — `Bar.java:5` — POST vs GET\n\n"
            "### 6. Summary\n\n"
            "**No Critical issues found.**\n"
            "**2 Important issues** (both in `Foo.java`).\n"
        )
        result = parse_code_review(text)
        self.assertEqual(result['critical'], 0)
        self.assertEqual(result['important'], 2)
        self.assertEqual(result['minor'], 1)
        self.assertEqual(len(result['issues']), 3)
        self.assertEqual(result['issues'][0]['severity'], 'important')

    def test_bullet_and_numbered_formats(self):
        text = (
            "- [Important] Foo.java:3 — bullet format\n"
            "1. **Minor:** Bar.java:7 — numbered bold\n"
            "- Critical: Baz.java:9 — plain bullet\n"
        )
        result = parse_code_review(text)
        self.assertEqual(result['critical'], 1)
        self.assertEqual(result['important'], 1)
        self.assertEqual(result['minor'], 1)


class TestParseDispatcher(unittest.TestCase):
    def test_dispatch_score(self):
        result = parse("SCORE: 90/100", "SCORE")
        self.assertEqual(result['score'], 90)

    def test_dispatch_checker(self):
        text = "---CHECKER_OUTPUT---\nSTATUS: CONSISTENT\n---END_CHECKER_OUTPUT---"
        result = parse(text, "CHECKER")
        self.assertEqual(result['status'], 'CONSISTENT')


if __name__ == '__main__':
    unittest.main()
