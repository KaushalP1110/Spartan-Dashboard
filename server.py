#!/usr/bin/env python
"""Spartan Dashboard - the team's one-click runner for every automation.

    python server.py                      # http://127.0.0.1:8765 (this PC only)
    python server.py --host 0.0.0.0       # share with the team on the LAN

Standard library only. Automations are defined in jobs.json: local ones are
started as subprocesses in their own project folder (exactly like running
them by hand), Jenkins ones are triggered and followed through Jenkins' REST
API. Settings (team password, Jenkins token, Maven/Java paths) live in .env.

Run history and logs are kept in data/ (gitignored).
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import json
import mimetypes
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import google_auth
import jenkins
import parsers

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
# Overridable so a test jobs file / data folder can be used.
JOBS_FILE = Path(os.environ.get("SPARTAN_JOBS_FILE", ROOT / "jobs.json"))
DATA_DIR = Path(os.environ.get("SPARTAN_DATA_DIR", ROOT / "data"))
LOG_DIR = DATA_DIR / "logs"
RUNS_FILE = DATA_DIR / "runs.json"
LOG_TAIL_LINES = 400
MAX_RUNS_KEPT = 1000
SESSION_COOKIE = "spartan_session"
SESSION_HOURS = 12


# --- settings ----------------------------------------------------------------

def load_env_file(path: Path) -> dict:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


# Dashboard-only settings: kept out of os.environ so the password and the
# Jenkins token never reach the automations' processes.
CONFIG = load_env_file(Path(os.environ.get("SPARTAN_ENV_FILE", ROOT / ".env")))
jenkins.CONFIG = CONFIG
google_auth.CONFIG = CONFIG
BASE_ENV = dict(os.environ)


def setting(key: str, default: str | None = None) -> str | None:
    return CONFIG.get(key) or os.environ.get(key) or default


def _newest(patterns: list[str]) -> str | None:
    found = [p for pattern in patterns for p in glob.glob(pattern)]
    return max(found, key=os.path.getmtime, default=None)


def detect_maven() -> str | None:
    return (setting("MAVEN_CMD") or shutil.which("mvn")
            or _newest(["C:/Program Files/JetBrains/*/plugins/maven/lib/maven3/bin/mvn.cmd"]))


def detect_java_home() -> str | None:
    home = Path.home().as_posix()
    return (setting("JAVA_HOME")
            or _newest([f"{home}/.jdks/*-21*", f"{home}/.jdks/*21.*"])
            or _newest(["C:/Program Files/Java/jdk-21*", "C:/Program Files/Eclipse Adoptium/jdk-21*"])
            or _newest(["C:/Program Files/JetBrains/*/jbr"]))


MAVEN_CMD = detect_maven()
JAVA_HOME = detect_java_home()


# --- jobs ----------------------------------------------------------------------

def load_jobs() -> dict[str, dict]:
    jobs = json.loads(JOBS_FILE.read_text(encoding="utf-8"))["jobs"]
    return {job["id"]: job for job in jobs}


JOBS = load_jobs()


def job_cwd(job: dict) -> Path:
    path = Path(job["cwd"])
    return (path if path.is_absolute() else ROOT / path).resolve()


def setup_problems(job: dict) -> list[str]:
    """Why a job can't run on this machine yet (empty list = ready)."""
    if job["type"] == "jenkins":
        return [] if jenkins.is_configured() else ["Set JENKINS_URL, JENKINS_USER and JENKINS_API_TOKEN in the dashboard .env"]
    problems = []
    cwd = job_cwd(job)
    if not cwd.is_dir():
        return [f"Project folder not found: {cwd}"]
    for required in job.get("requires", []):
        if not (cwd / required).exists():
            problems.append(f"Missing {required} in {cwd.name}")
    if "{mvn}" in job["command"] and not MAVEN_CMD:
        problems.append("Maven not found - set MAVEN_CMD in the dashboard .env")
    return problems


def build_command(job: dict) -> list[str]:
    tokens = {"{python}": sys.executable, "{mvn}": MAVEN_CMD or "mvn"}
    return [tokens.get(part, part) for part in job["command"]]


