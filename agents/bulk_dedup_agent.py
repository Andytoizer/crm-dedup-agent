# ============================================================
# MIXED FILE — Orchestration logic is universal; merge/fetch calls are HubSpot-specific
# What to change: execute_merge(), fetch_contacts(), fetch_companies() imports if swapping CRM
# What to preserve: The checkpoint pattern, scoring thresholds, review queue logic
# ============================================================

"""
Bulk deduplication agent.

Fetches all HubSpot contacts and companies, finds duplicates using the full
pipeline (fetch → block → score → route), and either executes merges (--live)
or logs proposed merges for review (dry-run, the default).

Usage:
    python agents/bulk_dedup_agent.py                          # dry-run all
    python agents/bulk_dedup_agent.py --live                   # merge all (use carefully)
    python agents/bulk_dedup_agent.py --live --contacts-only   # stage 1: contacts only
    python agents/bulk_dedup_agent.py --live --companies-only  # stage 2: companies only
    python agents/bulk_dedup_agent.py --live --min-score 1.0   # only exact matches
    python agents/bulk_dedup_agent.py --live --min-score 0.98  # exact + LinkedIn
    python agents/bulk_dedup_agent.py --limit 500              # sample first N records
"""
import argparse
import json
import sys
import uuid
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_session
from db.models import AgentRun, ReviewQueue
from pipeline.fetcher import fetch_contacts, fetch_companies
from pipeline.blocker import generate_contact_pairs, generate_company_pairs
from pipeline.scorer import score_contacts, score_companies
from pipeline.merger import execute_merge
from config.settings import AUTO_MERGE_THRESHOLD, REVIEW_THRESHOLD


def run(dry_run: bool = True, limit: int = None,
        contacts_only: bool = False, companies_only: bool = False,
        min_score: float = None, max_merges: int = None):
    init_db()
    run_id = str(uuid.uuid4())

    score_note = f" min_score={min_score}" if min_score else ""
    cap_note = f" max_merges={max_merges}" if max_merges else ""
    scope = " [contacts only]" if contacts_only else " [companies only]" if companies_only else ""
    print(f"\n{'='*60}")
    print(f"HubSpot Dedup — Bulk Run{scope}")
    print(f"Mode: {'DRY RUN (no changes)' if dry_run else '*** LIVE — merges will execute ***'}{score_note}{cap_note}")
    if max_merges and not dry_run:
        print(f"*** Hard cap: will stop after {max_merges} merges ***")
    print(f"Run ID: {run_id}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}\n")

    with get_session() as session:
        agent_run = AgentRun(
            run_id=run_id,
            run_type="bulk",
            started_at=datetime.now(timezone.utc),
            dry_run=dry_run,
        )
        session.add(agent_run)

    merge_counter = [0]  # mutable so subfunctions can increment it

    if not companies_only:
        _run_contacts(run_id, dry_run, limit, min_score=min_score,
                      max_merges=max_merges, merge_counter=merge_counter)
    if not contacts_only:
        remaining = (max_merges - merge_counter[0]) if max_merges else None
        if max_merges is None or remaining > 0:
            _run_companies(run_id, dry_run, limit, min_score=min_score,
                           max_merges=remaining, merge_counter=merge_counter)

    with get_session() as session:
        run_record = session.get(AgentRun, run_id)
        if run_record:
            run_record.completed_at = datetime.now(timezone.utc)

    print(f"\n{'='*60}")
    print(f"Run complete: {run_id}")
    print(f"{'='*60}")


