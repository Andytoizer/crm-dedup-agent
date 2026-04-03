# ============================================================
# INTEGRATION FILE — Adapt this for your CRM
# Original: Built for HubSpot
# What to change: API endpoints, auth headers, field names, pagination logic
# What to preserve: Generator/iterator pattern, normalized record shape (id, email, name, company, phone, linkedin fields)
# ============================================================

"""
Merger: executes HubSpot merges and writes audit log entries.

Master record selection uses engagement-weighted scoring from scorer.py.
Before merging, non-null properties from the secondary record are copied to the
primary where the primary has no value ("pre-merge property copy"), so the
surviving record retains the best data from both.
"""
import json
import time
import uuid
import requests
from datetime import datetime, timezone
from typing import Optional

from pipeline.scorer import select_master, MatchResult
from db.database import get_session
from db.models import AuditLog
from config.settings import HUBSPOT_ACCESS_TOKEN, HUBSPOT_API_BASE


# Properties to carry over from secondary → primary if primary field is empty
CONTACT_COPY_PROPERTIES = [
    "hs_linkedin_url", "lemlistlinkedinurl", "phone", "mobilephone",
    "jobtitle", "website",
]

COMPANY_COPY_PROPERTIES = [
    "linkedin_company_page", "lemlistprofileurl", "phone",
    "website", "domain", "description",
]


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _hs_patch(object_type: str, record_id: str, properties: dict) -> None:
    """Update a HubSpot record's properties."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/{object_type}/{record_id}"
    resp = requests.patch(url, json={"properties": properties}, headers=_hs_headers(), timeout=30)
    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", "10")))
        _hs_patch(object_type, record_id, properties)
        return
    if resp.status_code in (400, 404):
        # Record already merged/deleted — raise so execute_merge can skip this pair
        raise ValueError(f"HubSpot {resp.status_code} on PATCH {record_id}: {resp.text[:200]}")
    resp.raise_for_status()


def _hs_merge(object_type: str, primary_id: str, secondary_id: str) -> Optional[str]:
    """
    Merge secondary into primary. Returns the new merged record ID.
    Since Jan 2025, HubSpot creates a new record ID on merge.
    """
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/{object_type}/merge"
    payload = {
        "primaryObjectId": primary_id,
        "objectIdToMerge": secondary_id,
    }
    resp = requests.post(url, json=payload, headers=_hs_headers(), timeout=30)
    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", "10")))
        return _hs_merge(object_type, primary_id, secondary_id)
    if resp.status_code == 400:
        # Record was already merged or deleted — skip silently
        raise ValueError(f"HubSpot 400 on merge {primary_id}+{secondary_id}: {resp.text[:200]}")
    if resp.status_code == 404:
        raise ValueError(f"HubSpot 404 — record not found {primary_id} or {secondary_id}")
    resp.raise_for_status()
    data = resp.json()
    return data.get("id") or primary_id


def _pre_merge_copy(
    object_type: str,
    master: dict,
    secondary: dict,
    copy_props: list,
    dry_run: bool,
) -> None:
    """Copy non-null properties from secondary to master where master is empty."""
    to_copy = {}
    for prop in copy_props:
        if not master.get(prop) and secondary.get(prop):
            to_copy[prop] = secondary[prop]

    if to_copy and not dry_run:
        _hs_patch(object_type, master["id"], to_copy)


def execute_merge(
    match: MatchResult,
    record_a: dict,
    record_b: dict,
    object_type: str,       # "contacts" | "companies"
    run_id: str,
    dry_run: bool = True,
) -> Optional[str]:
    """
    Execute (or simulate) a merge. Returns the merged record ID, or None in dry_run.

    Steps:
    1. Select master record by engagement score
    2. Pre-merge: copy missing properties from secondary → primary
    3. Execute HubSpot merge API (skip in dry_run)
    4. Write audit log entry
    """
    master, secondary = select_master(record_a, record_b)

    copy_props = CONTACT_COPY_PROPERTIES if object_type == "contacts" else COMPANY_COPY_PROPERTIES
    try:
        _pre_merge_copy(object_type, master, secondary, copy_props, dry_run)
    except ValueError:
        raise  # bubble up so caller can skip this pair

    merged_id = None
    if not dry_run:
        merged_id = _hs_merge(object_type, master["id"], secondary["id"])

    # Determine object_type label for DB (singular, without trailing 's')
    object_type_label = "contact" if object_type == "contacts" else "company"

    with get_session() as session:
        entry = AuditLog(
            timestamp=datetime.now(timezone.utc),
            object_type=object_type_label,
            primary_id=master["id"],
            secondary_id=secondary["id"],
            merged_record_id=merged_id,
            score=match.score,
            match_signals=json.dumps(match.match_signals),
            match_reason=match.match_reason,
            dry_run=dry_run,
            agent_run_id=run_id,
        )
        session.add(entry)

    return merged_id
