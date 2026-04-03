"""
Blocking: generate candidate duplicate pairs without O(n²) comparison.

For contacts, we block on:
  - email domain (non-gmail/yahoo/hotmail contacts clustered by domain)
  - Soundex(lastname)
  - normalized company name prefix (first 5 chars)
  - phone prefix (first 7 digits)

For companies, we block on:
  - domain (exact)
  - normalized name prefix (first 6 chars)
"""
import re
from collections import defaultdict
from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

import jellyfish

# Free email domains — never block contacts against each other within these
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com",
    "live.com", "msn.com",
}

# Legal suffixes to strip before company name blocking
LEGAL_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|co|company|group|holdings|international"
    r"|ventures|partners|consulting|solutions|technologies|tech|labs|studio|studios)\b",
    re.IGNORECASE,
)


def _email_domain(email: str) -> Optional[str]:
    """Return domain if not a free email domain, else None."""
    if "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    return None if domain in FREE_EMAIL_DOMAINS else domain


def _soundex_lastname(record: dict) -> Optional[str]:
    lastname = record.get("lastname", "").strip()
    if not lastname:
        return None
    return jellyfish.soundex(lastname)


def _company_prefix(name: str, length: int = 5) -> Optional[str]:
    cleaned = LEGAL_SUFFIXES.sub("", name).lower()
    cleaned = re.sub(r"[^a-z0-9]", "", cleaned)
    return cleaned[:length] if len(cleaned) >= 3 else None


def _phone_prefix(phone: str, length: int = 7) -> Optional[str]:
    digits = re.sub(r"\D", "", phone)
    # Strip leading country code (1 for US/CA)
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits[:length] if len(digits) >= length else None


def _all_phones(record: dict) -> list[str]:
    phones = []
    for field in ("phone", "mobilephone", "phone_1"):
        val = record.get(field, "")
        if val:
            phones.append(val)
    return phones


def generate_contact_pairs(contacts: List[dict]) -> List[Tuple[str, str]]:
    """
    Return a deduplicated list of (id_a, id_b) candidate pairs to score.
    """
    blocks: dict[str, list[str]] = defaultdict(list)

    for c in contacts:
        cid = c["id"]

        # Block 1: email domain
        domain = _email_domain(c.get("email", ""))
        if domain:
            blocks[f"domain:{domain}"].append(cid)

        # Block 2: Soundex lastname
        sx = _soundex_lastname(c)
        if sx:
            blocks[f"soundex:{sx}"].append(cid)

        # Block 3: company prefix
        cp = _company_prefix(c.get("company", ""))
        if cp:
            blocks[f"co:{cp}"].append(cid)

        # Block 4: phone prefix
        for ph in _all_phones(c):
            pp = _phone_prefix(ph)
            if pp:
                blocks[f"ph:{pp}"].append(cid)

    return _dedup_pairs(blocks)


def generate_company_pairs(companies: List[dict]) -> List[Tuple[str, str]]:
    """
    Return a deduplicated list of (id_a, id_b) candidate pairs to score.
    """
    blocks: dict[str, list[str]] = defaultdict(list)

    for co in companies:
        cid = co["id"]

        # Block 1: exact domain
        domain = co.get("domain", "").strip().lower().removeprefix("www.")
        if domain:
            blocks[f"domain:{domain}"].append(cid)

        # Block 2: name prefix
        np = _company_prefix(co.get("name", ""), length=6)
        if np:
            blocks[f"name:{np}"].append(cid)

    return _dedup_pairs(blocks)


def _dedup_pairs(blocks: Dict[str, List[str]]) -> List[Tuple[str, str]]:
    """
    Generate all pairs within each block, deduplicate, and return sorted list.
    Skip blocks with > 500 members (too broad to be useful).
    """
    seen: Set[Tuple[str, str]] = set()
    pairs: List[Tuple[str, str]] = []

    for members in blocks.values():
        members = list(dict.fromkeys(members))  # deduplicate IDs (same record in multiple chunks)
        if len(members) < 2 or len(members) > 500:
            continue
        for a, b in combinations(members, 2):
            if a == b:
                continue
            key = (min(a, b), max(a, b))
            if key not in seen:
                seen.add(key)
                pairs.append(key)

    return pairs
