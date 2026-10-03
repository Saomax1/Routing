"""
App factory + entrypoint.   Run with:   python -m app      (or: uvicorn app.main:app)

Startup:  create DB -> seed durations -> bootstrap first admin (if no users) -> optional first sync
Background: re-sync every SYNC_INTERVAL_SECONDS (default 300).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .api import ROUTES
from .config import Config, load_config
from .db import Database, utcnow_iso
from .hcp.client import make_hcp_client
from .hcp.fixtures import DEMO_TECH_SETUP
from .security import LoginLimiter, hash_password
from .services.data_mode import has_real_data, purge_demo_data
from .services.geocode import make_geocoder
from .services.routing import make_road_routes
from .services.settings_store import get_settings, seed_durations
from .services.sync import SyncService

log = logging.getLogger("routing")

CSP = ("default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class SecurityHeadersMiddleware:
    def __init__(self, app, https_only: bool = False):
        self.app, self.https_only = app, https_only

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                h = dict(message.get("headers") or [])
                extra = {b"content-security-policy": CSP.encode(), b"x-content-type-options": b"nosniff",
                         b"x-frame-options": b"DENY", b"referrer-policy": b"no-referrer",
                         b"permissions-policy": b"geolocation=(), camera=(), microphone=()"}
                if self.https_only:
                    extra[b"strict-transport-security"] = b"max-age=31536000; includeSubDomains"
                message["headers"] = list(message.get("headers") or []) + [(k, v) for k, v in extra.items() if k not in h]
            await send(message)
        await self.app(scope, receive, send_wrapped)


class CSRFMiddleware:
    """Mutating /api requests must carry X-Requested-With (a browser form/cross-site request cannot set it)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH", "DELETE") \
                and scope["path"].startswith("/api/"):
            headers = {k.lower(): v for k, v in scope["headers"]}
            if headers.get(b"x-requested-with") != b"routing-app":
                resp = JSONResponse({"error": "Missing X-Requested-With header"}, status_code=403)
                return await resp(scope, receive, send)
        await self.app(scope, receive, send)


def bootstrap_users(db: Database, cfg: Config) -> Optional[str]:
    """Create the first admin if the users table is empty. Returns a one-time message to show, if any."""
    with db.session() as conn:
        if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return None
        email, pw, generated = cfg.bootstrap_admin_email, cfg.bootstrap_admin_password, False
        if not (email and pw):
            if cfg.hcp_mode != "mock":
                return "No users exist yet. Create the first admin with:  python scripts/create_user.py"
            email, pw, generated = "admin@example.com", secrets.token_urlsafe(12), True
        conn.execute("INSERT INTO users(email, name, password_hash, role, created_at) VALUES (?,?,?,?,?)",
                     (email.lower(), "Admin", hash_password(pw), "admin", utcnow_iso()))
    return (f"Demo admin created ->  email: {email}   password: {pw}   (shown once; set ADMIN_EMAIL/ADMIN_PASSWORD "
            "in .env to choose your own)") if generated else f"Admin account created for {email}"


async def _sync_once(app: Starlette) -> None:
    try:
        await asyncio.to_thread(app.state.sync.run)
    except Exception as e:  # one failed run must never stop the runs after it (cancellation is not an Exception)
        log.error("sync run crashed: %s", type(e).__name__)


async def sync_loop(app: Starlette) -> None:
    cfg: Config = app.state.cfg
    if cfg.sync_on_startup:
        await _sync_once(app)
    while True:
        await asyncio.sleep(max(30, cfg.sync_interval_seconds))
        await _sync_once(app)


def create_app(cfg: Optional[Config] = None, hcp=None, geocoder=None, background_sync: bool = True,
               routes=None) -> Starlette:
    cfg = cfg or load_config()
    if cfg.session_secret_generated:
        log.warning("SESSION_SECRET is not set: using a random one, so everyone is logged out whenever the server "
                    "restarts. Set SESSION_SECRET in .env for stable sessions.")
    db = Database(cfg.database_path)
    with db.session() as conn:
        seed_durations(conn)
        tz_name = get_settings(conn)["timezone"]
        if cfg.hcp_mode == "live":              # never show demo data next to real customers (even if the key is wrong)
            gone = purge_demo_data(conn)
            if any(gone.values()):
                log.warning("Removed demo data left from an earlier demo run (%d jobs, %d technicians).",
                            gone["jobs"], gone["technicians"])
        elif has_real_data(conn):
            log.warning("HCP_MODE is not 'live' but this database holds real Housecall Pro data: demo data will NOT be "
                        "loaded into it. Set HCP_MODE=live to use it.")
    hcp = hcp or make_hcp_client(cfg, tz_name)
    geocoder = geocoder or make_geocoder(cfg)
    sync = SyncService(db, cfg, hcp, geocoder,
                       new_tech_defaults=(lambda eid: DEMO_TECH_SETUP.get(eid)) if cfg.hcp_mode == "mock" else None)

    @asynccontextmanager
    async def lifespan(app):
        msg = await asyncio.to_thread(bootstrap_users, db, cfg)
        if msg:
            print(f"\n  >>> {msg}\n", flush=True)
        task = asyncio.create_task(sync_loop(app)) if background_sync else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    async def index(request: Request) -> Response:
        return FileResponse(f"{cfg.web_dir}/index.html", headers={"Cache-Control": "no-cache"})

    class NoCacheStatic(StaticFiles):
        async def get_response(self, path, scope):
            resp = await super().get_response(path, scope)
            resp.headers["Cache-Control"] = "no-cache"
            return resp

    app = Starlette(
        routes=[*ROUTES, Route("/", index), Mount("/static", NoCacheStatic(directory=cfg.web_dir), name="static")],
        middleware=[
            Middleware(SecurityHeadersMiddleware, https_only=cfg.session_https_only),
            Middleware(SessionMiddleware, secret_key=cfg.session_secret, session_cookie="dispatch_session",
                       max_age=12 * 3600, same_site="lax", https_only=cfg.session_https_only),
            Middleware(CSRFMiddleware),
        ],
        lifespan=lifespan,
    )
    app.state.cfg, app.state.db, app.state.hcp = cfg, db, hcp
    app.state.geocoder, app.state.sync = geocoder, sync
    app.state.routes = routes or make_road_routes(cfg)
    app.state.login_limiter = LoginLimiter()
    return app


def get_app() -> Starlette:  # for: uvicorn app.main:get_app --factory
    return create_app()
