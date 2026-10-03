"""
"Does routing work on my real Housecall Pro data?" - a read-only check you run after going live.

It syncs (READ-ONLY: Housecall Pro is only ever read), then looks at what came in and puts the routing engine through
its paces on the real jobs and technicians:

  * the data: jobs by status, how many addresses became map pins, which technicians are set up for routing
  * the slot finder: runs it for the most urgent mapped jobs and checks every suggestion against the rules it must
    keep (inside the window, inside the shift, a working day, right trade skill, under the daily maximum)
  * drive times: compares the straight-line estimate the slot finder uses with real road times (when a road
    router is configured) and suggests a speed setting if the estimate is off
  * pins that look misplaced (a long way from every technician)

The report keeps counts, timings and ratios only: no customer names, addresses, phone numbers or job ids, so it is safe
to share. Console output may name technicians (your own staff) to say who still needs setting up.
"""

from __future__ import annotations

import statistics
import time
from datetime import date, datetime, timezone
from typing import Callable, List, Optional
from zoneinfo import ZoneInfo

from ..domain.timeutil import hhmm_to_minutes
from ..domain.travel import HaversineTravel, haversine_miles
from .dispatch_view import build_areas, build_dispatch, compute_slots, load_technicians
from .settings_store import get_settings
from .sync import SyncService

FAR_MILES = 100          # a pin this far from every technician's home base is probably geocoded to the wrong place
MIN_ROAD_SHARE = 0.8     # at least this share of sampled legs should come back as road routes
RATIO_OK = (0.75, 1.35)  # road time / estimated time: outside this the speed setting is worth adjusting


def check_option(o: dict, tech: Optional[dict], trade: str, today: date, now_min: int, lead: int) -> List[str]:
    """Rules every suggested slot must keep. Returns what was broken (empty = fine)."""
    bad = []
    if tech is None:
        return ["suggested for a technician that does not exist"]
    s_start = hhmm_to_minutes(tech.get("shift_start", "08:00"), 480)
    s_end = hhmm_to_minutes(tech.get("shift_end", "17:00"), 1020)
    if not o["window_start_min"] <= o["start_min"] <= o["window_end_min"]:
        bad.append("arrival outside its own window")
    if not (s_start <= o["window_start_min"] and o["window_end_min"] <= s_end):
        bad.append("window outside the shift")
    if o["end_min"] > s_end:
        bad.append("job ends after the shift")
    if date.fromisoformat(o["date"]).weekday() not in set(tech.get("work_days", [])):
        bad.append("not a working day")
    if trade and trade.upper() not in [x.upper() for x in tech.get("trade_skills", [])]:
        bad.append("technician lacks the trade skill")
    if o["stops_in_day"] >= int(tech.get("max_jobs_per_day", 6)):
        bad.append("over the daily maximum")
    if not 1 <= o["position"] <= o["stops_in_day"] + 1:
        bad.append("impossible position in the route")
    if date.fromisoformat(o["date"]) == today and o["start_min"] < now_min + lead:
        bad.append("arrival sooner than the lead time allows")
    return bad


def _median(xs):
    return statistics.median(xs) if xs else None


CHECK_DAYS = 7           # look a week ahead: a check run on a Friday evening or a weekend must still reach Monday


