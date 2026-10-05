"""
What kind of job is this? One place decides, everything else (scoring, deadlines, the queue, the map, reports) asks.

    WARRANTY  a call from a warranty company. Housecall Pro carries it as one of three tags:
                  normal: expedited   ->  Expedited
                  normal: normal      ->  Normal
                  normal: recall      ->  Recall
              or, failing those, as a warranty SOURCE tag ("dispatch.me", which the Dispatch.me integration puts on
              every job it creates) together with dispatch text: the tier is then read from the text: the body's
              "Dispatch Priority" and the header line ("<no> PLB Expedited:NORMAL", "<no> PLB Normal:RECALL"):
              Expedited or Emergency -> Expedited, RECALL -> Recall, anything else -> Normal. The live account (2026-10-05) used no
              priority tags at all; 13 of its 14 warranty jobs carried "dispatch.me".
    RETAIL    everything else: a call that did not come from a warranty company. Not having a warranty tag means one of
              two things: a warranty call that was turned into a retail job, or a lead from an ad. Ad leads are told
              apart by the "meta lead" tag (``ad_lead``); they are still Retail.

The tag texts are settings (Admin > Settings > Job types), compared ignoring case, spacing around the colon and extra
spaces, so "Normal : Expedited" matches "normal: expedited". If a job has more than one warranty tag (a data slip) the
most urgent wins. The warranty TEXT is deliberately not used to decide the kind: a converted job usually still carries
the old dispatch text, and showing its "do not collect the fee" alerts would be wrong - it is retail now. The text only
counts next to a source tag, so a converted job (source tag removed) stays Retail. A job with text but no tag is
flagged (``warranty_text_without_tag``) so a forgotten tag can be spotted.

Pure functions only; classification is computed when a job is read, so changing the tag settings applies at once.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional

WARRANTY = "warranty"
RETAIL = "retail"
# most urgent first: this is also the order used when a job carries more than one warranty tag
WARRANTY_TIERS = ("Expedited", "Recall", "Normal")
TYPE_LABELS = ("Expedited", "Normal", "Recall", "Retail")      # in the order people expect to see them

DEFAULT_JOB_TYPES = {
    "warranty_tags": {
        "Expedited": ["normal: expedited"],
        "Normal": ["normal: normal"],
        "Recall": ["normal: recall"],
    },
    "ad_lead_tags": ["meta lead"],
    # a job with one of these tags AND warranty dispatch text is warranty work; its tier comes from the text's priority
    "warranty_source_tags": ["dispatch.me"],
}

# words in the dispatch text -> tier (anything not listed, or missing, is Normal)
TEXT_PRIORITY_TIERS = {"expedited": "Expedited", "emergency": "Expedited", "recall": "Recall"}


def text_tier(warranty: dict) -> str:
    """Tier named by the dispatch text: the body's "Dispatch Priority" and the header line "<no> PLB Normal:RECALL"
    (word before the colon, code after it). Expedited beats Recall beats Normal, like the tags. Deliberately NOT a
    search for the word "recall" anywhere in the text: every AHS dispatch says "30-DAY RECALL APPLIES"."""
    data = warranty.get("data") if isinstance(warranty.get("data"), dict) else {}
    words = {str(v).strip().lower() for v in (
        warranty.get("dispatch_priority") or data.get("dispatch_priority"),
        warranty.get("header_priority") or data.get("header_priority"),
        warranty.get("header_secondary_code") or data.get("header_secondary_code")) if v}
    found = {TEXT_PRIORITY_TIERS[w] for w in words if w in TEXT_PRIORITY_TIERS}
    for tier in WARRANTY_TIERS:
        if tier in found:
            return tier
    return "Normal"


def norm_tag(value) -> str:
    """Lower case, one space between words, ``a : b`` -> ``a: b``."""
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("label") or ""
    t = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    return re.sub(r"\s*:\s*", ": ", t)


def _tag_set(values: Iterable) -> set:
    return {t for t in (norm_tag(v) for v in values or []) if t}


def classify_job(job: dict, settings: dict, warranty: Optional[dict] = None) -> dict:
    """{"kind", "label", "tier", "ad_lead", "ad_tag", "warranty_text_without_tag"} for one job.

    ``job`` needs ``tags`` (list) and may have ``lead_source``; ``warranty`` is the parsed warranty record (a dict
    with ``dispatch_priority``), if the job's text looked like a warranty dispatch: with a source tag it gives the
    tier, without one it only flags a retail job that still carries dispatch text."""
    cfg = (settings or {}).get("job_types") or DEFAULT_JOB_TYPES
    tags = _tag_set(job.get("tags") or [])
    wanted = cfg.get("warranty_tags") or DEFAULT_JOB_TYPES["warranty_tags"]
    for tier in WARRANTY_TIERS:
        if tags & _tag_set(wanted.get(tier)):
            return {"kind": WARRANTY, "label": tier, "tier": tier, "ad_lead": False, "ad_tag": None,
                    "warranty_text_without_tag": False}
    sources = _tag_set(cfg.get("warranty_source_tags", DEFAULT_JOB_TYPES["warranty_source_tags"]))
    if warranty and tags & sources:
        tier = text_tier(warranty)
        return {"kind": WARRANTY, "label": tier, "tier": tier, "ad_lead": False, "ad_tag": None,
                "warranty_text_without_tag": False}
    ad = _tag_set(cfg.get("ad_lead_tags") or [])
    hit = sorted(tags & ad) or ([norm_tag(job.get("lead_source"))] if norm_tag(job.get("lead_source")) in ad else [])
    return {"kind": RETAIL, "label": "Retail", "tier": None, "ad_lead": bool(hit), "ad_tag": hit[0] if hit else None,
            "warranty_text_without_tag": bool(warranty)}


def count_types(kinds: Iterable[dict]) -> Dict[str, int]:
    """{"Expedited": n, "Normal": n, "Recall": n, "Retail": n} (zeros included, in display order)."""
    out = {label: 0 for label in TYPE_LABELS}
    for k in kinds:
        out[k["label"]] += 1
    return out


def validate_job_types(patch: dict) -> None:
    """Raises ValueError with a readable message for a job_types settings patch."""
    tiers = (patch.get("warranty_tags") or {})
    seen: Dict[str, str] = {}
    for tier, names in tiers.items():
        for n in names:
            if not isinstance(n, str) or not norm_tag(n):
                raise ValueError(f"Each {tier} tag must be some text")
            if len(n) > 80:
                raise ValueError("A tag must be 80 characters or fewer")
            key = norm_tag(n)
            if key in seen and seen[key] != tier:
                raise ValueError(f"The tag '{n}' is listed under both {seen[key]} and {tier}")
            seen[key] = tier
    for n in patch.get("warranty_source_tags") or []:
        if not isinstance(n, str) or not norm_tag(n) or len(n) > 80:
            raise ValueError("Each warranty source tag must be some text (80 characters or fewer)")
        if norm_tag(n) in seen:
            raise ValueError(f"The tag '{n}' is listed as both a warranty tag and a warranty source tag")
    sources = {norm_tag(n) for n in patch.get("warranty_source_tags") or [] if isinstance(n, str)}
    for n in patch.get("ad_lead_tags") or []:
        if not isinstance(n, str) or not norm_tag(n) or len(n) > 80:
            raise ValueError("Each ad lead tag must be some text (80 characters or fewer)")
        if norm_tag(n) in seen:
            raise ValueError(f"The tag '{n}' is listed as both a warranty tag and an ad lead tag")
        if norm_tag(n) in sources:
            raise ValueError(f"The tag '{n}' is listed as both a warranty source tag and an ad lead tag")
