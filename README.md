# Routing App

An in-house dispatch assistant for a home-service company that uses Housecall Pro (HCP). It shows unscheduled and
scheduled jobs on a map, reads AHS / Frontdoor-style warranty descriptions, ranks the unscheduled queue by urgency, and
suggests the best technician and time slot for each job using cheapest-insertion routing.

**Status: Phase 0 tooling + Phase 1 (read-only MVP).** The app never writes to Housecall Pro. A dispatcher can confirm a
suggested slot, which books the job here and adds it to the technician's route in this app (see *Confirming a slot*
below); it still has to be entered in Housecall Pro. Write-back, webhooks, cluster suggestions and the compliance tracker
are Phase 2 and are not built (see [Known gaps](#known-gaps)).

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

330 tests cover the warranty parser, private-notes handling, scoring, travel and slot engines, arrival windows, completed jobs, deadline
exceptions, area totals, slot confirmation, road routing, the sync pipeline, the read-only live connection (checked over real HTTP against a stand-in Housecall Pro server), the go-live and live-check scripts, and the API (auth, roles, CSRF, rate limiting). All fixtures are sanitized fake data. The suite uses
only the standard library `unittest`.

## Connecting to your real Housecall Pro account (read-only)

The app only ever **reads** Housecall Pro. Its connection to Housecall Pro is limited to `GET` requests in code
(`ReadOnlyTransport` in `app/hcp/http.py`: any other method is refused before a request is built), and the tests prove it
over real HTTP against a stand-in server that records everything it receives. What a *key* is allowed to do is decided by
Housecall Pro, not by this app: if Housecall Pro lets you limit an API key to read access, use such a key. API access
is reported to need the MAX plan, and an Admin user generates the key.

**One command does the switch:**

```bash
python scripts/go_live.py
```

It asks for your API key (typed hidden: not shown, never printed, never sent anywhere but Housecall Pro) and tests it with
a single read of the employee list. Then it asks how addresses become map pins and whether to draw road routes (both
send customer locations to the provider you pick, so it explains each choice; road routes default to off). It then
**removes the fake demo customers, jobs and technicians** from the database, offers to replace the demo login
(`admin@example.com`) with your own, and saves the settings to `.env` (never committed to git). If the key is rejected
it changes nothing.

Then:

1. **Start the app:** `python -m app`. The first sync loads your real employees, unscheduled jobs, the next
   `SCHEDULED_WINDOW_DAYS` of scheduled / in-progress jobs, and jobs marked complete over the last
   `COMPLETED_LOOKBACK_DAYS`, then repeats every `SYNC_INTERVAL_SECONDS`. The badge top right changes from *DEMO DATA* to
   *LIVE · read-only*.
2. **Set up technicians** under *Admin > Technicians*. Real technicians start with routing OFF until an admin sets their
   trade skills, home base, shift hours, work days and max jobs per day. Without that, they will never be suggested.
3. **Test routing on your real data:** `python scripts/live_check.py`. It syncs, then checks that jobs loaded and their
   addresses became pins, which technicians are ready, and puts the slot finder through its paces on your most urgent
   real jobs: every suggested slot is checked against the rules it must keep (inside the window and the shift, a working
   day, the right trade skill, under the daily maximum). With a road router configured it also compares real road
   drive times with the straight-line estimate the slot finder uses and suggests a travel speed if they differ
   (*Admin > Settings*), and it flags pins that are over 100 miles from every technician (a wrongly placed address). It prints
   PASS / WARN / FAIL lines and writes `data/live_check_report.json`: counts, timings and ratios only, with no customer
   names, addresses, phone numbers or job ids, so it is safe to share if something needs fixing.
   `--no-sync` uses what the app already loaded; `--jobs 10` tests more jobs; `--mock` rehearses the script on the demo
   data in a temporary database.
4. **If something does not match** (the HCP request details in this code were written from documentation and have not yet
   been seen against a live account): run `python scripts/phase0_probe.py --limit 30 --show-failures`. It is read-only and
   prints the *shape* of your data (field names and types, no customer values), shows where the warranty text lives, and
   dry-runs the parser on real descriptions with names and phone numbers redacted. Fix `app/hcp/client.py` (auth scheme,
   paths, parameters) or `app/hcp/normalize.py` (field names); everything HCP-specific is isolated there and in
   `app/hcp/http.py`.
5. **Check the job types and deadline rules** under *Admin > Settings*: the three warranty tag names (*Job types*), and the
   hours and scores for Expedited, Normal, Recall and Retail. Normal is 48 h; the others are placeholders (see below).

**Demo data and real data never mix.** Demo rows are recognised only by their ids (`job_demo_*`, `emp_demo_*`, which
Housecall Pro never uses). In live mode the app deletes them at every start and sync (and the caches built from the fake
addresses), and only ever those. In demo mode the app refuses to load demo data into a database that already holds real
Housecall Pro data, so flipping `.env` back to mock by mistake cannot put fake jobs into your real schedule: use another
`DATABASE_PATH` for demos. Logins, settings and technician set-up for real people are never touched.

Prefer to do it by hand? Set `HCP_MODE=live`, `HCP_API_KEY=...`, `GEOCODER=census` (and optionally `ROUTER`) in `.env` and
restart; the same cleanup applies.

### Testing against real data without it reaching GitHub

If you test with a real key somewhere that also pushes to GitHub (a cloud session, a shared machine), turn on the leak guard first:

```bash
python scripts/install_git_guard.py
```

Git then runs `scripts/leak_check.py` before every commit, every commit message and every push. It learns what to look for from the
real data on that machine (the API keys in the environment, and from the database every real customer name, phone number and
street address, warranty contact, technician home address, login email and dispatcher note) and **blocks the commit or push if any
of it appears**, in a file, a file name or a message. It never prints what it found, only the kind and where. It also refuses any
binary file (a screenshot of the real app can show customers and cannot be scanned), and **it fails closed**: with an API key set
but no readable customer database it refuses instead of passing. The push check still catches commits made with `--no-verify`.
Keep the live database and the reports outside the repository (`DATABASE_PATH=...`, `--out ...`); `data/`, `.env`, `*.db` and
the report files are git-ignored anyway. `CLAUDE.md` carries these rules and the step-by-step live-test procedure for any new
Claude session on this repo.

## Environment variables

See [`.env.example`](.env.example) for the full annotated list. The important ones:

| Variable | Purpose |
| --- | --- |
| `HCP_MODE` | `mock` (demo data, default) or `live` |
| `HCP_API_KEY` | Housecall Pro API key. Server-side only. |
| `GEOCODER`, `MAPS_API_KEY` | `mock`, `census`, `google` or `mapbox` |
| `ROUTER`, `ROUTER_URL` | Road routes on the map: `none`, `osrm` (any OSRM server; default is the public demo server) or `mapbox` (uses `MAPS_API_KEY`). Unset = `osrm` in demo mode, `none` with live data. |
| `COMPLETED_LOOKBACK_DAYS` | How many days back each sync looks for jobs HCP has marked complete (default 3). |
| `DATABASE_PATH` | SQLite file (default `data/routing.db`) |
| `SESSION_SECRET` | Signs login cookies. If empty, a random one is generated each start and everyone is logged out on restart. |
| `SESSION_HTTPS_ONLY` | `true` when served over HTTPS (required for `APP_ENV=production`) |
| `WEBHOOK_SECRET` | Reserved for Phase 2 |
| `LLM_API_KEY`, `LLM_MODEL` | Optional AI fallback for unreadable warranty text. Off unless **both** are set. |

## How it works

- **Warranty text in private notes** (`app/hcp/normalize.py`): warranty companies' dispatch text is often pasted into a job's
  *private notes* rather than its description, so the app reads both: the job description, plus any note that looks like a
  warranty dispatch (`Dispatch Priority:`, `Covered Property Address`, a `xxx:12345` dispatch line, ...). **Every other note is
  dropped before anything is stored, shown or logged**: a gate code, a remark about the customer or a payment note never
  reaches the database, the job card, the logs or the optional AI fallback. Notes are read from the job record
  (`notes`, also `private_notes` / `internal_notes` / `job_notes`; text, a list of strings or a list of `{content|text|note|body}`).
  This is still read-only: it is the same `GET /jobs` as before. Whether Housecall Pro includes private notes in the job list
  for your key is up to Housecall Pro: `python scripts/phase0_probe.py` (step 4) tells you how many of your jobs came back
  with notes and where the dispatch text was found, and `scripts/live_check.py` warns if no warranty text is found at all.
- **Job types** (`app/domain/jobkind.py`): every job is exactly one of **Expedited**, **Normal**, **Recall** (warranty work) or
  **Retail**, decided in one place from its Housecall Pro tags. Warranty work carries one of three tags (`normal: expedited`,
  `normal: normal`, `normal: recall`); matching ignores capitals and spacing around the colon, and the tag names are editable
  under *Admin > Settings > Job types*. **A job without one of those tags is Retail**: a call that did not come from a warranty
  company, which means either a warranty call that was turned into a retail job or a lead from an ad. Ad leads are told apart
  by the `meta lead` tag (also editable): they are Retail, marked *Ad lead* on the job card. If a job has more than one warranty
  tag the most urgent wins. The warranty *text* does not decide the type: a converted job usually still carries its old dispatch
  text, so it is shown as Retail (with a note on the card that the text is there), and its "do not collect the fee" alerts are
  not shown. `live_check.py` and the probe show how your jobs sort and tell you if none match a warranty tag, so a tag spelled
  differently from the Settings is caught quickly. The type is worked out when a job is read, so changing the tag names
  applies at once.
- **Warranty parser** (`app/domain/warranty_parser.py`): reads AHS / Frontdoor descriptions into dispatch priority, trade,
  items, authorization limits, address and contact. The `Dispatch Priority:` line in the body is shown on the job card but no
  longer sets the type (the tag does). Anything the regex cannot read is flagged in *Parse review*, where a dispatcher can fix it and mark it reviewed.
- **Scoring** (`app/domain/scoring.py`): urgency = base by type (Expedited, Recall, Normal, Retail) + points as the deadline
  approaches + urgency keywords + age. Every score shows its breakdown in the job card. It only orders the queue; the list does
  not show it.
- **Deadlines are targets, not hard limits.** Normal warranty calls have a 48 h window (Expedited, Recall and Retail have
  placeholder windows: see Known gaps). When a customer is not available, or there is any other reason to book
  later, a dispatcher opens the job card, chooses *Booking this past its deadline?*, picks a reason and optionally
  adds a note. That job then shows *deadline waived* instead of overdue, earns no deadline points and is no longer
  penalized by the slot finder. It is saved in this app only (never written to Housecall Pro), records who marked it,
  and survives syncs. *Remove* puts the job back on the normal deadline.
- **Areas** (`Dispatch > Areas` tab, `GET /api/areas`): a running total of unscheduled calls per area (city by default,
  or ZIP under *Admin > Settings > Areas*), with how many calls of each type and the soonest technician openings
  in each area for the next 1-14 days, so you can see where it pays to send someone first. Each call is checked on its
  own against skills, shifts and existing routes, so openings are a guide, not a booking plan (calls share the same
  technicians). Click an area to filter the queue and map to it; the queue's *All areas* filter shows the same totals.
  Calls with no map location are counted but cannot be checked for openings.
- **Arrival windows** (`app/domain/slots.py`): a customer is given a window ("between 8 AM and 12 PM"), not a time. The
  standard is 4 hours (*Admin > Settings > Find best slot*); a dispatcher can pick a different length for one job next to
  *Find best slot*. Windows start on the hour by default (change *Windows start every*), may overlap (job 1 8-12, job 2
  10-2, or two jobs both 8-12), and never run past the end of the technician's shift. Existing jobs use HCP's
  `arrival_window` (minutes after `scheduled_start`); a job with none gets the standard window. The scheduled end is read
  as the job's length, not the window. Suggestions only: windows are not written to HCP.
- **Slot finder** (`app/domain/slots.py`): for each eligible technician and day, tries the job at every position of the
  route (home base or last finished job -> ... -> new job -> ...). A position is feasible if every open stop is still
  reached inside its own window, driving and working straight through, and the day ends inside the shift. A stop that
  is already late by our (estimated) travel times is tolerated at the arrival it already has: inserting a job may never
  make it later. It also checks skills, work days, max jobs (finished jobs count) and same-day lead time, and ranks by
  added drive time plus a per-day delay penalty (so Expedited jobs prefer today) plus a penalty for finishing after the
  deadline (skipped for jobs whose deadline is waived). Each option shows the window to book, the planned arrival
  ("arrive about 9:30"), and - if a nearby job (within *Offer the same window within*, 20 min of driving by default) has
  a window holding that arrival - the same window start, with a note like "same window as Jane (12 min away)"; windows
  that merely overlap are labelled as such. If a job is already past its deadline, options are ranked by speed and cost
  and the card says so. The Areas tab runs the same engine in a "soonest opening" mode.
- **Confirming a slot** (`app/services/confirm_slot.py`, `app/services/bookings.py`,
  `POST /api/jobs/{id}/booking`): picking a slot card only previews it on the map. A box under it lists what will be booked
  (technician, day, the window the customer is told, planned arrival, place on the route, an optional note); only its
  **Confirm booking** button books it. The job then leaves the unscheduled queue and the area totals, becomes a dashed
  stop on that technician's route, and every later *Find best slot* plans around it. The browser only says which
  suggestion it saw; the server searches that technician's day again under a write lock and books only if the same
  window and neighbouring stops are still on offer, using its own recomputed times. So two dispatchers cannot take the
  same hole, and a route that changed meanwhile (another booking, a sync, time passing) gives "no longer available, find
  best slot again" and refreshes the options, never a wrong booking. **A booking lives in this app only**: Housecall
  Pro is not updated, so the *Booked* tab lists what still has to be entered there (with an *Open in Housecall Pro*
  link). A booking is dropped as soon as a sync shows the job scheduled in Housecall Pro (Housecall Pro is the truth),
  and once its arrival window has ended without that, the job goes back to the queue instead of staying hidden.
  *Remove booking* (job card or Booked tab, after a confirmation) puts it back at once. Who booked or removed what, and
  when, is kept in the `schedule_actions` table.
- **Completed jobs** (`app/services/sync.py`): each sync also pulls jobs HCP has marked complete (last
  `COMPLETED_LOOKBACK_DAYS`) and marks them `complete` here with their completion time. They stay on the map as dimmed
  check-marks in their technician's route (the toolbar shows "N completed", each technician chip "N done"), earlier
  days can be browsed from the date picker, and the slot finder treats them as finished: they count toward the daily
  maximum and today's route continues from the last finished job. A job under way (`in progress`) keeps its technician
  busy until it ends. If the completed-jobs call fails (the HCP status names are unverified) the rest of the sync still
  runs and the run is noted as partial; finished jobs are never hidden because of it.
- **Dispatch screen** (left list and map): the list shows one plain row per unscheduled call: its type (Expedited, Normal,
  Recall or Retail), the customer and the address, with a filter for type and one for area. Everything else (trade, deadline,
  score, warranty details, ad-lead mark, slot finder) is on the job card, which opens when a row is clicked. Every unscheduled
  call is the same **red "!"** on the map, and no other colour is used for it: colour only appears once a job is on a technician's
  route, in that technician's colour.
- **Map**: a small built-in slippy map. The tile source is `map.tile_url` in Settings (OpenStreetMap by default).
  Tile requests send the site's address (no path) as the `Referer`, which OpenStreetMap requires; without it OSM
  answers "403 Access blocked". OSM's free servers are for light use only, so for daily team use pick a commercial
  tile provider (Stadia, MapTiler, Mapbox...) and paste its URL into `map.tile_url`.
- **Road routes** (`app/services/routing.py`, `POST /api/routes`): the line between a technician's stops follows the
  roads, and hovering any leg shows its drive time and distance (also on the dashed preview when you hover or pin a
  slot; a technician's chip shows the day's total by road). The page asks its own server, which asks the routing
  provider (`ROUTER`), so keys never reach the browser. Lines start straight and snap to the roads a moment later.
  Every leg is cached in the database for 30 days, so it is fetched once. If the provider is off, rejecting us or
  unreachable, the leg stays straight and the tooltip says it is a straight-line estimate; after a failure the provider
  is skipped for a minute so a dead server cannot slow the page down. This is display only: the slot finder and the
  Areas tab still rank with straight-line estimates, so a slot card's "+4 min driving" can differ from the road time
  on the line.
- **Sync** (`app/services/sync.py`): pulls from HCP, geocodes with a cache, deduplicates by description hash, and
  deactivates open jobs that HCP no longer returns (completed jobs are history and stay).

## Security notes

- HCP, maps and LLM keys stay on the server. They are redacted from config logging and never reach the browser or git.
- Road routing sends stop coordinates (customer locations) to the routing provider (`ROUTER`), and only to it. Errors are
  logged by type only, never with coordinates. With live data it stays off until you set `ROUTER`.
- Real customer data and keys are kept out of git by a guard that checks every commit, message and push against the live data
  (see *Testing against real data without it reaching GitHub*).
- Login with `admin` and `dispatcher` roles; passwords hashed with scrypt (minimum 10 characters); failed logins are
  rate-limited; the session ID rotates on login.
- Mutating `/api` calls require the `X-Requested-With: routing-app` header (CSRF defense); strict CSP and security
  headers on every response; generic 500 responses.
- Full warranty descriptions and phone numbers are not written to logs. The front end renders HCP data with `textContent`
  only, since HCP text is untrusted.
- Put it behind HTTPS before exposing it beyond your own machine, and set `SESSION_HTTPS_ONLY=true`.
- The HCP client is read-only by construction: its transport refuses everything but `GET` (and a `GET` with a body), the
  write methods that exist raise `NotImplementedError`, and a test fails if the client ever gains a public method that is
  not a plain read. The API key is sent only to Housecall Pro, in the `Authorization` header (never in a URL), and
  `scripts/go_live.py` reads it hidden and writes it only to `.env`. Confirming a slot saves the
  booking in this app's database only. Phase 2 write-back must only run on an explicit dispatcher action with a
  confirmation step: the confirm box and the server-side re-check are that step.

## Known gaps

- **Some deadline rules and scores are still placeholders.** Normal warranty calls are 48 h. Expedited (24 h), Recall (24 h,
  base score 40, delay penalty 60) and Retail (24 h) are guesses: confirm them under *Admin > Settings*. There is no longer an
  Emergency type: warranty work is Expedited, Normal or Recall, as tagged in Housecall Pro.
- **The slot finder and Areas tab still use straight-line (Haversine) travel estimates.** Map lines and their hover
  times use real roads, but ranking does not yet. Using road times there needs a distance-matrix call (OSRM `table`,
  Mapbox Matrix) behind the `TravelTimeProvider` interface in `app/domain/travel.py`.
- **The public OSRM demo server is fair-use only.** It is fine to try and for light use; for daily use host your own OSRM
  (or use `ROUTER=mapbox`).
- **Stack deviation:** the original plan assumed FastAPI + React + Leaflet. The build sandbox could not install packages
  from PyPI or npm, so this uses Starlette + stdlib `sqlite3` + plain ES modules + a built-in map. Everything is
  standard and can be moved to a bigger stack later; the pure engines in `app/domain/` do not depend on the web layer.
- **A confirmed booking is not sent to Housecall Pro.** Until Phase 2 write-back exists (it needs the HCP scheduling
  and dispatch calls verified with the Phase 0 probe first), someone must enter each booking in Housecall Pro, so the
  technician's own app and the customer notices do not know about it. The *Booked* tab is that to-do list.
- **Not built yet (Phase 2):** write-back to HCP, `/api/webhooks`, route-cluster suggestions beyond the Areas tab,
  compliance tracker.
- **Not built yet (Phase 3):** schedule optimization and reporting.
- Map tiles load from the internet; in a locked-down network they will not, and the map shows a notice.

## Layout

```
app/            backend (api.py, main.py, config.py, db.py, security.py)
app/domain/     pure logic: parser, scoring, travel, slots, time helpers
app/hcp/        HCP client (live + mock), normalizer, demo fixtures
app/services/   sync, geocoding, AI fallback, dispatch views, settings, bookings, slot confirmation, road routes
scripts/        go_live.py, live_check.py, phase0_probe.py, seed.py, create_user.py, leak_check.py + githooks/ (the leak guard)
web/            no-build front end (index.html, css/, js/)
tests/          unittest suite + sanitized fixtures
```
