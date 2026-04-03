"""
Deterministic scoring engine for contact and company duplicate detection.

Contact matching priority:
  1. Email exact (normalized)           → 1.0
  2. Email near-match (Gmail dots)      → 0.97
  3. LinkedIn URL exact (normalized)    → 0.98
  4. Phone exact (digits-only)          → 0.92
  5. Fuzzy name + company               → variable

Company matching priority:
  1. Domain exact (normalized)          → 1.0
  2. LinkedIn company page exact        → 0.98
  3. Fuzzy company name                 → variable
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Tuple

from rapidfuzz import fuzz

# Legal suffixes to strip before name comparison
LEGAL_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|international"
    r"|ventures|partners|consulting|solutions|technologies|tech|labs|studio|studios)\b",
    re.IGNORECASE,
)

# Common nickname → canonical name map (expand as needed)
NICKNAME_MAP = {
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "bill": "william", "will": "william", "wm": "william", "billy": "william",
    "jim": "james", "jimmy": "james",
    "mike": "michael", "mick": "michael",
    "tom": "thomas", "tommy": "thomas",
    "dave": "david",
    "chris": "christopher",
    "liz": "elizabeth", "beth": "elizabeth", "lisa": "elizabeth",
    "kate": "katherine", "katie": "katherine", "kathy": "katherine",
    "alex": "alexander",
    "joe": "joseph",
    "dan": "daniel", "danny": "daniel",
    "rick": "richard", "dick": "richard",
    "matt": "matthew",
    "nick": "nicholas",
    "sam": "samuel",
    "tony": "anthony",
    "andy": "andrew",
}


@dataclass
class MatchResult:
    id_a: str
    id_b: str
    score: float
    action: str                    # "AUTO_MERGE" | "REVIEW" | "DISCARD"
    match_signals: List[str] = field(default_factory=list)
    match_reason: str = ""


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _gmail_normalize(email: str) -> str:
    """Strip dots from gmail local part for near-match detection."""
    email = _normalize_email(email)
    if email.endswith("@gmail.com"):
        local = email.split("@")[0].replace(".", "")
        return f"{local}@gmail.com"
    return email


def _normalize_linkedin(url: str) -> str:
    """Extract just the slug from a LinkedIn URL (handles with or without protocol)."""
    url = url.strip().lower()
    # Strip protocol if present
    url = re.sub(r"https?://", "", url)
    # Strip domain prefix variations
    url = re.sub(r"(www\.)?linkedin\.com/in/", "", url)
    return url.strip("/")


def _normalize_linkedin_company(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"https?://", "", url)
    url = re.sub(r"(www\.)?linkedin\.com/company/", "", url)
    return url.strip("/")


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits


def _normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    domain = re.sub(r"https?://", "", domain)
    domain = domain.removeprefix("www.")
    # Strip path, query, fragment — keep only the hostname
    domain = domain.split("/")[0].split("?")[0].split("#")[0]
    return domain.strip()


# Generic placeholder names that should never be used for matching
_SKIP_COMPANY_NAMES = {
    "your company", "yourcompany", "unknown", "n/a", "na", "none",
    "test", "example", "company", "my company", "the company",
    "your", "",
}

def _normalize_company_name(name: str) -> str:
    name = LEGAL_SUFFIXES.sub("", name).lower()
    name = re.sub(r"[^\w\s]", " ", name)
    return " ".join(name.split())


def _is_generic_company_name(normalized: str) -> bool:
    return normalized in _SKIP_COMPANY_NAMES or len(normalized) < 3


def _normalize_name(name: str) -> str:
    name = name.strip().lower()
    return NICKNAME_MAP.get(name, name)


def _full_name(record: dict) -> str:
    first = _normalize_name(record.get("firstname", ""))
    last = record.get("lastname", "").strip().lower()
    return f"{first} {last}".strip()


def _all_phones(record: dict) -> list[str]:
    phones = []
    for f in ("phone", "mobilephone", "phone_1"):
        v = record.get(f, "")
        if v:
            phones.append(_normalize_phone(v))
    return [p for p in phones if len(p) >= 7]


def _all_linkedin_slugs(record: dict) -> list[str]:
    slugs = []
    for f in ("hs_linkedin_url", "lemlistlinkedinurl"):
        v = record.get(f, "")
        if v:
            s = _normalize_linkedin(v)
            if s:
                slugs.append(s)
    return slugs


# ---------------------------------------------------------------------------
# Contact scoring
# ---------------------------------------------------------------------------

def score_contacts(a: dict, b: dict, auto_threshold: float = 0.95, review_threshold: float = 0.70) -> MatchResult:
    """Score a pair of contacts and return a MatchResult."""
    signals = []
    score = 0.0

    # 1. Exact email
    email_a = _normalize_email(a.get("email", ""))
    email_b = _normalize_email(b.get("email", ""))
    if email_a and email_b and email_a == email_b:
        signals.append("email_exact")
        score = 1.0

    # 2. Gmail near-match
    if not signals and email_a and email_b:
        if _gmail_normalize(email_a) == _gmail_normalize(email_b):
            signals.append("email_gmail_dots")
            score = 0.97

    # 3. LinkedIn URL
    if score < 0.95:
        slugs_a = set(_all_linkedin_slugs(a))
        slugs_b = set(_all_linkedin_slugs(b))
        if slugs_a and slugs_b and slugs_a & slugs_b:
            signals.append("linkedin_exact")
            score = max(score, 0.98)

    # 4. Phone exact
    if score < 0.90:
        phones_a = set(_all_phones(a))
        phones_b = set(_all_phones(b))
        if phones_a and phones_b and phones_a & phones_b:
            signals.append("phone_exact")
            score = max(score, 0.92)

    # 5. Fuzzy name + company
    # Requires BOTH first name AND last name to have some similarity — prevents
    # false positives where two people share only a last name (e.g. Maria Williams
    # vs Calvin Williams). First name must score >= 50% before we proceed.
    if score < review_threshold:
        first_a = _normalize_name(a.get("firstname", ""))
        first_b = _normalize_name(b.get("firstname", ""))
        last_a = a.get("lastname", "").strip().lower()
        last_b = b.get("lastname", "").strip().lower()

        if first_a and first_b and last_a and last_b:
            first_score = fuzz.ratio(first_a, first_b) / 100
            # Gate: first names must have at least 50% similarity
            if first_score >= 0.50:
                last_score = fuzz.ratio(last_a, last_b) / 100
                name_score = (first_score * 0.4) + (last_score * 0.6)
                co_a = _normalize_company_name(a.get("company", ""))
                co_b = _normalize_company_name(b.get("company", ""))
                co_valid = co_a and co_b and not _is_generic_company_name(co_a) and not _is_generic_company_name(co_b)
                co_score = (fuzz.WRatio(co_a, co_b) / 100) if co_valid else 0.0
                fuzzy_score = (name_score * 0.7) + (co_score * 0.3)
                # Cap fuzzy at 0.89 — never crosses AUTO_MERGE threshold on name alone
                fuzzy_score = min(fuzzy_score, 0.89)
                if fuzzy_score >= review_threshold:
                    signals.append(f"fuzzy_name_co:{fuzzy_score:.2f}")
                    score = max(score, fuzzy_score)

    return _build_result(a["id"], b["id"], score, signals, auto_threshold, review_threshold)


# ---------------------------------------------------------------------------
# Company scoring
# ---------------------------------------------------------------------------

def score_companies(a: dict, b: dict, auto_threshold: float = 0.95, review_threshold: float = 0.70) -> MatchResult:
    signals = []
    score = 0.0

    # 1. Domain exact
    domain_a = _normalize_domain(a.get("domain", "") or a.get("website", ""))
    domain_b = _normalize_domain(b.get("domain", "") or b.get("website", ""))
    if domain_a and domain_b and domain_a == domain_b:
        signals.append("domain_exact")
        score = 1.0

    # 2. LinkedIn company page
    if score < 0.95:
        li_a = _normalize_linkedin_company(a.get("linkedin_company_page", "") or a.get("lemlistprofileurl", ""))
        li_b = _normalize_linkedin_company(b.get("linkedin_company_page", "") or b.get("lemlistprofileurl", ""))
        if li_a and li_b and li_a == li_b:
            signals.append("linkedin_company_exact")
            score = max(score, 0.98)

    # 3. Fuzzy company name
    if score < review_threshold:
        name_a = _normalize_company_name(a.get("name", ""))
        name_b = _normalize_company_name(b.get("name", ""))
        if name_a and name_b:
            if not _is_generic_company_name(name_a) and not _is_generic_company_name(name_b):
                fuzzy_score = fuzz.WRatio(name_a, name_b) / 100
                # Cap at 0.89 — fuzzy name alone never crosses AUTO_MERGE threshold.
                # Only domain_exact or linkedin_company_exact can trigger auto-merge.
                fuzzy_score = min(fuzzy_score, 0.89)
                if fuzzy_score >= review_threshold:
                    signals.append(f"fuzzy_name:{fuzzy_score:.2f}")
                    score = max(score, fuzzy_score)

    return _build_result(a["id"], b["id"], score, signals, auto_threshold, review_threshold)


# ---------------------------------------------------------------------------
# Master record selection
# ---------------------------------------------------------------------------

def score_for_master(record: dict) -> float:
    """
    Score a record for master selection. Higher = more likely to be canonical.
    Engagement activity is the primary multiplier.
    """
    pts = 0.0

    # Completeness signals
    pts += min(record.get("num_notes", 0), 20) * 1.5
    pts += min(record.get("hs_email_sends_since_last_engagement", 0), 50) * 1.0
    populated = sum(1 for v in record.values() if v)
    pts += populated * 0.3
    pts += 10 if record.get("email") else 0
    pts += 8 if (record.get("hs_linkedin_url") or record.get("lemlistlinkedinurl")) else 0
    pts += 5 if (record.get("phone") or record.get("mobilephone")) else 0

    # Age (older = more history)
    create_str = record.get("createdate") or record.get("createdate", "")
    if create_str:
        try:
            created = datetime.fromisoformat(create_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - created).days
            pts += min(age_days, 365) * 0.02
        except ValueError:
            pass

    # Engagement bonuses
    has_activity = False
    if record.get("hs_last_sales_activity_timestamp"):
        has_activity = True
    if record.get("num_associated_deals", 0) > 0:
        pts += 25
        has_activity = True
    if record.get("hs_email_replied", 0) > 0:
        pts += 20
        has_activity = True
    if record.get("hs_email_open", 0) > 0 or record.get("hs_email_click", 0) > 0:
        pts += 10
        has_activity = True
    if record.get("notes_last_contacted"):
        try:
            nc = datetime.fromisoformat(
                record["notes_last_contacted"].replace("Z", "+00:00")
            )
            if (datetime.now(timezone.utc) - nc) < timedelta(days=90):
                pts += 15
                has_activity = True
        except (ValueError, AttributeError):
            pass

    # Activity multiplier — most important signal
    if has_activity:
        pts *= 3.0

    return pts


def select_master(record_a: dict, record_b: dict) -> Tuple[dict, dict]:
    """Return (master, secondary) based on engagement scoring."""
    score_a = score_for_master(record_a)
    score_b = score_for_master(record_b)
    if score_a >= score_b:
        return record_a, record_b
    return record_b, record_a


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_result(
    id_a: str, id_b: str, score: float,
    signals: list[str], auto_threshold: float, review_threshold: float,
) -> MatchResult:
    if score >= auto_threshold:
        action = "AUTO_MERGE"
    elif score >= review_threshold:
        action = "REVIEW"
    else:
        action = "DISCARD"

    reason = _build_reason(signals, score)
    return MatchResult(
        id_a=id_a, id_b=id_b, score=score, action=action,
        match_signals=signals, match_reason=reason,
    )


def _build_reason(signals: list[str], score: float) -> str:
    if not signals:
        return f"No significant match signals (score={score:.2f})"
    readable = {
        "email_exact": "identical email address",
        "email_gmail_dots": "same Gmail address (dot-insensitive)",
        "linkedin_exact": "identical LinkedIn URL",
        "phone_exact": "identical phone number",
        "domain_exact": "identical company domain",
        "linkedin_company_exact": "identical LinkedIn company page",
    }
    parts = []
    for s in signals:
        if s in readable:
            parts.append(readable[s])
        elif s.startswith("fuzzy_name_co:"):
            parts.append(f"similar name + company (score {s.split(':')[1]})")
        elif s.startswith("fuzzy_name:"):
            parts.append(f"similar company name (score {s.split(':')[1]})")
        else:
            parts.append(s)
    return "; ".join(parts)
