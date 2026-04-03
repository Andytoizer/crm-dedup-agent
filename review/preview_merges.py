# ============================================================
# INTEGRATION FILE — Adapt this for your CRM
# Original: Built for HubSpot
# What to change: The record fetch URL and field list (uses HubSpot CRM object endpoints)
# What to preserve: The overall preview format and master/secondary comparison logic
# ============================================================

"""
Preview proposed merges from the last dry run with full details and HubSpot links.

Usage:
    python review/preview_merges.py               # show 50 merges (default)
    python review/preview_merges.py --limit 20    # show 20
    python review/preview_merges.py --type contact
    python review/preview_merges.py --type company
    python review/preview_merges.py --open-links  # print links you can cmd-click
"""
import argparse
import json
import sys
import os
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_session
from db.models import AuditLog
from pipeline.scorer import score_for_master
from pipeline.fetcher import _normalize_contact, _normalize_company
from config.settings import HUBSPOT_ACCESS_TOKEN, HUBSPOT_API_BASE, \
    CONTACT_FETCH_PROPERTIES, COMPANY_FETCH_PROPERTIES


def _get_portal_id() -> str:
    resp = requests.get(
        f"{HUBSPOT_API_BASE}/oauth/v1/access-tokens/{HUBSPOT_ACCESS_TOKEN}",
        timeout=10,
    )
    if resp.ok:
        return str(resp.json().get("hub_id", "YOUR_PORTAL_ID"))
    # Fallback: extract from a contact URL pattern
    return "YOUR_PORTAL_ID"


def _fetch_record(object_type: str, oid: str, portal_id: str):
    """Fetch a single HubSpot record with all dedup properties."""
    if object_type == "contact":
        props = CONTACT_FETCH_PROPERTIES
        normalizer = _normalize_contact
        hs_type = "contacts"
        url_path = "contact"
    else:
        props = COMPANY_FETCH_PROPERTIES
        normalizer = _normalize_company
        hs_type = "companies"
        url_path = "company"

    api_url = f"{HUBSPOT_API_BASE}/crm/v3/objects/{hs_type}/{oid}"
    resp = requests.get(
        api_url,
        params={"properties": ",".join(props)},
        headers={"Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    record = normalizer(resp.json())
    record["_hubspot_url"] = f"https://app.hubspot.com/contacts/{portal_id}/{url_path}/{oid}"
    return record


def _engagement_summary(r: dict) -> str:
    parts = []
    if r.get("num_associated_deals", 0) > 0:
        parts.append(f"deals={r['num_associated_deals']}")
    if r.get("num_notes", 0) > 0:
        parts.append(f"notes={r['num_notes']}")
    if r.get("hs_email_replied", 0) > 0:
        parts.append(f"replied={r['hs_email_replied']}")
    if r.get("hs_email_open", 0) > 0:
        parts.append(f"opens={r['hs_email_open']}")
    if r.get("hs_last_sales_activity_timestamp"):
        parts.append("has_activity")
    return ", ".join(parts) if parts else "no activity"


def _format_contact(r: dict, label: str, score: float) -> str:
    name = f"{r.get('firstname', '')} {r.get('lastname', '')}".strip() or "(no name)"
    email = r.get("email") or "(no email)"
    company = r.get("company") or "(no company)"
    phone = r.get("phone") or r.get("mobilephone") or "(no phone)"
    linkedin = r.get("hs_linkedin_url") or r.get("lemlistlinkedinurl") or "(no linkedin)"
    eng = _engagement_summary(r)
    url = r.get("_hubspot_url", "")
    return (
        f"  {label} (engagement={score:.0f}):\n"
        f"    Name:     {name}\n"
        f"    Email:    {email}\n"
        f"    Company:  {company}\n"
        f"    Phone:    {phone}\n"
        f"    LinkedIn: {linkedin}\n"
        f"    Activity: {eng}\n"
        f"    HubSpot:  {url}"
    )


def _format_company(r: dict, label: str, score: float) -> str:
    name = r.get("name") or "(no name)"
    domain = r.get("domain") or r.get("website") or "(no domain)"
    linkedin = r.get("linkedin_company_page") or r.get("lemlistprofileurl") or "(no linkedin)"
    contacts = r.get("num_associated_contacts", 0)
    deals = r.get("num_associated_deals", 0)
    url = r.get("_hubspot_url", "")
    return (
        f"  {label} (engagement={score:.0f}):\n"
        f"    Name:     {name}\n"
        f"    Domain:   {domain}\n"
        f"    LinkedIn: {linkedin}\n"
        f"    Contacts: {contacts} | Deals: {deals}\n"
        f"    HubSpot:  {url}"
    )


def run(limit: int = 50, object_type_filter: str = None):
    init_db()

    print("Fetching your HubSpot portal ID...")
    portal_id = _get_portal_id()

    with get_session() as session:
        query = session.query(AuditLog).filter(AuditLog.dry_run == True)
        if object_type_filter:
            query = query.filter(AuditLog.object_type == object_type_filter)
        rows = query.order_by(AuditLog.score.desc()).limit(limit).all()
        pairs = [(r.primary_id, r.secondary_id, r.object_type, r.match_reason) for r in rows]

    # Deduplicate pairs
    seen = set()
    unique_pairs = []
    for id_a, id_b, otype, reason in pairs:
        key = (min(id_a, id_b), max(id_a, id_b))
        if key not in seen:
            seen.add(key)
            unique_pairs.append((id_a, id_b, otype, reason))

    SEP = "=" * 70
    print(f"\n{SEP}")
    print(f"MERGE PREVIEW — {len(unique_pairs)} proposed merges (DRY RUN — nothing changed)")
    print(f"Portal ID: {portal_id}")
    print(f"{SEP}")
    print("For each pair: MASTER = record that survives | ABSORBED = record that gets merged in")
    print("Open the HubSpot links to verify before running --live\n")

    errors = 0
    for i, (id_a, id_b, otype, reason) in enumerate(unique_pairs, 1):
        try:
            rec_a = _fetch_record(otype, id_a, portal_id)
            rec_b = _fetch_record(otype, id_b, portal_id)
            score_a = score_for_master(rec_a)
            score_b = score_for_master(rec_b)
            master   = rec_a if score_a >= score_b else rec_b
            absorbed = rec_b if score_a >= score_b else rec_a
            ms = max(score_a, score_b)
            ss = min(score_a, score_b)

            print(f"--- {i}. {otype.upper()} | Match: {reason} ---")
            if otype == "contact":
                print(_format_contact(master, "MASTER  ", ms))
                print(_format_contact(absorbed, "ABSORBED", ss))
            else:
                print(_format_company(master, "MASTER  ", ms))
                print(_format_company(absorbed, "ABSORBED", ss))
            print()

        except Exception as e:
            errors += 1
            print(f"--- {i}. ERROR fetching {otype} {id_a}/{id_b}: {e} ---\n")

    print(SEP)
    print(f"Reviewed {len(unique_pairs)} pairs ({errors} errors).")
    print(f"If everything looks correct, run:")
    print(f"  python agents/bulk_dedup_agent.py --live")
    print(SEP)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview proposed merges before going live")
    parser.add_argument("--limit", type=int, default=50, help="Number of pairs to show (default: 50)")
    parser.add_argument("--type", dest="object_type", choices=["contact", "company"],
                        default=None, help="Filter to contacts or companies only")
    args = parser.parse_args()
    run(limit=args.limit, object_type_filter=args.object_type)
