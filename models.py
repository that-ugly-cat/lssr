"""
Database models for LSSR (Living Systematic Scoping Review).

ORM: SQLAlchemy with SQLite (./data/lssr.db, persisted via Docker volume).

Foundation scope (Fase 0): the multiuser / multiworkspace / public-sharing core.
  User, Workspace, WorkspaceMember, PublicShare

The pipeline entities (SearchQuery, Iteration, Record, RawReference, Import,
ExtractionField, Extraction, Synthesis, …) are specified in SPEC.md.

Migration strategy (borant house pattern): init_db() runs ALTER TABLE for each
new column on every startup; SQLite raises on duplicates, caught and ignored
(additive only).
"""
import json
import os
import re
import secrets
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
    UniqueConstraint, create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/lssr.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# ── Users ─────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id                    = Column(Integer, primary_key=True)
    email                 = Column(String, unique=True, nullable=False)
    name                  = Column(String, nullable=False)
    hashed_password       = Column(String, nullable=False)
    api_key_encrypted     = Column(String, nullable=True)   # Anthropic key, Fernet-encrypted
    # Publisher TDM credentials (step 6, last layer) — per user, because the
    # entitlement follows the person and their institution, not the server.
    elsevier_key_encrypted       = Column(String, nullable=True)
    elsevier_insttoken_encrypted = Column(String, nullable=True)
    springer_key_encrypted       = Column(String, nullable=True)
    wiley_token_encrypted        = Column(String, nullable=True)
    totp_secret_encrypted = Column(String, nullable=True)   # TOTP secret, Fernet-encrypted
    totp_enabled          = Column(Boolean, default=False)
    backup_codes_json     = Column(Text, nullable=True)     # sha256 hashes of unused backup codes
    is_admin              = Column(Boolean, default=False)
    is_active             = Column(Boolean, default=True)
    created_at            = Column(DateTime, default=datetime.utcnow)

    owned_workspaces = relationship("Workspace", back_populates="owner")


# ── Workspaces ────────────────────────────────────────────────────────────────

class Workspace(Base):
    """One workspace = one living scoping review."""
    __tablename__ = "workspaces"
    id                = Column(Integer, primary_key=True)
    name              = Column(String, nullable=False)
    description       = Column(Text, nullable=True)
    research_question = Column(Text, nullable=True)
    screening_model   = Column(String, default="claude-haiku-4-5")
    # how many independent human votes settle a screen-1 record (1 = single
    # screening; 2 = classic blind double screening). The LLM pre-screens either
    # way; with 0 human votes its decision stands (sole-screener mode).
    screen1_reviewers_required = Column(Integer, default=1)
    steps_done_json   = Column(Text, nullable=True)   # JSON list of completed pipeline steps
    # Search strategy: the primary database the canonical query is authored in
    # (translated from), the shared publication-year window applied to *every*
    # harvest source, and the active translation targets (JSON list of db keys).
    primary_db        = Column(String, default="pubmed")
    year_from         = Column(Integer, nullable=True)
    year_to           = Column(Integer, nullable=True)
    target_dbs_json   = Column(Text, nullable=True)
    owner_id          = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at        = Column(DateTime, default=datetime.utcnow)

    owner   = relationship("User", back_populates="owned_workspaces")
    members = relationship("WorkspaceMember", back_populates="workspace",
                           cascade="all, delete-orphan")
    shares  = relationship("PublicShare", back_populates="workspace",
                           cascade="all, delete-orphan")


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"), primary_key=True)

    workspace = relationship("Workspace", back_populates="members")
    user      = relationship("User")


# ── Public read-only sharing ───────────────────────────────────────────────────

class PublicShare(Base):
    """An opaque token that exposes ONLY the workspace's public synthesis page
    (step 10) at /r/{token}. No login, no access to raw records or criteria.
    Revocable and regenerable."""
    __tablename__ = "public_shares"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    token         = Column(String, unique=True, nullable=False)
    active        = Column(Boolean, default=True)
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)

    workspace  = relationship("Workspace", back_populates="shares")
    created_by = relationship("User")


def new_share_token() -> str:
    return secrets.token_urlsafe(24)


# ── Search strategy (steps 1-2) ────────────────────────────────────────────────

