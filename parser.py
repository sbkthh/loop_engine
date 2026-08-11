"""Parser: extract structured data from agent output blocks in result.md.

Agents write JSON objects (JSON-first); legacy text blocks are still
parsed as a fallback so in-flight runs and historical archives keep working.
Format violations raise ValueError with an "Output format error: " prefix,
which the runner turns into an in-place repair (resume the same LLM session
to rewrite result.md) instead of a full step replay.
"""

import json
import re

from constants import (
    MAKER_OUTPUT_START, MAKER_OUTPUT_END,
    CHECKER_OUTPUT_START, CHECKER_OUTPUT_END,
)


def _format_error(detail):
    return ValueError(f"Output format error: {detail}")


def _parse_json_output(text):
    """Parse a JSON object from agent output. Tolerates markdown code
    fences and surrounding prose; returns None when no JSON object found."""
    if not text:
        return None
    candidates = []
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.append(stripped)
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m and m.group(0) not in candidates:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _require_fields(data, *fields):
    missing = [f for f in fields if f not in data]
    if missing:
        raise _format_error(f"missing field(s): {', '.join(missing)}")


def _test_results_from_json(tr):
    if not isinstance(tr, dict):
        raise _format_error("'test_results' must be an object")
    _require_fields(tr, "class_name", "total", "passed", "failed")
    return {
        "class_name": tr.get("class_name"),
        "total": tr.get("total"),
        "passed": tr.get("passed"),
        "failed": tr.get("failed"),
        "errors": tr.get("errors"),
    }


def _maker_from_json(data):
    _require_fields(data, "status")
    result = {
        "status": data.get("status"),
        "plan_path": data.get("plan_path"),
        "mode": None,
    }
    if "tdd_red_evidence" in data:
        result["mode"] = "step1_red"
        ev = data["tdd_red_evidence"]
        if not isinstance(ev, dict):
            raise _format_error("'tdd_red_evidence' must be an object")
        _require_fields(ev, "tdd_skip", "red_confirmed")
        result["tdd_red_evidence"] = {
            "test_files_written": ev.get("test_files_written") or [],
            "red_test_output": ev.get("red_test_output"),
            "red_confirmed": ev.get("red_confirmed"),
            "tdd_skip": ev.get("tdd_skip"),
        }
    elif "fixed_items" in data:
        result["mode"] = "fix"
        result["fixed_items"] = data.get("fixed_items") or []
        result["remaining_items"] = data.get("remaining_items") or []
        if "test_results" not in data:
            raise _format_error("missing field(s): test_results")
        result["test_results"] = _test_results_from_json(data["test_results"])
    elif "files_created" in data:
        result["mode"] = "step2_green"
        result["files_created"] = data.get("files_created") or []
        result["files_modified"] = data.get("files_modified") or []
        if "test_results" not in data:
            raise _format_error("missing field(s): test_results")
        result["test_results"] = _test_results_from_json(data["test_results"])
        blockers = data.get("blockers")
        result["blockers"] = None if blockers in (None, "none") else blockers
        result["human_decisions"] = data.get("human_decisions") or 0
    elif data.get("plan_path"):
        result["mode"] = "step0"
    return result


def _checker_from_json(data):
    _require_fields(data, "status", "discrepancy_count", "hard_error_count",
                    "soft_warning_count", "info_count", "discrepancies")
    discrepancies = data["discrepancies"]
    if not isinstance(discrepancies, list):
        raise _format_error("'discrepancies' must be a list")
    parsed = []
    for i, d in enumerate(discrepancies):
        if not isinstance(d, dict):
            raise _format_error(f"discrepancies[{i}] must be an object")
        _require_fields(d, "severity", "type", "description")
        sev = d.get("severity")
        if sev not in ("HARD_ERROR", "SOFT_WARNING", "INFO"):
            raise _format_error(
                f"discrepancies[{i}] severity must be one of "
                f"HARD_ERROR/SOFT_WARNING/INFO (got {sev!r})")
        parsed.append({"severity": sev, "type": d.get("type"),
                       "description": d.get("description")})
    counts = {
        "HARD_ERROR": sum(1 for d in parsed if d["severity"] == "HARD_ERROR"),
        "SOFT_WARNING": sum(1 for d in parsed if d["severity"] == "SOFT_WARNING"),
        "INFO": sum(1 for d in parsed if d["severity"] == "INFO"),
    }
    if data["discrepancy_count"] != len(parsed):
        raise _format_error(
            f"discrepancy_count {data['discrepancy_count']} != "
            f"{len(parsed)} items")
    for field, sev in (("hard_error_count", "HARD_ERROR"),
                       ("soft_warning_count", "SOFT_WARNING"),
                       ("info_count", "INFO")):
        if data[field] != counts[sev]:
            raise _format_error(
                f"{field} {data[field]} != {counts[sev]} {sev} items")
    result = {
        "status": data.get("status"),
        "discrepancy_count": data["discrepancy_count"],
        "hard_error_count": data["hard_error_count"],
        "soft_warning_count": data["soft_warning_count"],
        "info_count": data["info_count"],
        "discrepancies": parsed,
        "test_results": None,
        "coverage": {"tested": None, "total": None},
    }
    if data.get("test_results") is not None:
        result["test_results"] = _test_results_from_json(data["test_results"])
    coverage = data.get("coverage")
    if isinstance(coverage, dict):
        result["coverage"] = {
            "tested": coverage.get("tested"),
            "total": coverage.get("total"),
        }
    return result


