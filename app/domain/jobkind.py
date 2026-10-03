"""
What kind of job is this? One place decides, everything else (scoring, deadlines, the queue, the map, reports) asks.

    WARRANTY  a call from a warranty company. Housecall Pro carries it as one of three tags:
                  normal: expedited   ->  Expedited
                  normal: normal      ->  Normal
                  normal: recall      ->  Recall
    RETAIL    everything else: a call that did not come from a warranty company. Not having a warranty tag means one of
              two things: a warranty call that was turned into a retail job, or a lead from an ad. Ad leads are told
              apart by the "meta lead" tag (``ad_lead``); they are still Retail.

The tag texts are settings (Admin > Settings > Job types), compared ignoring case, spacing around the colon and extra
spaces, so "Normal : Expedited" matches "normal: expedited". If a job has more than one warranty tag (a data slip) the
most urgent wins. The warranty TEXT is deliberately not used to decide the kind: a converted job usually still carries
the old dispatch text, and showing its "do not collect the fee" alerts would be wrong - it is retail now. Such a job is
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
}


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

    ``job`` needs ``tags`` (list) and may have ``lead_source``; ``warranty`` is the parsed warranty record, if the
    job's text looked like a warranty dispatch (used only to flag a retail job that still carries one)."""
    cfg = (settings or {}).get("job_types") or DEFAULT_JOB_TYPES
    tags = _tag_set(job.get("tags") or [])
    wanted = cfg.get("warranty_tags") or DEFAULT_JOB_TYPES["warranty_tags"]
    for tier in WARRANTY_TIERS:
        if tags & _tag_set(wanted.get(tier)):
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
    for n in patch.get("ad_lead_tags") or []:
        if not isinstance(n, str) or not norm_tag(n) or len(n) > 80:
            raise ValueError("Each ad lead tag must be some text (80 characters or fewer)")
        if norm_tag(n) in seen:
            raise ValueError(f"The tag '{n}' is listed as both a warranty tag and an ad lead tag")
