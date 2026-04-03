# ============================================================
# MIXED FILE — Orchestration logic is universal; merge/fetch calls are HubSpot-specific
# What to change: execute_merge(), fetch_contacts(), fetch_companies() imports if swapping CRM
# What to preserve: The checkpoint pattern, scoring thresholds, review queue logic
# ============================================================

"""
Incremental deduplication agent (always-on).

Fetches only records modified since the last successful run checkpoint,
runs the full dedup pipeline, and updates the checkpoint on completion.

Usage:
    python agents/incremental_dedup_agent.py              # dry-run
    python agents/incremental_dedup_agent.py --live       # execute real merges
"""
import argparse
import json
import sys
import uuid
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_session
from db.models import AgentRun, ReviewQueue, KnownNonDuplicate
from pipeline.fetcher import fetch_contacts, fetch_companies
from pipeline.blocker import generate_contact_pairs, generate_company_pairs
from pipeline.scorer import score_contacts, score_companies
from pipeline.merger import execute_merge
from sqlalchemy import and_


def _get_last_checkpoint() -> tuple:
    """Return (last_contact_ts, last_company_ts) from the most recent completed run."""
    last_contact_ts = None
    last_company_ts = None
    try:
        with get_session() as session:
            last_run = (
                session.query(AgentRun)
                .filter(
                    AgentRun.completed_at.isnot(None),
                    AgentRun.run_type == "incremental",
                )
                .order_by(AgentRun.completed_at.desc())
                .first()
            )
            if last_run:
                last_contact_ts = last_run.last_contact_timestamp
                last_company_ts = last_run.last_company_timestamp
    except Exception as e:
        print(f"  Warning: could not read checkpoint: {e}")
    return last_contact_ts, last_company_ts


def run(dry_run: bool = True):
    init_db()
    run_id = str(uuid.uuid4())

    last_contact_ts, last_company_ts = _get_last_checkpoint()

    print(f"\n{'='*60}")
    print(f"HubSpot Dedup — Incremental Run")
    print(f"Mode: {'DRY RUN' if dry_run else '*** LIVE ***'}")
    print(f"Run ID: {run_id}")
    print(f"Contact checkpoint: {last_contact_ts or 'full scan'}")
    print(f"Company checkpoint: {last_company_ts or 'full scan'}")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}\n")

    with get_session() as session:
        agent_run = AgentRun(
            run_id=run_id,
            run_type="incremental",
            started_at=datetime.now(timezone.utc),
            dry_run=dry_run,
        )
        session.add(agent_run)

    # --- Contacts ---
    print("--- Fetching modified contacts ---")
    contacts = list(fetch_contacts(since_timestamp=last_contact_ts))
    print(f"  {len(contacts)} contacts modified since checkpoint")

    new_contact_ts = None
    if contacts:
        new_contact_ts = max(c["lastmodifieddate"] for c in contacts if c["lastmodifieddate"])

    contact_merged, contact_flagged = _process_contacts(contacts, run_id, dry_run)

    # --- Companies ---
    print("\n--- Fetching modified companies ---")
    companies = list(fetch_companies(since_timestamp=last_company_ts))
    print(f"  {len(companies)} companies modified since checkpoint")

    new_company_ts = None
    if companies:
        new_company_ts = max(c["hs_lastmodifieddate"] for c in companies if c["hs_lastmodifieddate"])

    company_merged, company_flagged = _process_companies(companies, run_id, dry_run)

    # --- Expire stale review queue items ---
    _expire_stale_reviews()

    # --- AI review: process any pending review queue items ---
    if not dry_run:
        print("\n--- Running AI review on pending queue items ---")
        try:
            from review.ai_review import run as ai_review_run
            ai_review_run(live=True)
        except Exception as e:
            print(f"  Warning: AI review failed: {e}")
    else:
        print("\n--- Skipping AI review (dry-run mode) ---")

    # --- Update run record ---
    with get_session() as session:
        run_record = session.get(AgentRun, run_id)
        if run_record:
            run_record.completed_at = datetime.now(timezone.utc)
            run_record.contacts_fetched = len(contacts)
            run_record.contacts_merged = contact_merged
            run_record.contacts_flagged = contact_flagged
            run_record.companies_fetched = len(companies)
            run_record.companies_merged = company_merged
            run_record.companies_flagged = company_flagged
            # Only advance checkpoint if run succeeded
            if new_contact_ts:
                run_record.last_contact_timestamp = new_contact_ts
            if new_company_ts:
                run_record.last_company_timestamp = new_company_ts

    print(f"\n{'='*60}")
    print(f"Incremental run complete: {run_id}")
    print(f"  Contacts: {contact_merged} merged, {contact_flagged} flagged")
    print(f"  Companies: {company_merged} merged, {company_flagged} flagged")
    print(f"{'='*60}")


