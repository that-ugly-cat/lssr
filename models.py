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
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, create_engine,
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


# ── Init / helpers ────────────────────────────────────────────────────────────

def init_db():
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    # Additive migrations: attempted on every startup, duplicates ignored.
    from sqlalchemy import text
    with engine.connect() as conn:
        for stmt in [
            # placeholder for future additive columns, e.g.:
            # "ALTER TABLE workspaces ADD COLUMN research_question TEXT",
        ]:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass


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