def _run_contacts(run_id: str, dry_run: bool, limit: int = None, min_score: float = None,
                  max_merges: int = None, merge_counter: list = None):
    print("--- Fetching contacts ---")
    contacts = list(fetch_contacts(limit=limit))
    print(f"  Fetched {len(contacts)} contacts")

    with get_session() as session:
        run_record = session.get(AgentRun, run_id)
        if run_record:
            run_record.contacts_fetched = len(contacts)
            if contacts:
                run_record.last_contact_timestamp = contacts[-1]["lastmodifieddate"]

    id_map = {c["id"]: c for c in contacts}

    print("--- Generating candidate pairs ---")
    pairs = generate_contact_pairs(contacts)
    print(f"  {len(pairs)} candidate pairs to score")

    auto_count = 0
    review_count = 0
    discard_count = 0

    known_non_dupes = _load_known_non_dupes("contact")

    print("--- Scoring pairs ---")
    for id_a, id_b in pairs:
        rec_a = id_map.get(id_a)
        rec_b = id_map.get(id_b)
        if not rec_a or not rec_b:
            continue

        pair_key = (min(id_a, id_b), max(id_a, id_b))
        if pair_key in known_non_dupes:
            continue

        result = score_contacts(rec_a, rec_b)

        if min_score and result.score < min_score:
            discard_count += 1
            continue

        if result.action == "AUTO_MERGE":
            if max_merges and merge_counter and merge_counter[0] >= max_merges:
                print(f"  *** Reached max_merges={max_merges} cap. Stopping. ***")
                break
            try:
                merged_id = execute_merge(
                    match=result,
                    record_a=rec_a,
                    record_b=rec_b,
                    object_type="contacts",
                    run_id=run_id,
                    dry_run=dry_run,
                )
            except ValueError as e:
                print(f"  SKIP (already merged/deleted): {e}")
                discard_count += 1
                continue
            auto_count += 1
            if merge_counter is not None:
                merge_counter[0] += 1
            status = f"[{'DRY' if dry_run else 'MERGED→' + str(merged_id)}]"
            print(f"  AUTO {status} score={result.score:.2f} | {result.match_reason[:80]}")

        elif result.action == "REVIEW":
            review_count += 1
            _add_to_review_queue(
                object_type="contact",
                id_a=id_a, id_b=id_b,
                rec_a=rec_a, rec_b=rec_b,
                result=result,
            )

        else:
            discard_count += 1

    with get_session() as session:
        run_record = session.get(AgentRun, run_id)
        if run_record:
            run_record.contacts_merged = auto_count
            run_record.contacts_flagged = review_count

    print(f"\nContacts summary:")
    print(f"  AUTO_MERGE: {auto_count} ({'proposed' if dry_run else 'executed'})")
    print(f"  REVIEW:     {review_count} (added to review queue)")
    print(f"  DISCARD:    {discard_count}")


def _run_companies(run_id: str, dry_run: bool, limit: int = None, min_score: float = None,
                   max_merges: int = None, merge_counter: list = None):
    print("\n--- Fetching companies ---")
    companies = list(fetch_companies(limit=limit))
    print(f"  Fetched {len(companies)} companies")

    with get_session() as session:
        run_record = session.get(AgentRun, run_id)
        if run_record:
            run_record.companies_fetched = len(companies)
            if companies:
                run_record.last_company_timestamp = companies[-1]["hs_lastmodifieddate"]

    id_map = {c["id"]: c for c in companies}

    print("--- Generating candidate pairs ---")
    pairs = generate_company_pairs(companies)
    print(f"  {len(pairs)} candidate pairs to score")

    auto_count = 0
    review_count = 0
    discard_count = 0

    known_non_dupes = _load_known_non_dupes("company")

    print("--- Scoring pairs ---")
    for id_a, id_b in pairs:
        rec_a = id_map.get(id_a)
        rec_b = id_map.get(id_b)
        if not rec_a or not rec_b:
            continue

        pair_key = (min(id_a, id_b), max(id_a, id_b))
        if pair_key in known_non_dupes:
            continue

        result = score_companies(rec_a, rec_b)

        if min_score and result.score < min_score:
            discard_count += 1
            continue

        if result.action == "AUTO_MERGE":
            if max_merges and merge_counter and merge_counter[0] >= max_merges:
                print(f"  *** Reached max_merges={max_merges} cap. Stopping. ***")
                break
            try:
                merged_id = execute_merge(
                    match=result,
                    record_a=rec_a,
                    record_b=rec_b,
                    object_type="companies",
                    run_id=run_id,
                    dry_run=dry_run,
                )
            except ValueError as e:
                print(f"  SKIP (already merged/deleted): {e}")
                discard_count += 1
                continue
            auto_count += 1
            if merge_counter is not None:
                merge_counter[0] += 1
            status = f"[{'DRY' if dry_run else 'MERGED→' + str(merged_id)}]"
            print(f"  AUTO {status} score={result.score:.2f} | {result.match_reason[:80]}")

        elif result.action == "REVIEW":
            review_count += 1
            _add_to_review_queue(
                object_type="company",
                id_a=id_a, id_b=id_b,
                rec_a=rec_a, rec_b=rec_b,
                result=result,
            )

        else:
            discard_count += 1

    with get_session() as session:
        run_record = session.get(AgentRun, run_id)
        if run_record:
            run_record.companies_merged = auto_count
            run_record.companies_flagged = review_count

    print(f"\nCompanies summary:")
    print(f"  AUTO_MERGE: {auto_count} ({'proposed' if dry_run else 'executed'})")
    print(f"  REVIEW:     {review_count} (added to review queue)")
    print(f"  DISCARD:    {discard_count}")