# Every database LSSR can target for query translation. A workspace picks one as
# its *primary* (the query is authored/pasted there and translated from it) and
# any subset as active translation targets. HARVEST_DBS are the ones we can pull
# records from directly via a free, open API (no institutional credentials); the
# rest are translation-only (copy the query out, export, import manually).
DATABASES = [
    "pubmed", "europepmc", "openalex", "eric",
    "scopus", "wos", "cinahl", "jstor",
    "embase-ovid", "embase-ebsco", "psycinfo-ovid", "psycinfo-ebsco",
    "philpapers", "heinonline",
]

DB_LABELS = {
    "pubmed": "PubMed",
    "europepmc": "Europe PMC",
    "openalex": "OpenAlex",
    "eric": "ERIC",
    "scopus": "Scopus",
    "wos": "Web of Science",
    "cinahl": "CINAHL",
    "jstor": "JSTOR",
    "embase-ovid": "Embase (Ovid)",
    "embase-ebsco": "Embase (EBSCO)",
    "psycinfo-ovid": "APA PsycInfo (Ovid)",
    "psycinfo-ebsco": "APA PsycInfo (EBSCO)",
    "philpapers": "PhilPapers",
    "heinonline": "HeinOnline",
}

# Databases we can harvest directly (open API, no institutional auth).
HARVEST_DBS = {"pubmed", "europepmc", "openalex", "eric"}

# Databases a workspace may author its canonical query in (the "start from"
# source that translation flows out of). Kept to the two sensible poles: PubMed
# (richest syntax — MeSH + field tags — so down-translation loses the least) and
# OpenAlex (broadest, most multidisciplinary corpus). Every other database is
# target-only. PubMed is the recommended default; see the note in the Query tab.
SOURCE_DBS = ["pubmed", "openalex"]

# Entry point to each database's (advanced) search UI, so a reviewer can jump
# there to run the translated query. Institutional databases route through the
# library proxy after login — these are the canonical public entry URLs.
DB_SEARCH_URLS = {
    "pubmed": "https://pubmed.ncbi.nlm.nih.gov/advanced/",
    "europepmc": "https://europepmc.org/advancesearch",
    "openalex": "https://openalex.org/works",
    "eric": "https://eric.ed.gov/",
    "scopus": "https://www.scopus.com/search/form.uri?display=advanced",
    "wos": "https://www.webofscience.com/wos/woscc/advanced-search",
    "cinahl": "https://search.ebscohost.com/",
    "jstor": "https://www.jstor.org/action/showAdvancedSearch",
    "embase-ovid": "https://ovidsp.ovid.com/",
    "embase-ebsco": "https://search.ebscohost.com/",
    "psycinfo-ovid": "https://ovidsp.ovid.com/",
    "psycinfo-ebsco": "https://search.ebscohost.com/",
    "philpapers": "https://philpapers.org/search.pl",
    "heinonline": "https://heinonline.org/HOL/Welcome",
}


def db_label(database: str) -> str:
    return DB_LABELS.get(database, database)


def db_search_url(database: str) -> str | None:
    return DB_SEARCH_URLS.get(database)


class SearchQuery(Base):
    """One query per database. PubMed is the primary (is_primary=True); the
    others are its translations (step 2). year_from/year_to apply to PubMed runs."""
    __tablename__ = "search_queries"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    database     = Column(String, nullable=False)   # pubmed | scopus | wos | cinahl | jstor
    query_string = Column(Text, nullable=True)
    is_primary   = Column(Boolean, default=False)
    year_from    = Column(Integer, nullable=True)
    year_to      = Column(Integer, nullable=True)
    updated_at   = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", backref="queries")


# ── Iterations (the "living" unit) ─────────────────────────────────────────────

class Iteration(Base):
    __tablename__ = "iterations"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    number       = Column(Integer, nullable=False)
    status       = Column(String, default="open")   # open | closed
    note         = Column(Text, nullable=True)
    started_at   = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    workspace = relationship("Workspace", backref="iterations")


# ── Imports & raw references (step 3) ──────────────────────────────────────────

