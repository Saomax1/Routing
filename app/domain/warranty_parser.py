"""
Warranty job parser for Housecall Pro job descriptions.

Takes the semi-structured text that home-warranty dispatches (AHS / Frontdoor via
Dispatch.me) place on a Housecall Pro job and returns structured fields for the
routing app.  Ported from the project's ``warranty_parser.py`` (same field names, so it is
a drop-in) with these changes:

* urgency keywords match on word boundaries ("no ac" no longer matches "no access",
  "gas" no longer matches "gasket")
* "Area Of Home" and money lines tolerate markdown bold markers
* falls back to a "Contract Contact" name when no "Dispatch Contact" is present
* ``looks_like_warranty`` / ``missing_key_fields`` helpers for the AI fallback
* ``parsed_by`` field ("regex" or "ai")

Rules (from the build spec, section 5.2):
* HTML entities are decoded before parsing.
* Every field is optional; a missing field produces a ``parse_warnings`` entry, never a crash.
* Priority ALWAYS comes from the body ``Dispatch Priority:`` line, never from the header
  (the header suffix stayed NORMAL on an Expedited job).
"""

from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

# Words in the problem text that suggest extra urgency regardless of dispatch priority.
URGENCY_KEYWORDS = [
    "secondary damage", "leak", "flood", "no water", "no hot water",
    "no heat", "no cooling", "no ac", "gas", "sewage", "backup", "burst",
]

PRIORITY_RANK = {"emergency": 3, "expedited": 2, "normal": 1}


@dataclass
class WorkItem:
    name: str
    problem: Optional[str] = None
    status: Optional[str] = None
    area_of_home: Optional[str] = None


@dataclass
class WarrantyJob:
    is_warranty: bool = False
    warranty_company: Optional[str] = None       # e.g. "AHS"
    dispatch_number: Optional[str] = None
    trade_code: Optional[str] = None             # e.g. "PLB", "HVAC", "APPL"
    dispatch_priority: Optional[str] = None      # Normal / Expedited / Emergency
    priority_rank: int = 0                       # 3 = emergency, 2 = expedited, 1 = normal
    header_secondary_code: Optional[str] = None  # value after the colon in the header line
    authorization_required: Optional[bool] = None
    service_request_id: Optional[str] = None
    vendor_id: Optional[str] = None
    contract_id: Optional[str] = None
    contract_effective_date: Optional[str] = None
    contract_expiration_date: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phones: list = field(default_factory=list)
    street: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    full_address: Optional[str] = None
    items: list = field(default_factory=list)
    plan_name: Optional[str] = None
    payment_type: Optional[str] = None
    do_not_collect_service_fee: bool = False
    completion_date_required: bool = False
    recall_applies: bool = False
    total: Optional[float] = None
    paid: Optional[float] = None
    remaining: Optional[float] = None
    authorization_link: Optional[str] = None
    dispatch_me_links: list = field(default_factory=list)
    urgency_flags: list = field(default_factory=list)
    parse_warnings: list = field(default_factory=list)
    parsed_by: str = "regex"


# --------------------------------------------------------------------------- helpers

def _clean(text: str) -> str:
    """Decode HTML entities and normalise line endings."""
    text = html.unescape(text or "")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _find(pattern: str, text: str, flags: int = re.IGNORECASE) -> Optional[str]:
    m = re.search(pattern, text, flags)
    return m.group(1).strip() if m else None


