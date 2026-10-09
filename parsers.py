"""Turns a finished run into a common summary the dashboard can show:

    {"overall_status": "PASS" | "FAIL" | "ERROR", "passed": int, "total": int,
     "sections": [{"title", "passed", "total"}], "failures": [{"section", "name", "reason"}],
     "error": str | None, "drive_url": str | None}

Each job's "result" block in jobs.json picks a parser. A result file is only
trusted when it was written after the run started, so a stale file from an
earlier run is never shown as this run's result.
"""

from __future__ import annotations

import html
import json
import re
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


def _text(fragment: str) -> str:
    """HTML fragment -> plain text."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def parse_reminder_sanity(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
    """Reminder Sanity, read from what a normal run already produces (the
    project itself is not changed for the dashboard):

    - per-section counts: the stat tiles at the top of its HTML report
      (same counting as the report and the Chat card)
    - failures: the report's FAIL rows (tables) and FAIL row cards
    - overall: the "Overall: PASS|FAIL" line of its console report (the log)
    - Drive link: the file id it logs after uploading the PDF
    """
    log = ""
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    report = newest_match(cwd, cfg["glob"], started_ts)
    if report is None:
        failed = re.findall(r"Reminder Sanity run failed: (.+)", log)
        return _empty("ERROR", failed[-1].strip() if failed else "Run ended before writing its report (see log)")
    try:
        page = report.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _empty("ERROR", f"Unreadable report: {exc}")

    sections = [{"title": _text(title), "passed": int(passed), "total": int(total)} for title, passed, total in re.findall(
        r'<div class="stat-label">(.*?)</div><div class="stat-value">(\d+)/(\d+)', page)]

    failures = []
    for card in page.split('<div class="card">')[1:]:
        heading = re.search(r"<h2>(.*?)</h2>", card, re.S)
        section = _text(heading.group(1)).split(" \u2014 ")[0] if heading else ""
        for row in re.findall(r'<tr class="fail">(.*?)</tr>', card, re.S):
            cells = [_text(c) for c in re.findall(r"<td>(.*?)</td>", row, re.S)]
            if len(cells) >= 4:
                failures.append({"section": section, "name": cells[1], "reason": cells[3]})
        for row in re.findall(r'<div class="row fail">(.*?)<div class="proof">(.*?)</div>', card, re.S):
            title = re.search(r'<div class="row-title">(.*?)</div>', row[0], re.S)
            failures.append({"section": section, "name": _text(title.group(1)) if title else "", "reason": _text(row[1])})

    overall = re.findall(r"^Overall: (PASS|FAIL)\s*$", log, re.M)
    passed, total = sum(s["passed"] for s in sections), sum(s["total"] for s in sections)
    if overall:
        status = overall[-1]
    else:  # no console summary captured - fall back to the exit code
        status = "PASS" if exit_code == 0 else "FAIL"
    drive_ids = re.findall(r"Google Drive \(id=([\w-]+)\)|new revision \(id=([\w-]+)\)", log)
    drive_id = next((a or b for a, b in reversed(drive_ids)), None)
    summary = _empty(status)
    summary.update(passed=passed, total=total, sections=sections, failures=failures,
                   drive_url=f"https://drive.google.com/file/d/{drive_id}/view" if drive_id else None)
    return summary


def parse_recall_results(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
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


def parse_testng(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
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


def parse_playwright_summary(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
    """Reminder UI (Playwright), read from the test-reports/<run>/report.txt its
    Google Chat reporter writes at the end of every run: "Total N · Pass N ·
    Fail N · Skip N", one "<icon> <section> (passed/total)" line per page and a
    "*Failures*" list of "• <section> → <step>: <reason>" lines."""
    path = newest_match(cwd, cfg["glob"], started_ts)
    if path is None:
        return _empty("ERROR", "Run ended before writing its summary - npm/browser setup or config error? (see log)")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _empty("ERROR", f"Unreadable summary: {exc}")

    counts = re.search(r"Total (\d+) · Pass (\d+) · Fail (\d+)", text)
    sections = [{"title": title.strip(), "passed": int(passed), "total": int(total)}
                for title, passed, total in re.findall(r"^\S+ (.+?) \((\d+)/(\d+)\)\s*$", text, re.M)]
    failures = []
    for line in text.partition("*Failures*")[2].splitlines():
        line = line.strip()
        if not line.startswith("•"):
            continue
        where, sep, reason = line[1:].strip().partition(": ")
        section, arrow, name = where.partition(" → ")
        if sep and arrow:
            failures.append({"section": section, "name": name, "reason": reason})
        else:  # an error outside any step (login, timeout, ...)
            failures.append({"section": "", "name": "Run", "reason": line[1:].strip()})

    passed = int(counts.group(2)) if counts else sum(s["passed"] for s in sections)
    total = int(counts.group(1)) if counts else sum(s["total"] for s in sections)
    # Skipped steps (after a failed step on the same page) count as not passed.
    status = "FAIL" if failures else _status_from_counts(passed, total, exit_code)
    summary = _empty(status)
    summary.update(passed=passed, total=total, sections=sections, failures=failures)
    return summary


def parse_exit_code(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
    return _empty("PASS" if exit_code == 0 else "FAIL",
                  None if exit_code == 0 else f"Exited with code {exit_code} (see log and report)")


PARSERS = {
    "reminder_sanity": parse_reminder_sanity,
    "recall_results": parse_recall_results,
    "testng": parse_testng,
    "playwright_summary": parse_playwright_summary,
    "exit_code": parse_exit_code,
}


def parse(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
    parser = PARSERS.get(cfg.get("type", "exit_code"), parse_exit_code)
    try:
        return parser(cfg, cwd, log_path, exit_code, started_ts)
    except Exception as exc:  # noqa: BLE001 - a parser bug must not lose the run
        return _empty("ERROR", f"Could not read results: {exc}")
