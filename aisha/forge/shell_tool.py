"""Shell execution — run arbitrary shell commands and capture their output.

Marked ``dangerous`` in the registry. Defaults are conservative:
  * cwd defaults to ``$HOME`` (shell commonly run from there)
  * 30s timeout, max 300s
  * stdout and stderr each capped at 8 KB so the model context doesn't bloat
  * ``shell=True`` so pipes, redirects, and globs work as expected

No denylist — aisha decides what to run, the logs record what was run.
Every invocation is visible in ``aisha.log`` via the registry's
``dangerous`` warning plus the narrator event. This is not a sandbox, just
a thin safety harness.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_STREAM_CAP = 8192  # bytes per stream
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 300


def run_shell(
    command: str,
    *,
    cwd: Optional[str] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> dict:
    """Execute ``command`` via the user's shell. Returns a result dict.

    ``{"returncode": int, "stdout": str, "stderr": str, "truncated": bool,
       "cwd": str, "timed_out": bool}``

    Raises ``ValueError`` on clearly malformed input. Never raises on
    subprocess failure — the non-zero returncode is returned like any
    other result so the caller can decide.
    """
    if not command or not command.strip():
        raise ValueError("command required")
    timeout = max(1, min(int(timeout), _MAX_TIMEOUT))

    work_dir = cwd or os.path.expanduser("~")
    work_dir = os.path.abspath(os.path.expanduser(work_dir))
    if not os.path.isdir(work_dir):
        raise ValueError(f"cwd is not a directory: {work_dir}")

    log.warning("shell: run cwd=%s timeout=%ds cmd=%r",
                work_dir, timeout, command[:500])

    timed_out = False
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
        rc = proc.returncode
        out = proc.stdout or ""
        err = proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = -1
        out = (e.stdout or "") if isinstance(e.stdout, str) else (
            e.stdout.decode("utf-8", "replace") if e.stdout else "")
        err = (e.stderr or "") if isinstance(e.stderr, str) else (
            e.stderr.decode("utf-8", "replace") if e.stderr else "")
        err = (err + f"\n[timed out after {timeout}s]").strip()

    truncated = False
    if len(out) > _STREAM_CAP:
        out = out[:_STREAM_CAP] + f"\n…[stdout truncated at {_STREAM_CAP} bytes]"
        truncated = True
    if len(err) > _STREAM_CAP:
        err = err[:_STREAM_CAP] + f"\n…[stderr truncated at {_STREAM_CAP} bytes]"
        truncated = True

    log.info("shell: rc=%d out=%d err=%d truncated=%s",
             rc, len(out), len(err), truncated)
    return {
        "returncode": rc,
        "stdout": out,
        "stderr": err,
        "truncated": truncated,
        "cwd": work_dir,
        "timed_out": timed_out,
    }
