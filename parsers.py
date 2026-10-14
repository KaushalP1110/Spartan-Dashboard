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


_RS_RULE = "-" * 60
_RS_ENTRY = re.compile(r"^(?P<name>.+?) \(PatNum=(?P<pat>\d+)\)(?: \[(?P<scenario>.*)\])?$")


def _reminder_sanity_console(log: str) -> Optional[tuple[list, list]]:
    """(sections, failures) from the console summary Reminder Sanity prints at
    the end of every run ("REMINDER SANITY REPORT (combined)" ... "Overall:"),
    or None if the log has none. Counted like its HTML report / Chat card
    (report_builder._effective_display / section_counts): PASS, FAMILY and
    SKIP count as passed, and a ScheduleConfirmation FAIL counts as passed
    when the same patient's Appointment Reminder passed."""
    start = log.rfind("REMINDER SANITY REPORT (combined)")
    if start < 0:
        return None
    lines = log[start:].splitlines()
    sections, entries = [], []  # sections: [title], entries: dicts
    section, entry, i = None, None, 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Overall:"):
            break
        if line == _RS_RULE and i + 2 < len(lines) and lines[i + 2] == _RS_RULE:
            section = lines[i + 1].strip()
            sections.append(section)
            i += 3
            continue
        match = _RS_ENTRY.match(line) if section else None
        if match:
            entry = {"section": section, "pat": match["pat"], "name": match["name"],
                     "scenario": match["scenario"], "status": None, "reason": []}
            entries.append(entry)
        elif entry and line.startswith("  Status: "):
            entry["status"] = line[len("  Status: "):].strip()
        elif entry and line.startswith("  Failure Reason: "):
            entry["reason"].append(line[len("  Failure Reason: "):].strip())
        elif entry and entry["reason"] and line.startswith("  ") and not line.startswith("  Last Checked At:"):
            entry["reason"].append(line.strip())  # the Insight log line under the reason
        i += 1
    if not entries:
        return None

    passed_appointment = {e["pat"] for e in entries
                          if e["section"] == "Appointment Reminder" and e["status"] in ("PASS", "FAMILY")}

    def passed(e: dict) -> bool:
        if e["status"] in ("PASS", "FAMILY", "SKIP"):
            return True
        # A past appointment gets no Schedule Confirmation - the report shows it as PASS.
        return e["section"] == "Schedule Confirmation" and e["pat"] in passed_appointment

    counts = [{"title": title, "passed": sum(1 for e in entries if e["section"] == title and passed(e)),
               "total": sum(1 for e in entries if e["section"] == title)} for title in sections]
    failures = [{"section": e["section"],
                 "name": e["name"] + (f" [{e['scenario']}]" if e["scenario"] else ""),
                 "reason": " ".join(e["reason"])}
                for e in entries if not passed(e)]
    return [c for c in counts if c["total"]], failures


def _reminder_sanity_html(page: str) -> tuple[list, list]:
    """(sections, failures) from its HTML report: the stat tiles and FAIL rows."""
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
    return sections, failures


def parse_reminder_sanity(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
    """Reminder Sanity, read from what a normal run already produces (the
    project itself is not changed for the dashboard):

    - counts and failures: the console summary it prints at the end of every
      run (in the run log). Its HTML report is only a fallback - the run
      deletes it once the PDF is on Google Drive.
    - overall: the "Overall: PASS|FAIL" line of that summary
    - Drive link: the Drive folder its reports go to (GDRIVE_FOLDER_ID in its
      .env), when the run says the report is on Drive - it doesn't print the
      file's own link
    """
    log = ""
    try:
        log = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    parsed = _reminder_sanity_console(log)
    if parsed is None:
        report = newest_match(cwd, cfg["glob"], started_ts)
        if report is None:
            failed = re.findall(r"Reminder Sanity run failed: (.+)", log)
            return _empty("ERROR", failed[-1].strip() if failed else "Run ended before printing its results (see log)")
        try:
            parsed = _reminder_sanity_html(report.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return _empty("ERROR", f"Unreadable report: {exc}")
    sections, failures = parsed

    overall = re.findall(r"^Overall: (PASS|FAIL)\s*$", log, re.M)
    status = overall[-1] if overall else ("PASS" if exit_code == 0 else "FAIL")

    drive_url = None
    if "Report is on Google Drive" in log:
        folder = _env_value(cwd / ".env", "GDRIVE_FOLDER_ID")
        if folder:
            drive_url = f"https://drive.google.com/drive/folders/{folder}"
    summary = _empty(status)
    summary.update(passed=sum(s["passed"] for s in sections), total=sum(s["total"] for s in sections),
                   sections=sections, failures=failures, drive_url=drive_url)
    return summary


def _env_value(path: Path, key: str) -> Optional[str]:
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


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


def playwright_summary(text: str, exit_code: int) -> dict:
    """Reminder UI (Playwright): the test-reports/<run>/report.txt its Google
    Chat reporter writes at the end of every run (Jenkins archives it):
    "Total N · Pass N · Fail N · Skip N", one "<icon> <section> (passed/total)"
    line per page and a "*Failures*" list of "• <section> → <step>: <reason>"."""
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
    "exit_code": parse_exit_code,
}


# Parsers for a result file's text (e.g. a Jenkins build artifact).
TEXT_PARSERS = {
    "playwright_summary": playwright_summary,
}


def parse_text(kind: str, text: str, exit_code: int) -> Optional[dict]:
    parser = TEXT_PARSERS.get(kind)
    if parser is None:
        return None
    try:
        return parser(text, exit_code)
    except Exception as exc:  # noqa: BLE001 - a parser bug must not lose the run
        return _empty("ERROR", f"Could not read results: {exc}")


def parse(cfg: dict, cwd: Path, log_path: Path, exit_code: int, started_ts: float) -> dict:
    parser = PARSERS.get(cfg.get("type", "exit_code"), parse_exit_code)
    try:
        return parser(cfg, cwd, log_path, exit_code, started_ts)
    except Exception as exc:  # noqa: BLE001 - a parser bug must not lose the run
        return _empty("ERROR", f"Could not read results: {exc}")
