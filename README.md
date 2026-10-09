# Spartan Dashboard

The team's one-click runner for our QA automations. Open it in a browser,
click **Run** on any automation, watch the live log, and get PASS/FAIL counts,
the failures that need attention, and links to every report - all in one place.

| Automation | Runs | Result shown from |
|---|---|---|
| Reminder Sanity | `python runners/run_all_reminders_sanity.py` | its HTML report + console summary (+ HTML/PDF/Drive links) |
| Recall Status | `python runners/run_recall_status_check.py` | `recall_status_results_*.json` (+ HTML/PDF) |
| Spartan API | `mvn -B test` | `target/surefire-reports/testng-results.xml` (+ comparison report) |
| Cron Reporting | `mvn -B test` | `testng-results.xml` (+ Excel export) |
| Reminder UI | Jenkins job `Reminder UI Automation Playwright` | the build's archived `test-reports/<run>/report.txt` (+ Playwright HTML report) |

Each local automation runs **in its own project folder exactly as if you ran
it by hand**, so Jenkins/CLI runs and dashboard runs behave the same.

## Run it in Docker (office network)

It is the `spartan-dashboard` service in **spartan's `docker-compose.yml`**,
next to **spartan-ui**, on the office Docker host (172.16.1.89). Like
spartan-ui it publishes **no host port**: it is reached only through the QA
portal (portal login + audit log). Traefik reaches it privately as
`http://spartan-dashboard:8765` over `portal-tools-net`, so the portal stack
needs a route for it - give it its own entrypoint/port or host, not a path
prefix (the dashboard uses absolute paths such as `/api` and `/files`).

1. On the Docker host, put all projects side by side in one folder:
   ```
   <folder>/Spartan-Dashboard
   <folder>/spartan
   <folder>/Reminder-Sanity-Automation
   <folder>/Recall-Status-Automation
   <folder>/CronReporting            (repo root; the Maven project is CronReporting/CronReporting)
   ```
   Each project needs its own `.env` there, as when you run it by hand.
2. In `Spartan-Dashboard`, copy `.env.example` to `.env` and fill in Google
   sign-in (below) and Jenkins (for Reminder UI).
3. Check `OPENDENTAL_DB_HOST` in `spartan/docker-compose.yml` (the PC
   running OpenDental's MySQL - see below), then from the `spartan` folder:
   ```
   docker compose up -d --build spartan-dashboard
   ```
   (`docker compose up -d --build` starts/updates both UIs.)
   First start takes a few minutes (image build + Python packages). Logs:
   `docker logs -f spartan-dashboard`.

The image contains Python 3.12, JDK 21 + Maven and Chromium (PDF export), so
every automation runs inside it. Projects are mounted live, so a `git pull`
on the host is picked up on the next run; reports and run history are
written to the host folders.

### OpenDental database access (required)

All projects connect to OpenDental's MySQL at `127.0.0.1:3306` - they were
written for the PC that runs OpenDental. Inside the container, that address
is forwarded to `OPENDENTAL_DB_HOST` (currently `172.16.3.71`), so the
projects' configs stay as they are. That MySQL must accept the connection:

- MySQL `bind-address` must not be only `127.0.0.1` (my.ini, then restart MySQL).
- The MySQL user in the projects' configs needs a grant for the Docker host,
  e.g. `'user'@'172.16.1.89'` (or the office subnet).
- Windows Firewall on that PC must allow inbound TCP 3306 from the Docker host.

Check from the Docker host: `docker exec spartan-dashboard bash -c "exec 3<>/dev/tcp/127.0.0.1/3306 && echo reachable"`.

### Running it without Docker

`python server.py` (or `start_dashboard.bat`) on any Windows PC that has the
projects side by side, Python, Maven + JDK 21 and Edge/Chrome - see
"Requirements" below. Then open `http://<that-PC-IP>:8765`.

## Google sign-in (@adit.com only)

Only verified Google Workspace accounts in `GOOGLE_ALLOWED_DOMAIN` (default
`adit.com`) can sign in - personal gmail.com accounts and other companies are
refused. The run history shows each person's Google name.

Google only allows its normal redirect sign-in on **https://** addresses (or
localhost), so there are two modes:

**A. Plain LAN address `http://<PC-IP>:8765` (default)** - device sign-in:
the page shows a code, the user clicks *Open Google*, enters the code and
picks their @adit.com account, and the dashboard signs them in.

