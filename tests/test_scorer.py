"""Unit tests for the deterministic scoring engine."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pipeline.scorer import (
    score_contacts, score_companies, score_for_master, select_master,
)


def make_contact(**kwargs) -> dict:
    defaults = {
        "id": "1", "email": "", "hs_linkedin_url": "", "lemlistlinkedinurl": "",
        "phone": "", "mobilephone": "", "phone_1": "",
        "firstname": "", "lastname": "", "company": "",
        "hs_merged_object_ids": "", "createdate": "2024-01-01T00:00:00.000Z",
        "lastmodifieddate": "", "hs_last_sales_activity_timestamp": "",
        "notes_last_contacted": "", "num_associated_deals": 0,
        "hs_email_replied": 0, "hs_email_open": 0, "hs_email_click": 0,
        "hs_email_sends_since_last_engagement": 0, "num_notes": 0,
        "lifecyclestage": "", "hubspot_owner_id": "",
    }
    defaults.update(kwargs)
    return defaults


def make_company(**kwargs) -> dict:
    defaults = {
        "id": "1", "name": "", "domain": "", "website": "",
        "linkedin_company_page": "", "lemlistprofileurl": "", "phone": "",
        "hs_merged_object_ids": "", "createdate": "2024-01-01T00:00:00.000Z",
        "hs_lastmodifieddate": "", "num_associated_contacts": 0,
        "num_associated_deals": 0, "hs_last_sales_activity_timestamp": "",
        "lifecyclestage": "", "hubspot_owner_id": "",
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Contact scoring tests
# ---------------------------------------------------------------------------

class TestContactEmailMatching:
    def test_exact_email_match(self):
        a = make_contact(id="1", email="jane@acme.com")
        b = make_contact(id="2", email="jane@acme.com")
        result = score_contacts(a, b)
        assert result.score == 1.0
        assert result.action == "AUTO_MERGE"
        assert "email_exact" in result.match_signals

    def test_email_case_insensitive(self):
        a = make_contact(id="1", email="Jane@Acme.COM")
        b = make_contact(id="2", email="jane@acme.com")
        result = score_contacts(a, b)
        assert result.score == 1.0
        assert "email_exact" in result.match_signals

    def test_gmail_dot_insensitive(self):
        a = make_contact(id="1", email="j.smith@gmail.com")
        b = make_contact(id="2", email="jsmith@gmail.com")
        result = score_contacts(a, b)
        assert result.score == 0.97
        assert result.action == "AUTO_MERGE"
        assert "email_gmail_dots" in result.match_signals

    def test_different_emails_no_match(self):
        a = make_contact(id="1", email="alice@acme.com")
        b = make_contact(id="2", email="bob@acme.com")
        result = score_contacts(a, b)
        assert result.score < 0.70
        assert result.action == "DISCARD"

    def test_empty_emails_no_false_positive(self):
        a = make_contact(id="1", email="")
        b = make_contact(id="2", email="")
        result = score_contacts(a, b)
        assert result.action == "DISCARD"


class TestContactLinkedIn:
    def test_exact_linkedin_match(self):
        a = make_contact(id="1", hs_linkedin_url="https://linkedin.com/in/janesmith")
        b = make_contact(id="2", hs_linkedin_url="https://www.linkedin.com/in/janesmith/")
        result = score_contacts(a, b)
        assert result.score == 0.98
        assert "linkedin_exact" in result.match_signals

    def test_lemlist_linkedin_field(self):
        a = make_contact(id="1", lemlistlinkedinurl="linkedin.com/in/janesmith")
        b = make_contact(id="2", hs_linkedin_url="https://linkedin.com/in/janesmith")
        result = score_contacts(a, b)
        assert "linkedin_exact" in result.match_signals

    def test_different_linkedin(self):
        a = make_contact(id="1", hs_linkedin_url="https://linkedin.com/in/alice")
        b = make_contact(id="2", hs_linkedin_url="https://linkedin.com/in/bob")
        result = score_contacts(a, b)
        assert result.action == "DISCARD"


class TestContactPhone:
    def test_exact_phone_match(self):
        a = make_contact(id="1", phone="604-555-1234")
        b = make_contact(id="2", phone="6045551234")
        result = score_contacts(a, b)
        assert result.score == 0.92
        assert "phone_exact" in result.match_signals

    def test_mobile_matches_phone(self):
        a = make_contact(id="1", phone="6045551234")
        b = make_contact(id="2", mobilephone="(604) 555-1234")
        result = score_contacts(a, b)
        assert "phone_exact" in result.match_signals

    def test_us_country_code_stripped(self):
        a = make_contact(id="1", phone="16045551234")
        b = make_contact(id="2", phone="6045551234")
        result = score_contacts(a, b)
        assert "phone_exact" in result.match_signals


class TestContactFuzzyName:
    def test_same_name_same_company(self):
        a = make_contact(id="1", firstname="John", lastname="Smith", company="Acme Corp")
        b = make_contact(id="2", firstname="John", lastname="Smith", company="Acme Inc")
        result = score_contacts(a, b)
        assert result.score >= 0.70
        assert result.action in ("AUTO_MERGE", "REVIEW")

    def test_nickname_expansion(self):
        a = make_contact(id="1", firstname="Bob", lastname="Jones", company="TechCo")
        b = make_contact(id="2", firstname="Robert", lastname="Jones", company="TechCo")
        result = score_contacts(a, b)
        # Bob → robert should match Robert → robert
        assert result.score >= 0.70

    def test_different_names_discard(self):
        a = make_contact(id="1", firstname="Alice", lastname="Smith", company="Acme")
        b = make_contact(id="2", firstname="Bob", lastname="Jones", company="OtherCo")
        result = score_contacts(a, b)
        assert result.action == "DISCARD"


# ---------------------------------------------------------------------------
# Company scoring tests
# ---------------------------------------------------------------------------

class TestCompanyScoring:
    def test_exact_domain_match(self):
        a = make_company(id="1", domain="acme.com")
        b = make_company(id="2", domain="www.acme.com")
        result = score_companies(a, b)
        assert result.score == 1.0
        assert "domain_exact" in result.match_signals

    def test_domain_from_website(self):
        a = make_company(id="1", domain="", website="https://acme.com/about")
        b = make_company(id="2", domain="acme.com")
        result = score_companies(a, b)
        assert result.score == 1.0

    def test_linkedin_company_match(self):
        a = make_company(id="1", linkedin_company_page="https://linkedin.com/company/acme-corp")
        b = make_company(id="2", linkedin_company_page="linkedin.com/company/acme-corp/")
        result = score_companies(a, b)
        assert result.score == 0.98
        assert "linkedin_company_exact" in result.match_signals

    def test_fuzzy_company_name(self):
        a = make_company(id="1", name="Acme Corporation")
        b = make_company(id="2", name="Acme Corp Inc")
        result = score_companies(a, b)
        assert result.score >= 0.70

    def test_different_companies_discard(self):
        a = make_company(id="1", name="Apple Inc", domain="apple.com")
        b = make_company(id="2", name="Orange Corp", domain="orange.com")
        result = score_companies(a, b)
        assert result.action == "DISCARD"


# ---------------------------------------------------------------------------
# Master record selection tests
# ---------------------------------------------------------------------------

class TestMasterSelection:
    def test_active_record_wins(self):
        active = make_contact(
            id="1",
            num_associated_deals=3,
            hs_email_replied=5,
            hs_last_sales_activity_timestamp="2025-01-01T00:00:00Z",
        )
        inactive = make_contact(id="2")
        master, secondary = select_master(active, inactive)
        assert master["id"] == "1"
        assert secondary["id"] == "2"

    def test_more_notes_wins_when_both_inactive(self):
        rich = make_contact(id="1", num_notes=15, email="rich@acme.com")
        sparse = make_contact(id="2", num_notes=2)
        master, secondary = select_master(rich, sparse)
        assert master["id"] == "1"

    def test_activity_multiplier_dominates(self):
        # A has many notes but no activity; B has few notes but deals
        a = make_contact(id="1", num_notes=20)
        b = make_contact(id="2", num_notes=1, num_associated_deals=1,
                         hs_last_sales_activity_timestamp="2025-06-01T00:00:00Z")
        master, _ = select_master(a, b)
        assert master["id"] == "2"


# ---------------------------------------------------------------------------
# Result fields
# ---------------------------------------------------------------------------

class TestMatchResultFields:
    def test_result_has_reason(self):
        a = make_contact(id="1", email="test@co.com")
        b = make_contact(id="2", email="test@co.com")
        result = score_contacts(a, b)
        assert result.match_reason
        assert "email" in result.match_reason.lower()

    def test_discard_result(self):
        a = make_contact(id="1")
        b = make_contact(id="2")
        result = score_contacts(a, b)
        assert result.action == "DISCARD"
        assert result.score < 0.70
