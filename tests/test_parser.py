"""Tests for parser.py — all output format parsing (JSON only)."""

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
        text = ('{"status": "SUCCESS", '
                '"plan_path": "openspec/changes/cross-dock-v2/plans/dashboard-plan.md"}')
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['plan_path'],
                         'openspec/changes/cross-dock-v2/plans/dashboard-plan.md')
        self.assertEqual(result['mode'], 'step0')

    def test_step0_failed(self):
        text = '{"status": "FAILED"}'
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'FAILED')
        self.assertIsNone(result['plan_path'])


class TestParseMakerOutputStep1Red(unittest.TestCase):
    def test_step1_red_confirmed(self):
        text = ('{"status": "SUCCESS", '
                '"tdd_red_evidence": {"test_files_written": '
                '["/src/test/FooTest.java", "/src/test/BarTest.java"], '
                '"red_test_output": "Tests run: 2, Failures: 2, Errors: 0, Skipped: 0\\n'
                '[ERROR] FooTest.testCreate()", '
                '"red_confirmed": true, "tdd_skip": false}}')
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'step1_red')
        self.assertEqual(result['status'], 'SUCCESS')
        evidence = result['tdd_red_evidence']
        self.assertEqual(len(evidence['test_files_written']), 2)
        self.assertTrue(evidence['red_confirmed'])
        self.assertIsNotNone(evidence['red_test_output'])

    def test_step1_red_not_confirmed(self):
        text = ('{"status": "FAILED", '
                '"tdd_red_evidence": {"test_files_written": '
                '["/src/test/FooTest.java"], '
                '"red_test_output": "Tests run: 1, Failures: 0, Errors: 1", '
                '"red_confirmed": false, "tdd_skip": false}}')
        result = parse_maker_output(text)
        self.assertFalse(result['tdd_red_evidence']['red_confirmed'])

    def test_step1_red_tdd_skip(self):
        text = ('{"status": "SUCCESS", '
                '"tdd_red_evidence": {"test_files_written": [], '
                '"red_test_output": "Tests run: 19, Failures: 0, Errors: 0\\nBUILD SUCCESS", '
                '"red_confirmed": true, "tdd_skip": true}}')
        result = parse_maker_output(text)
        evidence = result['tdd_red_evidence']
        self.assertTrue(evidence['tdd_skip'])
        self.assertEqual(evidence['test_files_written'], [])


class TestParseMakerOutputStep2Green(unittest.TestCase):
    def test_step2_success(self):
        text = ('{"status": "SUCCESS", '
                '"files_created": ["/src/main/Foo.java", "/src/main/Bar.java"], '
                '"files_modified": ["/src/main/Baz.java"], '
                '"plan_path": "/abs/plan.md", '
                '"test_results": {"class_name": "FooTest", "total": 5, '
                '"passed": 5, "failed": 0, "errors": 0}, '
                '"blockers": "none", "human_decisions": 0}')
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
        text = ('{"status": "PARTIAL", '
                '"files_created": ["/src/main/Foo.java"], '
                '"files_modified": [], '
                '"plan_path": "/abs/plan.md", '
                '"test_results": {"class_name": "FooTest", "total": 5, '
                '"passed": 3, "failed": 2, "errors": 0}, '
                '"blockers": "some dependency issue", "human_decisions": 2}')
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['blockers'], 'some dependency issue')
        self.assertEqual(result['human_decisions'], 2)


class TestParseMakerOutputFixMode(unittest.TestCase):
    def test_fix_mode(self):
        text = ('{"status": "SUCCESS", '
                '"fixed_items": ["Missing field \'skuCode\' in Entity", '
                '"Method signature mismatch"], '
                '"remaining_items": [], '
                '"build_result": "BUILD SUCCESS"}')
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'fix')
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(len(result['fixed_items']), 2)
        self.assertEqual(len(result['remaining_items']), 0)
        self.assertEqual(result['build_result'], 'BUILD SUCCESS')

    def test_fix_mode_requires_build_result(self):
        text = ('{"status": "SUCCESS", '
                '"fixed_items": ["fixed something"], '
                '"remaining_items": []}')
        with self.assertRaises(ValueError) as ctx:
            parse_maker_output(text)
        self.assertIn('build_result', str(ctx.exception))


