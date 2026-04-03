# ============================================================
# MIXED FILE — AI reasoning and queue logic are universal; record fetch calls are HubSpot-specific
# What to change: _fetch_record() if swapping CRM
# What to preserve: The 3-stage decision pipeline (fast rules → web research → Claude)
# ============================================================

"""
CLI tool to approve or reject review queue items.

Usage:
    python review/queue_action.py approve 42        # approve item ID 42 → merge
    python review/queue_action.py reject 42         # reject item 42 → known non-duplicate
    python review/queue_action.py list              # show all pending items
    python review/queue_action.py approve 42 --live # approve and execute real merge
"""
import argparse
import json
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_session
from db.models import ReviewQueue, KnownNonDuplicate, AuditLog


def approve(item_id: int, live: bool = False):
    init_db()
    with get_session() as session:
        item = session.get(ReviewQueue, item_id)
        if not item:
            print(f"Item {item_id} not found.")
            return
        if item.status != "PENDING":
            print(f"Item {item_id} is already {item.status}.")
            return

        print(f"Approving {item.object_type} pair: {item.id_a} ↔ {item.id_b}")
        print(f"  Match reason: {item.match_reason}")

        if live:
            # Import here to avoid circular
            from pipeline.fetcher import fetch_contacts, fetch_companies
            from pipeline.scorer import score_contacts, score_companies, MatchResult
            from pipeline.merger import execute_merge
            import uuid

            # Re-fetch both records for current data
            object_type = item.object_type + "s"
            import requests
            from config.settings import HUBSPOT_ACCESS_TOKEN, HUBSPOT_API_BASE, CONTACT_FETCH_PROPERTIES, COMPANY_FETCH_PROPERTIES
            from pipeline.fetcher import _normalize_contact, _normalize_company

            props = CONTACT_FETCH_PROPERTIES if item.object_type == "contact" else COMPANY_FETCH_PROPERTIES
            normalizer = _normalize_contact if item.object_type == "contact" else _normalize_company

            def fetch_single(oid):
                url = f"{HUBSPOT_API_BASE}/crm/v3/objects/{object_type}/{oid}"
                resp = requests.get(
                    url,
                    params={"properties": ",".join(props)},
                    headers={"Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}"},
                    timeout=30,
                )
                resp.raise_for_status()
                return normalizer(resp.json())

            rec_a = fetch_single(item.id_a)
            rec_b = fetch_single(item.id_b)

            # Build a synthetic MatchResult from stored data
            match = MatchResult(
                id_a=item.id_a,
                id_b=item.id_b,
                score=item.score,
                action="AUTO_MERGE",
                match_signals=json.loads(item.match_signals or "[]"),
                match_reason=item.match_reason,
            )

            merged_id = execute_merge(
                match=match,
                record_a=rec_a,
                record_b=rec_b,
                object_type=object_type,
                run_id=f"review-approve-{item_id}",
                dry_run=False,
            )
            print(f"  Merged → new record ID: {merged_id}")
        else:
            print("  (dry-run — pass --live to execute the merge)")

        item.status = "APPROVED"
        item.reviewed_at = datetime.now(timezone.utc)


def reject(item_id: int):
    init_db()
    with get_session() as session:
        item = session.get(ReviewQueue, item_id)
        if not item:
            print(f"Item {item_id} not found.")
            return
        if item.status != "PENDING":
            print(f"Item {item_id} is already {item.status}.")
            return

        item.status = "REJECTED"
        item.reviewed_at = datetime.now(timezone.utc)

        # Add to suppression table
        nondupe = KnownNonDuplicate(
            object_type=item.object_type,
            id_a=item.id_a,
            id_b=item.id_b,
        )
        session.add(nondupe)
        print(f"Rejected {item.object_type} pair {item.id_a} ↔ {item.id_b}. Added to known non-duplicates.")


def list_pending():
    init_db()
    with get_session() as session:
        items = (
            session.query(ReviewQueue)
            .filter(ReviewQueue.status == "PENDING")
            .order_by(ReviewQueue.score.desc())
            .all()
        )
        if not items:
            print("No pending review items.")
            return
        print(f"\n{'ID':<5} {'Type':<10} {'Score':<8} {'Reason'}")
        print("-" * 70)
        for item in items:
            rec_a = json.loads(item.record_a_summary or "{}")
            rec_b = json.loads(item.record_b_summary or "{}")
            name_a = rec_a.get("name", item.id_a)
            name_b = rec_b.get("name", item.id_b)
            print(f"{item.id:<5} {item.object_type:<10} {item.score:<8.2f} {item.match_reason[:50]}")
            print(f"       A: {name_a}  |  B: {name_b}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Review queue actions")
    subparsers = parser.add_subparsers(dest="command")

    approve_p = subparsers.add_parser("approve", help="Approve and optionally merge a pair")
    approve_p.add_argument("item_id", type=int)
    approve_p.add_argument("--live", action="store_true")

    reject_p = subparsers.add_parser("reject", help="Reject a pair (mark as non-duplicate)")
    reject_p.add_argument("item_id", type=int)

    subparsers.add_parser("list", help="List pending review items")

    args = parser.parse_args()

    if args.command == "approve":
        approve(args.item_id, live=args.live)
    elif args.command == "reject":
        reject(args.item_id)
    elif args.command == "list":
        list_pending()
    else:
        parser.print_help()