class Import(Base):
    __tablename__ = "imports"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    iteration_id  = Column(Integer, ForeignKey("iterations.id"), nullable=True)
    database      = Column(String, nullable=False)          # pubmed | scopus | wos | cinahl | jstor | manual
    fmt           = Column(String, nullable=False)          # api | bibtex | ris | manual
    source_name   = Column(String, nullable=True)           # filename or query label
    raw_count     = Column(Integer, default=0)              # references parsed
    new_count     = Column(Integer, default=0)              # records newly created
    merged_count  = Column(Integer, default=0)              # references merged into existing records
    created_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    workspace = relationship("Workspace")


class DedupDismissal(Base):
    """Remembers record pairs the user marked 'not duplicates' during a manual
    dedup pass, so the uncertain-cluster scan won't surface them again."""
    __tablename__ = "dedup_dismissals"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    pair_key     = Column(String, nullable=False)   # "min_id-max_id"
    created_at   = Column(DateTime, default=datetime.utcnow)


class RawReference(Base):
    """A single reference as parsed from a source, before dedup. Kept for
    provenance and to reconstruct 'keep the most complete' decisions."""
    __tablename__ = "raw_references"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    import_id     = Column(Integer, ForeignKey("imports.id"), nullable=True)
    record_id     = Column(Integer, ForeignKey("records.id"), nullable=True)  # merge target
    database      = Column(String, nullable=True)
    canonical_key = Column(String, nullable=True)
    raw_json      = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    record = relationship("Record", back_populates="raw_refs")


# ── Records (the persistent, deduplicated pool) ────────────────────────────────

class Record(Base):
    __tablename__ = "records"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    type          = Column(String, default="article")   # article | book | book_chapter | grey
    authors       = Column(Text, nullable=True)
    year          = Column(Integer, nullable=True)
    title         = Column(Text, nullable=True)
    abstract      = Column(Text, nullable=True)
    doi           = Column(String, nullable=True)
    url           = Column(String, nullable=True)
    source        = Column(String, nullable=True)        # journal / publisher
    keywords_json = Column(Text, nullable=True)          # JSON list
    mesh_json     = Column(Text, nullable=True)          # JSON list
    language      = Column(String, nullable=True)
    # provenance / lifecycle
    source_dbs_json    = Column(Text, nullable=True)     # JSON list of databases that returned it
    canonical_key      = Column(String, nullable=True)
    added_manually     = Column(Boolean, default=False)
    is_removed         = Column(Boolean, default=False)
    removed_reason     = Column(Text, nullable=True)
    first_seen_iter_id = Column(Integer, ForeignKey("iterations.id"), nullable=True)
    last_seen_iter_id  = Column(Integer, ForeignKey("iterations.id"), nullable=True)
    # full text (steps 6-7)
    full_text_status = Column(String, default="none")  # none | url | fetched | converted | failed
    full_text_url    = Column(String, nullable=True)   # OA URL fallback when direct download is blocked
    full_text_path   = Column(String, nullable=True)
    full_text_md     = Column(Text, nullable=True)
    # sticky screening decisions (steps 5, 8) — unused in Fase 1, schema-ready
    screen1_decision = Column(String, default="pending")  # include | exclude | pending
    screen1_reason   = Column(Text, nullable=True)
    screen1_by       = Column(String, nullable=True)      # model | user
    screen1_at       = Column(DateTime, nullable=True)
    screen2_decision = Column(String, default="pending")
    screen2_reason   = Column(Text, nullable=True)
    screen2_by       = Column(String, nullable=True)
    screen2_at       = Column(DateTime, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    workspace = relationship("Workspace", backref="records")
    raw_refs  = relationship("RawReference", back_populates="record",
                             cascade="all, delete-orphan")


# ── Criteria (steps 5, 8, 9) ───────────────────────────────────────────────────

class Criterion(Base):
    """The two criterion sets a workspace screens against: exclusion → screening 1
    (title+abstract), inclusion → screening 2 (full text). `description` becomes
    the LLM guidance. Step 9 uses ExtractionField, not criteria."""
    __tablename__ = "criteria"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    kind         = Column(String, nullable=False)   # exclusion | inclusion
    label        = Column(String, nullable=False)
    description  = Column(Text, nullable=True)
    position     = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)

    workspace = relationship("Workspace", backref="criteria")


# ── Structured extraction (step 9): field schema + per-reviewer values ─────────

