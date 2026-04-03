# ============================================================
# MIXED FILE — AI reasoning and queue logic are universal; record fetch calls are HubSpot-specific
# What to change: _fetch_record() if swapping CRM
# What to preserve: The 3-stage decision pipeline (fast rules → web research → Claude)
# ============================================================

"""
AI-powered review queue processor.

Decision pipeline for each pending pair:
  1. Fast CRM rules  — high-confidence decisions from data alone (no API calls)
  2. Web research    — follow redirects, scrape LinkedIn/phone from homepages
  3. Claude reasoning — fallback for ambiguous cases

Outcomes:
  YES    → merge (approve)
  NO     → reject (add to known non-duplicates)
  UNSURE → leave in queue for manual review

Usage:
    python review/ai_review.py                    # dry-run: show decisions, don't act
    python review/ai_review.py --live             # execute merges + rejections
    python review/ai_review.py --live --limit 50  # process 50 items
    python review/ai_review.py --type company     # companies only
    python review/ai_review.py --type contact     # contacts only
"""
import argparse
import os
import sys
import requests
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from db.database import init_db, get_session
from db.models import ReviewQueue, KnownNonDuplicate
from pipeline.scorer import MatchResult
from pipeline.merger import execute_merge
from pipeline.fetcher import _normalize_contact, _normalize_company
from pipeline.web_enricher import check_same_company
from config.settings import (
    HUBSPOT_ACCESS_TOKEN, HUBSPOT_API_BASE,
    CONTACT_FETCH_PROPERTIES, COMPANY_FETCH_PROPERTIES,
    ANTHROPIC_API_KEY,
)


# ---------------------------------------------------------------------------
# HubSpot fetch
# ---------------------------------------------------------------------------

