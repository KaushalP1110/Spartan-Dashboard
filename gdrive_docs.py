"""Documentation & Test Cases: lists the Google Drive folders and single
files named in docs.json, for the dashboard's "Docs & Test Cases" view.

Read-only, through a Google service account (the same one the automations
use to upload reports). Each folder or file must be shared with that service
account's email (Viewer is enough); Shared Drive folders need it as a member.

docs.json:
    {"folders": [{"name": "Documentation", "folder": "<folder link or ID>"}],
     "files":   [{"name": "Eaglesoft Update Delete", "file": "<Docs/Sheets/Drive link>",
                  "group": "Test Cases"}]}
Single files are shown under a chip per "group" (default "Files").

Settings (dashboard .env): GDRIVE_SERVICE_ACCOUNT_FILE - path to the
service-account JSON key. Uses google-auth (already installed for Reminder
Sanity / Recall Status); without it the view says what's missing.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

FILES_URL = "https://www.googleapis.com/drive/v3/files"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_DEPTH = 3            # subfolder levels listed under each configured folder
MAX_FILES = 2000         # per configured folder - a safety limit
CACHE_SECONDS = 300

# Filled by server.py from the dashboard .env.
CONFIG: dict = {}

_cache: dict = {"at": 0.0, "data": None}
_cache_lock = threading.Lock()


class DocsError(Exception):
    pass


def folder_id(value: str) -> str:
    """Accepts a folder ID or any Drive folder link."""
    match = re.search(r"/folders/([\w-]+)", value) or re.search(r"[?&]id=([\w-]+)", value)
    return match.group(1) if match else value.strip()


def file_id(value: str) -> str:
    """Accepts a file ID or any Docs/Sheets/Slides/Drive file link."""
    match = re.search(r"/d/([\w-]+)", value) or re.search(r"[?&]id=([\w-]+)", value)
    return match.group(1) if match else value.strip()


def load_config(path: Path) -> tuple[list[dict], list[dict]]:
    """(folders, files) from docs.json."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], []
    except ValueError as exc:
        raise DocsError(f"{path.name} is not valid JSON: {exc}") from exc
    folders = [{"name": f["name"], "id": folder_id(f["folder"]), "description": f.get("description", "")}
               for f in data.get("folders", []) if f.get("name") and f.get("folder")]
    files = [{"name": f["name"], "id": file_id(f["file"]), "link": f["file"],
              "group": f.get("group") or "Files", "description": f.get("description", "")}
             for f in data.get("files", []) if f.get("name") and f.get("file")]
    return folders, files


def _service_account_email() -> Optional[str]:
    try:
        return json.loads(Path(CONFIG.get("GDRIVE_SERVICE_ACCOUNT_FILE", "")).read_text(encoding="utf-8")).get("client_email")
    except (OSError, ValueError):
        return None


def _session():
    key_file = CONFIG.get("GDRIVE_SERVICE_ACCOUNT_FILE")
    if not key_file:
        raise DocsError("Set GDRIVE_SERVICE_ACCOUNT_FILE in the dashboard .env (the Google service-account JSON key)")
    if not Path(key_file).is_file():
        raise DocsError(f"Service-account key not found: {key_file}")
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError as exc:
        raise DocsError("google-auth is not installed - run: python -m pip install google-auth requests") from exc
    credentials = service_account.Credentials.from_service_account_file(key_file, scopes=SCOPES)
    return AuthorizedSession(credentials), credentials.service_account_email


