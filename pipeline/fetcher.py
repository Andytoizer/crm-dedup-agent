# ============================================================
# INTEGRATION FILE — Adapt this for your CRM
# Original: Built for HubSpot
# What to change: API endpoints, auth headers, field names, pagination logic
# What to preserve: Generator/iterator pattern, normalized record shape (id, email, name, company, phone, linkedin fields)
# ============================================================

"""
Paginated HubSpot fetcher for contacts and companies.
Uses the HubSpot Search API with rate limiting and checkpoint support.
"""
import time
import requests
from typing import Iterator, Optional
from config.settings import (
    HUBSPOT_ACCESS_TOKEN, HUBSPOT_API_BASE, HUBSPOT_PAGE_SIZE,
    HUBSPOT_RATE_LIMIT_PER_10S, CONTACT_FETCH_PROPERTIES, COMPANY_FETCH_PROPERTIES,
)

_request_times: list[float] = []


def _rate_limited_post(url: str, payload: dict) -> dict:
    """POST with simple sliding-window rate limiter (90 req/10s)."""
    now = time.time()
    _request_times[:] = [t for t in _request_times if now - t < 10]
    if len(_request_times) >= HUBSPOT_RATE_LIMIT_PER_10S:
        sleep_for = 10 - (now - _request_times[0]) + 0.1
        if sleep_for > 0:
            time.sleep(sleep_for)

    headers = {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=60)
    _request_times.append(time.time())

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "10"))
        time.sleep(retry_after)
        return _rate_limited_post(url, payload)

    if not resp.ok:
        print(f"  HubSpot error {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


def fetch_contacts(
    since_timestamp: Optional[str] = None,
    limit: Optional[int] = None,
) -> Iterator[dict]:
    """
    Yield all HubSpot contacts, one at a time.

    HubSpot Search API caps pagination at 10,000 records per query.
    For full scans we use chunked date-cursor pagination: after each
    10K chunk, advance the GTE filter to the last record's timestamp.

    Args:
        since_timestamp: ISO 8601 string — only fetch records modified >= this.
        limit: Max records to yield (for testing/sampling).
    """
    yield from _fetch_objects(
        object_type="contacts",
        modified_prop="lastmodifieddate",
        properties=CONTACT_FETCH_PROPERTIES,
        normalizer=_normalize_contact,
        since_timestamp=since_timestamp,
        limit=limit,
    )


def fetch_companies(
    since_timestamp: Optional[str] = None,
    limit: Optional[int] = None,
) -> Iterator[dict]:
    """Yield all HubSpot companies using chunked date-cursor pagination."""
    yield from _fetch_objects(
        object_type="companies",
        modified_prop="hs_lastmodifieddate",
        properties=COMPANY_FETCH_PROPERTIES,
        normalizer=_normalize_company,
        since_timestamp=since_timestamp,
        limit=limit,
    )


def _fetch_objects(
    object_type: str,
    modified_prop: str,
    properties: list,
    normalizer,
    since_timestamp: Optional[str],
    limit: Optional[int],
) -> Iterator[dict]:
    """
    Generic chunked fetcher. Pages through HubSpot search in chunks of
    up to 10,000 records by advancing the date cursor after each chunk.
    """
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/{object_type}/search"
    cursor_ts = since_timestamp  # advances after each 10K chunk
    yielded = 0
    chunk_count = 0

    while True:
        after = None
        chunk_yielded = 0

        while True:
            filters = []
            if cursor_ts:
                filters.append({
                    "propertyName": modified_prop,
                    "operator": "GTE",
                    "value": cursor_ts,
                })

            payload = {
                "properties": properties,
                "limit": HUBSPOT_PAGE_SIZE,
                "sorts": [{"propertyName": modified_prop, "direction": "ASCENDING"}],
                "filterGroups": [{"filters": filters}] if filters else [],
            }
            if after:
                payload["after"] = after

            data = _rate_limited_post(url, payload)
            results = data.get("results", [])
            if not results:
                return

            for record in results:
                normalized = normalizer(record)
                yield normalized
                yielded += 1
                chunk_yielded += 1
                last_ts = normalized.get(modified_prop, "") or normalized.get("lastmodifieddate", "") or normalized.get("hs_lastmodifieddate", "")
                if limit and yielded >= limit:
                    return

            paging = data.get("paging", {})
            after = paging.get("next", {}).get("after")
            if not after:
                # End of this chunk — no more pages
                return

            # HubSpot caps at 10,000 per query; after 9,900 records advance cursor
            if chunk_yielded >= 9900:
                break

        # Advance cursor to last record's timestamp for next chunk
        chunk_count += 1
        print(f"  Fetched chunk {chunk_count} ({chunk_yielded} records), advancing cursor...")
        if last_ts:
            cursor_ts = last_ts
        else:
            return



def _normalize_contact(raw: dict) -> dict:
    """Flatten HubSpot API response into a plain dict."""
    props = raw.get("properties", {})
    return {
        "id": raw["id"],
        "email": props.get("email", "") or "",
        "hs_linkedin_url": props.get("hs_linkedin_url", "") or "",
        "lemlistlinkedinurl": props.get("lemlistlinkedinurl", "") or "",
        "phone": props.get("phone", "") or "",
        "mobilephone": props.get("mobilephone", "") or "",
        "phone_1": props.get("phone_1", "") or "",
        "firstname": props.get("firstname", "") or "",
        "lastname": props.get("lastname", "") or "",
        "company": props.get("company", "") or "",
        "hs_merged_object_ids": props.get("hs_merged_object_ids", "") or "",
        "createdate": props.get("createdate", "") or "",
        "lastmodifieddate": props.get("lastmodifieddate", "") or "",
        "hs_last_sales_activity_timestamp": props.get("hs_last_sales_activity_timestamp", "") or "",
        "notes_last_contacted": props.get("notes_last_contacted", "") or "",
        "num_associated_deals": int(props.get("num_associated_deals") or 0),
        "hs_email_replied": int(props.get("hs_email_replied") or 0),
        "hs_email_open": int(props.get("hs_email_open") or 0),
        "hs_email_click": int(props.get("hs_email_click") or 0),
        "hs_email_sends_since_last_engagement": int(props.get("hs_email_sends_since_last_engagement") or 0),
        "num_notes": int(props.get("num_notes") or 0),
        "lifecyclestage": props.get("lifecyclestage", "") or "",
        "hubspot_owner_id": props.get("hubspot_owner_id", "") or "",
    }


def _normalize_company(raw: dict) -> dict:
    """Flatten HubSpot company API response into a plain dict."""
    props = raw.get("properties", {})
    return {
        "id": raw["id"],
        "name": props.get("name", "") or "",
        "domain": props.get("domain", "") or "",
        "website": props.get("website", "") or "",
        "linkedin_company_page": props.get("linkedin_company_page", "") or "",
        "lemlistprofileurl": props.get("lemlistprofileurl", "") or "",
        "phone": props.get("phone", "") or "",
        "hs_merged_object_ids": props.get("hs_merged_object_ids", "") or "",
        "createdate": props.get("createdate", "") or "",
        "hs_lastmodifieddate": props.get("hs_lastmodifieddate", "") or "",
        "num_associated_contacts": int(props.get("num_associated_contacts") or 0),
        "num_associated_deals": int(props.get("num_associated_deals") or 0),
        "hs_last_sales_activity_timestamp": props.get("hs_last_sales_activity_timestamp", "") or "",
        "lifecyclestage": props.get("lifecyclestage", "") or "",
        "hubspot_owner_id": props.get("hubspot_owner_id", "") or "",
    }
