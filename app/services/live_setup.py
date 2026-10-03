"""
Helpers for switching the app from the demo to your real Housecall Pro account (used by scripts/go_live.py).

Nothing here writes to Housecall Pro. The API key is only ever written to the local ``.env`` file and is never printed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

from ..config import Config
from ..hcp.client import HCPClient
from ..hcp.diagnose import explain_http_error
from ..hcp.http import HttpError

DEMO_ADMIN_EMAIL = "admin@example.com"


def _format(value: str) -> str:
    """A .env value; quoted only when it has to be."""
    return value if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]*", value) else '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_env_file(path: Path, values: Dict[str, str], template: Optional[Path] = None) -> None:
    """Set KEY=value lines in ``path`` and leave everything else (comments, other settings) as it was.

    An existing ``KEY=`` line is replaced; else a commented ``# KEY=`` line is switched on; else the setting is
    appended. A missing file starts from ``template`` (.env.example). Written atomically and, where the system
    supports it, readable by the owner only."""
    if path.exists():
        text = path.read_text(encoding="utf-8")
    elif template is not None and template.exists():
        text = template.read_text(encoding="utf-8")
    else:
        text = ""
    lines = text.splitlines()
    for key, value in values.items():
        new = f"{key}={_format(value)}"
        active = re.compile(rf"^\s*{re.escape(key)}\s*=")
        commented = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
        for pattern in (active, commented):
            hit = next((i for i, ln in enumerate(lines) if pattern.match(ln)), None)
            if hit is not None:
                lines[hit] = new
                break
        else:
            lines.append(new)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()                           # a leftover from an interrupted run may have looser permissions
    except FileNotFoundError:
        pass
    # created owner-only from the start: the key must never sit in a file other users can read, not even briefly
    # (the mode is ignored on Windows, which has no such permissions)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def check_key(cfg: Config, transport=None) -> dict:
    """Try the key with ONE read (the employee list). Returns {"ok", "employees", "message"}; never raises on a
    rejected key or a network problem, and never includes the key in the message."""
    try:
        employees = HCPClient(cfg, transport=transport).list_employees()
    except HttpError as e:
        return {"ok": False, "employees": 0, "message": explain_http_error(e)}
    except ValueError as e:
        return {"ok": False, "employees": 0, "message": str(e)}
    return {"ok": True, "employees": len(employees), "message": f"The key works: {len(employees)} employees found."}


def only_the_demo_login_exists(conn) -> bool:
    rows = conn.execute("SELECT email FROM users").fetchall()
    return len(rows) == 1 and rows[0]["email"] == DEMO_ADMIN_EMAIL


def replace_demo_login(conn, email: str, name: str, password_hash: str, created_at: str) -> None:
    """Swap the demo admin (its password was printed on a screen) for a real login."""
    conn.execute("DELETE FROM users WHERE email = ?", (DEMO_ADMIN_EMAIL,))
    conn.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                 (email.lower(), name, password_hash, "admin", created_at))