class ExtractionField(Base):
    """One field in a workspace's data-extraction form. Builtin fields (country,
    study year, study type, methodology) are seeded and editable; project fields
    are user-added. `show_if_*` gates a field on another field's value."""
    __tablename__ = "extraction_fields"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    key           = Column(String, nullable=False)   # stable machine name
    label         = Column(String, nullable=False)
    help          = Column(Text, nullable=True)
    field_type    = Column(String, nullable=False, default="text")  # text|textarea|number|select|multiselect
    options_json  = Column(Text, nullable=True)      # JSON list for select/multiselect
    show_if_key   = Column(String, nullable=True)    # parent field key
    show_if_values_json = Column(Text, nullable=True)  # JSON list of parent values that reveal this
    builtin       = Column(Boolean, default=False)
    position      = Column(Integer, default=0)

    __table_args__ = (UniqueConstraint("workspace_id", "key", name="uq_extraction_field_key"),)

    def options(self) -> list:
        try:
            v = json.loads(self.options_json) if self.options_json else []
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []

    def show_if_values(self) -> list:
        try:
            v = json.loads(self.show_if_values_json) if self.show_if_values_json else []
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []


class Extraction(Base):
    """A record's extraction values, per reviewer. reviewer_kind: model (LLM
    draft) | user (a reviewer's own) | final (owner-curated, authoritative)."""
    __tablename__ = "extractions"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    record_id     = Column(Integer, ForeignKey("records.id"), nullable=False)
    reviewer_kind = Column(String, nullable=False)   # model | user | final
    reviewer_id   = Column(Integer, ForeignKey("users.id"), nullable=True)
    values_json   = Column(Text, nullable=True)      # {field_key: value}
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("record_id", "reviewer_kind", "reviewer_id",
                                       name="uq_extraction_reviewer"),)

    reviewer = relationship("User")

    def values(self) -> dict:
        try:
            v = json.loads(self.values_json) if self.values_json else {}
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}