class TestParseCheckerOutput(unittest.TestCase):
    def test_consistent(self):
        text = ('{"status": "CONSISTENT", "discrepancy_count": 0, '
                '"hard_error_count": 0, "soft_warning_count": 0, '
                '"info_count": 0, "discrepancies": [], '
                '"test_results": {"class_name": "FooTest", "total": 5, '
                '"passed": 5, "failed": 0, "errors": 0}, '
                '"coverage": {"tested": 3, "total": 3}}')
        result = parse_checker_output(text)
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertEqual(result['hard_error_count'], 0)
        self.assertEqual(result['coverage']['tested'], 3)
        self.assertEqual(result['coverage']['total'], 3)

    def test_inconsistent(self):
        text = ('{"status": "INCONSISTENT", "discrepancy_count": 2, '
                '"hard_error_count": 1, "soft_warning_count": 1, '
                '"info_count": 0, '
                '"discrepancies": ['
                '{"severity": "HARD_ERROR", "type": "A", '
                '"description": "Entity missing field \'skuCode\' (spec L42) — Foo.java:88"}, '
                '{"severity": "SOFT_WARNING", "type": "B", '
                '"description": "Method signature mismatch — Bar.java:15"}'
                '], '
                '"test_results": {"class_name": "FooTest", "total": 5, '
                '"passed": 3, "failed": 2, "errors": 0}, '
                '"coverage": {"tested": 2, "total": 3}}')
        result = parse_checker_output(text)
        self.assertEqual(result['status'], 'INCONSISTENT')
        self.assertEqual(result['hard_error_count'], 1)
        self.assertEqual(result['soft_warning_count'], 1)
        self.assertEqual(len(result['discrepancies']), 2)
        self.assertEqual(result['discrepancies'][0]['severity'], 'HARD_ERROR')
        self.assertEqual(result['discrepancies'][0]['type'], 'A')
        self.assertEqual(result['coverage']['tested'], 2)

    def test_discrepancy_with_hyphenated_types(self):
        text = ('{"status": "CONSISTENT", "discrepancy_count": 4, '
                '"hard_error_count": 0, "soft_warning_count": 1, '
                '"info_count": 3, '
                '"discrepancies": ['
                '{"severity": "SOFT_WARNING", "type": "scenario-coverage", '
                '"description": "Summary 场景\\"日期格式错误\\"无对应测试方法"}, '
                '{"severity": "INFO", "type": "spec-prose", '
                '"description": "spec.md:30 数据源残留"}, '
                '{"severity": "INFO", "type": "field-mapping", '
                '"description": "crossFlag 列位置"}, '
                '{"severity": "INFO", "type": "plan-drift", '
                '"description": "plan 未更新"}'
                ']}')
        result = parse_checker_output(text)
        self.assertEqual(len(result['discrepancies']), 4)
        first = result['discrepancies'][0]
        self.assertEqual(first['severity'], 'SOFT_WARNING')
        self.assertEqual(first['type'], 'scenario-coverage')
        self.assertEqual(result['soft_warning_count'], 1)
        self.assertEqual(result['info_count'], 3)

    def test_discrepancy_with_multiword_type(self):
        text = ('{"status": "INCONSISTENT", "discrepancy_count": 2, '
                '"hard_error_count": 0, "soft_warning_count": 1, '
                '"info_count": 1, '
                '"discrepancies": ['
                '{"severity": "SOFT_WARNING", "type": "test-coverage / plan deviation", '
                '"description": "Plan TDD table T7 mandates a direct test, '
                'implementation is correct"}, '
                '{"severity": "INFO", "type": "dual-write nuance", '
                '"description": "update() writes only new fields"}'
                ']}')
        result = parse_checker_output(text)
        self.assertEqual(len(result['discrepancies']), 2)
        soft = result['discrepancies'][0]
        self.assertEqual(soft['severity'], 'SOFT_WARNING')
        self.assertEqual(soft['type'], 'test-coverage / plan deviation')
        self.assertEqual(soft['description'],
                         'Plan TDD table T7 mandates a direct test, '
                         'implementation is correct')


class TestParseScore(unittest.TestCase):
    def test_pass(self):
        result = parse_score('{"score": 92, "cross_consistency": "PASS"}')
        self.assertEqual(result['score'], 92)
        self.assertEqual(result['cross_consistency'], 'PASS')

    def test_fail(self):
        result = parse_score('{"score": 75, "cross_consistency": "FAIL"}')
        self.assertEqual(result['score'], 75)
        self.assertEqual(result['cross_consistency'], 'FAIL')


