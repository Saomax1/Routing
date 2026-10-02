#!/usr/bin/env python3
"""
Phase 0 - validate Housecall Pro API access BEFORE trusting the app with real data (spec section 8).

What it does (READ-ONLY - it never writes to Housecall Pro):
  1. checks your API key works (lists employees)
  2. lists unscheduled jobs and the next 7 days of scheduled jobs
  3. prints the SHAPE of the data (field names + types, no customer values) so you can confirm which
     field holds the warranty text, how lead source / tags / address / schedule appear
  4. runs the warranty parser on up to N real descriptions, locally, and summarises what failed
  5. writes data/phase0_report.json - structure and counts only, safe to share (no names/phones/addresses)

Usage
  python scripts/phase0_probe.py                 # uses HCP_API_KEY from .env / environment (HCP_MODE ignored)
  python scripts/phase0_probe.py --limit 30      # parse up to 30 descriptions
  python scripts/phase0_probe.py --show-failures # print REDACTED snippets of descriptions that failed to parse
  python scripts/phase0_probe.py --mock          # dry-run the script itself against the demo data

Nothing leaves your machine except the calls to Housecall Pro's own API.
If something maps wrong, adjust app/hcp/client.py (paths/params/auth scheme) or app/hcp/normalize.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import load_config  # noqa: E402
from app.domain.warranty_parser import looks_like_warranty, missing_key_fields, parse_warranty_job  # noqa: E402
from app.hcp.client import HCPClient, MockHCPClient  # noqa: E402
from app.hcp.http import HttpError  # noqa: E402
from app.hcp.normalize import normalize_job  # noqa: E402
from app.services.ai_fallback import redact  # noqa: E402


def shape(obj, depth=3):
    """Structure of a JSON value with types instead of values."""
    if isinstance(obj, dict):
        return {k: (shape(v, depth - 1) if depth > 0 else type(v).__name__) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shape(obj[0], depth - 1)] if obj else []
    return type(obj).__name__


def text_fields(obj, prefix=""):
    """Yield (dotted_path, string) for every string value, to find where warranty text lives."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from text_fields(v, f"{prefix}{k}.")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from text_fields(v, f"{prefix}{i}.")
    elif isinstance(obj, str):
        yield prefix.rstrip("."), obj


def step(title):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def explain_http_error(e: HttpError) -> str:
    return {
        0: "Network problem (could not reach the API). Check HCP_BASE_URL and your internet / proxy.",
        401: "401 Unauthorized: the API key was rejected. Check HCP_API_KEY, and HCP_AUTH_SCHEME in app/config.py "
             "(the default is 'Token'; try 'Bearer' if the docs say so).",
        403: "403 Forbidden: the key works but this account/plan may not include API access (reported as the MAX plan), "
             "or the key lacks permission for this endpoint.",
        404: "404 Not Found: the endpoint path is probably different. Check JOBS_PATH / EMPLOYEES_PATH in app/hcp/client.py "
             "against the docs.",
        422: "422/400: a query parameter name is probably different. Check the PARAM_* constants in app/hcp/client.py.",
        400: "400 Bad Request: a query parameter name or value is probably different. Check the PARAM_* constants.",
        429: "429 Rate limited. Wait a minute and retry.",
    }.get(e.status, f"HTTP {e.status}")