def slug_field_key(label: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", (label or "field").lower()).strip("_")[:40] or "field"
    key, i = base, 2
    while key in taken:
        key = f"{base}_{i}"
        i += 1
    return key


def workspace_extraction_fields(db, workspace) -> list:
    return (db.query(ExtractionField)
              .filter(ExtractionField.workspace_id == workspace.id)
              .order_by(ExtractionField.position, ExtractionField.id).all())


def ensure_extraction_fields(db, workspace):
    """Seed builtin fields the first time; migrate any legacy free-text
    assessment criteria into textarea project fields. Runs once per workspace."""
    if db.query(ExtractionField).filter(ExtractionField.workspace_id == workspace.id).count() > 0:
        return
    from extraction_defaults import builtin_fields
    taken, pos = set(), 0
    for f in builtin_fields():
        taken.add(f["key"])
        db.add(ExtractionField(
            workspace_id=workspace.id, key=f["key"], label=f["label"], help=f.get("help"),
            field_type=f["field_type"], options_json=json.dumps(f.get("options") or []) or None,
            show_if_key=f.get("show_if_key"),
            show_if_values_json=json.dumps(f["show_if_values"]) if f.get("show_if_values") else None,
            builtin=True, position=pos))
        pos += 1
    for c in workspace_criteria(db, workspace, "assessment"):
        key = slug_field_key(c.label, taken)
        taken.add(key)
        db.add(ExtractionField(workspace_id=workspace.id, key=key, label=c.label,
                               help=c.description, field_type="textarea",
                               builtin=False, position=pos))
        pos += 1
    db.commit()


def field_visible(field, values: dict) -> bool:
    """Is this field asked, given the current values? (show_if evaluation)"""
    if not field.show_if_key:
        return True
    parent = values.get(field.show_if_key)
    allowed = field.show_if_values()
    if isinstance(parent, list):
        return any(p in allowed for p in parent)
    return parent in allowed


def authoritative_values(db, record) -> dict:
    """The extraction that counts for a record: the owner-curated `final` row if
    there is one, else the most recently saved reviewer's, else the model draft."""
    rows = db.query(Extraction).filter(Extraction.record_id == record.id).all()
    for kind in ("final",):
        hit = [r for r in rows if r.reviewer_kind == kind]
        if hit:
            return hit[0].values()
    users = [r for r in rows if r.reviewer_kind == "user"]
    if users:
        return max(users, key=lambda r: r.updated_at or datetime.min).values()
    model = [r for r in rows if r.reviewer_kind == "model"]
    return model[0].values() if model else {}


def upsert_extraction(db, workspace, record, reviewer_kind, reviewer_id, values: dict):
    row = (db.query(Extraction)
             .filter(Extraction.record_id == record.id,
                     Extraction.reviewer_kind == reviewer_kind,
                     Extraction.reviewer_id == reviewer_id).first())
    if row is None:
        row = Extraction(workspace_id=workspace.id, record_id=record.id,
                         reviewer_kind=reviewer_kind, reviewer_id=reviewer_id)
        db.add(row)
    row.values_json = json.dumps(values)
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


# ── Screening decisions (per reviewer, steps 5 & 8) ────────────────────────────

class ScreenDecision(Base):
    """One vote per (record × reviewer × stage). Reviewers are the LLM
    (reviewer_kind='model'), the workspace members ('user'), or the adjudicator
    who resolves conflicts ('adjudicator'). Record.screen1_decision is the cached
    resolution of these rows — see resolve_screen1()."""
    __tablename__ = "screen_decisions"
    id            = Column(Integer, primary_key=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    record_id     = Column(Integer, ForeignKey("records.id"), nullable=False)
    stage         = Column(String, nullable=False, default="screen1")  # screen1 | screen2
    reviewer_kind = Column(String, nullable=False)   # model | user | adjudicator
    reviewer_id   = Column(Integer, ForeignKey("users.id"), nullable=True)  # null for model
    decision      = Column(String, nullable=False)   # include | exclude
    reason        = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("record_id", "stage", "reviewer_kind", "reviewer_id",
                         name="uq_screen_decision_reviewer"),
    )

    record   = relationship("Record")
    reviewer = relationship("User")


def upsert_screen_decision(db, record, stage, reviewer_kind, reviewer_id,
                           decision, reason=None):
    """Insert or update a reviewer's vote for a record+stage. No commit."""
    row = (db.query(ScreenDecision)
             .filter(ScreenDecision.record_id == record.id,
                     ScreenDecision.stage == stage,
                     ScreenDecision.reviewer_kind == reviewer_kind,
                     ScreenDecision.reviewer_id == reviewer_id).first())
    if row is None:
        row = ScreenDecision(workspace_id=record.workspace_id, record_id=record.id,
                             stage=stage, reviewer_kind=reviewer_kind, reviewer_id=reviewer_id)
        db.add(row)
    row.decision = decision
    row.reason = reason
    row.updated_at = datetime.utcnow()
    db.flush()  # session is autoflush=False; make the row visible to recompute
    return row


def resolve_screen1(rows, reviewers_required: int):
    """Reduce a record's screen-1 votes to (decision, by, reason).
    Priority: adjudicator > human consensus (≥ required) > model > pending.
    Humans disagreeing → 'conflict'. Humans agreeing but too few yet → falls
    back to the model's provisional decision (or pending)."""
    adj = [r for r in rows if r.reviewer_kind == "adjudicator"]
    if adj:
        r = max(adj, key=lambda x: x.updated_at or datetime.min)
        return r.decision, "adjudicator", r.reason
    humans = [r for r in rows if r.reviewer_kind == "user"]
    if humans:
        decisions = {r.decision for r in humans}
        if len(decisions) > 1:
            return "conflict", "conflict", None
        if len(humans) >= max(1, reviewers_required or 1):
            reason = "; ".join(filter(None, (r.reason for r in humans))) or None
            return next(iter(decisions)), "human", reason
        # partial agreement — not enough independent reviewers yet; fall through
    model = [r for r in rows if r.reviewer_kind == "model"]
    if model:
        return model[0].decision, "model", model[0].reason
    return "pending", None, None


def recompute_record_screen1(db, workspace, record):
    """Recache Record.screen1_* from the ScreenDecision rows. No commit."""
    rows = (db.query(ScreenDecision)
              .filter(ScreenDecision.record_id == record.id,
                      ScreenDecision.stage == "screen1").all())
    dec, by, reason = resolve_screen1(rows, workspace.screen1_reviewers_required or 1)
    record.screen1_decision = dec
    record.screen1_by = by
    record.screen1_reason = reason
    record.screen1_at = datetime.utcnow()


def recompute_record_screen2(db, workspace, record):
    """Recache Record.screen2_* from the stage='screen2' ScreenDecision rows.
    Same resolution as screen 1 (adjudicator > human consensus > model). No commit."""
    rows = (db.query(ScreenDecision)
              .filter(ScreenDecision.record_id == record.id,
                      ScreenDecision.stage == "screen2").all())
    dec, by, reason = resolve_screen1(rows, workspace.screen1_reviewers_required or 1)
    record.screen2_decision = dec
    record.screen2_by = by
    record.screen2_reason = reason
    record.screen2_at = datetime.utcnow()


# ── Cost tracking (LLM steps) ──────────────────────────────────────────────────

# Pricing per million tokens (input, output) — approximate, update when Anthropic
# changes rates. Mirrors the AutoCode table.
PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (0.8,   4.0),
    "claude-sonnet-5":  (3.0,  15.0),
    "claude-opus-4-8":  (15.0, 75.0),
}
DEFAULT_SCREENING_MODEL = "claude-haiku-4-5"