1. Google Cloud Console (an Adit project) > APIs & Services > OAuth consent
   screen: User type **Internal** (Adit users only), app name "Spartan Dashboard".
2. Credentials > Create credentials > OAuth client ID > type
   **TVs and Limited Input devices**.
3. Put its Client ID and Client secret in `.env` as `GOOGLE_CLIENT_ID` /
   `GOOGLE_CLIENT_SECRET`. Leave `GOOGLE_PUBLIC_URL` empty.

**B. HTTPS address (e.g. `https://spartan.adit.com`)** - the usual
"Sign in with Google" redirect. Needs a DNS name and HTTPS in front of the
dashboard (IT). OAuth client type **Web application** with authorized
redirect URI `https://spartan.adit.com/auth/callback`; set
`GOOGLE_PUBLIC_URL=https://spartan.adit.com`.

Without Google settings the dashboard falls back to `DASHBOARD_PASSWORD`
(shared password), and with neither it only allows this PC (127.0.0.1).
Sessions last 12 hours and are cleared when the dashboard restarts.

### Requirements (without Docker)

- Python 3.10+ with each Python project's `requirements.txt` installed.
- Maven + JDK 21 for the Maven projects - auto-detected (PATH, then IntelliJ's
  bundled Maven and `~/.jdks`), or set `MAVEN_CMD` / `JAVA_HOME` in `.env`.
- Each project's own `.env`, and the same network access they need anyway
  (OpenDental MySQL, Adit, Google).
- All projects checked out side by side in one folder (paths are in `jobs.json`).

## How it behaves

- **One run per automation at a time.** Jobs that must never overlap can
  share a `lock` in `jobs.json`.
- The Spartan card links to the existing Spartan UI (via the QA portal,
  `http://172.16.1.89:8102`) for editing its `.env`.
  Don't start tests there while the dashboard runs Spartan - both use the
  same folder.
- **Stop** kills the run (the whole process tree). Whatever it already set
  up - appointments, recalls - is **not** cleaned up.
- A result is only trusted if its file was written during this run, so a
  stale report from an earlier run is never shown as the new result. A run
  that dies before producing results shows **ERROR** with the reason.
- History records who started each run. It is kept in `data/` (gitignored)
  together with every run's full log.
- The Reminder UI card also shows the latest Jenkins build, including the
  daily 12:00 builds Jenkins starts itself.
- If the dashboard is restarted mid-run, that run is marked **interrupted**.

## Adding or changing an automation

Edit `jobs.json` and restart. A local job:

```json
{
  "id": "my_check", "name": "My Check", "kind": "Python", "type": "local",
  "description": "What it checks.",
  "cwd": "../My-Check",                        // relative to this folder, or absolute
  "command": ["{python}", "-u", "run.py"],     // {python}, {mvn} are filled in
  "requires": [".env"],                        // files that must exist before Run
  "result": {"type": "testng", "path": "target/surefire-reports/testng-results.xml"},
  "reports": [{"label": "HTML report", "glob": "reports/report_*.html"}],
  "lock": "optional-shared-lock", "warning": "optional note on the card", "est_minutes": 10
}
```

Result types: `reminder_sanity`, `recall_results`, `testng`, `exit_code`.
No automation is changed for the dashboard - each is read from its normal output. Only files matching a job's `reports`
globs can be opened from the dashboard.

A Jenkins job: `"type": "jenkins"`, `"jenkins_job": "Folder/Job"`, optional
`"jenkins_params": {...}` (uses buildWithParameters) and `"jenkins_links"`
(paths relative to the build URL, e.g. an HTML Publisher report).
`"jenkins_artifacts"` links the newest archived file matching each `glob`; one
with `"result": "playwright_summary"` also gives the run's result.

## Files

```
server.py         HTTP server, sessions, run management
google_auth.py    Google sign-in (device + redirect flows, domain check)
parsers.py        reads each automation's results into one summary format
jenkins.py        trigger / follow / stop Jenkins builds (API token)
jobs.json         the automations
Dockerfile, docker-entrypoint.sh   the image (service defined in ../spartan/docker-compose.yml)
static/           index.html (dashboard), login.html
data/             run history + logs (created on first run, gitignored)
```
