# ============================================================
# CONFIG FILE — Contains HubSpot-specific field names and API settings
# What to change: CONTACT_FETCH_PROPERTIES, COMPANY_FETCH_PROPERTIES to match your CRM's field names
# What to preserve: Threshold values (AUTO_MERGE_THRESHOLD, REVIEW_THRESHOLD) — these are tuned
# ============================================================

import os
from pathlib import Path
from dotenv import load_dotenv

# Always load from the project root .env, regardless of cwd
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

# HubSpot
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_PAGE_SIZE = 100  # Max per Search API page
HUBSPOT_RATE_LIMIT_PER_10S = 90  # Conservative (limit is 100)

# Database
DB_PATH = os.getenv("DB_PATH", "./dedup.db")

# Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Slack
SLACK_REVIEW_CHANNEL = os.getenv("SLACK_REVIEW_CHANNEL", "#hubspot-dedup-review")

# Dedup scoring thresholds
AUTO_MERGE_THRESHOLD = 0.95     # Score >= this → auto-merge
REVIEW_THRESHOLD = 0.70         # Score >= this → review queue
# Score < REVIEW_THRESHOLD → discard

# Master record selection
MERGE_LIMIT_WARNING = 200       # Warn when a record has this many prior merges
REVIEW_QUEUE_EXPIRY_DAYS = 90   # Days before PENDING items are auto-expired

# Contact fields to fetch from HubSpot
CONTACT_FETCH_PROPERTIES = [
    "email", "hs_linkedin_url", "lemlistlinkedinurl",
    "phone", "mobilephone", "phone_1",
    "firstname", "lastname", "company",
    "hs_merged_object_ids", "createdate", "lastmodifieddate",
    "hs_last_sales_activity_timestamp", "notes_last_contacted",
    "num_associated_deals", "hs_email_replied", "hs_email_open",
    "hs_email_click", "hs_email_sends_since_last_engagement", "num_notes",
    "lifecyclestage", "hubspot_owner_id",
]

# Company fields to fetch from HubSpot
COMPANY_FETCH_PROPERTIES = [
    "name", "domain", "website", "linkedin_company_page",
    "lemlistprofileurl", "phone",
    "hs_merged_object_ids", "createdate", "hs_lastmodifieddate",
    "num_associated_contacts", "num_associated_deals",
    "hs_last_sales_activity_timestamp",
    "lifecyclestage", "hubspot_owner_id",
]