def _contact_summary(record: dict) -> dict:
    return {
        "name": f"{record.get('firstname', '')} {record.get('lastname', '')}".strip(),
        "email": record.get("email", ""),
        "phone": record.get("phone", "") or record.get("mobilephone", ""),
        "company": record.get("company", ""),
        "linkedin": record.get("hs_linkedin_url", "") or record.get("lemlistlinkedinurl", ""),
        "createdate": record.get("createdate", ""),
        "deals": record.get("num_associated_deals", 0),
        "notes": record.get("num_notes", 0),
    }


def _company_summary(record: dict) -> dict:
    return {
        "name": record.get("name", ""),
        "domain": record.get("domain", ""),
        "website": record.get("website", ""),
        "linkedin": record.get("linkedin_company_page", ""),
        "contacts": record.get("num_associated_contacts", 0),
        "deals": record.get("num_associated_deals", 0),
        "createdate": record.get("createdate", ""),
    }


def _add_to_review_queue(
    object_type: str, id_a: str, id_b: str,
    rec_a: dict, rec_b: dict, result,
):
    from pipeline.scorer import MatchResult
    summary_fn = _contact_summary if object_type == "contact" else _company_summary
    with get_session() as session:
        # Don't add if already in queue
        from sqlalchemy import and_
        existing = session.query(ReviewQueue).filter(
            and_(
                ReviewQueue.object_type == object_type,
                ReviewQueue.status == "PENDING",
                ReviewQueue.id_a.in_([id_a, id_b]),
                ReviewQueue.id_b.in_([id_a, id_b]),
            )
        ).first()
        if existing:
            return

        item = ReviewQueue(
            object_type=object_type,
            id_a=id_a,
            id_b=id_b,
            score=result.score,
            match_signals=json.dumps(result.match_signals),
            match_reason=result.match_reason,
            record_a_summary=json.dumps(summary_fn(rec_a)),
            record_b_summary=json.dumps(summary_fn(rec_b)),
            status="PENDING",
        )
        session.add(item)


def _load_known_non_dupes(object_type: str):
    """Load suppression pairs from DB."""
    from db.models import KnownNonDuplicate
    known = set()
    try:
        with get_session() as session:
            rows = session.query(KnownNonDuplicate).filter(
                KnownNonDuplicate.object_type == object_type
            ).all()
            for row in rows:
                known.add((min(row.id_a, row.id_b), max(row.id_a, row.id_b)))
    except Exception:
        pass
    return known


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HubSpot bulk deduplication")
    parser.add_argument("--live", action="store_true", help="Execute real merges (default: dry run)")
    parser.add_argument("--limit", type=int, default=None, help="Max records to fetch (for testing)")
    parser.add_argument("--contacts-only", action="store_true", help="Only process contacts")
    parser.add_argument("--companies-only", action="store_true", help="Only process companies")
    parser.add_argument("--min-score", type=float, default=None,
                        help="Only merge pairs at or above this score (e.g. 1.0, 0.98, 0.92)")
    parser.add_argument("--max-merges", type=int, default=None,
                        help="Hard cap on total merges executed — stops after N regardless of remaining pairs")
    args = parser.parse_args()
    run(
        dry_run=not args.live,
        limit=args.limit,
        contacts_only=args.contacts_only,
        companies_only=args.companies_only,
        min_score=args.min_score,
        max_merges=args.max_merges,
    )
