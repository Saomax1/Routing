# Notes for Claude sessions on this repository

An in-house dispatch assistant for a home-service company that uses Housecall Pro (HCP). Read `README.md` for what it does. The
app is READ-ONLY against Housecall Pro. Standard library + Starlette; no build step.

## The rule that matters most: real customer data and keys never reach GitHub

When `HCP_API_KEY` is set in the environment this is a LIVE session and the database holds real customers.

- **Never put customer information or any API key into anything that is committed, pushed or posted to GitHub, in any form:**
  code, tests, fixtures, docs, comments, commit messages, branch names, PR or issue text, screenshots, logs. That includes the
  GitHub MCP tools (they do not run the git hooks; do not use them to send file contents or text from a live session).
- **Keep real data outside the repo.** Point everything at the scratchpad directory: `DATABASE_PATH=<scratchpad>/live/live.db`, and
  write reports with `--out <scratchpad>/live/...`. Do **not** create a `.env` file in a cloud session; use environment variables.
  Screenshots of the real app go in the scratchpad only, never in the repo.
- **Turn the guard on first:** `python scripts/install_git_guard.py`. Git then runs `scripts/leak_check.py` before every commit,
  commit message and push. It learns the real customer names, phones, street addresses and the keys from the live database and
  environment and blocks any commit containing them (and any binary file). It fails closed: with a key set but no readable
  database it refuses. **Never use `--no-verify`.**
- **Before staging:** `python scripts/leak_check.py --tree`. Stage files by name (not `git add -A`) so nothing unintended is picked up.
- **Debugging with a real job:** reproduce the problem with a made-up job (fake names, addresses, phone numbers, ids), never by
  pasting real text. If a real description breaks the parser, write a scrubbed synthetic equivalent for the test.
- In chat, report counts and shapes, not customer names, addresses or phone numbers.

## Live test with a real Housecall Pro key (cloud session)

The user adds, in the cloud environment's settings, `api.housecallpro.com` and `geocoding.geo.census.gov` to the allowed domains and a
read-only key as the environment variable `HCP_API_KEY`, then starts a new session. Never ask them to paste a key into chat.

1. `python scripts/install_git_guard.py`
2. Check reachability without printing secrets: `env | grep -o '^HCP_[A-Z_]*'` (names only) and
   `curl -sS -m 10 -o /dev/null -w '%{http_code}\n' https://api.housecallpro.com/employees` (401 = reachable, 000 = blocked).
3. Install dependencies in a venv in the scratchpad: `python -m venv <scratchpad>/venv && <scratchpad>/venv/bin/pip install -r requirements.txt`.
4. Use environment variables only: `LIVE=<scratchpad>/live; mkdir -p $LIVE; export DATABASE_PATH=$LIVE/live.db HCP_MODE=live
   GEOCODER=census ROUTER=none` (`ROUTER=none`: road routing would send customer locations to a third party).
5. `python scripts/phase0_probe.py --limit 30 --out $LIVE/phase0_report.json` shows the shape of the real data (field names, where the
   warranty text lives, whether private notes come back, how the tags classify). Then
   `python scripts/live_check.py --jobs 6 --out $LIVE/live_check_report.json` (it syncs, then tests routing; reports are counts only).
6. To see the UI: run `python -m app` with the same environment on `127.0.0.1` and drive it with Playwright; screenshots to the scratchpad.
7. Real technicians start with routing OFF: set skills and home address through the admin API/page before testing slots.
8. Fix what the real data shows (tag names, field names, parsing) in code, with tests that use made-up data. Run
   `python scripts/leak_check.py --tree` before each commit.

HCP request details (auth scheme, paths, parameter names, where private notes live) were written from documentation and had not been
seen against a live account when this was written: `app/hcp/client.py` and `app/hcp/normalize.py` isolate all of it.

## Working on the code

- Tests: `python -m unittest discover -s tests -t .` (standard library only). Keep them passing; the suite is also run under simulated
  clocks (weekday/weekend, morning/evening) because the demo data and slot results depend on the time of day.
- Run the app: `python -m app` (demo data by default, `HCP_MODE=live` for real data).
- A job's type (Expedited / Normal / Recall warranty, or Retail) comes from its HCP tags: `app/domain/jobkind.py`.
- Unscheduled jobs are the same red "!" on the map and a plain row (type, customer, address) in the list; colour only appears once a job
  is on a technician's route.