def run_probe(client, limit=20, show_failures=False, tz=ZoneInfo("America/Phoenix"), out_path=None, printer=print):
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "notes": []}

    step("1. Employees (also verifies the API key)")
    try:
        employees = client.list_employees()
    except HttpError as e:
        printer("FAILED:", explain_http_error(e))
        report["employees_error"] = str(e)
        return report
    printer(f"OK - {len(employees)} employees returned")
    if employees:
        report["employee_shape"] = shape(employees[0])
        printer("employee fields:", json.dumps(report["employee_shape"]))

    step("2. Unscheduled jobs")
    try:
        unscheduled = client.list_unscheduled()
    except HttpError as e:
        printer("FAILED:", explain_http_error(e))
        report["unscheduled_error"] = str(e)
        return report
    printer(f"OK - {len(unscheduled)} unscheduled jobs returned" + ("  (list was truncated!)" if getattr(client, "truncated", False) else ""))
    statuses = Counter(str(j.get("work_status") or j.get("status")) for j in unscheduled)
    printer("work_status values seen:", dict(statuses), "  <- all should mean 'unscheduled'; if not, the filter param is ignored")
    report["unscheduled_count"], report["unscheduled_statuses"] = len(unscheduled), dict(statuses)
    if unscheduled:
        report["job_shape"] = shape(unscheduled[0])
        printer("job fields:", json.dumps(report["job_shape"], indent=1))

    step("3. Scheduled jobs (today .. +7 days)")
    today = datetime.now(timezone.utc).astimezone(tz).date()
    try:
        scheduled = client.list_scheduled(today, today + timedelta(days=7), tz)
    except HttpError as e:
        printer("FAILED:", explain_http_error(e))
        scheduled = []
        report["scheduled_error"] = str(e)
    printer(f"OK - {len(scheduled)} scheduled jobs returned" if scheduled or "scheduled_error" not in report else "")
    report["scheduled_count"] = len(scheduled)
    if scheduled:
        report["schedule_shape"] = shape(scheduled[0].get("schedule", {}))
        printer("schedule fields:", json.dumps(report["schedule_shape"]))
        with_emp = sum(1 for j in scheduled if (j.get("assigned_employees") or j.get("assigned_employee_ids")))
        printer(f"{with_emp}/{len(scheduled)} scheduled jobs have an assigned employee")

    step("3b. Completed jobs and arrival windows (last 3 days)")
    try:
        completed = client.list_completed(today - timedelta(days=3), today, tz)
    except HttpError as e:
        printer("FAILED:", explain_http_error(e))
        printer("-> the app asks for work_status 'complete rated' and 'complete unrated' (STATUS_COMPLETED in app/hcp/client.py). "
                "If HCP names them differently, fix that list. Until it works, finished jobs just stay as they were.")
        completed = []
        report["completed_error"] = str(e)
    report["completed_count"] = len(completed)
    if completed:
        done_statuses = Counter(str(j.get("work_status") or j.get("status")) for j in completed)
        printer(f"OK - {len(completed)} completed jobs returned; work_status values:", dict(done_statuses),
                "  <- all should be 'complete ...'; if not, the status filter is being ignored")
        stamped = sum(1 for j in completed if normalize_job(j)["completed_at"])
        printer(f"{stamped}/{len(completed)} have a completion time (work_timestamps.completed_at); the rest fall back to "
                "the job's last update. If none do, find the field and add it to normalize_job() in app/hcp/normalize.py.")
        report["completed_with_timestamp"] = stamped
    elif "completed_error" not in report:
        printer("No completed jobs in the last 3 days (nothing to check yet).")
    windows = Counter(normalize_job(j)["arrival_window_minutes"] for j in scheduled + completed)
    report["arrival_window_values"] = {str(k): v for k, v in windows.items()}
    if windows:
        printer("arrival window (minutes) seen on scheduled/completed jobs:", dict(windows))
        printer("-> the app reads this as 'the technician may arrive any time from the scheduled start until start + this many "
                "minutes'. Jobs with no value get the standard window (Admin > Settings, 4 hours). If the values look like the "
                "job's LENGTH rather than a promise to the customer, say so: it changes how windows are read.")
    if scheduled:
        open_statuses = Counter(str(j.get("work_status") or j.get("status")) for j in scheduled)
        printer("work_status values among scheduled jobs:", dict(open_statuses), "  (scheduled and in progress are expected)")

    step("4. Where does the warranty text live?")
    where = Counter()
    for j in unscheduled + scheduled:
        for path, s in text_fields(j):
            if looks_like_warranty(s):
                where[path] += 1
    report["warranty_text_fields"] = dict(where)
    if where:
        printer("fields containing warranty-looking text:", dict(where))
        printer("-> the app reads 'description' (then job_description/summary/notes). If the text is elsewhere, "
                "edit normalize_job() in app/hcp/normalize.py.")
    else:
        printer("No warranty-looking text found in any field. Either there are no warranty jobs right now, or the text lives "
                "in a field that was not returned.")

    lead = Counter(normalize_job(j)["lead_source"] or "(none)" for j in unscheduled)
    tags = Counter(t for j in unscheduled for t in normalize_job(j)["tags"])
    printer("lead_source values:", dict(lead))
    printer("tags:", dict(tags.most_common(15)))
    report["lead_sources"], report["tags"] = dict(lead), dict(tags.most_common(15))

    step(f"5. Parser dry-run on up to {limit} descriptions (local only)")
    results, failures = Counter(), []
    sample = [j for j in unscheduled + scheduled if looks_like_warranty(normalize_job(j)["description_raw"])][:limit]
    for j in sample:
        n = normalize_job(j)
        w = parse_warranty_job(n["description_raw"])
        miss = missing_key_fields(w)
        results["parsed_clean" if not miss and not w.parse_warnings else "needs_attention"] += 1
        results[f"priority={w.dispatch_priority}"] += 1
        results[f"trade={w.trade_code}"] += 1
        for m in miss:
            results[f"missing:{m}"] += 1
        for warn in w.parse_warnings:
            results[f"warning:{warn}"] += 1
        if miss or w.parse_warnings:
            failures.append((n["hcp_job_id"], miss, w.parse_warnings, n["description_raw"]))
    printer(f"{len(sample)} warranty-looking descriptions parsed")
    for k, v in sorted(results.items()):
        printer(f"  {v:3d}  {k}")
    report["parser_summary"] = dict(results)
    if failures and show_failures:
        printer("\n--- REDACTED snippets of descriptions that need attention (names/phones removed) ---")
        for jid, miss, warns, desc in failures[:5]:
            printer(f"\n[{jid}] missing={miss} warnings={warns}\n{redact(desc)[:700]}")
    elif failures:
        printer(f"({len(failures)} need attention; re-run with --show-failures to see redacted snippets)")

    step("6. Write operations")
    printer("This probe never writes. Before Phase 2, confirm in the HCP docs which endpoints can set a job's schedule and "
            "assigned employee, and test them on ONE throwaway job in a sandbox/test customer.")

    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        printer(f"\nReport (structure + counts only, no customer values) written to {out_path}")
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--show-failures", action="store_true")
    ap.add_argument("--mock", action="store_true", help="run against the built-in demo data (tests the script itself)")
    args = ap.parse_args()
    cfg = load_config()
    if args.mock:
        client = MockHCPClient()
    else:
        if not cfg.hcp_api_key:
            sys.exit("HCP_API_KEY is not set. Put it in .env (never in git) and run again, or use --mock.")
        client = HCPClient(cfg)
    run_probe(client, args.limit, args.show_failures, out_path=os.path.join("data", "phase0_report.json"))


if __name__ == "__main__":
    main()