def calc_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p = PRICING.get(model, PRICING[DEFAULT_SCREENING_MODEL])
    return (tokens_in * p[0] + tokens_out * p[1]) / 1_000_000


class UserCostLog(Base):
    __tablename__ = "user_cost_log"
    id            = Column(Integer, primary_key=True)
    user_id       = Column(Integer, ForeignKey("users.id"), nullable=True)
    workspace_id  = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    step          = Column(String, nullable=False)  # screen1 | screen2 | assessment | translate | synthesis
    input_tokens  = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd      = Column(Float, default=0.0)
    recorded_at   = Column(DateTime, default=datetime.utcnow)


# ── Synthesis (step 10) ────────────────────────────────────────────────────────

class Synthesis(Base):
    """The public deliverable (step 10): one per workspace, regenerated on demand.
    Visible at /r/{token} only when published."""
    __tablename__ = "syntheses"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False, unique=True)
    prisma_json  = Column(Text, nullable=True)
    published    = Column(Boolean, default=False)
    generated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    blocks = relationship("SynthesisBlock", back_populates="synthesis",
                          cascade="all, delete-orphan")


class SynthesisBlock(Base):
    """One narrative block per free-text extraction field; `heading` is its label."""
    __tablename__ = "synthesis_blocks"
    id           = Column(Integer, primary_key=True)
    synthesis_id = Column(Integer, ForeignKey("syntheses.id"), nullable=False)
    heading      = Column(String, nullable=True)
    narrative    = Column(Text, nullable=True)
    position     = Column(Integer, default=0)

    synthesis = relationship("Synthesis", back_populates="blocks")


# ── Init / helpers ────────────────────────────────────────────────────────────

