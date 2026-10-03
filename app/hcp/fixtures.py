"""
Sanitized demo data for mock mode, tests and local development.

Everything here is FAKE: names, phone numbers (555-01xx), street addresses and ids.
Nothing in this file came from a real customer.

* ``build_ahs_description`` renders an AHS/Frontdoor-style description block in the same
  shape as ``tests/fixtures/sample_ahs_job.txt`` (used by parser tests and demo jobs).
* ``make_demo_dataset`` builds raw, HCP-shaped employees + jobs relative to "now" so the demo
  always has jobs today and over the next two working days.
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------- description

def build_ahs_description(
    *,
    dispatch: str = "90000001",
    trade: str = "PLB",
    priority: str = "Normal",
    header_code: str = "NORMAL",
    name: str = "JANE SAMPLE",
    phone: str = "4805550101",
    street: Optional[str] = "123 W EXAMPLE ST",
    city: Optional[str] = "CHANDLER",
    state: str = "AZ",
    zip_code: Optional[str] = "85226",
    items: Optional[list] = None,
    plan: str = "SHIELDGOLD HOME WARRANTY",
    total: int = 100,
    paid: int = 0,
    svc_req: str = "21000001",
    vendor: str = "1000000",
    contract: str = "600000001",
    autho_required: bool = False,
    include_address: bool = True,
    entity_encoded: bool = True,
) -> str:
    """Render an AHS-style job description (sanitized)."""
    items = items or [{"name": "Stoppage", "problem": "Not Working", "status": "Open"}]
    amp = "&amp;" if entity_encoded else "&"
    pretty_phone = f"\\({phone[:3]}\\) {phone[3:6]}-{phone[6:]}"
    lines = [
        f'<a href="http://dispa.me/AAAA{dispatch[-4:]}" target="_blank">Ahs Authorization Link</a>',
        f'<a href="http://dispa.me/BBBB{dispatch[-4:]}" target="_blank">Open in web</a>',
        f"ahs:{dispatch} ",
        f"{dispatch} {trade} {priority}:{header_code}",
        "# Job Brand Information", "", "This is an AHS customer.", "",
        "# Customer Information", "",
        f" **{name}** (Dispatch Contact)", "",
        f"**HOME:**[{pretty_phone}](tel:+1{phone})", "",
        f"**{name}** (Contract Contact)", "",
        f"**Cellular:**[{pretty_phone}](tel:+1{phone})", "",
        "# Autho Link", "",
        ("Authorization Link: [Click Here] (https://contractor.frontdoorhome.com/redirect?dest=ER"
         f"{amp}dispatch={dispatch}{amp}svcReq={svc_req}{amp}tenant=AHS{amp}vendorId={vendor}{amp}tradeCode={trade})"),
        "", "# Vendor", "", f"**Vendor ID**:{vendor}", "",
        "# Contract Information", "", f"**Contract ID:**{contract}", "",
        "**Contract Effective Date:** 2026-01-04", "",
        "**Contract Expiration Date:** 2027-01-04", "", "",
    ]
    if include_address and street:
        lines += ["# Covered Property Address", "", street]
        if city and zip_code:
            lines.append(f"{city}, {state} {zip_code}")
        lines.append("")
    lines += [
        "# Work Order Information", "",
        f"**Dispatch Priority:**{priority}", "",
        f"**Autho Required?:** {'True' if autho_required else 'False'} ", "",
        " ## **Items** ", "",
    ]
    for n, it in enumerate(items, 1):
        lines += [f"## Item {n}: {it['name']}", "", "**Problem:**", f"{it.get('problem', 'Not Working')}, ", "", ""]
        if it.get("area"):
            lines += [f"**Area Of Home:** {it['area']}", ""]
        lines += [f"**Status:** {it.get('status', 'Open')}", ""]
    lines += [
        "# Coverage Information", "", "## Coverage Notes", "", "", "",
        plan, "",
        "30-DAY RECALL APPLIES TO THE SERVICE WORK PERFORMED ON THE PREVIOUS DISPATCH REQUEST", "",
        "REMINDER: COMPLETION DATE MUST BE PROVIDED TO AHS FOR ALL SERVICE CALLS", "",
        "*** DO NOT COLLECT TRADE SERVICE FEE ***", "",
        "*** Payment Type : PREPAY ***",
        "## Coverage Details", "", "- Rust and Corrosion",
        "# Payment", "",
        f"**Total: ${total}", "", f"**Paid: ${paid}", "", f"**Remaining: ${total - paid}", "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- demo dataset

CITY_CENTERS = {
    # city: (lat, lng, [zips])
    "Chandler": (33.3062, -111.8413, ["85224", "85225", "85226", "85286", "85249"]),
    "Gilbert": (33.3528, -111.7890, ["85233", "85234", "85295", "85296"]),
    "Mesa": (33.4152, -111.8315, ["85201", "85204", "85210", "85212"]),
    "Queen Creek": (33.2487, -111.6343, ["85142", "85140"]),
    "San Tan Valley": (33.1912, -111.5254, ["85140", "85143"]),
    "Tempe": (33.4255, -111.9400, ["85281", "85283"]),
}

_STREETS = ["N Sample Dr", "S Example Ave", "E Demo Way", "W Placeholder Ln", "N Fakeville Ct",
            "S Mockingbird Rd", "E Testing Blvd", "W Nowhere St"]
_FIRST = ["Jane", "John", "Maria", "Chris", "Pat", "Dana", "Lee", "Sam", "Taylor", "Morgan", "Robin", "Jamie"]
_LAST = ["Sample", "Example", "Demo", "Placeholder", "Testcase", "Mockson", "Fakeman", "Nobody"]

EMPLOYEES = [
    {"id": "emp_demo_1", "first_name": "Alex", "last_name": "R.", "role": "field tech", "active": True,
     "home": ("Chandler", "850 W Sample Way, Chandler, AZ 85225"), "skills": ["PLB"]},
    {"id": "emp_demo_2", "first_name": "Jordan", "last_name": "T.", "role": "field tech", "active": True,
     "home": ("Gilbert", "410 E Example Dr, Gilbert, AZ 85234"), "skills": ["PLB", "HVAC"]},
    {"id": "emp_demo_3", "first_name": "Casey", "last_name": "M.", "role": "field tech", "active": True,
     "home": ("Queen Creek", "22 N Demo Rd, Queen Creek, AZ 85142"), "skills": ["HVAC"]},
    {"id": "emp_demo_4", "first_name": "Riley", "last_name": "S.", "role": "field tech", "active": True,
     "home": ("Mesa", "1200 S Mockingbird Rd, Mesa, AZ 85204"), "skills": ["PLB"]},
    {"id": "emp_demo_5", "first_name": "Former", "last_name": "Tech", "role": "field tech", "active": False,
     "home": ("Mesa", "1 W Nowhere St, Mesa, AZ 85201"), "skills": ["PLB"]},
]

DEMO_TECH_SETUP = {e["id"]: {"home_address": e["home"][1], "trade_skills": e["skills"]} for e in EMPLOYEES}


def next_workdays(start: date, n: int) -> list:
    """First ``n`` Mon–Fri dates on or after ``start``."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _street(rng: random.Random) -> str:
    return f"{rng.randint(100, 9899)} {rng.choice(_STREETS)}"