def title_case(text: str) -> str:
    """str.title() for street and city names, minus its two mistakes: '5th Ave' -> '5Th Ave', "Martin's" -> "Martin'S"."""
    t = re.sub(r"(?<=\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1).lower(), text.title())
    return re.sub(r"(?<=[A-Za-z])'S\b", "'s", t)


def _money(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def _section(text: str, heading: str) -> Optional[str]:
    """Return the text under a '# Heading' up to the next top-level '# ' heading."""
    m = re.search(rf"^#\s*{re.escape(heading)}\s*$(.*?)(?=^#\s|\Z)",
                  text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def keyword_present(keyword: str, text: str) -> bool:
    """Whole-word / whole-phrase match (case-insensitive)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text, re.IGNORECASE) is not None


def find_urgency_flags(text: str, keywords=None) -> list:
    return [k for k in (keywords or URGENCY_KEYWORDS) if keyword_present(k, text)]


# ------------------------------------------------------------------ public helpers

def looks_like_warranty(raw: Optional[str]) -> bool:
    """Cheap check used to decide whether the warranty parser should run on a job."""
    if not raw:
        return False
    t = _clean(raw)
    return bool(
        re.search(r"^\s*[a-z]{2,10}:\d{5,}\s*$", t, re.IGNORECASE | re.MULTILINE)
        or re.search(r"This is an?\s+\w+\s+customer", t, re.IGNORECASE)
        or re.search(r"Dispatch Priority:", t, re.IGNORECASE)
        or re.search(r"Covered Property Address", t, re.IGNORECASE)
    )


def missing_key_fields(job: WarrantyJob) -> list:
    """Key fields the AI fallback should try to recover (spec 5.2: address, priority, trade)."""
    missing = []
    if not job.full_address or not job.street or not job.zip_code:
        missing.append("address")
    if not job.dispatch_priority:
        missing.append("dispatch_priority")
    if not job.trade_code:
        missing.append("trade_code")
    return missing


# -------------------------------------------------------------------------- parser

def parse_warranty_job(raw: str) -> WarrantyJob:
    text = _clean(raw)
    job = WarrantyJob()

    # --- Header: "ahs:88164669" then "88164669 PLB Expedited:NORMAL"
    tag = re.search(r"^\s*([a-z]{2,10}):(\d{5,})\s*$", text, re.IGNORECASE | re.MULTILINE)
    if tag:
        job.warranty_company = tag.group(1).upper()
        job.dispatch_number = tag.group(2)

    header = re.search(r"^\s*(\d{5,})\s+([A-Z]{2,6})\s+(\w+):(\w+)\s*$", text, re.MULTILINE)
    if header:
        job.dispatch_number = job.dispatch_number or header.group(1)
        job.trade_code = header.group(2).upper()
        job.header_secondary_code = header.group(4).upper()  # stored, NOT used for priority

    brand = _find(r"This is an?\s+(\w+)\s+customer", text)
    if brand:
        job.warranty_company = job.warranty_company or brand.upper()

    job.is_warranty = bool(job.warranty_company and job.dispatch_number)

    # --- Priority: always from the body, never from the header
    prio = _find(r"Dispatch Priority:\**\s*([A-Za-z]+)", text)
    if prio:
        job.dispatch_priority = prio.capitalize()
        job.priority_rank = PRIORITY_RANK.get(prio.lower(), 0)
        if job.priority_rank == 0:
            job.parse_warnings.append(f"Unrecognised dispatch priority '{prio}'")
    else:
        job.parse_warnings.append("Dispatch Priority not found")

    autho = _find(r"Autho Required\?:\**\s*(True|False|Yes|No)", text)
    if autho is not None:
        job.authorization_required = autho.lower() in ("true", "yes")

    # --- Authorization link (+ IDs inside its query string)
    auth_url = _find(r"Authorization Link:.*?\((https?://[^\s)]+)\)", text, re.IGNORECASE | re.DOTALL)
    if auth_url:
        job.authorization_link = auth_url
        job.service_request_id = _find(r"svcReq=(\d+)", auth_url)
        job.trade_code = job.trade_code or _find(r"tradeCode=([A-Za-z]+)", auth_url)
        if job.trade_code:
            job.trade_code = job.trade_code.upper()
        job.vendor_id = _find(r"vendorId=(\d+)", auth_url)
    job.dispatch_me_links = list(dict.fromkeys(re.findall(r"https?://dispa\.me/\w+", text)))

    job.vendor_id = _find(r"Vendor ID\**:\**\s*(\d+)", text) or job.vendor_id
    job.contract_id = _find(r"Contract ID:\**\s*(\d+)", text)
    job.contract_effective_date = _find(r"Contract Effective Date:\**\s*([\d-]+)", text)
    job.contract_expiration_date = _find(r"Contract Expiration Date:\**\s*([\d-]+)", text)

    # --- Customer contact
    contact = re.search(r"\*\*([^*\n]+?)\*\*\s*\(Dispatch Contact\)", text)
    if not contact:
        contact = re.search(r"\*\*([^*\n]+?)\*\*\s*\([A-Za-z ]*Contact\)", text)
    if contact:
        job.contact_name = contact.group(1).strip().title()
    phones = re.findall(r"tel:\+?(\d{10,11})", text)
    job.contact_phones = sorted(set(p[-10:] for p in phones))

    # --- Covered property address (this is the address to geocode)
    addr_block = _section(text, "Covered Property Address")
    if addr_block:
        lines = [ln.strip() for ln in addr_block.strip().splitlines() if ln.strip()]
        if lines:
            job.street = title_case(lines[0])
        if len(lines) >= 2:
            m = re.match(r"(.+?),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)", lines[1])
            if m:
                job.city, job.state, job.zip_code = title_case(m.group(1)), m.group(2).upper(), m.group(3)
        if job.street and job.city and job.state and job.zip_code:
            job.full_address = f"{job.street}, {job.city}, {job.state} {job.zip_code}"
        elif job.street:
            job.parse_warnings.append("Covered property address incomplete (city/state/zip not parsed)")
    if not job.full_address and not any("address" in w.lower() for w in job.parse_warnings):
        job.parse_warnings.append("Covered property address not found")

    # --- Work items (one or more "## Item N: Name" blocks)
    for m in re.finditer(r"##\s*Item\s*\d+:\s*(.+?)\n(.*?)(?=##\s*Item\s*\d+:|^#\s|\Z)",
                         text, re.IGNORECASE | re.MULTILINE | re.DOTALL):
        body = m.group(2)
        problem = _find(r"Problem:\**\s*\n?\s*([^\n]+)", body)
        item = WorkItem(
            name=m.group(1).strip().strip("*").strip(),
            problem=problem.rstrip(", ").strip() if problem else None,
            status=_find(r"Status:\**\s*([^\n]+)", body),
            area_of_home=_find(r"Area Of Home:\**\s*([^\n]+)", body),
        )
        job.items.append(asdict(item))
    if not job.items:
        job.parse_warnings.append("No work items found")

    # --- Coverage notes and payment rules
    job.plan_name = _find(r"^\s*([A-Z][A-Z ]+HOME WARRANTY)\s*$", text, re.MULTILINE)
    job.payment_type = _find(r"Payment Type\s*:\s*([A-Za-z]+)", text)
    job.do_not_collect_service_fee = bool(re.search(r"DO NOT COLLECT TRADE SERVICE FEE", text, re.I))
    job.completion_date_required = bool(re.search(r"COMPLETION DATE MUST BE PROVIDED", text, re.I))
    job.recall_applies = bool(re.search(r"\d+-DAY RECALL APPLIES", text, re.I))

    job.total = _money(_find(r"Total:\**\s*\$([\d,.]+)", text))
    job.paid = _money(_find(r"Paid:\**\s*\$([\d,.]+)", text))
    job.remaining = _money(_find(r"Remaining:\**\s*\$([\d,.]+)", text))

    # --- Urgency keywords in item names + problem text
    item_text = " ".join(f"{i['name']} {i.get('problem') or ''}" for i in job.items)
    job.urgency_flags = find_urgency_flags(item_text)

    return job


def to_dict(job: WarrantyJob) -> dict:
    return asdict(job)
