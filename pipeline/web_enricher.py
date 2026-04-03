"""
Web enricher: fetches real signals from company websites to inform dedup decisions.

For a given domain it:
  1. Follows HTTP redirects to find the canonical final URL
  2. Extracts signals from the homepage: LinkedIn URL, phone, og:site_name, title
  3. Returns a structured dict for use in AI review prompts

This turns "similar names" into definitive evidence — if two domains redirect
to the same final host, they are the same company.
"""
import re
import time
from typing import Optional
from urllib.parse import urlparse, urljoin

import requests
from requests.exceptions import RequestException

# Browser-like headers to avoid bot blocks
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

TIMEOUT = 8  # seconds per request
MAX_CONTENT_BYTES = 200_000  # don't read huge pages


def _normalize_domain(domain: str) -> str:
    domain = domain.strip().lower()
    domain = re.sub(r"https?://", "", domain)
    domain = domain.removeprefix("www.")
    return domain.split("/")[0].strip()


def _domain_to_url(domain: str) -> str:
    domain = domain.strip()
    if not domain.startswith("http"):
        return f"https://{domain}"
    return domain


def _get_canonical_host(domain: str) -> Optional[str]:
    """
    Follow redirects and return the final hostname (no www, lowercase).
    Returns None if the domain is unreachable.
    """
    url = _domain_to_url(domain)
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT,
            allow_redirects=True, stream=True,
        )
        final_url = resp.url
        host = urlparse(final_url).hostname or ""
        return host.lower().removeprefix("www.")
    except RequestException:
        # Try http fallback
        try:
            url_http = url.replace("https://", "http://")
            resp = requests.get(
                url_http, headers=HEADERS, timeout=TIMEOUT,
                allow_redirects=True, stream=True,
            )
            host = urlparse(resp.url).hostname or ""
            return host.lower().removeprefix("www.")
        except RequestException:
            return None


def _extract_signals(domain: str) -> dict:
    """
    Fetch the homepage and extract: title, og:site_name, LinkedIn URL, phone numbers.
    Returns an empty dict on failure.
    """
    url = _domain_to_url(domain)
    try:
        resp = requests.get(
            url, headers=HEADERS, timeout=TIMEOUT,
            allow_redirects=True, stream=True,
        )
        # Read limited content
        content = b""
        for chunk in resp.iter_content(chunk_size=8192):
            content += chunk
            if len(content) >= MAX_CONTENT_BYTES:
                break
        html = content.decode("utf-8", errors="ignore")
    except RequestException:
        return {}

    signals = {}

    # Page title
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        signals["title"] = title_match.group(1).strip()[:120]

    # og:site_name
    og_match = re.search(
        r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    )
    if og_match:
        signals["og_site_name"] = og_match.group(1).strip()

    # og:description
    desc_match = re.search(
        r'<meta[^>]+(?:property=["\']og:description["\']|name=["\']description["\'])'
        r'[^>]+content=["\']([^"\']{10,200})["\']',
        html, re.IGNORECASE,
    )
    if desc_match:
        signals["description"] = desc_match.group(1).strip()[:200]

    # LinkedIn company URL
    li_matches = re.findall(
        r'https?://(?:www\.)?linkedin\.com/company/([a-zA-Z0-9\-_]+)',
        html,
    )
    if li_matches:
        # Most common slug on the page
        from collections import Counter
        slug = Counter(li_matches).most_common(1)[0][0]
        signals["linkedin_slug"] = slug

    # Phone numbers (basic E.164 / US format)
    phone_matches = re.findall(
        r'(?:\+1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}',
        html,
    )
    if phone_matches:
        # Normalize and deduplicate
        phones = list({re.sub(r"\D", "", p) for p in phone_matches if len(re.sub(r"\D", "", p)) >= 10})
        if phones:
            signals["phones"] = phones[:3]

    return signals


def enrich_domain(domain: str) -> dict:
    """
    Full enrichment for a single domain.
    Returns a dict with canonical_host, title, og_site_name, linkedin_slug, phones.
    """
    if not domain:
        return {"domain": domain, "error": "no domain"}

    canonical = _get_canonical_host(domain)
    result = {
        "domain": domain,
        "canonical_host": canonical,
    }

    if canonical:
        signals = _extract_signals(domain)
        result.update(signals)

    return result


def check_same_company(domain_a: str, domain_b: str) -> dict:
    """
    Compare two domains and return evidence about whether they're the same company.

    Returns:
        {
            "verdict": "SAME" | "DIFFERENT" | "UNKNOWN",
            "confidence": "HIGH" | "MEDIUM" | "LOW",
            "evidence": [...],
            "enrichment_a": {...},
            "enrichment_b": {...},
        }
    """
    if not domain_a or not domain_b:
        return {"verdict": "UNKNOWN", "confidence": "LOW", "evidence": ["missing domain(s)"]}

    # Fetch both in parallel-ish (sequential but fast)
    enrich_a = enrich_domain(domain_a)
    time.sleep(0.3)
    enrich_b = enrich_domain(domain_b)

    evidence = []
    same_signals = 0
    diff_signals = 0

    # Signal 1: canonical host match (highest weight)
    host_a = enrich_a.get("canonical_host")
    host_b = enrich_b.get("canonical_host")
    if host_a and host_b:
        if host_a == host_b:
            evidence.append(f"Both redirect to the same host: {host_a}")
            same_signals += 3  # very strong
        else:
            evidence.append(f"Different canonical hosts: {host_a} vs {host_b}")
            diff_signals += 2

    # Signal 2: same LinkedIn company slug
    li_a = enrich_a.get("linkedin_slug")
    li_b = enrich_b.get("linkedin_slug")
    if li_a and li_b:
        if li_a == li_b:
            evidence.append(f"Same LinkedIn company page: /company/{li_a}")
            same_signals += 3
        else:
            evidence.append(f"Different LinkedIn pages: /company/{li_a} vs /company/{li_b}")
            diff_signals += 2

    # Signal 3: same phone number
    phones_a = set(enrich_a.get("phones", []))
    phones_b = set(enrich_b.get("phones", []))
    shared_phones = phones_a & phones_b
    if shared_phones:
        evidence.append(f"Shared phone number: {list(shared_phones)[0]}")
        same_signals += 2

    # Signal 4: og:site_name comparison
    og_a = enrich_a.get("og_site_name", "").strip().lower()
    og_b = enrich_b.get("og_site_name", "").strip().lower()
    if og_a and og_b:
        if og_a == og_b:
            evidence.append(f"Same site name: '{og_a}'")
            same_signals += 1
        else:
            evidence.append(f"Different site names: '{og_a}' vs '{og_b}'")
            diff_signals += 1

    # Determine verdict
    if same_signals >= 3 and diff_signals == 0:
        verdict, confidence = "SAME", "HIGH"
    elif same_signals >= 2 and diff_signals <= 1:
        verdict, confidence = "SAME", "MEDIUM"
    elif diff_signals >= 3 and same_signals == 0:
        verdict, confidence = "DIFFERENT", "HIGH"
    elif diff_signals >= 2 and same_signals == 0:
        verdict, confidence = "DIFFERENT", "MEDIUM"
    elif same_signals > diff_signals:
        verdict, confidence = "SAME", "LOW"
    elif diff_signals > same_signals:
        verdict, confidence = "DIFFERENT", "LOW"
    else:
        verdict, confidence = "UNKNOWN", "LOW"

    if not evidence:
        evidence.append("Could not fetch either website")

    return {
        "verdict": verdict,
        "confidence": confidence,
        "evidence": evidence,
        "enrichment_a": enrich_a,
        "enrichment_b": enrich_b,
    }