class TestParseClassifyChange(unittest.TestCase):
    def test_lightweight(self):
        result = parse_classify_change('{"magnitude": "轻量", "reason": "typo fix"}')
        self.assertEqual(result['magnitude'], '轻量')

    def test_heavy(self):
        result = parse_classify_change('{"magnitude": "重量"}')
        self.assertEqual(result['magnitude'], '重量')


class TestParseCodeReview(unittest.TestCase):
    def test_json_format(self):
        text = ('{"issues": [{"severity": "critical", '
                '"text": "null pointer risk in line 42"}, '
                '{"severity": "important", '
                '"text": "missing test for edge case"}, '
                '{"severity": "minor", '
                '"text": "naming convention"}]}')
        result = parse_code_review(text)
        self.assertEqual(result['critical'], 1)
        self.assertEqual(result['important'], 1)
        self.assertEqual(result['minor'], 1)

    def test_no_issues(self):
        result = parse_code_review('{"issues": []}')
        self.assertEqual(result['critical'], 0)
        self.assertEqual(result['issues'], [])


class TestParseDispatcher(unittest.TestCase):
    def test_dispatch_score(self):
        result = parse('{"score": 90}', "SCORE")
        self.assertEqual(result['score'], 90)

    def test_dispatch_checker(self):
        text = ('{"status": "CONSISTENT", "discrepancy_count": 0, '
                '"hard_error_count": 0, "soft_warning_count": 0, '
                '"info_count": 0, "discrepancies": []}')
        result = parse(text, "CHECKER")
        self.assertEqual(result['status'], 'CONSISTENT')


