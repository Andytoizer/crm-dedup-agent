import json
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, Boolean, Integer, DateTime, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class AgentRun(Base):
    """Checkpoint and metadata for each dedup run."""
    __tablename__ = "agent_runs"

    run_id = Column(String, primary_key=True)
    run_type = Column(String, nullable=False)  # "bulk" | "incremental"
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)

    # Checkpoints (ISO timestamp strings for HubSpot filter)
    last_contact_timestamp = Column(String, nullable=True)
    last_company_timestamp = Column(String, nullable=True)

    # Counters
    contacts_fetched = Column(Integer, default=0)
    contacts_merged = Column(Integer, default=0)
    contacts_flagged = Column(Integer, default=0)
    companies_fetched = Column(Integer, default=0)
    companies_merged = Column(Integer, default=0)
    companies_flagged = Column(Integer, default=0)

    dry_run = Column(Boolean, default=True)
    error_log = Column(Text, default="[]")  # JSON array of error strings

    def add_error(self, msg: str):
        errors = json.loads(self.error_log or "[]")
        errors.append(msg)
        self.error_log = json.dumps(errors)


class AuditLog(Base):
    """Immutable record of every merge decision (proposed or executed)."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    object_type = Column(String, nullable=False)   # "contact" | "company"
    primary_id = Column(String, nullable=False)    # HubSpot ID of master record
    secondary_id = Column(String, nullable=False)  # HubSpot ID of record to absorb
    merged_record_id = Column(String, nullable=True)  # New ID after merge (HubSpot creates new)
    score = Column(Float, nullable=False)
    match_signals = Column(Text, nullable=False)   # JSON list of signal names
    match_reason = Column(Text, nullable=False)    # Human-readable explanation
    dry_run = Column(Boolean, default=True)
    agent_run_id = Column(String, nullable=True)


class ReviewQueue(Base):
    """Uncertain match pairs queued for human review."""
    __tablename__ = "review_queue"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    object_type = Column(String, nullable=False)   # "contact" | "company"
    id_a = Column(String, nullable=False)
    id_b = Column(String, nullable=False)
    score = Column(Float, nullable=False)
    match_signals = Column(Text, nullable=False)   # JSON list
    match_reason = Column(Text, nullable=False)
    # Snapshot of key fields for digest display (JSON)
    record_a_summary = Column(Text, nullable=True)
    record_b_summary = Column(Text, nullable=True)
    status = Column(String, default="PENDING")     # PENDING | APPROVED | REJECTED | EXPIRED
    reviewed_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)


class KnownNonDuplicate(Base):
    """Confirmed non-duplicate pairs — suppresses future re-flagging."""
    __tablename__ = "known_non_duplicates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    object_type = Column(String, nullable=False)
    id_a = Column(String, nullable=False)
    id_b = Column(String, nullable=False)
    confirmed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
