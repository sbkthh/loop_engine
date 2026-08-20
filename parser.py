"""Parser: extract structured data from agent output blocks in result.md.

Agents write JSON objects. Format violations raise ValueError with an
"Output format error: " prefix, which the runner turns into an in-place
repair (resume the same LLM session to rewrite result.md) instead of a
full step replay.
"""

import json
import re


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
    if isinstance(tr, list):
        if not tr:
            raise _format_error("'test_results' array is empty")
        totals = {"total": 0, "passed": 0, "failed": 0, "errors": 0}
        for item in tr:
            _require_fields(item, "class_name", "total", "passed", "failed")
            for k in totals:
                v = item.get(k, 0) or 0
                totals[k] += v if isinstance(v, (int, float)) else 0
        return {"class_name": f"{len(tr)} test suites", **totals}
    if not isinstance(tr, dict):
        raise _format_error("'test_results' must be an object or array")
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
        result["gap_audit"] = data.get("gap_audit")
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
            "cross_consistency": data.get("cross_consistency"),
            "dimensions": data.get("dimensions")}


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


def parse_maker_output(text):
    data = _parse_json_output(text)
    if data is None:
        return None
    return _maker_from_json(data)


def parse_checker_output(text):
    data = _parse_json_output(text)
    if data is None:
        return None
    return _checker_from_json(data)


def parse_score(text):
    data = _parse_json_output(text)
    if data is None:
        return None
    return _score_from_json(data)


def parse_classify_change(text):
    data = _parse_json_output(text)
    if data is None:
        return None
    return _classify_from_json(data)


def parse_code_review(text):
    data = _parse_json_output(text)
    if data is None:
        return None
    return _review_from_json(data)


def parse_align_docs(text):
    data = _parse_json_output(text)
    if data is None:
        return None
    _require_fields(data, "status", "updated_files")
    if data["status"] not in ("SUCCESS", "FAILED"):
        raise _format_error(f"status must be SUCCESS or FAILED (got {data['status']!r})")
    return {
        "status": data["status"],
        "updated_files": data.get("updated_files", []),
        "alignment_report": data.get("alignment_report"),
    }


def parse(text, action):
    from constants import (
        SCORE, CLASSIFY_CHANGE, MAKER_STEP0, MAKER_STEP1_RED,
        MAKER_STEP2_GREEN, MAKER_FIX, CHECKER, CODE_REVIEW, CODE_REVIEW_FIX,
        ALIGN_DOCS,
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
    if action == ALIGN_DOCS:
        return parse_align_docs(text)
    raise ValueError(f"Unknown action: {action}")