def _process_contacts(contacts: list, run_id: str, dry_run: bool) -> tuple:
    if not contacts:
        return 0, 0

    id_map = {c["id"]: c for c in contacts}
    pairs = generate_contact_pairs(contacts)
    print(f"  {len(pairs)} candidate pairs")

    known = _load_known_non_dupes("contact")
    merged = 0
    flagged = 0

    for id_a, id_b in pairs:
        rec_a = id_map.get(id_a)
        rec_b = id_map.get(id_b)
        if not rec_a or not rec_b:
            continue
        if (min(id_a, id_b), max(id_a, id_b)) in known:
            continue

        result = score_contacts(rec_a, rec_b)

        if result.action == "AUTO_MERGE":
            try:
                execute_merge(
                    match=result, record_a=rec_a, record_b=rec_b,
                    object_type="contacts", run_id=run_id, dry_run=dry_run,
                )
                merged += 1
                print(f"  AUTO {'(dry)' if dry_run else '(live)'} score={result.score:.2f} | {result.match_reason[:70]}")
            except ValueError as exc:
                print(f"  SKIP contact pair {id_a}+{id_b}: {exc}")

        elif result.action == "REVIEW":
            flagged += 1
            _add_to_review_queue("contact", id_a, id_b, rec_a, rec_b, result)

    return merged, flagged


def _process_companies(companies: list, run_id: str, dry_run: bool) -> tuple:
    if not companies:
        return 0, 0

    id_map = {c["id"]: c for c in companies}
    pairs = generate_company_pairs(companies)
    print(f"  {len(pairs)} candidate pairs")

    known = _load_known_non_dupes("company")
    merged = 0
    flagged = 0

    for id_a, id_b in pairs:
        rec_a = id_map.get(id_a)
        rec_b = id_map.get(id_b)
        if not rec_a or not rec_b:
            continue
        if (min(id_a, id_b), max(id_a, id_b)) in known:
            continue

        result = score_companies(rec_a, rec_b)

        if result.action == "AUTO_MERGE":
            try:
                execute_merge(
                    match=result, record_a=rec_a, record_b=rec_b,
                    object_type="companies", run_id=run_id, dry_run=dry_run,
                )
                merged += 1
                print(f"  AUTO {'(dry)' if dry_run else '(live)'} score={result.score:.2f} | {result.match_reason[:70]}")
            except ValueError as exc:
                print(f"  SKIP company pair {id_a}+{id_b}: {exc}")

        elif result.action == "REVIEW":
            flagged += 1
            _add_to_review_queue("company", id_a, id_b, rec_a, rec_b, result)

    return merged, flagged


def _add_to_review_queue(
    object_type: str, id_a: str, id_b: str, rec_a: dict, rec_b: dict, result
):
    def _contact_summary(r):
        return {
            "name": f"{r.get('firstname', '')} {r.get('lastname', '')}".strip(),
            "email": r.get("email", ""),
            "phone": r.get("phone", "") or r.get("mobilephone", ""),
            "company": r.get("company", ""),
            "linkedin": r.get("hs_linkedin_url", "") or r.get("lemlistlinkedinurl", ""),
            "deals": r.get("num_associated_deals", 0),
        }

    def _company_summary(r):
        return {
            "name": r.get("name", ""),
            "domain": r.get("domain", ""),
            "linkedin": r.get("linkedin_company_page", ""),
            "contacts": r.get("num_associated_contacts", 0),
            "deals": r.get("num_associated_deals", 0),
        }

    summary_fn = _contact_summary if object_type == "contact" else _company_summary

    with get_session() as session:
        existing = session.query(ReviewQueue).filter(
            and_(
                ReviewQueue.object_type == object_type,
                ReviewQueue.status.in_(["PENDING", "APPROVED", "REJECTED"]),
                ReviewQueue.id_a.in_([id_a, id_b]),
                ReviewQueue.id_b.in_([id_a, id_b]),
            )
        ).first()
        if existing:
            return

        item = ReviewQueue(
            object_type=object_type,
            id_a=id_a, id_b=id_b,
            score=result.score,
            match_signals=json.dumps(result.match_signals),
            match_reason=result.match_reason,
            record_a_summary=json.dumps(summary_fn(rec_a)),
            record_b_summary=json.dumps(summary_fn(rec_b)),
            status="PENDING",
        )
        session.add(item)


def _load_known_non_dupes(object_type: str) -> set:
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


def _expire_stale_reviews():
    """Auto-expire PENDING review items older than 90 days."""
    from datetime import timedelta
    from db.models import KnownNonDuplicate
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    try:
        with get_session() as session:
            stale = session.query(ReviewQueue).filter(
                ReviewQueue.status == "PENDING",
                ReviewQueue.created_at < cutoff,
            ).all()
            for item in stale:
                item.status = "EXPIRED"
                # Add to known_non_duplicates so they're not re-flagged
                nondupe = KnownNonDuplicate(
                    object_type=item.object_type,
                    id_a=item.id_a,
                    id_b=item.id_b,
                )
                session.add(nondupe)
            if stale:
                print(f"  Expired {len(stale)} stale review items (>90 days)")
    except Exception as e:
        print(f"  Warning: could not expire stale reviews: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HubSpot incremental deduplication")
    parser.add_argument("--live", action="store_true", help="Execute real merges")
    args = parser.parse_args()
    run(dry_run=not args.live)
