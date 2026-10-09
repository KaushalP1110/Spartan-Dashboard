"""Turns a finished run into a common summary the dashboard can show:

    {"overall_status": "PASS" | "FAIL" | "ERROR", "passed": int, "total": int,
     "sections": [{"title", "passed", "total"}], "failures": [{"section", "name", "reason"}],
     "error": str | None, "drive_url": str | None}

Each job's "result" block in jobs.json picks a parser. A result file is only
trusted when it was written after the run started, so a stale file from an
earlier run is never shown as this run's result.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional


def _empty(status: str, error: Optional[str] = None) -> dict:
    return {"overall_status": status, "passed": None, "total": None, "sections": [], "failures": [],
            "error": error, "drive_url": None}


def _fresh(path: Path, started_ts: float) -> bool:
    try:
        return path.is_file() and path.stat().st_mtime >= started_ts - 1
    except OSError:
        return False


def newest_match(cwd: Path, pattern: str, started_ts: Optional[float] = None) -> Optional[Path]:
    matches = [p for p in cwd.glob(pattern) if p.is_file()]
    if started_ts is not None:
        matches = [p for p in matches if _fresh(p, started_ts)]
    return max(matches, key=lambda p: p.stat().st_mtime, default=None)


def _status_from_counts(passed: int, total: int, exit_code: int) -> str:
    if total == 0:
        return "PASS" if exit_code == 0 else "ERROR"
    return "PASS" if passed == total and exit_code == 0 else "FAIL"


def parse_summary_json(cfg: dict, cwd: Path, summary_path: Path, exit_code: int, started_ts: float) -> dict:
    """Reminder Sanity's own summary (runner writes it when the dashboard
    sets REMINDER_SANITY_SUMMARY_PATH)."""
    if not _fresh(summary_path, started_ts):
        return _empty("ERROR", "Run ended before writing its result (see log)")
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _empty("ERROR", f"Unreadable result file: {exc}")
    status = data.get("overall_status") or "ERROR"
    return {
        "overall_status": status if status in ("PASS", "FAIL") else "ERROR",
        "passed": data.get("passed"), "total": data.get("total"),
        "sections": [{"title": s["title"], "passed": s["passed"], "total": s["total"]} for s in data.get("sections", [])],
        "failures": [{"section": f.get("section", ""), "name": f.get("patient", ""), "reason": f.get("reason", "")}
                     for f in data.get("failures", [])],
        "error": data.get("error"), "drive_url": data.get("drive_url"),
    }


def parse_recall_results(cfg: dict, cwd: Path, summary_path: Path, exit_code: int, started_ts: float) -> dict:
    """Recall-Status-Automation's recall_status_results_<stamp>.json."""
    path = newest_match(cwd, cfg["glob"], started_ts)
    if path is None:
        return _empty("ERROR", "Run ended before writing its results (see log)")
    try:
        scenarios = json.loads(path.read_text(encoding="utf-8")).get("scenarios", [])
    except (OSError, ValueError) as exc:
        return _empty("ERROR", f"Unreadable result file: {exc}")
    passed = sum(1 for s in scenarios if s.get("passed"))
    failures = [{
        "section": s.get("id", ""),
        "name": f"{s.get('name', '')} ({s.get('first_name', '')} {s.get('last_name', '')})".strip(),
        "reason": s.get("error") or f"Expected {' + '.join(s.get('expected_status') or [])}, "
                                    f"Adit showed {' + '.join(s.get('actual_status') or []) or 'nothing'}",
    } for s in scenarios if not s.get("passed")]
    summary = _empty(_status_from_counts(passed, len(scenarios), exit_code))
    summary.update(passed=passed, total=len(scenarios), failures=failures,
                   sections=[{"title": "Recall scenarios", "passed": passed, "total": len(scenarios)}])
    return summary


def parse_testng(cfg: dict, cwd: Path, summary_path: Path, exit_code: int, started_ts: float) -> dict:
    """TestNG's testng-results.xml (Maven Surefire writes it)."""
    path = cwd / cfg["path"]
    if not _fresh(path, started_ts):
        return _empty("ERROR", "No TestNG results from this run - build/compile failure? (see log)")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return _empty("ERROR", f"Unreadable TestNG results: {exc}")

    sections, failures, passed_total, total = [], [], 0, 0
    for test in root.iter("test"):
        methods = [m for m in test.iter("test-method") if m.get("is-config") != "true"]
        if not methods:
            continue
        passed = sum(1 for m in methods if m.get("status") == "PASS")
        sections.append({"title": test.get("name", "Tests"), "passed": passed, "total": len(methods)})
        passed_total += passed
        total += len(methods)
        for m in methods:
            if m.get("status") in ("FAIL", "SKIP"):
                message = m.findtext("exception/message") or m.findtext("exception/full-stacktrace") or ""
                failures.append({
                    "section": test.get("name", ""),
                    "name": m.get("name", "") + (" (skipped)" if m.get("status") == "SKIP" else ""),
                    "reason": " ".join(message.split())[:400],
                })
    # A single default "Surefire test" section adds nothing over the totals.
    if len(sections) == 1 and sections[0]["title"].startswith("Surefire"):
        sections[0]["title"] = "Tests"
    summary = _empty(_status_from_counts(passed_total, total, exit_code))
    summary.update(passed=passed_total, total=total, sections=sections, failures=failures)
    return summary


def parse_exit_code(cfg: dict, cwd: Path, summary_path: Path, exit_code: int, started_ts: float) -> dict:
    return _empty("PASS" if exit_code == 0 else "FAIL",
                  None if exit_code == 0 else f"Exited with code {exit_code} (see log and report)")


PARSERS = {
    "summary_json": parse_summary_json,
    "recall_results": parse_recall_results,
    "testng": parse_testng,
    "exit_code": parse_exit_code,
}


def parse(cfg: dict, cwd: Path, summary_path: Path, exit_code: int, started_ts: float) -> dict:
    parser = PARSERS.get(cfg.get("type", "exit_code"), parse_exit_code)
    try:
        return parser(cfg, cwd, summary_path, exit_code, started_ts)
    except Exception as exc:  # noqa: BLE001 - a parser bug must not lose the run
        return _empty("ERROR", f"Could not read results: {exc}")
