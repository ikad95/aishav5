"""File tool — read, write, search, and Slack-upload files.

Minimal surface:
  • read/write text or bytes at a path
  • search file contents (``ag`` if available, else ``grep``)
  • find files by name (``locate``)
  • run an ``awk`` expression over a file
  • upload a file to a Slack channel/thread (``files.upload_v2``)

One module, stdlib only; shells out for search via subprocess.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"

PathLike = Union[str, Path]


# ── Write ──────────────────────────────────────────────────────────────

def write_text(
    path: PathLike,
    content: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    """Write ``content`` to ``path`` as text. Returns the resolved Path."""
    p = Path(path)
    p.write_text(content, encoding=encoding)
    log.info("filetool: wrote %d chars to %s", len(content), p)
    return p


def write_bytes(path: PathLike, data: bytes) -> Path:
    """Write raw bytes to ``path``. Returns the resolved Path."""
    p = Path(path)
    p.write_bytes(data)
    log.info("filetool: wrote %d bytes to %s", len(data), p)
    return p


# ── Read ───────────────────────────────────────────────────────────────

def read_text(path: PathLike, *, encoding: str = "utf-8") -> str:
    """Read ``path`` as text."""
    p = Path(path)
    content = p.read_text(encoding=encoding)
    log.info("filetool: read %d chars from %s", len(content), p)
    return content


def read_bytes(path: PathLike) -> bytes:
    """Read ``path`` as raw bytes."""
    p = Path(path)
    data = p.read_bytes()
    log.info("filetool: read %d bytes from %s", len(data), p)
    return data


# ── Search ─────────────────────────────────────────────────────────────

def search_content(
    pattern: str,
    path: PathLike = ".",
    *,
    case_insensitive: bool = False,
) -> list[str]:
    """Search file contents recursively for ``pattern`` under ``path``.

    Prefers ``ag`` (Silver Searcher) when available; falls back to ``grep -rn``.
    Returns raw match lines in ``file:line:match`` form. Empty list on no
    matches or tool failure.
    """
    target = str(path)
    if shutil.which("ag"):
        cmd = ["ag", "--nocolor"]
        if case_insensitive:
            cmd.append("-i")
        cmd += [pattern, target]
    else:
        cmd = ["grep", "-rn"]
        if case_insensitive:
            cmd.append("-i")
        cmd += ["--", pattern, target]

    result = subprocess.run(cmd, capture_output=True, text=True)
    # exit 1 = "no matches" for both ag and grep — not an error
    if result.returncode not in (0, 1):
        log.error("search_content failed (%s): %s", cmd[0], result.stderr.strip())
        return []
    lines = result.stdout.splitlines()
    log.info("filetool: search_content %r under %s → %d lines", pattern, target, len(lines))
    return lines


def find_files(name: str) -> list[Path]:
    """Find files whose path matches ``name`` via ``locate``.

    Requires the system ``locate`` database to be populated (``updatedb``).
    Returns an empty list on failure.
    """
    if not shutil.which("locate"):
        log.error("find_files: `locate` not installed")
        return []
    result = subprocess.run(
        ["locate", "--", name],
        capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        log.error("find_files failed: %s", result.stderr.strip())
        return []
    paths = [Path(line) for line in result.stdout.splitlines() if line.strip()]
    log.info("filetool: find_files %r → %d paths", name, len(paths))
    return paths


def awk(expression: str, path: PathLike) -> list[str]:
    """Run an ``awk`` expression over ``path``. Returns output lines."""
    result = subprocess.run(
        ["awk", expression, str(path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("awk failed: %s", result.stderr.strip())
        return []
    return result.stdout.splitlines()


# ── Slack upload (files.upload_v2 flow) ────────────────────────────────

def _slack_post(path: str, token: str, body: dict, *, json_body: bool = False) -> dict:
    url = f"{_SLACK_API}/{path}"
    headers = {"Authorization": f"Bearer {token}"}
    if json_body:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    else:
        data = urllib.parse.urlencode(body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upload_to_slack(
    path: PathLike,
    *,
    channel: str,
    thread_ts: Optional[str] = None,
    title: Optional[str] = None,
    initial_comment: Optional[str] = None,
) -> dict:
    """Upload a file to a Slack channel/thread via files.upload_v2.

    Returns ``{"ok": bool, "permalink": str, ...}`` on success; raises on
    network errors. Logs and returns ``{"ok": False, ...}`` on API errors.
    """
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}

    p = Path(path)
    size = p.stat().st_size
    filename = p.name

    # Step 1: get a presigned upload URL + file id
    step1 = _slack_post(
        "files.getUploadURLExternal",
        token,
        {"filename": filename, "length": str(size)},
    )
    if not step1.get("ok"):
        log.error("slack upload step1 failed: %s", step1)
        return {"ok": False, "error": step1.get("error", "getUploadURLExternal failed")}
    upload_url = step1["upload_url"]
    file_id = step1["file_id"]

    # Step 2: POST the raw bytes to the presigned URL (no auth header).
    with p.open("rb") as f:
        payload = f.read()
    req = urllib.request.Request(
        upload_url,
        data=payload,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp.read()
    except Exception as e:
        log.error("slack upload step2 (binary POST) failed: %s", e)
        return {"ok": False, "error": f"upload POST failed: {e}"}

    # Step 3: complete + share to channel/thread
    body: dict = {
        "files": [{"id": file_id, "title": title or filename}],
        "channel_id": channel,
    }
    if thread_ts:
        body["thread_ts"] = thread_ts
    if initial_comment:
        body["initial_comment"] = initial_comment
    step3 = _slack_post("files.completeUploadExternal", token, body, json_body=True)
    if not step3.get("ok"):
        log.error("slack upload step3 failed: %s", step3)
        return {"ok": False, "error": step3.get("error", "completeUploadExternal failed")}

    files = step3.get("files") or [{}]
    permalink = files[0].get("permalink", "")
    log.info("filetool: uploaded to slack channel=%s thread=%s permalink=%s",
             channel, thread_ts, permalink)
    return {"ok": True, "permalink": permalink, "file_id": file_id}
