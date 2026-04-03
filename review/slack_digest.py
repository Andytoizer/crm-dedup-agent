# ============================================================
# MIXED FILE — Digest formatting is universal; posting uses Slack API
# What to change: The slack_sdk calls if using a different notification channel
# What to preserve: The queue query and item formatting logic
# ============================================================

"""
Daily Slack digest of pending review queue items.

Sends a formatted message to the configured Slack channel showing
the top uncertain duplicate pairs waiting for human review.

Usage:
    python review/slack_digest.py
"""
import json
import os
import sys
import requests
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import init_db, get_session
from db.models import ReviewQueue, KnownNonDuplicate
from config.settings import HUBSPOT_ACCESS_TOKEN, SLACK_REVIEW_CHANNEL

HUBSPOT_BASE_URL = "https://app.hubspot.com/contacts"
MAX_DIGEST_ITEMS = 20  # Max pairs shown per digest


def _get_pending_items(limit: int = MAX_DIGEST_ITEMS) -> list:
    """Fetch highest-scoring PENDING review items."""
    with get_session() as session:
        items = (
            session.query(ReviewQueue)
            .filter(ReviewQueue.status == "PENDING")
            .order_by(ReviewQueue.score.desc())
            .limit(limit)
            .all()
        )
        # Detach from session by converting to dicts
        return [
            {
                "id": item.id,
                "object_type": item.object_type,
                "id_a": item.id_a,
                "id_b": item.id_b,
                "score": item.score,
                "match_reason": item.match_reason,
                "record_a": json.loads(item.record_a_summary or "{}"),
                "record_b": json.loads(item.record_b_summary or "{}"),
                "created_at": item.created_at.isoformat() if item.created_at else "",
            }
            for item in items
        ]


def _get_pending_count() -> int:
    with get_session() as session:
        return session.query(ReviewQueue).filter(ReviewQueue.status == "PENDING").count()


def _format_contact_row(record: dict, label: str) -> str:
    name = record.get("name", "—")
    email = record.get("email", "—")
    company = record.get("company", "—")
    phone = record.get("phone", "—") or "—"
    deals = record.get("deals", 0)
    notes = record.get("notes", 0)
    return (
        f"  *{label}:* {name} | {email} | {company}\n"
        f"  Phone: {phone} | Deals: {deals} | Notes: {notes}"
    )


def _format_company_row(record: dict, label: str) -> str:
    name = record.get("name", "—")
    domain = record.get("domain", "—")
    linkedin = record.get("linkedin", "—") or "—"
    contacts = record.get("contacts", 0)
    deals = record.get("deals", 0)
    return (
        f"  *{label}:* {name} | {domain}\n"
        f"  LinkedIn: {linkedin} | Contacts: {contacts} | Deals: {deals}"
    )


def _format_item(item: dict, index: int) -> str:
    pct = int(item["score"] * 100)
    header = f"*{index}. {item['object_type'].title()} pair — {pct}% match*\n"
    header += f"  _{item['match_reason']}_\n"

    if item["object_type"] == "contact":
        row_a = _format_contact_row(item["record_a"], "A")
        row_b = _format_contact_row(item["record_b"], "B")
    else:
        row_a = _format_company_row(item["record_a"], "A")
        row_b = _format_company_row(item["record_b"], "B")

    approve_cmd = f"`python review/queue_action.py approve {item['id']}`"
    reject_cmd = f"`python review/queue_action.py reject {item['id']}`"

    return f"{header}{row_a}\n{row_b}\n  {approve_cmd}  |  {reject_cmd}"



def _post_message(text: str, channel: str, slack_token: str) -> dict | None:
    """Post a single message and return the API response dict."""
    resp = requests.post(
        "https://slack.com/api/chat.postMessage",
        json={"channel": channel, "text": text, "mrkdwn": True},
        headers={"Authorization": f"Bearer {slack_token}"},
        timeout=15,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"  Slack API error: {data.get('error')}")
        return None
    return data


def send_to_slack(items: list, total_pending: int, channel: str = None) -> bool:
    """
    Send header + one message per pair so users can emoji-react to each.
    Falls back to printing to stdout.
    """
    target_channel = channel or SLACK_REVIEW_CHANNEL
    slack_token = os.getenv("SLACK_BOT_TOKEN", "")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    header = (
        f":mag: *HubSpot Dedup Review Digest — {now}*\n"
        f"*{total_pending} pairs* pending review. Showing top {len(items)}. "
        f"React :white_check_mark: to approve or :x: to reject each pair below."
    )

    if slack_token:
        if not _post_message(header, target_channel, slack_token):
            return False
        for i, item in enumerate(items, start=1):
            msg = _format_item(item, i)
            if not _post_message(msg, target_channel, slack_token):
                return False
        print(f"  Sent {len(items)} messages to Slack channel {target_channel}")
        return True

    # Fallback: print to stdout
    print("\n" + "=" * 60)
    print("SLACK DIGEST (stdout fallback — configure SLACK_BOT_TOKEN to send directly)")
    print("=" * 60)
    print(header)
    for i, item in enumerate(items, start=1):
        print(_format_item(item, i))
    return False


def run(channel: str = None):
    init_db()
    total_pending = _get_pending_count()

    if total_pending == 0:
        print("No pending review items. Nothing to send.")
        return

    items = _get_pending_items()
    send_to_slack(items, total_pending, channel)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Send Slack review digest")
    parser.add_argument("--channel", default=None, help="Override Slack channel")
    parser.add_argument("--json", action="store_true", help="Output items as JSON for MCP-based posting")
    args = parser.parse_args()

    if args.json:
        init_db()
        total_pending = _get_pending_count()
        items = _get_pending_items()
        print(json.dumps({"total_pending": total_pending, "items": items}))
    else:
        run(channel=args.channel)