def build_env(job: dict, summary_path: Path) -> dict:
    env = dict(BASE_ENV, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if job.get("env_file"):
        # For projects that expect their .env exported by the shell.
        for key, value in load_env_file(job_cwd(job) / job["env_file"]).items():
            env.setdefault(key, value)
    env.update(job.get("env", {}))
    if "{mvn}" in job["command"] and JAVA_HOME:
        env["JAVA_HOME"] = JAVA_HOME
        env["PATH"] = str(Path(JAVA_HOME) / "bin") + os.pathsep + env.get("PATH", "")
    result = job.get("result", {})
    if result.get("env"):
        env[result["env"]] = str(summary_path)
    return env


# --- run records ---------------------------------------------------------------

_lock = threading.RLock()
_active: dict[str, dict] = {}  # job_id -> {"run_id", "process", "stop", "build_url"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_runs() -> list[dict]:
    try:
        return json.loads(RUNS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_runs(runs: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(runs[:MAX_RUNS_KEPT], indent=1), encoding="utf-8")
    tmp.replace(RUNS_FILE)


def update_run(run_id: str, **fields) -> None:
    with _lock:
        runs = load_runs()
        for run in runs:
            if run["id"] == run_id:
                run.update(fields)
        save_runs(runs)


def get_run(run_id: str) -> dict | None:
    return next((r for r in load_runs() if r["id"] == run_id), None)


def log_path(run_id: str) -> Path:
    return LOG_DIR / f"{run_id}.log"


def read_tail(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def current_step(job: dict, run_id: str) -> int | None:
    steps = job.get("steps")
    if not steps:
        return None
    try:
        text = log_path(run_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    step = 0
    for index, (_, marker) in enumerate(steps):
        if marker and marker in text:
            step = index
    return step


def file_url(job_id: str, rel: str) -> str:
    return f"/files/{job_id}/{quote(rel)}"


def run_reports(job: dict, started_ts: float | None) -> list[dict]:
    cwd = job_cwd(job)
    reports = []
    for spec in job.get("reports", []):
        match = parsers.newest_match(cwd, spec["glob"], started_ts)
        if match:
            reports.append({"label": spec["label"], "url": file_url(job["id"], match.relative_to(cwd).as_posix())})
    return reports


def mark_interrupted_runs() -> None:
    with _lock:
        runs = load_runs()
        changed = False
        for run in runs:
            if run.get("status") == "running":
                run.update(status="interrupted", finished_at=run.get("finished_at") or _now())
                changed = True
        if changed:
            save_runs(runs)


def _finish(job: dict, run_id: str, **fields) -> None:
    with _lock:
        stopped = _active.get(job["id"], {}).get("stop")
        if stopped is not None and stopped.is_set():
            fields["status"] = "stopped"
        update_run(run_id, finished_at=_now(), **fields)
        _active.pop(job["id"], None)


def _watch_local(job: dict, run_id: str, process: subprocess.Popen, log_file, started_ts: float, summary_path: Path) -> None:
    exit_code = process.wait()
    log_file.close()
    summary = parsers.parse(job.get("result", {}), job_cwd(job), summary_path, exit_code, started_ts)
    _finish(job, run_id, status=summary["overall_status"].lower(), exit_code=exit_code, summary=summary,
            reports=run_reports(job, started_ts))


def _follow_jenkins(job: dict, run_id: str, stop: threading.Event) -> None:
    log = log_path(run_id)
    build_url = None
    try:
        with log.open("w", encoding="utf-8") as fh:
            fh.write(f"Triggering Jenkins job {job['jenkins_job']}...\n")
        queue_url = jenkins.trigger(job["jenkins_job"], job.get("jenkins_params"))
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"Queued: {queue_url}\nWaiting for an executor...\n")
        build_url = jenkins.wait_for_build(queue_url, stop.is_set)
        if not build_url:
            _finish(job, run_id, status="stopped", summary=parsers._empty("ERROR", "Cancelled before the build started"))
            return
        with _lock:
            if job["id"] in _active:
                _active[job["id"]]["build_url"] = build_url
        update_run(run_id, jenkins_build_url=build_url)
        if stop.is_set():
            jenkins.stop(build_url)
        while True:
            info = jenkins.build_info(build_url)
            log.write_text(jenkins.console_text(build_url), encoding="utf-8")
            if not info.get("building"):
                break
            time.sleep(10)
        summary = jenkins.summarize(info, jenkins.test_report(build_url))
        links = [{"label": f"Jenkins build #{info.get('number')}", "url": build_url},
                 {"label": "Test results", "url": build_url + "testReport/"}]
        links += [{"label": link["label"], "url": build_url + link["path"]} for link in job.get("jenkins_links", [])]
        status = "stopped" if info.get("result") == "ABORTED" else summary["overall_status"].lower()
        _finish(job, run_id, status=status, summary=summary, reports=links, jenkins_build_url=build_url)
    except jenkins.JenkinsError as exc:
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"\n[dashboard] {exc}\n")
        _finish(job, run_id, status="error", summary=parsers._empty("ERROR", str(exc)),
                reports=[{"label": "Jenkins build", "url": build_url}] if build_url else [])


def start_run(job_id: str, user: str) -> tuple[bool, str]:
    job = JOBS.get(job_id)
    if not job:
        return False, "Unknown automation"
    problems = setup_problems(job)
    if problems:
        return False, "Not set up: " + "; ".join(problems)
    with _lock:
        if job_id in _active:
            return False, f"{job['name']} is already running"
        if job.get("lock"):
            for other_id in _active:
                if JOBS[other_id].get("lock") == job["lock"]:
                    return False, f"Wait for {JOBS[other_id]['name']} to finish - both change the same reminder templates"

        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{job_id}"
        started_ts = time.time()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {"id": run_id, "job_id": job_id, "job_name": job["name"], "status": "running",
                  "started_at": _now(), "finished_at": None, "started_by": user,
                  "exit_code": None, "summary": None, "reports": []}
        stop = threading.Event()

        if job["type"] == "jenkins":
            runs = load_runs()
            runs.insert(0, record)
            save_runs(runs)
            _active[job_id] = {"run_id": run_id, "process": None, "stop": stop, "build_url": None}
            threading.Thread(target=_follow_jenkins, args=(job, run_id, stop), daemon=True).start()
            return True, run_id

        summary_path = LOG_DIR / f"{run_id}.summary.json"
        log_file = open(log_path(run_id), "w", encoding="utf-8", buffering=1)
        log_file.write(f"[dashboard] {job['name']} started by {user} - {' '.join(job['command'])}\n")
        try:
            process = subprocess.Popen(
                build_command(job), cwd=str(job_cwd(job)), env=build_env(job, summary_path),
                stdout=log_file, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",  # own process group, so Stop can kill the whole tree
            )
        except OSError as exc:
            log_file.close()
            return False, f"Could not start {job['name']}: {exc}"
        runs = load_runs()
        runs.insert(0, record)
        save_runs(runs)
        _active[job_id] = {"run_id": run_id, "process": process, "stop": stop, "build_url": None}
    threading.Thread(target=_watch_local, args=(job, run_id, process, log_file, started_ts, summary_path),
                     daemon=True).start()
    return True, run_id


def stop_run(job_id: str, user: str) -> tuple[bool, str]:
    with _lock:
        active = _active.get(job_id)
        if not active:
            return False, "Not running"
        active["stop"].set()
        process, build_url, run_id = active["process"], active["build_url"], active["run_id"]
    update_run(run_id, stopped_by=user)
    if process is not None:
        if os.name == "nt":
            # Whole tree: Maven forks a JVM, the Python runners may have a headless browser open.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)  # Maven's forked JVM, headless Chromium, ...
            except ProcessLookupError:
                pass
    elif build_url:
        try:
            jenkins.stop(build_url)
        except jenkins.JenkinsError as exc:
            return False, str(exc)
    return True, "Stopping"


# --- status --------------------------------------------------------------------

_jenkins_last: dict[str, dict | None] = {}
_report_cache: dict[str, tuple[float, list]] = {}


def _jenkins_poller() -> None:
    """Keeps the latest Jenkins build per Jenkins job fresh, so builds started
    from Jenkins itself (schedules, other people) show on the card too."""
    while True:
        if jenkins.is_configured():
            for job in JOBS.values():
                if job["type"] == "jenkins":
                    _jenkins_last[job["id"]] = jenkins.last_build(job["jenkins_job"])
        time.sleep(60)


def latest_reports(job: dict) -> list[dict]:
    """Newest report files on disk - includes runs started outside the dashboard."""
    if job["type"] != "local" or not job_cwd(job).is_dir():
        return []
    cached = _report_cache.get(job["id"])
    if cached and time.time() - cached[0] < 20:
        return cached[1]
    reports = run_reports(job, None)
    _report_cache[job["id"]] = (time.time(), reports)
    return reports


def status_payload(selected_job: str | None, user: str) -> dict:
    runs = load_runs()
    with _lock:
        active = {job_id: dict(a) for job_id, a in _active.items()}
    jobs = []
    for job in JOBS.values():
        job_runs = [r for r in runs if r["job_id"] == job["id"]]
        running = active.get(job["id"])
        running_run = next((r for r in job_runs if running and r["id"] == running["run_id"]), None)
        done = next((r for r in job_runs if r["status"] != "running"), None)
        jobs.append({
            "id": job["id"], "name": job["name"], "description": job.get("description", ""),
            "kind": job.get("kind", ""), "type": job["type"], "warning": job.get("warning"),
            "est_minutes": job.get("est_minutes"), "lock": job.get("lock"),
            "links": job.get("links", []),
            "problems": setup_problems(job),
            "steps": [s[0] for s in job.get("steps", [])],
            "running": running_run and {**running_run, "step": current_step(job, running_run["id"])},
            "last_run": done,
            "latest_reports": latest_reports(job),
            "jenkins_last": _jenkins_last.get(job["id"]),
            "pass_streak": [r["status"] for r in job_runs if r["status"] != "running"][:10],
        })
    payload = {"user": user, "server_time": _now(), "jobs": jobs, "recent": runs[:40]}
    if selected_job in JOBS:
        job_runs = [r for r in runs if r["job_id"] == selected_job]
        shown = job_runs[0] if job_runs else None
        payload["selected"] = {
            "job_id": selected_job,
            "run": shown,
            "log": read_tail(log_path(shown["id"])) if shown else [],
            "history": job_runs[:25],
        }
    return payload


# --- auth ----------------------------------------------------------------------
# Google sign-in (company domain only) when GOOGLE_CLIENT_ID/SECRET are set,
# else a shared DASHBOARD_PASSWORD, else no sign-in (this PC only).

PASSWORD = setting("DASHBOARD_PASSWORD")
AUTH_MODE = "google" if google_auth.is_configured() else "password" if PASSWORD else "none"
SECURE_COOKIES = (google_auth.public_url() or "").startswith("https://")
_sessions: dict[str, dict] = {}
_oauth_states: dict[str, float] = {}      # web flow: state -> expiry
_device_flows: dict[str, dict] = {}       # device flow: flow id -> device_code, interval, ...


def _purge(store: dict, now: float) -> None:
    for key in [k for k, v in store.items() if (v if isinstance(v, float) else v["expires"]) < now]:
        store.pop(key, None)


def create_session(name: str, email: str | None = None) -> str:
    _purge(_sessions, time.time())
    token = secrets.token_urlsafe(32)
    _sessions[token] = {"name": name, "email": email, "expires": time.time() + SESSION_HOURS * 3600}
    return token


def _cookie(name: str, value: str, max_age: int) -> str:
    # Lax (not Strict) so the session survives the redirect back from Google;
    # cross-site POSTs (the Run/Stop actions) still never carry it.
    return f"{name}={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}" + ("; Secure" if SECURE_COOKIES else "")


def session_cookie(token: str) -> str:
    return _cookie(SESSION_COOKIE, token, SESSION_HOURS * 3600)


def _read_cookie(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:  # noqa: BLE001 - malformed cookie = not logged in
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None


def session_user(cookie_header: str | None) -> str | None:
    if AUTH_MODE == "none":
        return "local"
    session = _sessions.get(_read_cookie(cookie_header, SESSION_COOKIE) or "")
    if not session or session["expires"] < time.time():
        return None
    return session["name"]


def auth_config() -> dict:
    mode = f"google-{google_auth.mode()}" if AUTH_MODE == "google" else AUTH_MODE
    return {"mode": mode, "domains": google_auth.allowed_domains() if AUTH_MODE == "google" else []}


def device_start() -> dict:
    now = time.time()
    _purge(_device_flows, now)
    data = google_auth.device_start()
    flow = secrets.token_urlsafe(24)
    _device_flows[flow] = {"device_code": data["device_code"], "interval": int(data.get("interval", 5)),
                           "expires": now + int(data.get("expires_in", 900)), "next_poll": now}
    return {"flow": flow, "user_code": data["user_code"], "verification_url": data["verification_url"],
            "interval": int(data.get("interval", 5)), "expires_in": int(data.get("expires_in", 900))}


def device_poll(flow_id: str) -> tuple[str, str | None, str | None]:
    """(status, session token, message)"""
    flow = _device_flows.get(flow_id)
    now = time.time()
    if not flow or flow["expires"] < now:
        _device_flows.pop(flow_id, None)
        return "expired", None, "The code expired - start again"
    if now < flow["next_poll"]:
        return "pending", None, None  # respect Google's polling interval
    status, who = google_auth.device_poll(flow["device_code"])
    if status == "slow_down":
        flow["interval"] += 5
    flow["next_poll"] = now + flow["interval"]
    if status in ("pending", "slow_down"):
        return "pending", None, None
    _device_flows.pop(flow_id, None)
    if status == "ok":
        print(f"[auth] {who['email']} signed in")
        return "ok", create_session(who["name"], who["email"]), None
    return status, None, "Sign-in was cancelled" if status == "denied" else "The code expired - start again"


# --- HTTP ----------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SpartanDashboard/1.0"

    def log_message(self, fmt, *args):
        if not (args and "/api/status" in str(args[0])):
            super().log_message(fmt, *args)

    def _send(self, status, body: bytes, content_type: str, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status=HTTPStatus.OK, headers=None):
        self._send(status, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8", headers)

    def _redirect(self, location: str):
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def _static(self, name: str):
        self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), "text/html; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length < 10_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _serve_report(self, route: str):
        _, _, job_id, rel = route.split("/", 3)
        job = JOBS.get(job_id)
        if not job or job["type"] != "local":
            return self.send_error(HTTPStatus.NOT_FOUND)
        cwd = job_cwd(job)
        target = (cwd / unquote(rel)).resolve()
        # Only files inside the project that match one of its report globs.
        if not target.is_file() or not target.is_relative_to(cwd):
            return self.send_error(HTTPStatus.NOT_FOUND)
        rel_posix = target.relative_to(cwd).as_posix()
        if not any(fnmatch.fnmatch(rel_posix, spec["glob"]) for spec in job.get("reports", [])):
            return self.send_error(HTTPStatus.NOT_FOUND)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = {}
        if not content_type.startswith(("text/html", "application/pdf")):
            headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
        self._send(HTTPStatus.OK, target.read_bytes(), content_type, headers)

    def _google_callback(self, query: dict):
        state = (query.get("state") or [""])[0]
        cookie_state = _read_cookie(self.headers.get("Cookie"), "spartan_oauth_state")
        expiry = _oauth_states.pop(state, 0)
        if not state or state != cookie_state or expiry < time.time():
            return self._redirect("/login?error=" + quote("Sign-in expired - please try again"))
        if "error" in query or "code" not in query:
            return self._redirect("/login?error=" + quote("Sign-in was cancelled"))
        try:
            who = google_auth.exchange_code(query["code"][0])
        except google_auth.GoogleAuthError as exc:
            return self._redirect("/login?error=" + quote(str(exc)))
        print(f"[auth] {who['email']} signed in")
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", session_cookie(create_session(who["name"], who["email"])))
        self.send_header("Set-Cookie", _cookie("spartan_oauth_state", "", 0))
        self.end_headers()
        return None

    def do_GET(self):
        url = urlparse(self.path)
        route = url.path
        if route == "/login":
            return self._static("login.html")
        if route == "/api/auth/config":
            return self._json(auth_config())
        if route == "/auth/google" and auth_config()["mode"] == "google-web":
            state = secrets.token_urlsafe(24)
            _purge(_oauth_states, time.time())
            _oauth_states[state] = time.time() + 600
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", google_auth.authorize_url(state))
            self.send_header("Set-Cookie", _cookie("spartan_oauth_state", state, 600))
            self.end_headers()
            return None
        if route == "/auth/callback" and auth_config()["mode"] == "google-web":
            return self._google_callback(parse_qs(url.query))
        user = session_user(self.headers.get("Cookie"))
        if user is None:
            if route.startswith("/api/"):
                return self._json({"error": "login required"}, HTTPStatus.UNAUTHORIZED)
            return self._redirect("/login")
        if route in ("/", "/index.html"):
            return self._static("index.html")
        if route == "/api/status":
            job = (parse_qs(url.query).get("job") or [None])[0]
            return self._json(status_payload(job, user))
        if route.startswith("/api/log/"):
            run_id = route.rsplit("/", 1)[-1]
            if not all(c.isalnum() or c == "_" for c in run_id):
                return self.send_error(HTTPStatus.BAD_REQUEST)
            path = log_path(run_id)
            if not path.is_file():
                return self.send_error(HTTPStatus.NOT_FOUND)
            return self._send(HTTPStatus.OK, path.read_bytes(), "text/plain; charset=utf-8")
        if route.startswith("/files/"):
            return self._serve_report(route)
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/login":
            if AUTH_MODE != "password":
                return self._json({"ok": False, "message": "Password sign-in is disabled"}, HTTPStatus.FORBIDDEN)
            body = self._body()
            name = " ".join(str(body.get("name", "")).split())[:40]
            if not name or not secrets.compare_digest(str(body.get("password", "")).encode(), PASSWORD.encode()):
                time.sleep(1)  # slow down guessing
                return self._json({"ok": False, "message": "Wrong password" if name else "Enter your name"},
                                  HTTPStatus.UNAUTHORIZED)
            return self._json({"ok": True}, headers={"Set-Cookie": session_cookie(create_session(name))})
        if route in ("/api/auth/device/start", "/api/auth/device/poll"):
            if auth_config()["mode"] != "google-device":
                return self.send_error(HTTPStatus.NOT_FOUND)
            try:
                if route.endswith("start"):
                    return self._json(device_start())
                status, token, message = device_poll(str(self._body().get("flow", "")))
            except google_auth.GoogleAuthError as exc:
                return self._json({"status": "error", "message": str(exc)}, HTTPStatus.FORBIDDEN)
            headers = {"Set-Cookie": session_cookie(token)} if token else None
            return self._json({"status": status, "message": message}, headers=headers)
        if route == "/api/logout":
            cookie = SimpleCookie(self.headers.get("Cookie") or "")
            if SESSION_COOKIE in cookie:
                _sessions.pop(cookie[SESSION_COOKIE].value, None)
            return self._json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0"})

        user = session_user(self.headers.get("Cookie"))
        if user is None:
            return self._json({"ok": False, "message": "Login required"}, HTTPStatus.UNAUTHORIZED)
        parts = route.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "jobs"] and parts[3] in ("run", "stop"):
            action = start_run if parts[3] == "run" else stop_run
            ok, message = action(parts[2], user)
            return self._json({"ok": ok, "message": message}, HTTPStatus.OK if ok else HTTPStatus.CONFLICT)
        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> None:
    parser = argparse.ArgumentParser(description="Spartan Dashboard")
    parser.add_argument("--host", default=setting("DASHBOARD_HOST", "127.0.0.1"),
                        help="0.0.0.0 shares it with the team on the network")
    parser.add_argument("--port", type=int, default=int(setting("DASHBOARD_PORT", "8765")))
    args = parser.parse_args()

    if AUTH_MODE == "none" and args.host not in ("127.0.0.1", "localhost"):
        sys.exit("Refusing to share the dashboard without sign-in: set GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET "
                 "(or DASHBOARD_PASSWORD) in .env")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    mark_interrupted_runs()
    threading.Thread(target=_jenkins_poller, daemon=True).start()

    print(f"Maven: {MAVEN_CMD or 'NOT FOUND'}\nJava:  {JAVA_HOME or 'NOT FOUND (using PATH)'}")
    for job in JOBS.values():
        problems = setup_problems(job)
        print(f"  {'OK ' if not problems else '!! '} {job['name']}" + (f" - {'; '.join(problems)}" if problems else ""))
    shown_host = "<this-PC-IP>" if args.host == "0.0.0.0" else args.host
    sign_in = {"google": f"Google sign-in ({google_auth.mode()} flow, @{', @'.join(google_auth.allowed_domains())} only)",
               "password": "shared team password", "none": "no sign-in - this PC only"}[AUTH_MODE]
    print(f"\nSpartan Dashboard: http://{shown_host}:{args.port}  [{sign_in}]")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