def _score_from_json(data):
    _require_fields(data, "score")
    score = data.get("score")
    if not isinstance(score, int) or isinstance(score, bool):
        raise _format_error("'score' must be an integer")
    return {"score": score,
            "cross_consistency": data.get("cross_consistency")}


def _classify_from_json(data):
    _require_fields(data, "magnitude")
    mag = data.get("magnitude")
    if mag not in ("轻量", "重量"):
        raise _format_error(
            f"'magnitude' must be 轻量 or 重量 (got {mag!r})")
    return {"magnitude": mag}


def _review_from_json(data):
    _require_fields(data, "issues")
    issues = data["issues"]
    if not isinstance(issues, list):
        raise _format_error("'issues' must be a list")
    parsed = []
    for i, d in enumerate(issues):
        if not isinstance(d, dict):
            raise _format_error(f"issues[{i}] must be an object")
        _require_fields(d, "severity", "text")
        sev = d.get("severity")
        if sev not in ("critical", "important", "minor"):
            raise _format_error(
                f"issues[{i}] severity must be one of "
                f"critical/important/minor (got {sev!r})")
        parsed.append({"severity": sev, "text": d.get("text")})
    return {
        "critical": sum(1 for i in parsed if i["severity"] == "critical"),
        "important": sum(1 for i in parsed if i["severity"] == "important"),
        "minor": sum(1 for i in parsed if i["severity"] == "minor"),
        "issues": parsed,
    }


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


_LIST_RESULT_RE = re.compile(
    r'^\s*-\s*([^:]+):\s*total=(\d+),\s*passed=(\d+),\s*failed=(\d+)',
    re.MULTILINE)


def _parse_test_results(block):
    result = {
        'class_name': _extract_field(block, r'class') or _extract_field(block, 'class'),
        'total': _extract_int(block, 'total'),
        'passed': _extract_int(block, 'passed'),
        'failed': _extract_int(block, 'failed'),
        'errors': _extract_int(block, 'errors'),
    }
    # tolerate list-style rows: "- FooTest: total=7, passed=7, failed=0"
    rows = _LIST_RESULT_RE.findall(block)
    if rows and result['total'] is None:
        result['class_name'] = rows[0][0].strip()
        result['total'] = sum(int(t) for _, t, _, _ in rows)
        result['passed'] = sum(int(p) for _, _, p, _ in rows)
        result['failed'] = sum(int(f) for _, _, _, f in rows)
    return result


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
    data = _parse_json_output(text)
    if data is not None:
        return _maker_from_json(data)
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
    data = _parse_json_output(text)
    if data is not None:
        return _checker_from_json(data)
    block = _extract_block(text, CHECKER_OUTPUT_START, CHECKER_OUTPUT_END)
    if not block:
        return None

    discrepancies = re.findall(
        r'\d+\.\s*\[([\w-]+)\]\s*\[([^\]\n]+)\]\s*(.*)', block
    )

    coverage_match = re.search(r'(\d+)/(\d+)\s*Scenarios', block)

    return {
        'status': _extract_field(block, 'STATUS'),
        'discrepancy_count': _extract_int(block, 'DISCREPANCY_COUNT'),
        'hard_error_count': _extract_int(block, 'HARD_ERROR_COUNT'),
        'soft_warning_count': _extract_int(block, 'SOFT_WARNING_COUNT'),
        'info_count': _extract_int(block, 'INFO_COUNT'),
        'discrepancies': [
            {'severity': s, 'type': t,
             'description': (d.strip() or t).strip()}
            for s, t, d in discrepancies
        ],
        'test_results': _parse_test_results(block),
        'coverage': {
            'tested': int(coverage_match.group(1)) if coverage_match else None,
            'total': int(coverage_match.group(2)) if coverage_match else None,
        },
    }


def parse_score(text):
    data = _parse_json_output(text)
    if data is not None:
        return _score_from_json(data)
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
    data = _parse_json_output(text)
    if data is not None:
        return _classify_from_json(data)
    m = re.search(r'CHANGE_MAGNITUDE:\s*(轻量|重量)', text)
    return {'magnitude': m.group(1) if m else None}


_REVIEW_ISSUE_RE = re.compile(
    r'^\s*(?:[-*+]\s+|\d+[\.\)]\s*)?\*{0,2}\[?(Critical|Important|Minor)\]?\*{0,2}',
    re.MULTILINE | re.IGNORECASE,
)


def parse_code_review(text):
    data = _parse_json_output(text)
    if data is not None:
        return _review_from_json(data)
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
