"""
Database models for LSSR (Living Systematic Scoping Review).

ORM: SQLAlchemy with SQLite (./data/lssr.db, persisted via Docker volume).

Foundation scope (Fase 0): the multiuser / multiworkspace / public-sharing core.
  User, Workspace, WorkspaceMember, PublicShare

The pipeline entities (SearchQuery, Iteration, Record, RawReference, Import,
Assessment, Synthesis, …) are specified in SPEC.md and added in later phases.

Migration strategy (borant house pattern): init_db() runs ALTER TABLE for each
new column on every startup; SQLite raises on duplicates, caught and ignored
(additive only).
"""
import os
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

DATABASES = ["pubmed", "scopus", "wos", "cinahl", "jstor"]


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
    """Unified table for the three criterion sets defined in a workspace:
      exclusion → screening 1 (title+abstract), inclusion → screening 2 (full
      text), assessment → step 9. `description` becomes the LLM guidance."""
    __tablename__ = "criteria"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    kind         = Column(String, nullable=False)   # exclusion | inclusion | assessment
    label        = Column(String, nullable=False)
    description  = Column(Text, nullable=True)
    position     = Column(Integer, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)

    workspace = relationship("Workspace", backref="criteria")


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


# ── Assessment (step 9) & synthesis (step 10) ──────────────────────────────────

class Assessment(Base):
    """One finding per (record, assessment criterion), produced by the combined
    screening-2 + assessment call. Re-generated per iteration."""
    __tablename__ = "assessments"
    id           = Column(Integer, primary_key=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id"), nullable=False)
    record_id    = Column(Integer, ForeignKey("records.id"), nullable=False)
    criterion_id = Column(Integer, ForeignKey("criteria.id"), nullable=False)
    finding      = Column(Text, nullable=True)
    citation     = Column(Text, nullable=True)   # quote / locator from the full text
    model        = Column(String, nullable=True)
    created_at   = Column(DateTime, default=datetime.utcnow)

    record    = relationship("Record")
    criterion = relationship("Criterion")


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
    __tablename__ = "synthesis_blocks"
    id           = Column(Integer, primary_key=True)
    synthesis_id = Column(Integer, ForeignKey("syntheses.id"), nullable=False)
    criterion_id = Column(Integer, ForeignKey("criteria.id"), nullable=True)
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
            "ALTER TABLE records ADD COLUMN full_text_status VARCHAR DEFAULT 'none'",
            "ALTER TABLE records ADD COLUMN full_text_url VARCHAR",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass
    _backfill_screen_decisions()


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
                        is_primary=(database == "pubmed"))
        db.add(q)
    q.query_string = query_string
    if year_from is not None:
        q.year_from = year_from
    if year_to is not None:
        q.year_to = year_to
    db.commit()
    db.refresh(q)
    return q
