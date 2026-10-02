"""
Runtime configuration, read from environment variables (and an optional .env file).

SECURITY: HCP_API_KEY / MAPS_API_KEY / LLM_API_KEY live only here, server-side. They are never
sent to the browser, never logged, and are redacted from ``repr(config)``.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("routing.config")

ROOT = Path(__file__).resolve().parent.parent

try:  # python-dotenv is optional; plain environment variables work too
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name)
    return default if v == "" else v.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


_SECRET_FIELDS = {"hcp_api_key", "maps_api_key", "llm_api_key", "session_secret", "webhook_secret"}


@dataclass
class Config:
    # HCP: "mock" runs entirely on sanitized demo data (no network); "live" calls Housecall Pro.
    hcp_mode: str = "mock"
    hcp_api_key: str = ""
    hcp_base_url: str = "https://api.housecallpro.com"
    hcp_auth_scheme: str = "Token"       # UNVERIFIED: confirm in Phase 0 (probe script prints the result)
    hcp_page_size: int = 100

    # Geocoding: mock | census | google | mapbox
    geocoder: str = "mock"
    maps_api_key: str = ""

    # Road routes drawn between jobs (display + drive time on hover): none | osrm | mapbox.
    # osrm = any OSRM server (default: the public demo server, fair-use only); mapbox uses MAPS_API_KEY.
    # NOTE: stop coordinates (customer locations) are sent to the provider, so with live data it is opt-in.
    router: str = "none"
    router_url: str = "https://router.project-osrm.org"

    # Optional LLM fallback for warranty text the regex parser cannot read. OFF unless key+model set.
    llm_api_key: str = ""
    llm_model: str = ""
    llm_api_url: str = "https://api.anthropic.com/v1/messages"

    database_path: str = str(ROOT / "data" / "routing.db")
    session_secret: str = ""
    session_https_only: bool = False     # set true behind HTTPS in production
    webhook_secret: str = ""

    sync_interval_seconds: int = 300
    sync_on_startup: bool = True
    scheduled_window_days: int = 14
    completed_lookback_days: int = 3     # how far back each sync looks for jobs HCP has marked complete

    host: str = "127.0.0.1"
    port: int = 8000
    env: str = "development"

    # First-run admin account (created by scripts/seed.py / on startup if no users exist)
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    web_dir: str = str(ROOT / "web")
    session_secret_generated: bool = False
    secrets: set = field(default_factory=lambda: set(_SECRET_FIELDS), repr=False)

    def __repr__(self) -> str:  # never leak keys into logs / tracebacks
        parts = []
        for k, v in self.__dict__.items():
            if k == "secrets":
                continue
            parts.append(f"{k}={'***' if (k in _SECRET_FIELDS and v) else v!r}")
        return f"Config({', '.join(parts)})"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def load_config() -> Config:
    cfg = Config(
        hcp_mode=_env("HCP_MODE", "mock").lower(),
        hcp_api_key=_env("HCP_API_KEY"),
        hcp_base_url=_env("HCP_BASE_URL", "https://api.housecallpro.com").rstrip("/"),
        hcp_auth_scheme=_env("HCP_AUTH_SCHEME", "Token"),
        hcp_page_size=_env_int("HCP_PAGE_SIZE", 100),
        geocoder=_env("GEOCODER", "mock").lower(),
        maps_api_key=_env("MAPS_API_KEY"),
        router=_env("ROUTER", "osrm" if _env("HCP_MODE", "mock").lower() == "mock" else "none").lower(),
        router_url=_env("ROUTER_URL", "https://router.project-osrm.org").rstrip("/"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        llm_api_url=_env("LLM_API_URL", "https://api.anthropic.com/v1/messages"),
        database_path=_env("DATABASE_PATH", str(ROOT / "data" / "routing.db")),
        session_secret=_env("SESSION_SECRET"),
        session_https_only=_env_bool("SESSION_HTTPS_ONLY", False),
        webhook_secret=_env("WEBHOOK_SECRET"),
        sync_interval_seconds=_env_int("SYNC_INTERVAL_SECONDS", 300),
        sync_on_startup=_env_bool("SYNC_ON_STARTUP", True),
        scheduled_window_days=_env_int("SCHEDULED_WINDOW_DAYS", 14),
        completed_lookback_days=max(0, _env_int("COMPLETED_LOOKBACK_DAYS", 3)),
        host=_env("HOST", "127.0.0.1"),
        port=_env_int("PORT", 8000),
        env=_env("APP_ENV", "development").lower(),
        bootstrap_admin_email=_env("ADMIN_EMAIL"),
        bootstrap_admin_password=_env("ADMIN_PASSWORD"),
    )
    problems = validate_config(cfg)
    for p in problems:
        log.warning("config: %s", p)
    if not cfg.session_secret:
        cfg.session_secret = secrets.token_urlsafe(48)
        cfg.session_secret_generated = True   # the server warns about this at startup (see main.create_app)
    return cfg


def validate_config(cfg: Config) -> list:
    problems = []
    if cfg.hcp_mode not in ("mock", "live"):
        problems.append(f"HCP_MODE must be 'mock' or 'live' (got {cfg.hcp_mode!r})")
    if cfg.hcp_mode == "live" and not cfg.hcp_api_key:
        problems.append("HCP_MODE=live but HCP_API_KEY is empty")
    if cfg.geocoder not in ("mock", "census", "google", "mapbox"):
        problems.append(f"GEOCODER must be mock|census|google|mapbox (got {cfg.geocoder!r})")
    if cfg.geocoder in ("google", "mapbox") and not cfg.maps_api_key:
        problems.append(f"GEOCODER={cfg.geocoder} needs MAPS_API_KEY")
    if cfg.router not in ("none", "osrm", "mapbox"):
        problems.append(f"ROUTER must be none|osrm|mapbox (got {cfg.router!r})")
    if cfg.router == "mapbox" and not cfg.maps_api_key:
        problems.append("ROUTER=mapbox needs MAPS_API_KEY")
    if cfg.router == "osrm" and not cfg.router_url.startswith(("http://", "https://")):
        problems.append("ROUTER_URL must start with http:// or https://")
    if cfg.router == "osrm" and cfg.hcp_mode == "live" and "router.project-osrm.org" in cfg.router_url:
        problems.append("ROUTER=osrm points at the public OSRM demo server: it is fair-use only and customer "
                        "locations are sent to a third party. For daily use host your own OSRM or use mapbox.")
    if cfg.hcp_mode == "live" and cfg.geocoder == "mock":
        problems.append("Live HCP data with GEOCODER=mock will place pins at city centers only; "
                        "use census, google or mapbox")
    if cfg.env == "production" and not cfg.session_https_only:
        problems.append("APP_ENV=production but SESSION_HTTPS_ONLY is false")
    return problems
