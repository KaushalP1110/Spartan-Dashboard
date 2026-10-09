"""Minimal Jenkins REST client: trigger a job, follow the build, read its
console and test results, stop it. Uses an API token (Jenkins > your user >
Security > API Token), which needs no CSRF crumb.

Settings (.env): JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

TIMEOUT = 20

# Filled by server.py from the dashboard .env (kept out of os.environ so the
# token never reaches the automations' own processes).
CONFIG: dict = {}


class JenkinsError(Exception):
    pass


def is_configured() -> bool:
    return all(CONFIG.get(k) for k in ("JENKINS_URL", "JENKINS_USER", "JENKINS_API_TOKEN"))


def _base() -> str:
    return CONFIG["JENKINS_URL"].rstrip("/") + "/"


def job_url(job: str) -> str:
    # "Folder/Job" -> job/Folder/job/Job/
    return _base() + "".join(f"job/{urllib.parse.quote(part)}/" for part in job.split("/"))


def _request(url: str, method: str = "GET", data: Optional[bytes] = None):
    token = base64.b64encode(f"{CONFIG['JENKINS_USER']}:{CONFIG['JENKINS_API_TOKEN']}".encode()).decode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Basic {token}"})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise JenkinsError(f"Jenkins {method} {url} -> HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise JenkinsError(f"Jenkins not reachable: {exc.reason}") from exc


def _json(url: str) -> dict:
    with _request(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def trigger(job: str, params: Optional[dict] = None) -> str:
    """Queues a build and returns its queue item URL."""
    if params:
        url = job_url(job) + "buildWithParameters?" + urllib.parse.urlencode(params)
    else:
        url = job_url(job) + "build"
    with _request(url, method="POST", data=b"") as resp:
        location = resp.headers.get("Location")
    if not location:
        raise JenkinsError("Jenkins accepted the build but returned no queue location")
    return location.rstrip("/") + "/"


def wait_for_build(queue_url: str, should_stop, poll_seconds: int = 5, timeout_seconds: int = 3600) -> Optional[str]:
    """Waits until the queued item becomes a build; returns the build URL
    (None if cancelled in the queue or should_stop() turns true)."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline and not should_stop():
        item = _json(queue_url + "api/json")
        if item.get("cancelled"):
            return None
        executable = item.get("executable")
        if executable and executable.get("url"):
            return executable["url"]
        time.sleep(poll_seconds)
    return None


def build_info(build_url: str) -> dict:
    return _json(build_url + "api/json?tree=number,building,result,url,timestamp,duration")


def console_text(build_url: str) -> str:
    with _request(build_url + "consoleText") as resp:
        return resp.read().decode("utf-8", errors="replace")


def test_report(build_url: str) -> Optional[dict]:
    """JUnit/TestNG results published by the build, or None if it has none."""
    tree = "passCount,failCount,skipCount,suites[name,cases[className,name,status,errorDetails]]"
    try:
        return _json(build_url + "testReport/api/json?tree=" + urllib.parse.quote(tree, safe=",[]"))
    except JenkinsError:
        return None


def stop(build_url: str) -> None:
    with _request(build_url + "stop", method="POST", data=b""):
        pass


def last_build(job: str) -> Optional[dict]:
    try:
        return _json(job_url(job) + "lastBuild/api/json?tree=number,building,result,url,timestamp,duration")
    except JenkinsError:
        return None


def summarize(build: dict, report: Optional[dict]) -> dict:
    result = build.get("result")
    status = {"SUCCESS": "PASS", "UNSTABLE": "FAIL", "FAILURE": "FAIL"}.get(result, "ERROR")
    summary = {"overall_status": status, "passed": None, "total": None, "sections": [], "failures": [],
               "error": None if status != "ERROR" else f"Jenkins result: {result or 'unknown'}",
               "drive_url": None}
    if report:
        passed, failed, skipped = report.get("passCount", 0), report.get("failCount", 0), report.get("skipCount", 0)
        summary.update(passed=passed, total=passed + failed + skipped)
        for suite in report.get("suites", []):
            cases = suite.get("cases", [])
            if cases:
                ok = sum(1 for c in cases if c.get("status") in ("PASSED", "FIXED"))
                summary["sections"].append({"title": suite.get("name", "Tests"), "passed": ok, "total": len(cases)})
            for c in cases:
                if c.get("status") in ("FAILED", "REGRESSION"):
                    summary["failures"].append({
                        "section": c.get("className", ""), "name": c.get("name", ""),
                        "reason": " ".join((c.get("errorDetails") or "").split())[:400],
                    })
    return summary
