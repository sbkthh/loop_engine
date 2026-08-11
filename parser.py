"""Parser: extract structured data from agent output blocks in result.md."""

import re

from constants import (
    MAKER_OUTPUT_START, MAKER_OUTPUT_END,
    CHECKER_OUTPUT_START, CHECKER_OUTPUT_END,
)


def _extract_block(text, start_delim, end_delim):
    pattern = re.escape(start_delim) + r'\s*\n(.*?)\n\s*' + re.escape(end_delim)
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1) if m else None


def _extract_field(block, key):
    m = re.search(rf'^{key}:\s*(.+)$', block, re.MULTILINE)
    return m.group(1).strip() if m else None


def _extract_list(block, key):
    pattern = rf'{key}:\s*\n((?:\s+-\s+.+\n?)+)'
    m = re.search(pattern, block)
    if not m:
        return []
    items = re.findall(r'-\s+(.+)', m.group(1))
    return [item.strip() for item in items]


def _extract_int(block, key):
    m = re.search(rf'{key}:\s*(\d+|none)', block, re.IGNORECASE)
    if not m:
        return None
    val = m.group(1).lower()
    return 0 if val == 'none' else int(val)


def _extract_bool(block, key):
    m = re.search(rf'{key}:\s*(true|false)', block, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).lower() == 'true'


def _parse_test_results(block):
    return {
        'class_name': _extract_field(block, r'class') or _extract_field(block, 'class'),
        'total': _extract_int(block, 'total'),
        'passed': _extract_int(block, 'passed'),
        'failed': _extract_int(block, 'failed'),
        'errors': _extract_int(block, 'errors'),
    }


def _parse_tdd_red_evidence(block):
    evidence_match = re.search(
        r'TDD_RED_EVIDENCE:\s*\n((?:[ \t]+.*\n?|\n)+)', block
    )
    if not evidence_match:
        return None
    evidence = evidence_match.group(1)
    files = re.findall(r'-\s+(.+)', evidence)
    output_match = re.search(
        r'red_test_output:\s*\|?\s*\n((?:[ \t]+.*\n?|\n)+?)(?=  \w+:|\Z)',
        evidence, re.MULTILINE
    )
    red_output = output_match.group(1).strip() if output_match else None
    return {
        'test_files_written': [f.strip() for f in files],
        'red_test_output': red_output,
        'red_confirmed': _extract_bool(evidence, 'red_confirmed'),
        'tdd_skip': _extract_bool(evidence, 'tdd_skip'),
    }


def parse_maker_output(text):
    block = _extract_block(text, MAKER_OUTPUT_START, MAKER_OUTPUT_END)
    if not block:
        return None

    result = {
        'status': _extract_field(block, 'STATUS'),
        'plan_path': _extract_field(block, 'PLAN_PATH'),
        'mode': None,
    }

    if 'TDD_RED_EVIDENCE' in block:
        result['mode'] = 'step1_red'
        result['tdd_red_evidence'] = _parse_tdd_red_evidence(block)
    elif 'FIXED_ITEMS' in block:
        result['mode'] = 'fix'
        result['fixed_items'] = _extract_list(block, 'FIXED_ITEMS')
        result['remaining_items'] = _extract_list(block, 'REMAINING_ITEMS')
        result['test_results'] = _parse_test_results(block)
    elif 'FILES_CREATED' in block:
        result['mode'] = 'step2_green'
        result['files_created'] = _extract_list(block, 'FILES_CREATED')
        result['files_modified'] = _extract_list(block, 'FILES_MODIFIED')
        result['test_results'] = _parse_test_results(block)
        blockers = _extract_field(block, 'BLOCKERS')
        result['blockers'] = None if blockers and blockers.lower() == 'none' else blockers
        result['human_decisions'] = _extract_int(block, 'HUMAN_DECISIONS') or 0
    elif result['plan_path']:
        result['mode'] = 'step0'

    return result


def parse_checker_output(text):
    block = _extract_block(text, CHECKER_OUTPUT_START, CHECKER_OUTPUT_END)
    if not block:
        return None

    discrepancies = re.findall(
        r'\d+\.\s*\[([\w-]+)\]\s*\[([\w-]+)\]\s*(.+)', block
    )

    coverage_match = re.search(r'(\d+)/(\d+)\s*Scenarios', block)

    return {
        'status': _extract_field(block, 'STATUS'),
        'discrepancy_count': _extract_int(block, 'DISCREPANCY_COUNT'),
        'hard_error_count': _extract_int(block, 'HARD_ERROR_COUNT'),
        'soft_warning_count': _extract_int(block, 'SOFT_WARNING_COUNT'),
        'info_count': _extract_int(block, 'INFO_COUNT'),
        'discrepancies': [
            {'severity': s, 'type': t, 'description': d.strip()}
            for s, t, d in discrepancies
        ],
        'test_results': _parse_test_results(block),
        'coverage': {
            'tested': int(coverage_match.group(1)) if coverage_match else None,
            'total': int(coverage_match.group(2)) if coverage_match else None,
        },
    }


def parse_score(text):
    score_match = re.search(r'SCORE:\s*(\d+)/100', text)
    cross_match = re.search(
        r'(?:cross[_-]consistency|交叉一致性):\s*(PASS|FAIL)',
        text, re.IGNORECASE
    )
    return {
        'score': int(score_match.group(1)) if score_match else None,
        'cross_consistency': cross_match.group(1).upper() if cross_match else None,
    }


def parse_classify_change(text):
    m = re.search(r'CHANGE_MAGNITUDE:\s*(轻量|重量)', text)
    return {'magnitude': m.group(1) if m else None}


_REVIEW_ISSUE_RE = re.compile(
    r'^\s*(?:[-*+]\s+|\d+[\.\)]\s*)?\*{0,2}\[?(Critical|Important|Minor)\]?\*{0,2}',
    re.MULTILINE | re.IGNORECASE,
)


def parse_code_review(text):
    # Tolerant of LLM format drift: [Important] / **Important** / - Important: / 1. **Important:**
    # Lines like "**No Critical issues found.**" or "**2 Important issues**" do not match
    # because the severity keyword must start the line after optional bullets/markup.
    issues = []
    for m in _REVIEW_ISSUE_RE.finditer(text):
        issues.append({
            "severity": m.group(1).lower(),
            "text": text[m.start():].splitlines()[0].strip(),
        })
    return {
        'critical': sum(1 for i in issues if i['severity'] == 'critical'),
        'important': sum(1 for i in issues if i['severity'] == 'important'),
        'minor': sum(1 for i in issues if i['severity'] == 'minor'),
        'issues': issues,
    }


def parse(text, action):
    from constants import (
        SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
        MAKER_STEP2_GREEN, MAKER_FIX, CHECKER, CODE_REVIEW, CODE_REVIEW_FIX,
    )
    maker_actions = (MAKER_STEP0, MAKER_STEP1_RED, MAKER_STEP2_GREEN,
                     MAKER_FIX, CODE_REVIEW_FIX)
    if action in maker_actions:
        return parse_maker_output(text)
    if action == CHECKER:
        return parse_checker_output(text)
    if action == SCORE:
        return parse_score(text)
    if action == CLASSIFY_CHANGE:
        return parse_classify_change(text)
    if action == CODE_REVIEW:
        return parse_code_review(text)
    raise ValueError(f"Unknown action: {action}")
