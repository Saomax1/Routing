#!/usr/bin/env python3
"""
Refuse to let real customer information or an API key reach git (and so GitHub).

It learns what to look for from the REAL data on this machine: the API keys in the environment (HCP_API_KEY, MAPS_API_KEY,
...) and, from the app's database, every real customer name, phone number and street address, warranty contact, technician
home address, login email and dispatcher note. Then it scans what you are about to commit or push and fails if any of it
appears: in a file, a file name, or a commit message. Demo rows (job_demo_*, emp_demo_*) are ignored.

    python scripts/leak_check.py --tree          what `git add -A` would pick up (tracked + untracked, not ignored)
    python scripts/leak_check.py --staged        what is staged right now            (the pre-commit hook)
    python scripts/leak_check.py --message FILE  a commit message                    (the commit-msg hook)
    python scripts/leak_check.py --pushing       the commits about to be pushed, read from stdin  (the pre-push hook)

Where it finds the database: $DATABASE_PATH, then DATABASE_PATH in .env, then data/routing.db, then any --db PATH or
$LEAK_CHECK_DBS (separated by ':' or ';'). It opens them read-only.

It never prints the value it found, only what kind it was and where. It FAILS CLOSED: when an API key is set (a live session)
but no customer database can be read, it refuses (exit 2) instead of passing. Binary files are refused too, because a
screenshot of the real app can show customers and cannot be scanned. Exit codes: 0 clean, 1 found something, 2 could not check.

Standard library only, so it runs anywhere git does.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SECRET_ENV = ("HCP_API_KEY", "MAPS_API_KEY", "LLM_API_KEY", "SESSION_SECRET", "WEBHOOK_SECRET")
MIN_SECRET = 8           # shorter than this is not a key
MIN_TEXT = 6             # shorter values (a lone first name, "AZ") would match ordinary code
ZEROS = "0" * 40
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]*)?\(?(\d{3})\)?[\s.\-]*(\d{3})[\s.\-]*(\d{4})(?!\d)")


def norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def git(*args: str, cwd: Path, data: Optional[bytes] = None) -> bytes:
    return subprocess.run(["git", *args], cwd=str(cwd), input=data, capture_output=True, check=True).stdout


def repo_root() -> Path:
    try:
        return Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("leak_check: this is not a git repository (or git is missing)")


def read_env_file(root: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    f = root / ".env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                out[m.group(1)] = m.group(2).strip().strip("'\"")
    return out


# ---------------------------------------------------------------------------------------------- what to look for

class Targets:
    def __init__(self) -> None:
        self.text: List[Tuple[str, str]] = []              # (label, normalised value)
        self.phones: Dict[str, str] = {}                   # last 10 digits -> label
        self.notes: List[str] = []
        self.real_rows = 0
        self.databases = 0                                  # databases that could be read

    def add_text(self, label: str, value, minimum: int = MIN_TEXT) -> None:
        v = norm(value)
        if len(v) >= minimum and (label, v) not in self.text:
            self.text.append((label, v))

    def add_phone(self, label: str, value) -> None:
        digits = re.sub(r"\D", "", str(value or ""))
        if len(digits) >= 10:
            self.phones.setdefault(digits[-10:], label)


def database_paths(root: Path, env: Dict[str, str], extra: List[str]) -> List[Path]:
    wanted = [os.environ.get("DATABASE_PATH"), env.get("DATABASE_PATH"), str(root / "data" / "routing.db"), *extra]
    wanted += [p for p in re.split(r"[:;]", os.environ.get("LEAK_CHECK_DBS", "")) if p]
    seen, out = set(), []
    for w in wanted:
        if not w:
            continue
        p = Path(w)
        p = p if p.is_absolute() else root / p
        if p.is_file() and str(p.resolve()) not in seen:
            seen.add(str(p.resolve()))
            out.append(p)
    return out


def collect(root: Path, extra_dbs: List[str]) -> Tuple[Targets, bool]:
    """Everything sensitive on this machine, and whether this looks like a live session (a key is set)."""
    t = Targets()
    env_file = read_env_file(root)
    live = False
    for name in SECRET_ENV:
        for value in (os.environ.get(name, ""), env_file.get(name, "")):
            if len(value) >= MIN_SECRET:
                t.add_text(f"API key or secret ({name})", value, MIN_SECRET)
                live = live or name == "HCP_API_KEY"
    if (os.environ.get("HCP_MODE") or env_file.get("HCP_MODE", "")).lower() == "live":
        live = True
    dbs = database_paths(root, env_file, extra_dbs)
    for path in dbs:
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            t.notes.append("a database could not be opened")
            continue
        try:
            read_database(conn, t)
            t.databases += 1
        finally:
            conn.close()
    t.notes.append(f"checked against {t.databases} database(s), {t.real_rows} real job(s), {len(t.text)} text value(s), {len(t.phones)} phone number(s)")
    return t, live


def read_database(conn, t: Targets) -> None:
    def rows(sql: str):
        try:
            return conn.execute(sql).fetchall()
        except sqlite3.Error:
            return []                                      # an older database may lack a table
    real_job = "hcp_job_id NOT LIKE 'job\\_demo\\_%' ESCAPE '\\'"
    for name, phone, street in rows(f"SELECT customer_name, customer_phone, street FROM jobs WHERE {real_job}"):
        t.real_rows += 1
        t.add_text("customer name", name)
        t.add_phone("customer phone number", phone)
        t.add_text("customer street address", street)
    for (data,) in rows(f"SELECT data FROM warranty_details WHERE {real_job}"):
        try:
            d = json.loads(data or "{}")
        except ValueError:
            continue
        t.add_text("warranty contact name", d.get("contact_name"))
        for p in d.get("contact_phones") or []:
            t.add_phone("warranty contact phone number", p)
        t.add_text("customer street address", d.get("street"))
        t.add_text("customer address", d.get("full_address"))
    for (note,) in rows(f"SELECT note FROM job_exceptions WHERE {real_job}"):
        t.add_text("dispatcher note", note)
    for (note,) in rows(f"SELECT note FROM bookings WHERE {real_job}"):
        t.add_text("dispatcher note", note)
    for (addr,) in rows("SELECT home_address FROM technicians WHERE hcp_employee_id NOT LIKE 'emp\\_demo\\_%' ESCAPE '\\'"):
        t.add_text("technician home address", addr)
    for (email,) in rows("SELECT email FROM users"):
        t.add_text("login email", email)


# ---------------------------------------------------------------------------------------------- scanning

def scan(name: str, text: str, t: Targets) -> List[str]:
    """Findings for one piece of text, as 'kind in name:line'. The value found is never included."""
    found: List[str] = []
    hay = norm(text)
    hits = [label for label, v in t.text if v in hay]
    for label in dict.fromkeys(hits):
        values = [v for lb, v in t.text if lb == label and v in hay]
        lines = sorted({i for i, line in enumerate(text.splitlines(), 1) for v in values if v in norm(line)})
        found.append(f"{label} in {name}" + (f":{lines[0]}" if lines else " (spans lines)"))
    for i, line in enumerate(text.splitlines(), 1):
        for m in PHONE.finditer(line):
            label = t.phones.get(m.group(1) + m.group(2) + m.group(3))
            if label:
                found.append(f"{label} in {name}:{i}")
    return found


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:8192]


def check_blob(name: str, data: bytes, t: Targets) -> List[str]:
    out = scan("the file name " + name, name, t)
    if is_binary(data):
        out.append(f"a binary file ({name}): images and other binary files are not allowed in the repo, because a screenshot "
                   "of the real app can show customers and cannot be scanned")
    else:
        out += scan(name, data.decode("utf-8", errors="replace"), t)
    return out


def files_staged(root: Path) -> List[Tuple[str, bytes]]:
    names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR", cwd=root).split(b"\0")
    return [(n.decode(), git("show", f":{n.decode()}", cwd=root)) for n in names if n]


def files_tree(root: Path) -> List[Tuple[str, bytes]]:
    names = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", cwd=root).split(b"\0")
    out = []
    for n in names:
        p = root / n.decode()
        if n and p.is_file():
            out.append((n.decode(), p.read_bytes()))
    return out


def check_pushing(root: Path, stdin_text: str, t: Targets) -> List[str]:
    out: List[str] = []
    for line in stdin_text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] == ZEROS:
            continue                                        # a deletion, or not a ref line
        commits = git("rev-list", parts[1], "--not", "--remotes", cwd=root).decode().split()
        for c in commits:
            short = c[:8]
            out += scan(f"the message of commit {short}", git("log", "-1", "--format=%B", c, cwd=root).decode("utf-8", "replace"), t)
            for entry in git("show", "--format=", "--numstat", c, cwd=root).decode("utf-8", "replace").splitlines():
                if entry.startswith("-\t-\t"):
                    out.append(f"a binary file ({entry.split(chr(9), 2)[2]}) in commit {short}: binary files are not allowed in the repo")
            patch = git("show", "--format=", "-U0", "--no-color", c, cwd=root).decode("utf-8", "replace")
            added = "\n".join(ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
            out += scan(f"commit {short}", added, t)
            out += scan(f"a file name in commit {short}", "\n".join(git("show", "--format=", "--name-only", c, cwd=root).decode("utf-8", "replace").split("\n")), t)
    return out


# ---------------------------------------------------------------------------------------------- command line

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--tree", action="store_true")
    mode.add_argument("--message", metavar="FILE")
    mode.add_argument("--pushing", action="store_true")
    ap.add_argument("--db", action="append", default=[], help="another database to learn from (repeatable)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    root = repo_root()
    targets, live = collect(root, args.db)
    if not args.quiet:
        print("leak check: " + targets.notes[-1], file=sys.stderr)
    if live and targets.databases == 0:
        print("leak check: REFUSING. An API key is set (a live session) but no customer database could be read, so customer data "
              "cannot be checked. Point DATABASE_PATH (or LEAK_CHECK_DBS) at the live database and try again.", file=sys.stderr)
        return 2

    if args.staged:
        findings = [f for n, d in files_staged(root) for f in check_blob(n, d, targets)]
    elif args.tree:
        findings = [f for n, d in files_tree(root) for f in check_blob(n, d, targets)]
    elif args.message:
        findings = scan("the commit message", Path(args.message).read_text(encoding="utf-8", errors="replace"), targets)
    else:
        findings = check_pushing(root, sys.stdin.read(), targets)

    if findings:
        print("\nBLOCKED: customer information, a key or a binary file would reach git:", file=sys.stderr)
        for f in dict.fromkeys(findings):
            print(f"  - {f}", file=sys.stderr)
        print("\nRemove it (use made-up names, addresses and phone numbers in tests and examples) and try again. "
              "Never bypass this with --no-verify.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