def _customer(rng: random.Random, i: int) -> dict:
    return {"first_name": rng.choice(_FIRST), "last_name": rng.choice(_LAST),
            "mobile_number": f"480555{(100 + i) % 1000:04d}"[:10]}


def make_demo_dataset(now: Optional[datetime] = None, tz_name: str = "America/Phoenix") -> dict:
    """Return {'employees': [...], 'jobs': [...]} in an HCP-like raw shape (all fake)."""
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_today = now.astimezone(tz).date()
    rng = random.Random(20261001)  # deterministic

    employees = [{
        "id": e["id"], "first_name": e["first_name"], "last_name": e["last_name"],
        "role": e["role"], "active": e["active"], "email": f"{e['first_name'].lower()}@example.invalid",
    } for e in EMPLOYEES]

    jobs: list = []
    jid = 0

    def next_id() -> str:
        nonlocal jid
        jid += 1
        return f"job_demo_{jid:03d}"

    def address_for(city: str, street: str, zip_code: str) -> dict:
        return {"street": street, "city": city, "state": "AZ", "zip": zip_code, "country": "US"}

    # ---- scheduled jobs: ~3 per active tech per workday, clustered around each tech's area
    workdays = next_workdays(local_today, 3)
    slots = [(8, 30, 90), (11, 0, 60), (13, 30, 90), (15, 30, 60)]
    area_cities = {
        "emp_demo_1": ["Chandler", "Chandler", "Gilbert"],
        "emp_demo_2": ["Gilbert", "Gilbert", "Chandler", "Mesa"],
        "emp_demo_3": ["Queen Creek", "San Tan Valley", "Queen Creek"],
        "emp_demo_4": ["Mesa", "Mesa", "Tempe", "Gilbert"],
    }
    descriptions = {
        "PLB": ["Water heater flushing + inspection", "Replace kitchen faucet", "Hose bib repair",
                "Toilet fill valve replacement", "Main shutoff valve replacement"],
        "HVAC": ["AC tune-up", "Capacitor replacement", "Thermostat install", "Condensate line clear"],
    }
    for e in EMPLOYEES:
        if not e["active"]:
            continue
        for d_i, d in enumerate(workdays):
            n = 3 if (d_i + len(e["id"])) % 2 == 0 else 2
            for s_i, (hh, mm, dur) in enumerate(slots[:n] if n == 2 else [slots[0], slots[1], slots[2]]):
                city = area_cities[e["id"]][(d_i + s_i) % len(area_cities[e["id"]])]
                zip_code = rng.choice(CITY_CENTERS[city][2])
                street = _street(rng)
                trade = e["skills"][s_i % len(e["skills"])]
                start_local = datetime.combine(d, time(hh, mm), tzinfo=tz)
                end_local = start_local + timedelta(minutes=dur)
                job_id = next_id()
                is_ahs = (s_i % 2 == 0)
                if is_ahs:
                    desc = build_ahs_description(
                        dispatch=str(91000000 + jid), trade=trade, priority="Normal",
                        name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}".upper(), phone=f"480555{(200 + jid) % 1000:04d}"[:10],
                        street=street.upper(), city=city.upper(), zip_code=zip_code,
                        items=[{"name": rng.choice(["Water Heater", "Faucet", "Toilet"]) if trade == "PLB"
                                else rng.choice(["Cooling", "Thermostat"]), "problem": "Not Working"}],
                        total=100, svc_req=str(21000000 + jid))
                    lead, tags = "AHS", ["warranty", "ahs", "Normal: Normal"]
                else:
                    desc = rng.choice(descriptions[trade])
                    lead, tags = rng.choice(["Google LSA", "Referral", "Repeat customer"]), []
                created = start_local - timedelta(days=rng.randint(2, 6))
                # earlier today: done or under way, like a real day in HCP (rng use is unchanged)
                if end_local <= now:
                    status, stamps = "complete rated", {"completed_at": _iso(end_local)}
                elif start_local <= now:
                    status, stamps = "in progress", {}
                else:
                    status, stamps = "scheduled", {}
                jobs.append({
                    "id": job_id, "work_status": status, "work_timestamps": stamps, "description": desc,
                    "customer": _customer(rng, jid), "address": address_for(city, street, zip_code),
                    "schedule": {"scheduled_start": _iso(start_local), "scheduled_end": _iso(end_local),
                                 "arrival_window": 60},
                    "assigned_employees": [{"id": e["id"], "first_name": e["first_name"], "last_name": e["last_name"]}],
                    "tags": tags, "lead_source": lead,
                    "job_fields": {"job_type": {"name": "Plumbing" if trade == "PLB" else "HVAC"}},
                    "created_at": _iso(created), "updated_at": _iso(created),
                })

    # ---- unscheduled jobs (the interesting ones)
    # How warranty work is tagged in Housecall Pro: three tags, one per type
    tier_tags = {"Expedited": "Normal: Expedited", "Normal": "Normal: Normal", "Recall": "Normal: Recall"}

    def unscheduled(city, trade, priority, items, hours_ago, *, lead="AHS", tags=None, name=None,
                    header_code="NORMAL", include_address=True, plain_desc=None, job_type=None, tier=None):
        zip_code = rng.choice(CITY_CENTERS[city][2])
        street = _street(rng)
        job_id = next_id()
        created = now - timedelta(hours=hours_ago)
        nm = name or f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
        if plain_desc is None:
            desc = build_ahs_description(
                dispatch=str(92000000 + jid), trade=trade, priority=priority, header_code=header_code,
                name=nm.upper(), phone=f"480555{(300 + jid) % 1000:04d}"[:10], street=street.upper(),
                city=city.upper(), zip_code=zip_code, items=items, total=125 if priority != "Normal" else 100,
                svc_req=str(22000000 + jid), include_address=include_address)
        else:
            desc = plain_desc
        first, _, last = nm.partition(" ")
        jobs.append({
            "id": job_id, "work_status": "unscheduled", "description": desc,
            "customer": {"first_name": first.title(), "last_name": last.title(), "mobile_number": f"480555{(300 + jid) % 1000:04d}"[:10]},
            "address": address_for(city, street, zip_code) if include_address else {},
            "schedule": {}, "assigned_employees": [],
            "tags": tags if tags is not None else (["warranty", "ahs", tier_tags[tier or ("Expedited" if priority == "Emergency" else priority)]]
                                                   if lead == "AHS" else []),
            "lead_source": lead,
            "job_fields": {"job_type": {"name": job_type or ("Plumbing" if trade == "PLB" else "HVAC")}},
            "created_at": _iso(created), "updated_at": _iso(created),
        })

    unscheduled("Chandler", "PLB", "Emergency", [{"name": "Water Leak", "problem": "Leak causing secondary damage"}], 3)
    unscheduled("Queen Creek", "PLB", "Expedited", [{"name": "Leak", "problem": "Active leak under sink"}], 20, header_code="NORMAL")
    unscheduled("Gilbert", "PLB", "Normal", [{"name": "Stoppage", "problem": "Not Working"}], 52)
    unscheduled("Mesa", "PLB", "Normal", [{"name": "Water Heater", "problem": "No hot water"}], 30)
    unscheduled("San Tan Valley", "HVAC", "Normal", [{"name": "Cooling", "problem": "No cooling"}], 18)
    unscheduled("Chandler", "HVAC", "Expedited", [{"name": "Cooling", "problem": "No cooling, elderly resident"}], 10)
    unscheduled("Gilbert", "PLB", "Normal", [{"name": "Faucet", "problem": "Dripping", "area": "Kitchen"},
                                             {"name": "Toilet", "problem": "Running constantly", "area": "Master Bath"}], 26,
                tier="Recall")                                                    # a return visit to something repaired before
    unscheduled("Mesa", "PLB", "Normal", [{"name": "Stoppage", "problem": "Not Working"}], 6, include_address=False)

    unscheduled("Chandler", "PLB", "Normal", [], 1, lead="Google LSA", tags=["lead"],
                plain_desc="Customer wants a quote for a tankless water heater install.", job_type="Plumbing")
    unscheduled("Queen Creek", "HVAC", "Normal", [], 0.5, lead="Meta", tags=["lead", "Meta Lead"],      # an ad lead
                plain_desc="AC is running but not blowing cold air.", job_type="HVAC")
    unscheduled("Mesa", "PLB", "Normal", [], 48, lead="Referral", tags=["lead"],
                plain_desc="Replace garbage disposal.", job_type="Plumbing")
    # a warranty call turned into a retail job: it still carries the old dispatch text but no warranty tag
    unscheduled("Mesa", "PLB", "Normal", [], 30, lead="Choice Home Warranty", tags=["warranty"],
                plain_desc="Choice Home Warranty authorization #CHW-555-0101. Toilet leaking at base. Collect $85 trade fee.",
                job_type="Plumbing")

    # ---- completed history: the previous workday, so the date picker has finished routes to show.
    # Own rng: adding these must not change any job generated above.
    hist = random.Random(20261002)
    prev = local_today - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    for e in EMPLOYEES:
        if not e["active"]:
            continue
        for s_i, (hh, mm, dur) in enumerate(slots[:2]):
            city = area_cities[e["id"]][s_i % len(area_cities[e["id"]])]
            zip_code = hist.choice(CITY_CENTERS[city][2])
            street = _street(hist)
            trade = e["skills"][s_i % len(e["skills"])]
            start_local = datetime.combine(prev, time(hh, mm), tzinfo=tz)
            end_local = start_local + timedelta(minutes=dur)
            jobs.append({
                "id": next_id(), "work_status": "complete rated" if s_i == 0 else "complete unrated",
                "work_timestamps": {"completed_at": _iso(end_local + timedelta(minutes=hist.randint(0, 20)))},
                "description": hist.choice(descriptions[trade]),
                "customer": _customer(hist, jid), "address": address_for(city, street, zip_code),
                "schedule": {"scheduled_start": _iso(start_local), "scheduled_end": _iso(end_local), "arrival_window": 60},
                "assigned_employees": [{"id": e["id"], "first_name": e["first_name"], "last_name": e["last_name"]}],
                "tags": [], "lead_source": "Repeat customer",
                "job_fields": {"job_type": {"name": "Plumbing" if trade == "PLB" else "HVAC"}},
                "created_at": _iso(start_local - timedelta(days=3)), "updated_at": _iso(end_local),
            })

    return {"employees": employees, "jobs": jobs}