def run_live_check(db, cfg, hcp, geocoder, routes, now: Optional[datetime] = None, do_sync: bool = True,
                   jobs: int = 5, days: Optional[int] = None, legs: int = 12,
                   printer: Callable[..., None] = print) -> dict:
    now = now or datetime.now(timezone.utc)
    report: dict = {"generated_at": now.isoformat(timespec="seconds"), "mode": cfg.hcp_mode, "checks": []}

    def verdict(level: str, text: str) -> None:
        report["checks"].append({"level": level, "text": text})
        printer(f"  [{level}] {text}")

    # ---- 1. sync (read-only)
    printer("\n== 1. Reading Housecall Pro ==")
    if do_sync:
        t0 = time.perf_counter()
        res = SyncService(db, cfg, hcp, geocoder, new_tech_defaults=None).run(now)
        report["sync"] = {"status": res.get("status"), "jobs_seen": res.get("jobs_seen", 0),
                          "jobs_changed": res.get("jobs_changed", 0), "errors": res.get("errors", 0),
                          "seconds": round(time.perf_counter() - t0, 1)}
        if res.get("status") == "error":
            verdict("FAIL", "The sync failed: " + str(res.get("error"))[:200])
            return _conclude(report, printer)
        verdict("PASS" if res["status"] == "ok" else "WARN",
                f"Synced {res.get('jobs_seen', 0)} jobs in {report['sync']['seconds']} s"
                + ("" if res["status"] == "ok" else f" ({res.get('errors', 0)} could not be processed)"))
    else:
        printer("  (sync skipped: using what is already in the database)")

    with db.session() as conn:
        settings = get_settings(conn)
        tz = ZoneInfo(settings["timezone"])
        today = now.astimezone(tz).date()
        now_min = now.astimezone(tz).hour * 60 + now.astimezone(tz).minute
        lead = int(settings["scheduling"]["same_day_lead_minutes"])
        techs = load_technicians(conn)
        by_id = {t["id"]: t for t in techs}
        view = build_dispatch(conn, today, settings, now)
        report["timezone"] = settings["timezone"]

        # ---- 2. the data
        printer("\n== 2. The data ==")
        printer(f"  company time zone: {settings['timezone']} (Admin > Settings if that is not yours: every window follows it)")
        status = {r["work_status"]: r["n"] for r in conn.execute(
            "SELECT work_status, COUNT(*) AS n FROM jobs WHERE active = 1 GROUP BY work_status")}
        geo = {r["geocode_status"]: r["n"] for r in conn.execute(
            "SELECT geocode_status, COUNT(*) AS n FROM jobs WHERE active = 1 AND work_status = 'unscheduled' "
            "GROUP BY geocode_status")}
        n_uns = sum(geo.values())
        mapped = geo.get("ok", 0) + geo.get("from_hcp", 0)
        report["jobs"] = {"by_status": status, "unscheduled_geocode": geo}
        printer("  jobs by status: " + (", ".join(f"{k} {v}" for k, v in sorted(status.items())) or "none"))
        if not status:
            verdict("FAIL", "No jobs came back from Housecall Pro. Check the key and run scripts/phase0_probe.py.")
        elif n_uns:
            share = mapped / n_uns
            report["jobs"]["mapped_share"] = round(share, 2)
            verdict("PASS" if share >= 0.9 else "WARN",
                    f"{mapped} of {n_uns} unscheduled jobs have a map location ({share:.0%})"
                    + ("" if share >= 0.9 else ": check GEOCODER and the addresses on the rest"))
        warranty = conn.execute("SELECT COUNT(*) FROM warranty_details w JOIN jobs j USING (hcp_job_id) WHERE j.active = 1").fetchone()[0]
        report["jobs"]["warranty_parsed"] = warranty
        printer(f"  warranty descriptions recognised: {warranty}")
        if sum(status.values()) >= 5 and warranty == 0:
            verdict("WARN", "No job contains warranty dispatch text. If you take warranty jobs, the text may be somewhere the app is "
                            "not reading (it reads the job description and any note that looks like a dispatch): run "
                            "python scripts/phase0_probe.py and share its report.")

        routable = [t for t in techs if t["active"] and t["trade_skills"] and t["home_lat"] is not None]
        need = [t for t in techs if t["active"] and not (t["trade_skills"] and t["home_lat"] is not None)]
        report["technicians"] = {"total": len(techs), "active": sum(t["active"] for t in techs), "routable": len(routable),
                                 "need_setup": len(need)}
        printer(f"  technicians: {len(techs)} from Housecall Pro, {len(routable)} set up for routing")
        if need:
            printer("  still to set up (Admin > Technicians: trade skills, home address, hours): "
                    + ", ".join(t["name"] for t in need[:12]) + (" ..." if len(need) > 12 else ""))
        if not routable:
            verdict("WARN", "No technician is set up for routing yet, so the slot finder cannot be tested. "
                            "Open Admin > Technicians, set trade skills and a home address for each, then run this again.")
        else:
            verdict("PASS", f"{len(routable)} technician(s) set up for routing")

        # pins far from every technician: probably geocoded to the wrong place
        homes = [(t["home_lat"], t["home_lng"]) for t in routable]
        located = [u for u in view["unscheduled"] if u["lat"] is not None]
        if homes and located:
            far = sum(1 for u in located if min(haversine_miles((u["lat"], u["lng"]), h) for h in homes) > FAR_MILES)
            report["far_pins"] = far
            verdict("PASS" if not far else "WARN",
                    "No pins look misplaced" if not far else
                    f"{far} job pin(s) are over {FAR_MILES} miles from every technician: the address may have been placed wrongly")

        # ---- 3. the slot finder
        report["slots"] = {"jobs_tested": 0}
        if routable and located:
            printer(f"\n== 3. Slot finder on {min(jobs, len(located))} real job(s) ==")
            tested, with_options, broken, times = 0, 0, [], []
            reasons: dict = {}
            horizon = days or max(CHECK_DAYS, int(settings["scheduling"]["search_days"]))
            report["slots"]["days_searched"] = horizon
            for i, u in enumerate(located[:jobs], 1):
                t0 = time.perf_counter()
                res = compute_slots(conn, u["id"], settings, now, search_days=horizon)
                ms = round((time.perf_counter() - t0) * 1000)
                times.append(ms)
                tested += 1
                trade = (u["trade_code"] or "")
                for o in res["options"]:
                    broken += [f"job {i}: {b}" for b in check_option(o, by_id.get(o["tech_id"]), trade, today, now_min, lead)]
                if res["options"]:
                    with_options += 1
                    b = res["options"][0]
                    printer(f"  job {i} ({u['priority_label']} {trade or 'any trade'}): {len(res['options'])} option(s); best {b['date']} "
                            f"window {b['window_start_min'] // 60:02d}:{b['window_start_min'] % 60:02d}-"
                            f"{b['window_end_min'] // 60:02d}:{b['window_end_min'] % 60:02d}, +{b['added_drive_min']:.0f} min driving ({ms} ms)")
                else:
                    here: dict = {}
                    for x in res["ineligible"]:
                        here[x["reason"]] = here.get(x["reason"], 0) + 1
                        reasons[x["reason"]] = reasons.get(x["reason"], 0) + 1
                    why = "; ".join(f"{r} ({n})" for r, n in sorted(here.items(), key=lambda kv: -kv[1])) \
                        or (res["notes"][0] if res["notes"] else "no technician can take it")
                    printer(f"  job {i} ({u['priority_label']} {trade or 'any trade'}): no slot in {horizon} days ({ms} ms): {why}")
            report["slots"].update({"jobs_tested": tested, "with_options": with_options, "rule_violations": len(broken),
                                    "no_slot_reasons": reasons, "ms_median": _median(times), "ms_max": max(times)})
            verdict("PASS" if with_options == tested else "WARN" if with_options else "FAIL",
                    f"The slot finder offered options for {with_options} of {tested} job(s)"
                    + ("" if with_options == tested else " (see the reasons above: skills, shifts, days or full routes)"))
            verdict("PASS" if not broken else "FAIL",
                    "Every suggested slot keeps the rules (window, shift, working day, skill, daily maximum)" if not broken
                    else f"{len(broken)} suggested slot(s) broke a rule, e.g. {broken[0]}: please send this report")
            t0 = time.perf_counter()
            areas = build_areas(conn, settings, now)
            report["areas"] = {"count": len(areas["areas"]), "ms": round((time.perf_counter() - t0) * 1000)}
            verdict("PASS", f"Areas tab: {areas['totals']['unscheduled']} calls in {len(areas['areas'])} area(s), "
                            f"computed in {report['areas']['ms']} ms")
        else:
            printer("\n== 3. Slot finder == skipped (needs a technician set up and at least one mapped unscheduled job)")

        # ---- 4. drive times
        printer("\n== 4. Drive times: estimate vs road ==")
        provider = getattr(routes, "name", "none")
        if provider == "none":
            printer("  Road routing is off (ROUTER=none), so the estimate could not be compared with real roads. "
                    "Run scripts/go_live.py again to choose a provider.")
            report["drive"] = {"provider": "none"}
        else:
            pairs = []
            for t in view["technicians"]:           # real routes: home -> first stop -> next ...
                pts = ([(t["home"]["lat"], t["home"]["lng"])] if t["home"] else []) + [
                    (s["lat"], s["lng"]) for s in t["stops"] if s["lat"] is not None]
                pairs += list(zip(pts, pts[1:]))
            if len(pairs) < legs:                   # fall back to hops between mapped jobs
                pts = [(u["lat"], u["lng"]) for u in located]
                pairs += list(zip(pts, pts[1:]))
            pairs = pairs[:legs]
            travel = HaversineTravel.from_settings(settings)
            t0 = time.perf_counter()
            got = routes.routes(conn, pairs, travel, now) if pairs else []
            secs = round(time.perf_counter() - t0, 1)
            road = [(r, travel.minutes(a, b)) for r, (a, b) in zip(got, pairs) if r["source"] == "road"]
            share = len(road) / len(pairs) if pairs else 0
            ratios = [r["minutes"] / est for r, est in road if est >= 5]
            med = _median(ratios)
            report["drive"] = {"provider": provider, "legs": len(pairs), "road": len(road), "seconds": secs,
                               "median_road_over_estimate": round(med, 2) if med else None}
            verdict("PASS" if share >= MIN_ROAD_SHARE else "WARN",
                    f"{len(road)} of {len(pairs)} sampled legs came back as road routes from {provider} in {secs} s"
                    + ("" if share >= MIN_ROAD_SHARE else ": the routing provider is not answering well (see the app log)"))
            if med:
                speed = float(settings["scheduling"]["travel_speed_mph"])
                if RATIO_OK[0] <= med <= RATIO_OK[1]:
                    verdict("PASS", f"Real drive times are about {med:.2f}x the estimate the slot finder uses: close enough")
                else:
                    suggested = round(speed / med)
                    verdict("WARN", f"Real drive times are about {med:.2f}x the estimate. Slots will be "
                                    f"{'too tight' if med > 1 else 'too loose'}: in Admin > Settings set the travel speed to "
                                    f"about {suggested} mph (now {speed:.0f}).")
                    report["drive"]["suggested_speed_mph"] = suggested

    return _conclude(report, printer)


def _conclude(report: dict, printer) -> dict:
    levels = {c["level"] for c in report["checks"]}
    report["overall"] = "FAIL" if "FAIL" in levels else "WARN" if "WARN" in levels else "PASS"
    printer(f"\nOverall: {report['overall']}. Housecall Pro was only read; nothing was changed there.")
    return report
