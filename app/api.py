"""
JSON API (Starlette). Every /api route except health + login requires a logged-in user; settings,
technician edits and user management require the Admin role.

Conventions
* Handlers are plain sync functions ``fn(ctx, body)`` run in a thread pool (SQLite + network calls
  never block the event loop). Return a dict, or ``(dict, status)``.
* Errors are ``{"error": "<message>"}``. Unexpected exceptions return a generic 500: details go to the
  log by exception *type* only (never request bodies, descriptions, names or phone numbers).
* Mutating requests must carry ``X-Requested-With: routing-app`` (CSRF defence on top of SameSite
  cookies); see ``CSRFMiddleware`` in main.py.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .db import jdump, jload, utcnow_iso
from .domain.timeutil import hhmm_to_minutes, parse_date
from .security import MIN_PASSWORD_LENGTH, ROLES, burn_verify, hash_password, verify_password
from .services.dispatch_view import build_areas, build_dispatch, build_job_detail, compute_slots, load_technicians
from .services.geocode import geocode_cached
from .services.job_exceptions import clear_exception, reason_options, set_exception
from .services.settings_store import (get_durations, get_settings, replace_durations, save_settings, seed_durations)

log = logging.getLogger("routing.api")


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


@dataclass
class Ctx:
    request: Request
    app: Any
    user: Optional[dict]

    @property
    def db(self):
        return self.app.state.db

    @property
    def cfg(self):
        return self.app.state.cfg

    @property
    def q(self):
        return self.request.query_params

    @property
    def path(self):
        return self.request.path_params


def user_public(row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}


def endpoint(role: Optional[str] = "any", body: bool = False):
    """role: None = public, 'any' = logged in, 'admin' = admin only."""
    def deco(fn: Callable):
        @wraps(fn)
        async def handler(request: Request):
            try:
                payload = None
                if body:
                    try:
                        payload = await request.json()
                    except Exception:
                        raise ApiError(400, "Request body must be valid JSON")
                    if not isinstance(payload, dict):
                        raise ApiError(400, "Request body must be a JSON object")

                def work():
                    user = None
                    if role is not None:
                        uid = request.session.get("uid")
                        if uid:
                            with request.app.state.db.session() as conn:
                                row = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
                            user = user_public(row) if row else None
                        if not user:
                            raise ApiError(401, "Please log in")
                        if role == "admin" and user["role"] != "admin":
                            raise ApiError(403, "Admin access required")
                    return fn(Ctx(request, request.app, user), payload)

                result = await run_in_threadpool(work)
                status = 200
                if isinstance(result, tuple):
                    result, status = result
                return JSONResponse(result, status_code=status, headers={"Cache-Control": "no-store"})
            except ApiError as e:
                return JSONResponse({"error": e.message}, status_code=e.status)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            except Exception as e:  # never leak internals or customer data
                log.error("unhandled error in %s: %s", fn.__name__, type(e).__name__, exc_info=False)
                return JSONResponse({"error": "Internal error"}, status_code=500)
        return handler
    return deco


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------- auth

@endpoint(role=None)
def health(ctx: Ctx, _):
    return {"ok": True, "mode": ctx.cfg.hcp_mode}


@endpoint(role=None, body=True)
def login(ctx: Ctx, body):
    email = str(body.get("email") or "").strip().lower()
    password = str(body.get("password") or "")
    ip = ctx.request.client.host if ctx.request.client else "?"
    limiter = ctx.app.state.login_limiter
    keys = (f"ip:{ip}", f"acct:{email}")
    if any(limiter.blocked(k) for k in keys):
        raise ApiError(429, "Too many failed attempts. Try again in a few minutes.")
    with ctx.db.session() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        for k in keys:
            limiter.reset(k)
        ctx.request.session.clear()                  # new session on login (prevents fixation)
        ctx.request.session["uid"] = row["id"]
        return {"user": user_public(row)}
    if not row:
        burn_verify(password)
    for k in keys:
        limiter.record_failure(k)
    raise ApiError(401, "Invalid email or password")


@endpoint(role=None)
def logout(ctx: Ctx, _):
    ctx.request.session.clear()
    return {"ok": True}


@endpoint(role="any")
def me(ctx: Ctx, _):
    return {"user": ctx.user}


@endpoint(role="any")
def app_config(ctx: Ctx, _):
    with ctx.db.session() as conn:
        s = get_settings(conn)
    tz = ZoneInfo(s["timezone"])
    return {"mode": ctx.cfg.hcp_mode, "geocoder": ctx.cfg.geocoder, "timezone": s["timezone"],
            "today": now_utc().astimezone(tz).date().isoformat(), "map": s["map"],
            "sync_interval_seconds": ctx.cfg.sync_interval_seconds, "llm_enabled": ctx.cfg.llm_enabled,
            "exception_reasons": reason_options()}


# ------------------------------------------------------------------------ dispatch

@endpoint()
def dispatch(ctx: Ctx, _):
    with ctx.db.session() as conn:
        s = get_settings(conn)
        tz = ZoneInfo(s["timezone"])
        now = now_utc()
        d = parse_date(ctx.q.get("date")) or now.astimezone(tz).date()
        if abs((d - now.astimezone(tz).date()).days) > 400:
            raise ApiError(400, "Date out of range")
        return build_dispatch(conn, d, s, now)


@endpoint()
def job_detail(ctx: Ctx, _):
    with ctx.db.session() as conn:
        d = build_job_detail(conn, ctx.path["job_id"], get_settings(conn), now_utc())
    if d is None:
        raise ApiError(404, "Job not found")
    return d


@endpoint(body=True)
def job_slots(ctx: Ctx, body):
    days = body.get("days")
    if days is not None and (not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 14):
        raise ApiError(400, "days must be an integer from 1 to 14")
    with ctx.db.session() as conn:
        res = compute_slots(conn, ctx.path["job_id"], get_settings(conn), now_utc(), days)
    if res is None:
        raise ApiError(404, "Job not found")
    return res


def _days_param(raw) -> Optional[int]:
    if raw is None:
        return None
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= 14:
        raise ApiError(400, "days must be an integer from 1 to 14")
    return days


@endpoint()
def areas(ctx: Ctx, _):
    days = _days_param(ctx.q.get("days"))
    with ctx.db.session() as conn:
        return build_areas(conn, get_settings(conn), now_utc(), days)


@endpoint(body=True)
def exception_put(ctx: Ctx, body):
    """Mark an unscheduled job as being scheduled outside its deadline window (local note, nothing goes to HCP)."""
    with ctx.db.session() as conn:
        exc = set_exception(conn, ctx.path["job_id"], body.get("reason"), body.get("note"), ctx.user["id"])
    if exc is None:
        raise ApiError(404, "Job not found")
    return {"exception": exc}


@endpoint()
def exception_delete(ctx: Ctx, _):
    with ctx.db.session() as conn:
        if not clear_exception(conn, ctx.path["job_id"]):
            raise ApiError(404, "That job is not marked as outside the window")
    return {"ok": True}


# -------------------------------------------------------------------- technicians

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SKILL = re.compile(r"^[A-Z0-9]{2,8}$")


@endpoint()
def technicians_list(ctx: Ctx, _):
    with ctx.db.session() as conn:
        return {"technicians": load_technicians(conn)}


@endpoint(role="admin", body=True)
def technician_update(ctx: Ctx, body):
    tid = ctx.path["tech_id"]
    with ctx.db.session() as conn:
        row = conn.execute("SELECT * FROM technicians WHERE hcp_employee_id = ?", (tid,)).fetchone()
        if not row:
            raise ApiError(404, "Technician not found")
        sets, vals = {}, {}
        if "active" in body:
            sets["active"] = 1 if bool(body["active"]) else 0
        if "trade_skills" in body:
            skills = body["trade_skills"]
            if not isinstance(skills, list) or len(skills) > 10:
                raise ApiError(400, "trade_skills must be a list of up to 10 trade codes")
            clean = []
            for s in skills:
                s = str(s).strip().upper()
                if not _SKILL.match(s):
                    raise ApiError(400, f"Invalid trade code '{s}' (use 2-8 letters/digits, e.g. PLB, HVAC)")
                if s not in clean:
                    clean.append(s)
            sets["trade_skills"] = jdump(clean)
        for k in ("shift_start", "shift_end"):
            if k in body:
                if not _HHMM.match(str(body[k])):
                    raise ApiError(400, f"{k} must be HH:MM")
                sets[k] = str(body[k])
        s_start = hhmm_to_minutes(sets.get("shift_start", row["shift_start"]))
        s_end = hhmm_to_minutes(sets.get("shift_end", row["shift_end"]))
        if s_end <= s_start:
            raise ApiError(400, "Shift end must be after shift start")
        if "work_days" in body:
            wd = body["work_days"]
            if not isinstance(wd, list) or not all(isinstance(x, int) and 0 <= x <= 6 for x in wd):
                raise ApiError(400, "work_days must be a list of 0-6 (Monday=0)")
            sets["work_days"] = jdump(sorted(set(wd)))
        if "max_jobs_per_day" in body:
            m = body["max_jobs_per_day"]
            if not isinstance(m, int) or isinstance(m, bool) or not 1 <= m <= 20:
                raise ApiError(400, "max_jobs_per_day must be 1-20")
            sets["max_jobs_per_day"] = m
        if "color" in body:
            if not _HEX.match(str(body["color"])):
                raise ApiError(400, "color must look like #1a2b3c")
            sets["color"] = str(body["color"])
        geo = None
        if "home_address" in body:
            addr = str(body["home_address"] or "").strip()[:200]
            sets["home_address"] = addr
            if addr != row["home_address"] or row["home_lat"] is None:
                if addr:
                    lat, lng, st = geocode_cached(conn, ctx.app.state.geocoder, addr)
                    sets["home_lat"], sets["home_lng"] = lat, lng
                    geo = "ok" if st == "ok" else ("failed" if st == "failed" else "error")
                else:
                    sets["home_lat"] = sets["home_lng"] = None
        if sets:
            sets["updated_at"] = utcnow_iso()
            conn.execute(f"UPDATE technicians SET {', '.join(k + ' = ?' for k in sets)} WHERE hcp_employee_id = ?",
                         [*sets.values(), tid])
        out = next(t for t in load_technicians(conn) if t["id"] == tid)
    return {"technician": out, "home_geocode": geo}


# ----------------------------------------------------------------------- settings

@endpoint()
def settings_get(ctx: Ctx, _):
    with ctx.db.session() as conn:
        return {"settings": get_settings(conn), "durations": get_durations(conn)}


@endpoint(role="admin", body=True)
def settings_put(ctx: Ctx, body):
    with ctx.db.session() as conn:
        out = {}
        if "settings" in body:
            out["settings"] = save_settings(conn, body["settings"])
        if "durations" in body:
            if not isinstance(body["durations"], list):
                raise ApiError(400, "durations must be a list")
            try:
                out["durations"] = replace_durations(conn, body["durations"])
            except (TypeError, KeyError):
                raise ApiError(400, "Each duration needs trade_code, keyword and minutes")
        if not out:
            raise ApiError(400, "Nothing to save")
        out.setdefault("settings", get_settings(conn))
        out.setdefault("durations", get_durations(conn))
        return out


# --------------------------------------------------------------------------- sync

@endpoint()
def sync_status(ctx: Ctx, _):
    with ctx.db.session() as conn:
        runs = [dict(r) for r in conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 12")]
        counts = {
            "jobs_active": conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1").fetchone()[0],
            "unscheduled": conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1 AND work_status='unscheduled'").fetchone()[0],
            "geocode_failed": conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1 AND geocode_status = 'failed'").fetchone()[0],
            "geocode_pending": conn.execute("SELECT COUNT(*) FROM jobs WHERE active = 1 AND geocode_status = 'pending'").fetchone()[0],
            "warranty_jobs": conn.execute("SELECT COUNT(*) FROM warranty_details w JOIN jobs j USING(hcp_job_id) WHERE j.active = 1").fetchone()[0],
            "needs_review": conn.execute("SELECT COUNT(*) FROM warranty_details w JOIN jobs j USING(hcp_job_id) "
                                         "WHERE j.active = 1 AND w.reviewed = 0 AND (w.parsed_by = 'ai' OR w.parse_warnings != '[]')").fetchone()[0],
            "techs_needing_setup": conn.execute("SELECT COUNT(*) FROM technicians WHERE active = 1 AND (trade_skills = '[]' OR home_lat IS NULL)").fetchone()[0],
        }
    return {"mode": ctx.cfg.hcp_mode, "geocoder": ctx.cfg.geocoder, "interval_seconds": ctx.cfg.sync_interval_seconds,
            "llm_enabled": ctx.cfg.llm_enabled, "runs": runs, "counts": counts}


@endpoint(body=False)
def sync_run(ctx: Ctx, _):
    res = ctx.app.state.sync.run()
    if res.get("status") == "busy":
        raise ApiError(409, "A sync is already running")
    return res


@endpoint()
def parse_review(ctx: Ctx, _):
    with ctx.db.session() as conn:
        rows = conn.execute(
            "SELECT j.hcp_job_id, j.customer_name, j.street, j.city, j.zip, j.hcp_created_at, j.geocode_status, "
            "w.parse_warnings, w.parsed_by, w.dispatch_priority, w.reviewed FROM warranty_details w "
            "JOIN jobs j USING (hcp_job_id) WHERE j.active = 1 AND w.reviewed = 0 "
            "AND (w.parsed_by = 'ai' OR w.parse_warnings != '[]') ORDER BY j.hcp_created_at DESC LIMIT 100").fetchall()
    return {"items": [{"id": r["hcp_job_id"], "customer_name": r["customer_name"],
                       "address": ", ".join(x for x in (r["street"], r["city"], r["zip"]) if x),
                       "received_at": r["hcp_created_at"], "geocode_status": r["geocode_status"],
                       "warnings": jload(r["parse_warnings"], []), "parsed_by": r["parsed_by"],
                       "priority": r["dispatch_priority"]} for r in rows]}


@endpoint(body=False)
def mark_reviewed(ctx: Ctx, _):
    with ctx.db.session() as conn:
        n = conn.execute("UPDATE warranty_details SET reviewed = 1 WHERE hcp_job_id = ?", (ctx.path["job_id"],)).rowcount
    if not n:
        raise ApiError(404, "Job not found")
    return {"ok": True}


# --------------------------------------------------------------------------- users

@endpoint(role="admin")
def users_list(ctx: Ctx, _):
    with ctx.db.session() as conn:
        return {"users": [user_public(r) for r in conn.execute("SELECT * FROM users ORDER BY email")]}


@endpoint(role="admin", body=True)
def users_create(ctx: Ctx, body):
    email = str(body.get("email") or "").strip().lower()
    role = str(body.get("role") or "dispatcher")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise ApiError(400, "A valid email is required")
    if role not in ROLES:
        raise ApiError(400, "role must be admin or dispatcher")
    pw = str(body.get("password") or "")
    if len(pw) < MIN_PASSWORD_LENGTH:
        raise ApiError(400, f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    with ctx.db.session() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            raise ApiError(409, "A user with that email already exists")
        cur = conn.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                           (email, str(body.get("name") or "")[:80], hash_password(pw), role, utcnow_iso()))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"user": user_public(row)}, 201


@endpoint(role="admin")
def users_delete(ctx: Ctx, _):
    uid = int(ctx.path["user_id"])
    if uid == ctx.user["id"]:
        raise ApiError(400, "You cannot delete your own account")
    with ctx.db.session() as conn:
        admins = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'admin' AND id != ?", (uid,)).fetchone()[0]
        target = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if not target:
            raise ApiError(404, "User not found")
        if target["role"] == "admin" and admins == 0:
            raise ApiError(400, "Cannot delete the last admin")
        conn.execute("DELETE FROM users WHERE id = ?", (uid,))
    return {"ok": True}


ROUTES = [
    Route("/api/health", health),
    Route("/api/auth/login", login, methods=["POST"]),
    Route("/api/auth/logout", logout, methods=["POST"]),
    Route("/api/auth/me", me),
    Route("/api/config", app_config),
    Route("/api/dispatch", dispatch),
    Route("/api/jobs/{job_id}", job_detail),
    Route("/api/jobs/{job_id}/slots", job_slots, methods=["POST"]),
    Route("/api/jobs/{job_id}/exception", exception_put, methods=["PUT"]),
    Route("/api/jobs/{job_id}/exception", exception_delete, methods=["DELETE"]),
    Route("/api/areas", areas),
    Route("/api/technicians", technicians_list),
    Route("/api/technicians/{tech_id}", technician_update, methods=["PUT"]),
    Route("/api/settings", settings_get, methods=["GET"]),
    Route("/api/settings", settings_put, methods=["PUT"]),
    Route("/api/sync/status", sync_status),
    Route("/api/sync/run", sync_run, methods=["POST"]),
    Route("/api/parse-review", parse_review),
    Route("/api/parse-review/{job_id}/reviewed", mark_reviewed, methods=["POST"]),
    Route("/api/users", users_list, methods=["GET"]),
    Route("/api/users", users_create, methods=["POST"]),
    Route("/api/users/{user_id:int}", users_delete, methods=["DELETE"]),
]