def _children(session, parent: str) -> list[dict]:
    items, token = [], None
    while True:
        params = {
            "q": f"'{parent}' in parents and trashed = false",
            "fields": "nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink, lastModifyingUser(displayName))",
            "pageSize": 1000, "orderBy": "folder,name",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        resp = session.get(FILES_URL, params=params, timeout=30)
        if resp.status_code == 404:
            raise DocsError("not found, or not shared with the service account")
        if resp.status_code >= 400:
            raise DocsError(f"Drive API error {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        items += data.get("files", [])
        token = data.get("nextPageToken")
        if not token:
            return items


def _kind(mime: str, name: str) -> str:
    if mime == FOLDER_MIME:
        return "folder"
    for key, kind in (("spreadsheet", "sheet"), ("document", "doc"), ("presentation", "slides"),
                      ("pdf", "pdf"), ("excel", "sheet"), ("sheet", "sheet"), ("word", "doc"),
                      ("powerpoint", "slides"), ("image", "image"), ("video", "video")):
        if key in mime:
            return kind
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return {"xlsx": "sheet", "xls": "sheet", "csv": "sheet", "docx": "doc", "doc": "doc", "md": "doc",
            "txt": "doc", "pptx": "slides", "pdf": "pdf"}.get(ext, "file")


def _list_folder(session, root: dict) -> dict:
    files, queue = [], [(root["id"], [], 0)]
    while queue and len(files) < MAX_FILES:
        parent, path, depth = queue.pop(0)
        for item in _children(session, parent):
            kind = _kind(item.get("mimeType", ""), item.get("name", ""))
            if kind == "folder":
                if depth + 1 < MAX_DEPTH:
                    queue.append((item["id"], path + [item["name"]], depth + 1))
                continue
            files.append({
                "name": item.get("name", ""), "kind": kind, "path": " / ".join(path),
                "modified": item.get("modifiedTime"), "url": item.get("webViewLink"),
                "by": (item.get("lastModifyingUser") or {}).get("displayName", ""),
            })
    files.sort(key=lambda f: f["modified"] or "", reverse=True)
    return {**root, "url": f"https://drive.google.com/drive/folders/{root['id']}", "files": files, "error": None}


def _file_group(session, group: str, entries: list[dict], email: str) -> dict:
    """One chip for the single files of a group, each read with files.get."""
    files, errors = [], []
    for entry in entries:
        resp = session.get(f"{FILES_URL}/{entry['id']}", timeout=30, params={
            "fields": "id, name, mimeType, modifiedTime, webViewLink, lastModifyingUser(displayName)",
            "supportsAllDrives": "true"})
        if resp.status_code == 404:
            errors.append(f"{entry['name']}: not found, or not shared with {email} (Viewer)")
            continue
        if resp.status_code >= 400:
            errors.append(f"{entry['name']}: Drive API error {resp.status_code}")
            continue
        item = resp.json()
        files.append({
            # The name from docs.json; the link as given there (keeps a sheet's #gid tab).
            "name": entry["name"], "kind": _kind(item.get("mimeType", ""), item.get("name", "")), "path": "",
            "modified": item.get("modifiedTime"), "url": entry["link"] if entry["link"].startswith("http") else item.get("webViewLink"),
            "by": (item.get("lastModifyingUser") or {}).get("displayName", ""),
        })
    files.sort(key=lambda f: f["modified"] or "", reverse=True)
    return {"name": group, "id": "group:" + re.sub(r"\W+", "-", group.lower()), "url": None, "description": "",
            "files": files, "error": "; ".join(errors) or None}


def listing(folders_file: Path, refresh: bool = False) -> dict:
    """{"configured", "error", "service_account", "folders": [...], "fetched_at"} - cached for a few minutes."""
    with _cache_lock:
        if not refresh and _cache["data"] and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
    try:
        folders, single_files = load_config(folders_file)
    except DocsError as exc:
        return {"configured": False, "error": str(exc), "folders": [], "service_account": None, "fetched_at": None}
    if not folders and not single_files:  # setup instructions still need the account to share folders with
        return {"configured": False, "error": None, "folders": [], "service_account": _service_account_email(), "fetched_at": None}
    try:
        session, email = _session()
    except DocsError as exc:
        return {"configured": True, "error": str(exc), "folders": [], "service_account": None, "fetched_at": None}
    out = []
    for folder in folders:
        try:
            out.append(_list_folder(session, folder))
        except DocsError as exc:
            out.append({**folder, "url": f"https://drive.google.com/drive/folders/{folder['id']}", "files": [],
                        "error": f"{folder['name']}: {exc}. Share the folder with {email} (Viewer)."})
        except Exception as exc:  # noqa: BLE001 - network etc.; show it instead of failing the page
            out.append({**folder, "url": f"https://drive.google.com/drive/folders/{folder['id']}", "files": [],
                        "error": f"{folder['name']}: could not reach Google Drive ({exc})"})
    groups: dict[str, list[dict]] = {}
    for entry in single_files:
        groups.setdefault(entry["group"], []).append(entry)
    for group, entries in groups.items():
        try:
            chip = _file_group(session, group, entries, email)
        except Exception as exc:  # noqa: BLE001 - network etc.
            chip = {"name": group, "id": "group:" + group, "url": None, "files": [],
                    "error": f"{group}: could not reach Google Drive ({exc})"}
        # A folder with the same name: put the files in its chip.
        same = next((f for f in out if f["name"] == group), None)
        if same:
            same["files"] = sorted(same["files"] + chip["files"], key=lambda f: f["modified"] or "", reverse=True)
            same["error"] = "; ".join(e for e in (same["error"], chip["error"]) if e) or None
        else:
            out.append(chip)
    data = {"configured": True, "error": None, "folders": out, "service_account": email,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with _cache_lock:
        _cache.update(at=time.time(), data=data)
    return data