def _fetch_record(object_type: str, oid: str) -> dict:
    hs_type = "contacts" if object_type == "contact" else "companies"
    props = CONTACT_FETCH_PROPERTIES if object_type == "contact" else COMPANY_FETCH_PROPERTIES
    normalizer = _normalize_contact if object_type == "contact" else _normalize_company
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/{hs_type}/{oid}"
    resp = requests.get(
        url, params={"properties": ",".join(props)},
        headers={"Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    return normalizer(resp.json())


# ---------------------------------------------------------------------------
# Record summaries for prompts
# ---------------------------------------------------------------------------

def _contact_summary(r: dict) -> str:
    return "\n".join([
        f"Name: {r.get('firstname', '')} {r.get('lastname', '')}".strip(),
        f"Email: {r.get('email') or '(none)'}",
        f"Company: {r.get('company') or '(none)'}",
        f"Phone: {r.get('phone') or r.get('mobilephone') or '(none)'}",
        f"LinkedIn: {r.get('hs_linkedin_url') or r.get('lemlistlinkedinurl') or '(none)'}",
        f"Job title: {r.get('jobtitle') or '(none)'}",
        f"Created: {(r.get('createdate') or '')[:10]}",
        f"Deals: {r.get('num_associated_deals', 0)} | Notes: {r.get('num_notes', 0)} | Activity: {bool(r.get('hs_last_sales_activity_timestamp'))}",
    ])


def _company_summary(r: dict) -> str:
    return "\n".join([
        f"Name: {r.get('name') or '(none)'}",
        f"Domain: {r.get('domain') or '(none)'}",
        f"Website: {r.get('website') or '(none)'}",
        f"LinkedIn: {r.get('linkedin_company_page') or r.get('lemlistprofileurl') or '(none)'}",
        f"Phone: {r.get('phone') or '(none)'}",
        f"Created: {(r.get('createdate') or '')[:10]}",
        f"Contacts: {r.get('num_associated_contacts', 0)} | Deals: {r.get('num_associated_deals', 0)}",
    ])


def _summary(object_type: str, r: dict) -> str:
    return _contact_summary(r) if object_type == "contact" else _company_summary(r)


# ---------------------------------------------------------------------------
# Step 1: Fast CRM rules (no external calls)
# ---------------------------------------------------------------------------

def _fast_check(object_type: str, rec_a: dict, rec_b: dict):
    """
    Return (decision, reason) for high-confidence cases determinable from CRM
    data alone, or (None, None) to fall through to web research + Claude.
    """
    if object_type == "contact":
        name_a = f"{rec_a.get('firstname','')} {rec_a.get('lastname','')}".strip().lower()
        name_b = f"{rec_b.get('firstname','')} {rec_b.get('lastname','')}".strip().lower()
        co_a = (rec_a.get('company') or '').strip().lower()
        co_b = (rec_b.get('company') or '').strip().lower()

        # Identical name + identical company → merge
        if name_a and name_a == name_b and co_a and co_a == co_b:
            return "YES", f"Identical name '{name_a}' and company '{co_a}'"

        # Identical name, one record is a bare shell (no email, no company, no activity)
        def is_shell(r):
            return (not r.get('email') and not r.get('company')
                    and not r.get('phone') and not r.get('mobilephone')
                    and r.get('num_associated_deals', 0) == 0
                    and r.get('num_notes', 0) == 0)

        if name_a and name_a == name_b and (is_shell(rec_a) or is_shell(rec_b)):
            return "YES", f"Identical name '{name_a}', one record is an empty shell"

    elif object_type == "company":
        # One record has no domain at all → name-only match, check names
        name_a = (rec_a.get('name') or '').strip().lower()
        name_b = (rec_b.get('name') or '').strip().lower()
        dom_a = (rec_a.get('domain') or '').strip()
        dom_b = (rec_b.get('domain') or '').strip()

        if name_a and name_a == name_b and not dom_a and not dom_b:
            return "YES", f"Identical company name '{name_a}', neither has a domain"

        if name_a and name_a == name_b and (not dom_a or not dom_b):
            return "YES", f"Identical company name '{name_a}', one record has no domain"

    return None, None


# ---------------------------------------------------------------------------
# Step 2: Web research (follow redirects, scrape signals)
# ---------------------------------------------------------------------------

def _web_check(rec_a: dict, rec_b: dict):
    """
    Returns (decision, reason) if web evidence is HIGH confidence,
    or (None, web_evidence_text) to pass context to Claude.
    """
    domain_a = (rec_a.get('domain') or rec_a.get('website') or '').strip()
    domain_b = (rec_b.get('domain') or rec_b.get('website') or '').strip()

    if not domain_a or not domain_b:
        return None, "(one or both records have no domain — skipping web check)"

    result = check_same_company(domain_a, domain_b)
    verdict = result["verdict"]
    confidence = result["confidence"]
    evidence_text = "Web research findings:\n" + "\n".join(f"  - {e}" for e in result["evidence"])

    if confidence == "HIGH":
        if verdict == "SAME":
            return "YES", f"Web research (HIGH confidence): {result['evidence'][0]}"
        elif verdict == "DIFFERENT":
            return "NO", f"Web research (HIGH confidence): {result['evidence'][0]}"

    return None, evidence_text


# ---------------------------------------------------------------------------
# Step 3: Claude reasoning (fallback)
# ---------------------------------------------------------------------------

CONTACT_PROMPT = """You are helping deduplicate a B2B CRM. Decide if these two contacts are the SAME PERSON.

Record A:
{summary_a}

Record B:
{summary_b}

Already detected: {match_reason}

Rules:
- Identical name + same company → YES
- Identical name, one record nearly empty → YES
- Clearly different first names (e.g. Maria vs Calvin) → NO even if last name matches
- Same name, different companies: could be job change → UNSURE unless other signals confirm

Reply with exactly YES, NO, or UNSURE on the first line, then a one-sentence reason."""

COMPANY_PROMPT = """You are helping deduplicate a B2B CRM. Decide if these two companies are the SAME COMPANY.

Record A:
{summary_a}

Record B:
{summary_b}

Already detected: {match_reason}

{web_evidence}

Rules:
- Web research HIGH confidence result overrides everything else
- Same redirect destination or same LinkedIn page → YES
- Different LinkedIn pages → NO
- Similar names + different domains, no web evidence → lean NO unless name is identical
- One record has no domain + identical name → YES
- Subsidiary of same parent ≠ duplicate → NO
- Rebranded company (same entity, new name) = duplicate → YES

Reply with exactly YES, NO, or UNSURE on the first line, then a one-sentence reason."""


def _ask_claude(client: anthropic.Anthropic, prompt: str) -> tuple:
    for attempt in range(5):
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=120,
                messages=[{"role": "user", "content": prompt}],
            )
            text = msg.content[0].text.strip()
            lines = text.strip().splitlines()
            decision = lines[0].strip().upper()
            reason = lines[1].strip() if len(lines) > 1 else text
            if decision not in ("YES", "NO", "UNSURE"):
                decision = "UNSURE"
                reason = text
            return decision, reason
        except anthropic.RateLimitError:
            wait = 10 * (2 ** attempt)  # 10s, 20s, 40s, 80s, 160s
            print(f"  [rate limit] waiting {wait}s before retry {attempt+1}/5...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:  # overloaded
                wait = 15 * (2 ** attempt)
                print(f"  [API overloaded] waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    return "UNSURE", "Rate limit retries exhausted"


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def _decide(object_type: str, rec_a: dict, rec_b: dict,
            match_reason: str, client: anthropic.Anthropic) -> tuple:
    """
    Run the full decision pipeline. Returns (decision, reason, method).
    method is one of: "fast_rule", "web_high", "web+claude", "claude"
    """
    # Step 1: fast CRM rules
    decision, reason = _fast_check(object_type, rec_a, rec_b)
    if decision:
        return decision, reason, "fast_rule"

    # Step 2: web research (companies only)
    web_evidence_text = ""
    if object_type == "company":
        decision, web_result = _web_check(rec_a, rec_b)
        if decision:
            return decision, web_result, "web_high"
        web_evidence_text = web_result  # pass to Claude as context

    # Step 3: Claude reasoning
    sum_a = _summary(object_type, rec_a)
    sum_b = _summary(object_type, rec_b)
    if object_type == "contact":
        prompt = CONTACT_PROMPT.format(
            summary_a=sum_a, summary_b=sum_b, match_reason=match_reason,
        )
        method = "claude"
    else:
        prompt = COMPANY_PROMPT.format(
            summary_a=sum_a, summary_b=sum_b,
            match_reason=match_reason,
            web_evidence=web_evidence_text,
        )
        method = "web+claude" if web_evidence_text else "claude"

    decision, reason = _ask_claude(client, prompt)
    return decision, reason, method


def run(live: bool = False, limit: int = None, object_type_filter: str = None):
    init_db()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    with get_session() as session:
        query = session.query(ReviewQueue).filter(ReviewQueue.status == "PENDING")
        if object_type_filter:
            query = query.filter(ReviewQueue.object_type == object_type_filter)
        query = query.order_by(ReviewQueue.score.desc())
        if limit:
            query = query.limit(limit)
        items = query.all()
        item_data = [
            {"id": item.id, "object_type": item.object_type,
             "id_a": item.id_a, "id_b": item.id_b,
             "score": item.score, "match_reason": item.match_reason}
            for item in items
        ]

    # Pre-pass: bulk reject companies where both records have different domains
    # (different domain = different company, no AI needed)
    bulk_rejected = 0
    if not object_type_filter or object_type_filter == "company":
        with get_session() as session:
            company_items = session.query(ReviewQueue).filter(
                ReviewQueue.status == "PENDING",
                ReviewQueue.object_type == "company",
            ).all()
            for item in company_items:
                try:
                    import json as _json
                    a = _json.loads(item.record_a_summary) if isinstance(item.record_a_summary, str) else (item.record_a_summary or {})
                    b = _json.loads(item.record_b_summary) if isinstance(item.record_b_summary, str) else (item.record_b_summary or {})
                    dom_a = (a.get("domain") or "").strip().lower()
                    dom_b = (b.get("domain") or "").strip().lower()
                    if dom_a and dom_b and dom_a != dom_b:
                        item.status = "REJECTED"
                        item.notes = f"Auto-rejected: different domains ({dom_a} vs {dom_b})"
                        item.reviewed_at = datetime.now(timezone.utc)
                        # Suppress future pairing
                        exists = session.query(KnownNonDuplicate).filter(
                            KnownNonDuplicate.object_type == "company",
                            KnownNonDuplicate.id_a.in_([item.id_a, item.id_b]),
                            KnownNonDuplicate.id_b.in_([item.id_a, item.id_b]),
                        ).first()
                        if not exists:
                            session.add(KnownNonDuplicate(object_type="company", id_a=item.id_a, id_b=item.id_b))
                        bulk_rejected += 1
                except Exception:
                    pass
        if bulk_rejected:
            print(f"Pre-pass: bulk rejected {bulk_rejected} company pairs (different domains)")
        # Reload pending items after pre-pass
        with get_session() as session:
            query = session.query(ReviewQueue).filter(ReviewQueue.status == "PENDING")
            if object_type_filter:
                query = query.filter(ReviewQueue.object_type == object_type_filter)
            query = query.order_by(ReviewQueue.score.desc())
            if limit:
                query = query.limit(limit)
            items = query.all()
            item_data = [
                {"id": item.id, "object_type": item.object_type,
                 "id_a": item.id_a, "id_b": item.id_b,
                 "score": item.score, "match_reason": item.match_reason}
                for item in items
            ]

    total = len(item_data)
    print(f"\n{'='*60}")
    print(f"AI Review — {total} pending items")
    print(f"Mode: {'DRY RUN (no changes)' if not live else '*** LIVE ***'}")
    print(f"Pipeline: fast rules → web research → Claude reasoning")
    print(f"{'='*60}\n")

    counts = {"YES": 0, "NO": 0, "UNSURE": 0, "ERROR": 0}
    methods = {"fast_rule": 0, "web_high": 0, "web+claude": 0, "claude": 0}

    for i, item in enumerate(item_data, 1):
        try:
            rec_a = _fetch_record(item["object_type"], item["id_a"])
            rec_b = _fetch_record(item["object_type"], item["id_b"])

            # Both IDs resolve to same record (already merged) — skip
            if rec_a["id"] == rec_b["id"]:
                _update_item(item["id"], "REJECTED", "Already merged — both IDs resolve to same record")
                print(f"[{i}/{total}] SKIP — already merged into same record ({rec_a['id']})\n")
                continue

            decision, reason, method = _decide(
                item["object_type"], rec_a, rec_b,
                item["match_reason"], client,
            )

            counts[decision] = counts.get(decision, 0) + 1
            methods[method] = methods.get(method, 0) + 1

            sum_a = _summary(item["object_type"], rec_a).splitlines()[0]
            sum_b = _summary(item["object_type"], rec_b).splitlines()[0]
            print(f"[{i}/{total}] {item['object_type']} score={item['score']:.2f} → {decision} [{method}]")
            print(f"  A: {sum_a}")
            print(f"  B: {sum_b}")
            print(f"  {reason}")

            if live:
                if decision == "YES":
                    hs_type = "contacts" if item["object_type"] == "contact" else "companies"
                    match = MatchResult(
                        id_a=item["id_a"], id_b=item["id_b"],
                        score=item["score"], action="AUTO_MERGE",
                        match_signals=[], match_reason=f"AI review [{method}]: {reason}",
                    )
                    execute_merge(match=match, record_a=rec_a, record_b=rec_b,
                                  object_type=hs_type,
                                  run_id=f"ai-review-{item['id']}", dry_run=False)
                    _update_item(item["id"], "APPROVED", reason)
                    print(f"  → MERGED")
                elif decision == "NO":
                    _update_item(item["id"], "REJECTED", reason)
                    _suppress(item["object_type"], item["id_a"], item["id_b"])
                    print(f"  → REJECTED + suppressed")
                else:
                    print(f"  → LEFT FOR MANUAL REVIEW")
            print()
            time.sleep(1.0)

        except Exception as e:
            counts["ERROR"] = counts.get("ERROR", 0) + 1
            print(f"[{i}/{total}] ERROR on item {item['id']}: {e}\n")

    print(f"{'='*60}")
    print(f"Results {'(DRY RUN)' if not live else '(LIVE)'}:")
    print(f"  YES (merge):   {counts['YES']}")
    print(f"  NO (reject):   {counts['NO']}")
    print(f"  UNSURE:        {counts['UNSURE']}  ← these stay for manual review")
    print(f"  Errors:        {counts.get('ERROR', 0)}")
    print(f"\nDecision methods:")
    print(f"  Fast CRM rule: {methods['fast_rule']}")
    print(f"  Web (high):    {methods['web_high']}")
    print(f"  Web + Claude:  {methods['web+claude']}")
    print(f"  Claude only:   {methods['claude']}")
    if not live:
        print(f"\nRun with --live to execute these decisions.")
    print(f"{'='*60}")


def _update_item(item_id: int, status: str, notes: str):
    with get_session() as session:
        item = session.get(ReviewQueue, item_id)
        if item:
            item.status = status
            item.reviewed_at = datetime.now(timezone.utc)
            item.notes = notes


def _suppress(object_type: str, id_a: str, id_b: str):
    with get_session() as session:
        exists = session.query(KnownNonDuplicate).filter(
            KnownNonDuplicate.object_type == object_type,
            KnownNonDuplicate.id_a.in_([id_a, id_b]),
            KnownNonDuplicate.id_b.in_([id_a, id_b]),
        ).first()
        if not exists:
            session.add(KnownNonDuplicate(object_type=object_type, id_a=id_a, id_b=id_b))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI-powered review queue processor")
    parser.add_argument("--live", action="store_true", help="Execute merge/reject decisions")
    parser.add_argument("--limit", type=int, default=None, help="Max items to process")
    parser.add_argument("--type", dest="object_type", choices=["contact", "company"],
                        default=None, help="Filter to contacts or companies only")
    args = parser.parse_args()
    run(live=args.live, limit=args.limit, object_type_filter=args.object_type)
