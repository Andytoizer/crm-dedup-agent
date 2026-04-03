"""Unit tests for the blocking engine."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pipeline.blocker import generate_contact_pairs, generate_company_pairs


def make_contact(id, email="", lastname="", company="", phone="", mobilephone=""):
    return {
        "id": id, "email": email, "lastname": lastname,
        "company": company, "phone": phone, "mobilephone": mobilephone, "phone_1": "",
    }


def make_company(id, domain="", name=""):
    return {"id": id, "domain": domain, "name": name}


class TestContactBlocking:
    def test_same_email_domain_creates_pair(self):
        contacts = [
            make_contact("1", email="alice@acme.com"),
            make_contact("2", email="bob@acme.com"),
        ]
        pairs = generate_contact_pairs(contacts)
        assert ("1", "2") in pairs or ("2", "1") in pairs

    def test_free_email_domains_not_blocked(self):
        contacts = [
            make_contact("1", email="alice@gmail.com"),
            make_contact("2", email="bob@gmail.com"),
            make_contact("3", email="charlie@gmail.com"),
        ]
        # Gmail contacts should not be blocked against each other by domain
        pairs = generate_contact_pairs(contacts)
        # Pairs may still appear via soundex/company — just confirm domain block isn't used
        # We test by checking a set with NO other overlap doesn't produce pairs
        # (they have no lastname or company either, so no blocks should fire)
        assert len(pairs) == 0

    def test_same_soundex_lastname_creates_pair(self):
        # Smith / Smyth → same Soundex (S530)
        contacts = [
            make_contact("1", email="alice@gmail.com", lastname="Smith"),
            make_contact("2", email="bob@gmail.com", lastname="Smyth"),
        ]
        pairs = generate_contact_pairs(contacts)
        assert len(pairs) == 1

    def test_same_company_prefix_creates_pair(self):
        contacts = [
            make_contact("1", email="alice@gmail.com", company="Acme Corporation"),
            make_contact("2", email="bob@gmail.com", company="Acme Inc"),
        ]
        pairs = generate_contact_pairs(contacts)
        assert len(pairs) >= 1

    def test_same_phone_prefix_creates_pair(self):
        # Both numbers share the same first 7 digits (6045551)
        contacts = [
            make_contact("1", phone="604-555-1234"),
            make_contact("2", phone="604-555-1999"),
        ]
        pairs = generate_contact_pairs(contacts)
        assert len(pairs) == 1

    def test_no_duplicate_pairs(self):
        contacts = [
            make_contact("1", email="a@acme.com", lastname="Smith", company="Acme Corp"),
            make_contact("2", email="b@acme.com", lastname="Smith", company="Acme Inc"),
        ]
        pairs = generate_contact_pairs(contacts)
        # Each pair should appear at most once
        normalized = [tuple(sorted(p)) for p in pairs]
        assert len(normalized) == len(set(normalized))

    def test_pairs_are_sorted(self):
        contacts = [
            make_contact("10", email="a@acme.com"),
            make_contact("2", email="b@acme.com"),
        ]
        pairs = generate_contact_pairs(contacts)
        for a, b in pairs:
            assert a <= b

    def test_large_block_skipped(self):
        # Blocks with > 500 members should be skipped (too broad)
        contacts = [
            make_contact(str(i), company="BigCorp Inc")
            for i in range(600)
        ]
        pairs = generate_contact_pairs(contacts)
        assert len(pairs) == 0


class TestCompanyBlocking:
    def test_same_domain_creates_pair(self):
        companies = [
            make_company("1", domain="acme.com"),
            make_company("2", domain="www.acme.com"),
        ]
        pairs = generate_company_pairs(companies)
        assert len(pairs) == 1

    def test_same_name_prefix_creates_pair(self):
        companies = [
            make_company("1", name="Acme Corporation"),
            make_company("2", name="Acme Inc"),
        ]
        pairs = generate_company_pairs(companies)
        assert len(pairs) >= 1

    def test_different_domains_no_pair(self):
        companies = [
            make_company("1", domain="apple.com"),
            make_company("2", domain="orange.com"),
        ]
        pairs = generate_company_pairs(companies)
        assert len(pairs) == 0