def init_db():
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # Additive migrations: attempted on every startup, duplicates ignored.
    from sqlalchemy import text
    with engine.connect() as conn:
        for stmt in [
            "ALTER TABLE workspaces ADD COLUMN screening_model VARCHAR DEFAULT 'claude-haiku-4-5'",
            "ALTER TABLE workspaces ADD COLUMN screen1_reviewers_required INTEGER DEFAULT 1",
            "ALTER TABLE workspaces ADD COLUMN steps_done_json VARCHAR",
            "ALTER TABLE users ADD COLUMN elsevier_key_encrypted VARCHAR",
            "ALTER TABLE users ADD COLUMN elsevier_insttoken_encrypted VARCHAR",
            "ALTER TABLE users ADD COLUMN springer_key_encrypted VARCHAR",
            "ALTER TABLE users ADD COLUMN wiley_token_encrypted VARCHAR",
            "ALTER TABLE records ADD COLUMN full_text_status VARCHAR DEFAULT 'none'",
            "ALTER TABLE records ADD COLUMN full_text_url VARCHAR",
            "ALTER TABLE workspaces ADD COLUMN primary_db VARCHAR DEFAULT 'pubmed'",
            "ALTER TABLE workspaces ADD COLUMN year_from INTEGER",
            "ALTER TABLE workspaces ADD COLUMN year_to INTEGER",
            "ALTER TABLE workspaces ADD COLUMN target_dbs_json VARCHAR",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
    _backfill_screen_decisions()
    _retire_assessment_criteria()
    _upgrade_methodology_fields()
    _backfill_workspace_years()


def _backfill_workspace_years():
    """One-time: the year window used to live on the PubMed SearchQuery row; it's
    now workspace-level (applied to every harvest source). Copy it across for any
    workspace that has PubMed years but no workspace-level years yet. Idempotent."""
    db = SessionLocal()
    try:
        for ws in db.query(Workspace).filter(Workspace.year_from.is_(None)).all():
            pq = get_query(db, ws, "pubmed")
            if pq and (pq.year_from or pq.year_to):
                ws.year_from = pq.year_from
                ws.year_to = pq.year_to
        db.commit()
    finally:
        db.close()


def _upgrade_methodology_fields():
    """One-time: the single 'methodology_empirical' builtin became three
    single-choice axes (design / data / timeframe) + a free-text for 'Other'.
    Replace the old field wherever it's still around, in place. Idempotent."""
    db = SessionLocal()
    try:
        olds = (db.query(ExtractionField)
                  .filter(ExtractionField.key == "methodology_empirical").all())
        if not olds:
            return
        from extraction_defaults import builtin_fields
        new_defs = [f for f in builtin_fields()
                    if f["key"] in ("methodology_design", "methodology_data",
                                    "methodology_time", "methodology_other")]
        for old in olds:
            ws_id, base = old.workspace_id, old.position
            existing = {k for (k,) in db.query(ExtractionField.key)
                        .filter(ExtractionField.workspace_id == ws_id).all()}
            db.query(ExtractionField).filter(ExtractionField.id == old.id).delete()
            # shift later fields down to make room for the three extra axes
            for f in (db.query(ExtractionField)
                        .filter(ExtractionField.workspace_id == ws_id,
                                ExtractionField.position > base).all()):
                f.position += len(new_defs) - 1
            for i, d in enumerate(new_defs):
                if d["key"] in existing:
                    continue
                db.add(ExtractionField(
                    workspace_id=ws_id, key=d["key"], label=d["label"], help=d.get("help"),
                    field_type=d["field_type"],
                    options_json=json.dumps(d.get("options") or []) or None,
                    show_if_key=d.get("show_if_key"),
                    show_if_values_json=json.dumps(d["show_if_values"]) if d.get("show_if_values") else None,
                    builtin=True, position=base + i))
        db.commit()
    finally:
        db.close()


def _retire_assessment_criteria():
    """One-time: the free-text assessment criteria and their per-record findings
    were replaced by the extraction fields (ExtractionField/Extraction). Fold any
    criteria that never made it across into fields, then drop the retired rows and
    the `assessments` table. Idempotent — a no-op once there's nothing left."""
    from sqlalchemy import text
    db = SessionLocal()
    try:
        legacy = db.query(Criterion).filter(Criterion.kind == "assessment").count()
        if legacy:
            for ws in db.query(Workspace).all():
                ensure_extraction_fields(db, ws)   # seeds builtin + migrates this ws
            db.query(Criterion).filter(Criterion.kind == "assessment").delete()
            db.commit()
    finally:
        db.close()
    with engine.connect() as conn:
        for stmt in ["UPDATE synthesis_blocks SET criterion_id = NULL",
                     "DROP TABLE IF EXISTS assessments"]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass


def _backfill_screen_decisions():
    """One-time: seed ScreenDecision rows from the pre-existing single-decision
    Record.screen1_* fields, so history survives the multi-reviewer migration.
    Guarded by an empty table, so it runs only on the first startup after the
    schema change."""
    db = SessionLocal()
    try:
        if db.query(ScreenDecision).count() > 0:
            return
        recs = (db.query(Record)
                  .filter(Record.screen1_by.in_(["model", "user"]),
                          Record.screen1_decision.in_(["include", "exclude"])).all())
        owners = {w.id: w.owner_id for w in db.query(Workspace).all()}
        for rec in recs:
            if rec.screen1_by == "model":
                kind, rid = "model", None
            else:
                kind, rid = "user", owners.get(rec.workspace_id)
            db.add(ScreenDecision(workspace_id=rec.workspace_id, record_id=rec.id,
                                  stage="screen1", reviewer_kind=kind, reviewer_id=rid,
                                  decision=rec.screen1_decision, reason=rec.screen1_reason))
        if recs:
            db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def user_workspaces(db, user: "User") -> list["Workspace"]:
    """Workspaces the user owns or is a member of."""
    owned = db.query(Workspace).filter(Workspace.owner_id == user.id)
    member_ids = [m.workspace_id for m in
                  db.query(WorkspaceMember).filter(WorkspaceMember.user_id == user.id).all()]
    member = db.query(Workspace).filter(Workspace.id.in_(member_ids)) if member_ids else None
    result = {w.id: w for w in owned.all()}
    if member:
        for w in member.all():
            result[w.id] = w
    return sorted(result.values(), key=lambda w: w.created_at or datetime.min, reverse=True)


def can_access(db, user: "User", workspace: "Workspace") -> bool:
    if user.is_admin or workspace.owner_id == user.id:
        return True
    return db.query(WorkspaceMember).filter(
        WorkspaceMember.workspace_id == workspace.id,
        WorkspaceMember.user_id == user.id,
    ).first() is not None


def current_iteration(db, workspace: "Workspace") -> "Iteration":
    """Latest open iteration; creates iteration #1 on first use."""
    it = (db.query(Iteration)
            .filter(Iteration.workspace_id == workspace.id)
            .order_by(Iteration.number.desc()).first())
    if it is None:
        it = Iteration(workspace_id=workspace.id, number=1, status="open")
        db.add(it)
        db.commit()
        db.refresh(it)
    return it


PIPELINE_STEPS = ["query", "records", "screening", "fulltext", "assessment", "synthesis"]


def workspace_steps_done(workspace: "Workspace") -> set:
    try:
        v = json.loads(workspace.steps_done_json) if workspace.steps_done_json else []
        return set(v) if isinstance(v, list) else set()
    except (ValueError, TypeError):
        return set()


def set_step_done(db, workspace: "Workspace", step: str, done: bool):
    s = workspace_steps_done(workspace)
    s.add(step) if done else s.discard(step)
    workspace.steps_done_json = json.dumps(sorted(s))
    db.commit()


def workspace_target_dbs(workspace: "Workspace") -> list:
    """Active translation-target databases (never includes the primary)."""
    try:
        v = json.loads(workspace.target_dbs_json) if workspace.target_dbs_json else []
        v = v if isinstance(v, list) else []
    except (ValueError, TypeError):
        v = []
    prim = workspace.primary_db or "pubmed"
    return [d for d in DATABASES if d in v and d != prim]


def set_workspace_targets(db, workspace: "Workspace", targets: list):
    clean = [d for d in DATABASES if d in set(targets)]
    workspace.target_dbs_json = json.dumps(clean)
    db.commit()


def workspace_years(workspace: "Workspace") -> tuple:
    """(year_from, year_to) with sensible open-ended fallbacks."""
    return (workspace.year_from or 1950, workspace.year_to or 2100)


def workspace_criteria(db, workspace: "Workspace", kind: str) -> list["Criterion"]:
    return (db.query(Criterion)
              .filter(Criterion.workspace_id == workspace.id, Criterion.kind == kind)
              .order_by(Criterion.position, Criterion.id).all())


def get_query(db, workspace: "Workspace", database: str) -> "SearchQuery | None":
    return (db.query(SearchQuery)
              .filter(SearchQuery.workspace_id == workspace.id,
                      SearchQuery.database == database).first())


def upsert_query(db, workspace: "Workspace", database: str, query_string: str,
                 year_from: int | None = None, year_to: int | None = None) -> "SearchQuery":
    q = get_query(db, workspace, database)
    if q is None:
        q = SearchQuery(workspace_id=workspace.id, database=database,
                        is_primary=(database == (workspace.primary_db or "pubmed")))
        db.add(q)
    q.query_string = query_string
    if year_from is not None:
        q.year_from = year_from
    if year_to is not None:
        q.year_to = year_to
    db.commit()
    db.refresh(q)
    return q