class TestJsonOutput(unittest.TestCase):
    def test_maker_step0_json(self):
        text = '{"status": "SUCCESS", "plan_path": "/abs/plan.md"}'
        result = parse_maker_output(text)
        self.assertEqual(result['status'], 'SUCCESS')
        self.assertEqual(result['plan_path'], '/abs/plan.md')
        self.assertEqual(result['mode'], 'step0')

    def test_maker_step1_red_json(self):
        text = ('{"status": "SUCCESS", "plan_path": "/abs/plan.md", '
                '"tdd_red_evidence": {"test_files_written": ["/t/FooTest.java"], '
                '"red_test_output": "Tests run: 2, Failures: 2", '
                '"red_confirmed": true, "tdd_skip": false}}')
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'step1_red')
        ev = result['tdd_red_evidence']
        self.assertEqual(ev['test_files_written'], ['/t/FooTest.java'])
        self.assertTrue(ev['red_confirmed'])
        self.assertFalse(ev['tdd_skip'])

    def test_maker_step2_green_json(self):
        text = ('{"status": "SUCCESS", "plan_path": "/abs/plan.md", '
                '"files_created": ["/s/Foo.java"], "files_modified": [], '
                '"test_results": {"class_name": "FooTest", "total": 7, '
                '"passed": 7, "failed": 0, "errors": 0}, '
                '"blockers": "none", "human_decisions": 0}')
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'step2_green')
        self.assertEqual(result['files_created'], ['/s/Foo.java'])
        self.assertEqual(result['test_results']['passed'], 7)
        self.assertIsNone(result['blockers'])

    def test_maker_fix_json(self):
        text = ('{"status": "SUCCESS", "fixed_items": ["fixed a"], '
                '"remaining_items": [], '
                '"build_result": "BUILD SUCCESS"}')
        result = parse_maker_output(text)
        self.assertEqual(result['mode'], 'fix')
        self.assertEqual(result['fixed_items'], ['fixed a'])
        self.assertEqual(result['build_result'], 'BUILD SUCCESS')

    def test_json_inside_markdown_fence(self):
        text = ('Some reasoning text.\n\n```json\n'
                '{"score": 92, "cross_consistency": "PASS", '
                '"dimensions": {"scenario_coverage": "strong"}}\n```\n')
        result = parse_score(text)
        self.assertEqual(result['score'], 92)
        self.assertEqual(result['cross_consistency'], 'PASS')

    def test_score_json_validation(self):
        with self.assertRaises(ValueError) as ctx:
            parse_score('{"cross_consistency": "PASS"}')
        self.assertIn('Output format error', str(ctx.exception))
        self.assertIn('score', str(ctx.exception))

    def test_classify_json(self):
        result = parse_classify_change('{"magnitude": "重量", "reason": "new API"}')
        self.assertEqual(result['magnitude'], '重量')

    def test_classify_json_invalid_magnitude(self):
        with self.assertRaises(ValueError) as ctx:
            parse_classify_change('{"magnitude": "medium"}')
        self.assertIn('Output format error', str(ctx.exception))

    def test_checker_json_counts_must_match(self):
        bad = ('{"status": "INCONSISTENT", "discrepancy_count": 2, '
               '"hard_error_count": 1, "soft_warning_count": 0, '
               '"info_count": 0, '
               '"discrepancies": [{"severity": "HARD_ERROR", "type": "t", '
               '"description": "a"}]}')
        with self.assertRaises(ValueError) as ctx:
            parse_checker_output(bad)
        self.assertIn('Output format error', str(ctx.exception))
        self.assertIn('discrepancy_count', str(ctx.exception))

    def test_checker_json_invalid_severity(self):
        bad = ('{"status": "INCONSISTENT", "discrepancy_count": 1, '
               '"hard_error_count": 0, "soft_warning_count": 0, '
               '"info_count": 1, '
               '"discrepancies": [{"severity": "WARN", "type": "t", '
               '"description": "a"}]}')
        with self.assertRaises(ValueError) as ctx:
            parse_checker_output(bad)
        self.assertIn('Output format error', str(ctx.exception))
        self.assertIn('severity', str(ctx.exception))

    def test_checker_json_full(self):
        text = ('{"status": "INCONSISTENT", "discrepancy_count": 1, '
                '"hard_error_count": 1, "soft_warning_count": 0, '
                '"info_count": 0, '
                '"discrepancies": [{"severity": "HARD_ERROR", '
                '"type": "test-coverage", '
                '"description": "src/Foo.java:42 missing method"}], '
                '"test_results": {"class_name": "FooTest", "total": 7, '
                '"passed": 7, "failed": 0, "errors": 0}, '
                '"coverage": {"tested": 5, "total": 6}}')
        result = parse_checker_output(text)
        self.assertEqual(result['status'], 'INCONSISTENT')
        self.assertEqual(result['hard_error_count'], 1)
        self.assertEqual(result['discrepancies'][0]['severity'], 'HARD_ERROR')
        self.assertEqual(result['discrepancies'][0]['description'],
                         'src/Foo.java:42 missing method')
        self.assertEqual(result['test_results']['passed'], 7)
        self.assertEqual(result['coverage'], {'tested': 5, 'total': 6})

    def test_checker_json_zero_discrepancies(self):
        text = ('{"status": "CONSISTENT", "discrepancy_count": 0, '
                '"hard_error_count": 0, "soft_warning_count": 0, '
                '"info_count": 0, "discrepancies": []}')
        result = parse_checker_output(text)
        self.assertEqual(result['status'], 'CONSISTENT')
        self.assertEqual(result['discrepancies'], [])
        self.assertEqual(result['soft_warning_count'], 0)

    def test_review_json(self):
        text = ('{"issues": [{"severity": "critical", '
                '"text": "src/A.java:1 bug"}, '
                '{"severity": "minor", "text": "src/B.java:2 nit"}]}')
        result = parse_code_review(text)
        self.assertEqual(result['critical'], 1)
        self.assertEqual(result['minor'], 1)
        self.assertEqual(result['important'], 0)
        self.assertEqual(result['issues'][0]['text'], 'src/A.java:1 bug')

    def test_review_json_empty(self):
        result = parse_code_review('{"issues": []}')
        self.assertEqual(result['critical'], 0)
        self.assertEqual(result['issues'], [])

    def test_review_json_invalid_severity(self):
        with self.assertRaises(ValueError) as ctx:
            parse_code_review('{"issues": [{"severity": "major", "text": "x"}]}')
        self.assertIn('Output format error', str(ctx.exception))

    def test_legacy_text_returns_none(self):
        """Old text blocks now return None (format support removed)."""
        text = ("---CHECKER_OUTPUT---\nSTATUS: INCONSISTENT\n"
                "DISCREPANCY_COUNT: 1\nHARD_ERROR_COUNT: 1\n"
                "SOFT_WARNING_COUNT: 0\nINFO_COUNT: 0\n"
                "DISCREPANCIES:\n  1. [HARD_ERROR] [test] missing\n"
                "---END_CHECKER_OUTPUT---")
        self.assertIsNone(parse_checker_output(text))

    def test_garbage_returns_none(self):
        """Non-JSON garbage returns None from all parsers."""
        self.assertIsNone(parse_maker_output("whatever"))
        self.assertIsNone(parse_score("whatever"))
        self.assertIsNone(parse_classify_change("whatever"))
        self.assertIsNone(parse_code_review("whatever"))


if __name__ == '__main__':
    unittest.main()