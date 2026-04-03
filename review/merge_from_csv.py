# ============================================================
# INTEGRATION FILE — Adapt this for your CRM  
# Original: Built for HubSpot CSV export format (columns: FIRSTNAME_1, EMAIL_2, etc.)
# What to change: CONTACT_FIELD_MAP and COMPANY_FIELD_MAP to match your CRM's export column names
# What to preserve: The master selection logic and merge execution flow
# ============================================================

"""
Merge HubSpot-identified duplicate pairs from exported CSV files.

HubSpot's own dedup tool exports pairs as CSV with columns:
  ID_1, ID_2, FIRSTNAME_1, FIRSTNAME_2, ...

This script:
  1. Reads the CSV
  2. Builds record dicts from the _1/_2 columns
  3. Uses select_master() to pick the right master
  4. Executes the merge via HubSpot API

Usage:
    # Dry run (default)
    python review/merge_from_csv.py --contacts contacts.csv
    python review/merge_from_csv.py --companies companies.csv

    # Live
    python review/merge_from_csv.py --contacts contacts.csv --live
    python review/merge_from_csv.py --companies companies.csv --live
"""
import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.merger import execute_merge
from pipeline.scorer import MatchResult, select_master
from db.database import init_db


# Map CSV column names (uppercase, no suffix) → our internal record field names
CONTACT_FIELD_MAP = {
    "HS_OBJECT_ID": "id",
    "FIRSTNAME": "firstname",
    "LASTNAME": "lastname",
    "EMAIL": "email",
    "PHONE": "phone",
    "MOBILEPHONE": "mobilephone",
    "COMPANY": "company",
    "HS_LINKEDIN_URL": "hs_linkedin_url",
    "LEMLISTLINKEDINURL": "lemlistlinkedinurl",
    "NUM_ASSOCIATED_DEALS": "num_associated_deals",
    "NUM_NOTES": "num_notes",
    "HS_EMAIL_OPEN": "hs_email_open",
    "HS_EMAIL_SENDS_SINCE_LAST_ENGAGEMENT": "hs_email_sends_since_last_engagement",
    "HS_LAST_SALES_ACTIVITY_TIMESTAMP": "hs_last_sales_activity_timestamp",
    "NOTES_LAST_CONTACTED": "notes_last_contacted",
    "HS_SALES_EMAIL_LAST_REPLIED": "hs_email_replied",
    "CREATEDATE": "createdate",
    "LASTMODIFIEDDATE": "lastmodifieddate",
    "LIFECYCLESTAGE": "lifecyclestage",
    "HUBSPOT_OWNER_ID": "hubspot_owner_id",
    "HS_MERGED_OBJECT_IDS": "hs_merged_object_ids",
}

COMPANY_FIELD_MAP = {
    "HS_OBJECT_ID": "id",
    "NAME": "name",
    "DOMAIN": "domain",
    "WEBSITE": "website",
    "LINKEDIN_COMPANY_PAGE": "linkedin_company_page",
    "LEMLISTPROFILEURL": "lemlistprofileurl",
    "PHONE": "phone",
    "NUM_ASSOCIATED_CONTACTS": "num_associated_contacts",
    "NUM_ASSOCIATED_DEALS": "num_associated_deals",
    "HS_LAST_SALES_ACTIVITY_TIMESTAMP": "hs_last_sales_activity_timestamp",
    "CREATEDATE": "createdate",
    "HS_LASTMODIFIEDDATE": "lastmodifieddate",
    "LIFECYCLESTAGE": "lifecyclestage",
    "HUBSPOT_OWNER_ID": "hubspot_owner_id",
    "HS_MERGED_OBJECT_IDS": "hs_merged_object_ids",
}


def _build_record(row: dict, suffix: str, field_map: dict) -> dict:
    """Extract _1 or _2 fields from a CSV row into a normalised record dict."""
    record = {}
    for csv_col, internal_field in field_map.items():
        val = row.get(f"{csv_col}_{suffix}", "").strip()
        record[internal_field] = val if val else None
    # Coerce all numeric fields — must be 0 not None so scorer comparisons work
    numeric_fields = (
        "num_associated_deals", "num_notes", "num_associated_contacts",
        "hs_email_open", "hs_email_click", "hs_email_sends_since_last_engagement",
        "hs_email_replied",
    )
    for f in numeric_fields:
        try:
            record[f] = int(record.get(f) or 0)
        except (ValueError, TypeError):
            record[f] = 0
    return record


def run(csv_path: str, object_type: str, dry_run: bool = True, limit: int = None):
    init_db()
    hs_type = "contacts" if object_type == "contact" else "companies"

    print(f"\n{'='*60}")
    print(f"CSV Merge — {object_type}s")
    print(f"File: {csv_path}")
    print(f"Mode: {'DRY RUN' if dry_run else '*** LIVE ***'}")
    print(f"{'='*60}\n")

    field_map = CONTACT_FIELD_MAP if object_type == "contact" else COMPANY_FIELD_MAP

    merged = 0
    skipped = 0
    errors = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if limit:
        rows = rows[:limit]

    total = len(rows)

    for i, row in enumerate(rows, 1):
        rec_a = _build_record(row, "1", field_map)
        rec_b = _build_record(row, "2", field_map)

        id_a = rec_a.get("id")
        id_b = rec_b.get("id")

        if not id_a or not id_b:
            print(f"[{i}/{total}] SKIP — missing IDs")
            skipped += 1
            continue

        if id_a == id_b:
            print(f"[{i}/{total}] SKIP — same record ID ({id_a})")
            skipped += 1
            continue

        try:
            master, secondary = select_master(rec_a, rec_b)
            name_a = rec_a.get("firstname") or rec_a.get("name") or id_a
            name_b = rec_b.get("firstname") or rec_b.get("name") or id_b

            match = MatchResult(
                id_a=id_a, id_b=id_b,
                score=1.0, action="AUTO_MERGE",
                match_signals=[],
                match_reason="HubSpot native dedup export",
            )

            merged_id = execute_merge(
                match=match,
                record_a=rec_a,
                record_b=rec_b,
                object_type=hs_type,
                run_id="csv-import",
                dry_run=dry_run,
            )

            status = "DRY" if dry_run else f"MERGED→{merged_id}"
            master_name = master.get("firstname") or master.get("name") or master["id"]
            print(f"[{i}/{total}] {status} | master: {master_name} ({master['id']}) ← absorbs ({secondary['id']})")
            merged += 1

        except ValueError as e:
            print(f"[{i}/{total}] SKIP — {e}")
            skipped += 1
        except Exception as e:
            print(f"[{i}/{total}] ERROR — {e}")
            errors += 1

        if not dry_run:
            time.sleep(0.15)  # gentle rate limiting

    print(f"\n{'='*60}")
    print(f"{'DRY RUN ' if dry_run else ''}Results:")
    print(f"  Merged:  {merged}")
    print(f"  Skipped: {skipped}")
    print(f"  Errors:  {errors}")
    if dry_run:
        print(f"\nRun with --live to execute.")
    print(f"{'='*60}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--contacts", metavar="CSV", help="Path to contacts duplicate CSV")
    group.add_argument("--companies", metavar="CSV", help="Path to companies duplicate CSV")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if args.contacts:
        run(args.contacts, "contact", dry_run=not args.live, limit=args.limit)
    else:
        run(args.companies, "company", dry_run=not args.live, limit=args.limit)
