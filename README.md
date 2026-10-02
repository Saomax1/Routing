# Routing App

An in-house dispatch assistant for a home-service company that uses Housecall Pro (HCP). It shows unscheduled and
scheduled jobs on a map, reads AHS / Frontdoor-style warranty descriptions, ranks the unscheduled queue by urgency, and
suggests the best technician and time slot for each job using cheapest-insertion routing.

**Status: Phase 0 tooling + Phase 1 (read-only MVP).** The app never writes to Housecall Pro. Write-back, webhooks,
cluster suggestions and the compliance tracker are Phase 2 and are not built (see [Known gaps](#known-gaps)).

## Quick start (demo data, no API key needed)

Requires Python 3.11+ (developed on 3.13).

```bash
python -m venv .venv && source .venv/bin/activate      # optional but recommended
pip install -r requirements.txt
cp .env.example .env                                   # defaults to HCP_MODE=mock
python -m app                                          # run from the repo root
```

**Windows:** type these in *Command Prompt* (press the Windows key, type `cmd`), not in the "Python" app (its lines start
with `>>>` and it only understands Python, so shell commands give `SyntaxError: invalid syntax`). Use
`.venv\Scripts\activate` instead of `source .venv/bin/activate` and `copy .env.example .env` instead of `cp`. If `python`
is not recognized, use `py`. Windows has no built-in time zone database; the `tzdata` package in `requirements.txt`
supplies it (without it you get `No time zone found with key America/Phoenix`: run `pip install tzdata`).

Open <http://127.0.0.1:8000>. On first start in mock mode the app loads sanitized fake data and prints a one-time demo
login (`admin@example.com` plus a random password) to the terminal. Copy it from there; it is not shown again.
To pick your own admin login instead, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env` before the first start, or run
`python scripts/create_user.py`.

`python scripts/seed.py` loads the demo data into a fresh database without starting the server (always mock, never
touches real data).

## Running the tests

```bash
python -m unittest discover -s tests -t .
```

146 tests cover the warranty parser, scoring, travel and slot engines, deadline exceptions, area totals, the sync
pipeline and the API (auth, roles, CSRF, rate limiting). All fixtures are sanitized fake data. The suite uses only the standard library `unittest`.

## Connecting to your real Housecall Pro account

Do this in order. Step 1 matters: **the HCP request details in this code are unverified assumptions** (base URL, `Token`
auth header, `/jobs` and `/employees` paths, query parameter names, and where the warranty text lives in a job).

1. **Run the Phase 0 probe first.** Put your key in `.env` (never in chat, email or git), then:

   ```bash
   python scripts/phase0_probe.py --limit 30 --show-failures
   ```

   It is read-only. It checks that the key works, prints the *shape* of the data (field names and types, no customer
   values), shows where the warranty text lives, and dry-runs the parser on real descriptions (shown with names and
   phone numbers redacted). It writes `data/phase0_report.json`, which contains structure and counts only and is safe
   to share. Use `--mock` to rehearse the script against the demo data.
2. **Fix anything that did not match.** Adjust `app/hcp/client.py` (auth scheme, paths, parameters) and
   `app/hcp/normalize.py` (field names). Everything HCP-specific is isolated in those two files and `app/hcp/http.py`.
3. **Pick a real geocoder.** `GEOCODER=census` is free (US addresses). `google` or `mapbox` need `MAPS_API_KEY`. With
   `mock`, pins only land on city centers.
4. Set `HCP_MODE=live`, `HCP_API_KEY=...`, restart. The first sync pulls employees, unscheduled jobs and the next
   `SCHEDULED_WINDOW_DAYS` of scheduled jobs, then repeats every `SYNC_INTERVAL_SECONDS`.
5. **Set up technicians** under *Admin > Technicians*. New live technicians start with routing OFF until an admin sets
   their trade skills, home base, shift hours, work days and max jobs per day. Without that, they will never be
   suggested.
6. **Confirm the deadline rules** under *Admin > Settings*: Normal is 48 h and AHS Emergency has no clock; the
   Expedited and Direct hours are still placeholders (see below).

## Environment variables

See [`.env.example`](.env.example) for the full annotated list. The important ones:

| Variable | Purpose |
| --- | --- |
| `HCP_MODE` | `mock` (demo data, default) or `live` |
| `HCP_API_KEY` | Housecall Pro API key. Server-side only. |
| `GEOCODER`, `MAPS_API_KEY` | `mock`, `census`, `google` or `mapbox` |
| `DATABASE_PATH` | SQLite file (default `data/routing.db`) |
| `SESSION_SECRET` | Signs login cookies. If empty, a random one is generated each start and everyone is logged out on restart. |
| `SESSION_HTTPS_ONLY` | `true` when served over HTTPS (required for `APP_ENV=production`) |
| `WEBHOOK_SECRET` | Reserved for Phase 2 |
| `LLM_API_KEY`, `LLM_MODEL` | Optional AI fallback for unreadable warranty text. Off unless **both** are set. |

## How it works

- **Warranty parser** (`app/domain/warranty_parser.py`): reads AHS / Frontdoor descriptions into dispatch priority, trade,
  items, authorization limits, address and contact. Priority comes from the `Dispatch Priority:` line in the body, not the
  header. Anything the regex cannot read is flagged in *Parse review*, where a dispatcher can fix it and mark it reviewed.
- **Scoring** (`app/domain/scoring.py`): urgency = base by priority + points as the deadline approaches + urgency
  keywords + age. Every score shows its breakdown in the job card.
- **Deadlines are targets, not hard limits.** Normal warranty calls have a 48 h window; AHS Emergency has no deadline
  clock (it is ranked by its base score alone). When a customer is not available, or there is any other reason to book
  later, a dispatcher opens the job card, chooses *Scheduling this outside the window?*, picks a reason and optionally
  adds a note. That job then shows *outside window* instead of overdue, earns no deadline points and is no longer
  penalized by the slot finder. It is saved in this app only (never written to Housecall Pro), records who marked it,
  and survives syncs. *Remove* puts the job back on the normal deadline.
- **Areas** (`Dispatch > Areas` tab, `GET /api/areas`): a running total of unscheduled calls per area (city by default,
  or ZIP under *Admin > Settings > Areas*), with overdue / due-soon / trade counts and the soonest technician openings
  in each area for the next 1-14 days, so you can see where it pays to send someone first. Each call is checked on its
  own against skills, shifts and existing routes, so openings are a guide, not a booking plan (calls share the same
  technicians). Click an area to filter the queue and map to it; the queue's *All areas* filter shows the same totals.
  Calls with no map location are counted but cannot be checked for openings.
- **Slot finder** (`app/domain/slots.py`): for each eligible technician and day, tries the job in every gap of the route
  (previous stop or home base -> new job -> next stop). Existing start times stay fixed. It checks skills, shift hours,
  work days, max jobs and same-day lead time, and ranks by added drive time plus a per-day delay penalty (so Emergency
  jobs prefer today) plus a penalty for finishing after the deadline (skipped for jobs marked outside the window). If a
  job is already past its window, options are ranked by speed and cost and the card says so. The Areas tab runs the same
  engine in a "soonest opening" mode.
- **Map**: a small built-in slippy map. Pin shape = source (square AHS, diamond other warranty, circle direct), color =
  priority, ring = deadline status. The tile source is `map.tile_url` in Settings (OpenStreetMap by default; swap it for
  a commercial tile provider for heavier use).
- **Sync** (`app/services/sync.py`): pulls from HCP, geocodes with a cache, deduplicates by description hash, and
  deactivates jobs that HCP no longer returns.

## Security notes

- HCP, maps and LLM keys stay on the server. They are redacted from config logging and never reach the browser or git.
- Login with `admin` and `dispatcher` roles; passwords hashed with scrypt (minimum 10 characters); failed logins are
  rate-limited; the session ID rotates on login.
- Mutating `/api` calls require the `X-Requested-With: routing-app` header (CSRF defense); strict CSP and security
  headers on every response; generic 500 responses.
- Full warranty descriptions and phone numbers are not written to logs. The front end renders HCP data with `textContent`
  only, since HCP text is untrusted.
- Put it behind HTTPS before exposing it beyond your own machine, and set `SESSION_HTTPS_ONLY=true`.
- The HCP client has no write methods; the ones that exist raise `NotImplementedError`. Phase 2 write-back must only
  run on an explicit dispatcher action with a confirmation step.

## Known gaps

- **Some deadline rules are still placeholders.** Normal warranty calls are 48 h and AHS Emergency has no clock, but
  Expedited (24 h) and direct jobs (24 h) are guesses: confirm them under *Admin > Settings*. With no clock, an AHS
  Emergency no longer picks up deadline points, so an overdue Expedited job can tie with a fresh Emergency in the
  queue. Raise *Base: Emergency* under *Priority score* if you want Emergency to stay on top.
- **Travel times are straight-line (Haversine) estimates**, not road routing. The `TravelTimeProvider` interface in
  `app/domain/travel.py` is where a routing API goes.
- **Stack deviation:** the original plan assumed FastAPI + React + Leaflet. The build sandbox could not install packages
  from PyPI or npm, so this uses Starlette + stdlib `sqlite3` + plain ES modules + a built-in map. Everything is
  standard and can be moved to a bigger stack later; the pure engines in `app/domain/` do not depend on the web layer.
- **Not built yet (Phase 2):** write-back to HCP, `/api/webhooks`, route-cluster suggestions beyond the Areas tab,
  compliance tracker.
- **Not built yet (Phase 3):** schedule optimization and reporting.
- Map tiles load from the internet; in a locked-down network they will not, and the map shows a notice.

## Layout

```
app/            backend (api.py, main.py, config.py, db.py, security.py)
app/domain/     pure logic: parser, scoring, travel, slots, time helpers
app/hcp/        HCP client (live + mock), normalizer, demo fixtures
app/services/   sync, geocoding, AI fallback, dispatch views, settings
scripts/        phase0_probe.py, seed.py, create_user.py
web/            no-build front end (index.html, css/, js/)
tests/          unittest suite + sanitized fixtures
```
