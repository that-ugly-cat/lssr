"""
LSSR — Living Systematic Scoping Review.

Fase 0 (fondazione): auth + multiuser + multiworkspace + public read-only share.
The 10-step pipeline (SPEC.md §5) is added in later phases; the workspace page
shows the step scaffold with everything past the foundation marked "coming".
"""
import json
import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import (
    create_token, get_current_user, get_user_or_none, hash_password, verify_password,
)
from models import (
    DATABASES, PRICING, Criterion, Import, PublicShare, Record, User, Workspace,
    WorkspaceMember, can_access, current_iteration, get_db, get_query, init_db,
    new_share_token, upsert_query, user_workspaces, workspace_criteria,
)

BASE = Path(__file__).parent
app = FastAPI(title="LSSR")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _md(text: str) -> str:
    import markdown as _mdlib
    from markupsafe import Markup
    return Markup(_mdlib.markdown(text or "", extensions=["extra", "nl2br"]))


templates.env.filters["markdown"] = _md

init_db()


# Unauthenticated HTML requests get bounced to /login instead of a raw 401 JSON.
@app.exception_handler(HTTPException)
async def auth_redirect(request: Request, exc: HTTPException):
    accepts_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and accepts_html:
        return RedirectResponse("/login", status_code=302)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


def render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _load_ws(db: Session, user: User, ws_id: int) -> Workspace:
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    if not ws or not can_access(db, user, ws):
        raise HTTPException(404, "Workspace not found")
    return ws


def _user_api_key(user: User) -> str | None:
    """Decrypted Anthropic key: per-user if set, else the ANTHROPIC_API_KEY env."""
    if user.api_key_encrypted:
        try:
            import crypto
            return crypto.decrypt(user.api_key_encrypted)
        except Exception:
            pass
    return os.environ.get("ANTHROPIC_API_KEY")


# ── Auth ────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: int = 0):
    return render(request, "login.html", {"user": None, "error": bool(error)})


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email.strip().lower(),
                                 User.is_active == True).first()  # noqa: E712
    if not user or not verify_password(password, user.hashed_password):
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie("session", create_token(user.id), httponly=True, samesite="lax",
                    max_age=86400 * 7)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("session")
    return resp


# ── Workspaces ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)):
    workspaces = user_workspaces(db, user)
    return render(request, "workspaces.html", {"user": user, "workspaces": workspaces})


@app.post("/workspaces")
async def create_workspace(
    name: str = Form(...),
    description: str = Form(""),
    research_question: str = Form(""),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(400, "Name required")
    ws = Workspace(name=name.strip(), description=description.strip() or None,
                   research_question=research_question.strip() or None, owner_id=user.id)
    db.add(ws)
    db.commit()
    return RedirectResponse(f"/w/{ws.id}", status_code=302)


@app.get("/w/{ws_id}", response_class=HTMLResponse)
async def workspace_overview(ws_id: int, request: Request,
                             user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    if not ws or not can_access(db, user, ws):
        raise HTTPException(404, "Workspace not found")
    members = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws.id).all()
    shares = db.query(PublicShare).filter(PublicShare.workspace_id == ws.id,
                                          PublicShare.active == True).all()  # noqa: E712
    from models import Iteration
    iterations = (db.query(Iteration).filter(Iteration.workspace_id == ws.id)
                    .order_by(Iteration.number.desc()).all())
    iter_new = {it.id: db.query(Record).filter(Record.workspace_id == ws.id,
                                               Record.first_seen_iter_id == it.id).count()
                for it in iterations}
    return render(request, "workspace_overview.html", {
        "user": user, "ws": ws,
        "is_owner": ws.owner_id == user.id or user.is_admin,
        "members": members, "shares": shares,
        "iterations": iterations, "iter_new": iter_new,
    })


@app.post("/w/{ws_id}/iterations/new")
async def new_iteration(ws_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Refresh: close the current iteration and open the next. Subsequent
    searches/imports attach here; dedup marks existing records last_seen to this
    iteration, new records get first_seen here, and screening/assessment only
    touch still-pending records — the living-review loop."""
    ws = _load_ws(db, user, ws_id)
    from datetime import datetime as _dt
    from models import Iteration
    latest = (db.query(Iteration).filter(Iteration.workspace_id == ws.id)
                .order_by(Iteration.number.desc()).first())
    if latest is None:
        db.add(Iteration(workspace_id=ws.id, number=1, status="open"))
    else:
        latest.status = "closed"
        latest.completed_at = _dt.utcnow()
        db.add(Iteration(workspace_id=ws.id, number=latest.number + 1, status="open"))
    db.commit()
    return RedirectResponse(f"/w/{ws_id}", status_code=302)


# ── Public read-only sharing ────────────────────────────────────────────────

@app.post("/w/{ws_id}/share")
async def create_share(ws_id: int, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    if not ws or not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Owner required")
    db.add(PublicShare(workspace_id=ws.id, token=new_share_token(), created_by_id=user.id))
    db.commit()
    return RedirectResponse(f"/w/{ws_id}", status_code=302)


@app.post("/share/{share_id}/revoke")
async def revoke_share(share_id: int, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    share = db.query(PublicShare).filter(PublicShare.id == share_id).first()
    if share and (share.workspace.owner_id == user.id or user.is_admin):
        share.active = False
        db.commit()
        return RedirectResponse(f"/w/{share.workspace_id}", status_code=302)
    raise HTTPException(403, "Owner required")


@app.get("/r/{token}", response_class=HTMLResponse)
async def public_review(token: str, request: Request, db: Session = Depends(get_db)):
    """Public, no-login view. Exposes ONLY the synthesis page (step 10).
    Placeholder until Fase 3 builds the Synthesis entity."""
    share = db.query(PublicShare).filter(PublicShare.token == token,
                                         PublicShare.active == True).first()  # noqa: E712
    if not share:
        raise HTTPException(404, "Not found")
    from models import Synthesis
    syn = db.query(Synthesis).filter(Synthesis.workspace_id == share.workspace_id,
                                     Synthesis.published == True).first()  # noqa: E712
    prisma = json.loads(syn.prisma_json) if (syn and syn.prisma_json) else None
    blocks = sorted(syn.blocks, key=lambda b: b.position) if syn else []
    return render(request, "public_review.html", {
        "user": None, "ws": share.workspace, "syn": syn, "prisma": prisma, "blocks": blocks,
    })


# ── Profile (Anthropic API key) ─────────────────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request, user: User = Depends(get_current_user)):
    return render(request, "profile.html", {"user": user, "has_key": bool(user.api_key_encrypted)})


@app.post("/profile/api-key")
async def set_api_key(api_key: str = Form(...), user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    key = api_key.strip()
    import crypto
    user.api_key_encrypted = crypto.encrypt(key) if key else None
    db.commit()
    return RedirectResponse("/profile", status_code=302)


# ── Query strategy (steps 1-2) ──────────────────────────────────────────────

@app.get("/w/{ws_id}/query", response_class=HTMLResponse)
async def query_page(ws_id: int, request: Request, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from ingest import term_frequencies
    pubmed = get_query(db, ws, "pubmed")
    translations = [(d, get_query(db, ws, d)) for d in DATABASES if d != "pubmed"]
    freqs = term_frequencies(db, ws.id, top_n=30)
    return render(request, "workspace_query.html", {
        "user": user, "ws": ws, "tab": "query", "pubmed": pubmed,
        "translations": translations, "freqs": freqs,
        "has_key": bool(_user_api_key(user)),
    })


@app.post("/w/{ws_id}/query/pubmed")
async def save_pubmed_query(ws_id: int, query: str = Form(...),
                            year_from: int = Form(...), year_to: int = Form(...),
                            user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if year_from > year_to:
        year_from, year_to = year_to, year_from
    upsert_query(db, ws, "pubmed", query.strip(), year_from, year_to)
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.post("/w/{ws_id}/pubmed/run")
async def run_pubmed(ws_id: int, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    q = get_query(db, ws, "pubmed")
    if not q or not q.query_string:
        raise HTTPException(400, "Save a PubMed query first")
    from pubmed import get_job, start_pubmed
    job = get_job(ws.id)
    if job and job.get("status") in ("searching", "downloading"):
        raise HTTPException(409, "A PubMed run is already in progress")
    start_pubmed(ws.id, q.query_string, q.year_from or 1950, q.year_to or 2100, user.id)
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.get("/w/{ws_id}/pubmed/status")
async def pubmed_status(ws_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    from pubmed import get_job
    return JSONResponse(get_job(ws_id) or {"status": "idle"})


@app.post("/w/{ws_id}/query/{database}/save")
async def save_translation(ws_id: int, database: str, query: str = Form(...),
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if database not in DATABASES or database == "pubmed":
        raise HTTPException(400, "Invalid database")
    upsert_query(db, ws, database, query.strip())
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.post("/w/{ws_id}/query/{database}/translate")
async def translate_route(ws_id: int, database: str, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    pubmed = get_query(db, ws, "pubmed")
    if not pubmed or not pubmed.query_string:
        raise HTTPException(400, "Save a PubMed query first")
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    from translate import translate_query
    try:
        translated = translate_query(api_key, pubmed.query_string, database)
    except Exception as exc:
        raise HTTPException(502, f"Translation failed: {exc}")
    upsert_query(db, ws, database, translated)
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


# ── Records pool (steps 3-4) ────────────────────────────────────────────────

@app.get("/w/{ws_id}/records", response_class=HTMLResponse)
async def records_page(ws_id: int, request: Request, show: str = "active",
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    q = db.query(Record).filter(Record.workspace_id == ws.id)
    if show == "removed":
        q = q.filter(Record.is_removed == True)  # noqa: E712
    else:
        q = q.filter(Record.is_removed == False)  # noqa: E712
    records = q.order_by(Record.year.desc().nullslast(), Record.id.desc()).limit(500).all()
    active_n = db.query(Record).filter(Record.workspace_id == ws.id,
                                       Record.is_removed == False).count()  # noqa: E712
    removed_n = db.query(Record).filter(Record.workspace_id == ws.id,
                                        Record.is_removed == True).count()  # noqa: E712
    imports = db.query(Import).filter(Import.workspace_id == ws.id).order_by(
        Import.created_at.desc()).limit(10).all()
    return render(request, "workspace_records.html", {
        "user": user, "ws": ws, "tab": "records", "records": records,
        "active_n": active_n, "removed_n": removed_n, "show": show,
        "imports": imports, "json": json,
    })


@app.post("/w/{ws_id}/records/import")
async def import_file(ws_id: int, file: UploadFile = File(...), database: str = Form("scopus"),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    raw = (await file.read()).decode("utf-8", errors="replace")
    from ingest import ingest_references, parse_file
    try:
        refs = parse_file(file.filename or "upload", raw)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse file: {exc}")
    it = current_iteration(db, ws)
    fmt = "ris" if (file.filename or "").lower().endswith((".ris", ".nbib")) or raw.lstrip().startswith("TY  -") else "bibtex"
    ingest_references(db, ws, it, refs, database=database, fmt=fmt,
                      source_name=file.filename, user_id=user.id)
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


@app.post("/w/{ws_id}/records/add")
async def add_record(ws_id: int, title: str = Form(...), authors: str = Form(""),
                     year: str = Form(""), doi: str = Form(""), abstract: str = Form(""),
                     source: str = Form(""), type: str = Form("article"),
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    it = current_iteration(db, ws)
    from ingest import canonical_key, normalize_doi
    yr = int("".join(c for c in year if c.isdigit())[:4]) if any(c.isdigit() for c in year) else None
    ref = {"type": type, "authors": authors.strip(), "year": yr, "title": title.strip(),
           "abstract": abstract.strip(), "doi": doi.strip(), "url": "", "source": source.strip(),
           "keywords": [], "mesh": [], "language": ""}
    rec = Record(workspace_id=ws.id, type=type, authors=authors.strip() or None, year=yr,
                 title=title.strip(), abstract=abstract.strip() or None,
                 doi=normalize_doi(doi) or None, source=source.strip() or None,
                 keywords_json="[]", mesh_json="[]",
                 source_dbs_json=json.dumps(["manual"]), canonical_key=canonical_key(ref) or None,
                 added_manually=True, first_seen_iter_id=it.id, last_seen_iter_id=it.id)
    db.add(rec)
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


@app.post("/w/{ws_id}/records/{rid}/remove")
async def remove_record(ws_id: int, rid: int, reason: str = Form(""),
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if rec:
        rec.is_removed = True
        rec.removed_reason = reason.strip() or "manual removal"
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


@app.post("/w/{ws_id}/records/{rid}/restore")
async def restore_record(ws_id: int, rid: int, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if rec:
        rec.is_removed = False
        rec.removed_reason = None
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/records?show=removed", status_code=302)


# ── Settings: criteria & model (steps 5, 8, 9) ──────────────────────────────

@app.get("/w/{ws_id}/settings", response_class=HTMLResponse)
async def settings_page(ws_id: int, request: Request, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    return render(request, "workspace_settings.html", {
        "user": user, "ws": ws, "tab": "settings",
        "exclusion": workspace_criteria(db, ws, "exclusion"),
        "inclusion": workspace_criteria(db, ws, "inclusion"),
        "assessment": workspace_criteria(db, ws, "assessment"),
        "models": list(PRICING.keys()),
    })


@app.post("/w/{ws_id}/settings/model")
async def set_model(ws_id: int, screening_model: str = Form(...),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if screening_model in PRICING:
        ws.screening_model = screening_model
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/criteria/add")
async def add_criterion(ws_id: int, kind: str = Form(...), label: str = Form(...),
                        description: str = Form(""), user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if kind not in ("exclusion", "inclusion", "assessment") or not label.strip():
        raise HTTPException(400, "Invalid criterion")
    pos = db.query(Criterion).filter(Criterion.workspace_id == ws.id,
                                     Criterion.kind == kind).count()
    db.add(Criterion(workspace_id=ws.id, kind=kind, label=label.strip(),
                     description=description.strip() or None, position=pos))
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/criteria/{cid}/delete")
async def delete_criterion(ws_id: int, cid: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    c = db.query(Criterion).filter(Criterion.id == cid, Criterion.workspace_id == ws.id).first()
    if c:
        db.delete(c)
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


# ── Screening 1 (step 5) ────────────────────────────────────────────────────

@app.get("/w/{ws_id}/screening", response_class=HTMLResponse)
async def screening_page(ws_id: int, request: Request, decision: str = "pending",
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)

    def _count(d):
        return db.query(Record).filter(Record.workspace_id == ws.id,
                                       Record.is_removed == False,  # noqa: E712
                                       Record.screen1_decision == d).count()
    counts = {d: _count(d) for d in ("pending", "include", "exclude")}
    q = db.query(Record).filter(Record.workspace_id == ws.id, Record.is_removed == False)  # noqa: E712
    if decision in ("pending", "include", "exclude"):
        q = q.filter(Record.screen1_decision == decision)
    records = q.order_by(Record.id.desc()).limit(300).all()
    return render(request, "workspace_screening.html", {
        "user": user, "ws": ws, "tab": "screening", "counts": counts,
        "records": records, "decision": decision,
        "n_exclusion": len(workspace_criteria(db, ws, "exclusion")),
        "has_key": bool(_user_api_key(user)),
    })


@app.post("/w/{ws_id}/screening/run")
async def run_screening(ws_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    from screening import get_job, start_screen1
    job = get_job(ws.id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "Screening already in progress")
    start_screen1(ws.id, api_key, user.id)
    return RedirectResponse(f"/w/{ws_id}/screening", status_code=302)


@app.get("/w/{ws_id}/screening/status")
async def screening_status(ws_id: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    from screening import get_job
    return JSONResponse(get_job(ws_id) or {"status": "idle"})


@app.post("/w/{ws_id}/records/{rid}/screen1")
async def override_screen1(ws_id: int, rid: int, decision: str = Form(...),
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if decision not in ("include", "exclude", "pending"):
        raise HTTPException(400, "Invalid decision")
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if rec:
        from datetime import datetime
        rec.screen1_decision = decision
        rec.screen1_by = "user"
        rec.screen1_reason = "manual override"
        rec.screen1_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/screening?decision={decision}", status_code=302)


# ── Full text: download + paper2md (steps 6-7) ──────────────────────────────

@app.post("/w/{ws_id}/fulltext/run")
async def run_fulltext(ws_id: int, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    import fulltext
    job = fulltext.get_job(ws.id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "Full-text fetch already in progress")
    email = os.environ.get("UNPAYWALL_EMAIL") or ws.owner.email
    fulltext.start_fulltext(ws.id, email, fulltext.paper2md_url())
    return RedirectResponse(f"/w/{ws_id}/screening?decision=include", status_code=302)


@app.get("/w/{ws_id}/fulltext/status")
async def fulltext_status(ws_id: int, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    import fulltext
    return JSONResponse(fulltext.get_job(ws_id) or {"status": "idle"})


@app.post("/w/{ws_id}/records/{rid}/fulltext/upload")
async def upload_fulltext(ws_id: int, rid: int, file: UploadFile = File(...),
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec:
        raise HTTPException(404, "Record not found")
    pdf_bytes = await file.read()
    if pdf_bytes[:4] != b"%PDF":
        raise HTTPException(400, "File does not look like a PDF")
    import fulltext
    try:
        fulltext.ingest_uploaded_pdf(db, ws.id, rec, pdf_bytes, fulltext.paper2md_url())
    except Exception as exc:
        raise HTTPException(502, f"Conversion failed: {exc}")
    return RedirectResponse(f"/w/{ws_id}/screening?decision=include", status_code=302)


# ── Assessment: combined screening 2 + assessment (steps 8-9) ───────────────

@app.get("/w/{ws_id}/assessment", response_class=HTMLResponse)
async def assessment_page(ws_id: int, request: Request, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import Assessment
    records = (db.query(Record)
                 .filter(Record.workspace_id == ws.id, Record.is_removed == False,  # noqa: E712
                         Record.screen1_decision == "include").order_by(Record.id.desc()).all())
    findings = {}
    for a in db.query(Assessment).filter(Assessment.workspace_id == ws.id).all():
        findings.setdefault(a.record_id, []).append(a)
    ready = sum(1 for r in records if r.full_text_status == "converted"
                and r.screen2_decision == "pending")
    return render(request, "workspace_assessment.html", {
        "user": user, "ws": ws, "tab": "assessment", "records": records,
        "findings": findings, "ready": ready,
        "n_assessment": len(workspace_criteria(db, ws, "assessment")),
        "has_key": bool(_user_api_key(user)),
    })


@app.post("/w/{ws_id}/assessment/run")
async def run_assessment(ws_id: int, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    import assessment
    job = assessment.get_job(ws.id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "Assessment already in progress")
    assessment.start_assessment(ws.id, api_key, user.id)
    return RedirectResponse(f"/w/{ws_id}/assessment", status_code=302)


@app.get("/w/{ws_id}/assessment/status")
async def assessment_status(ws_id: int, user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    import assessment
    return JSONResponse(assessment.get_job(ws_id) or {"status": "idle"})


# ── Synthesis (step 10) ─────────────────────────────────────────────────────

@app.get("/w/{ws_id}/synthesis", response_class=HTMLResponse)
async def synthesis_page(ws_id: int, request: Request, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import Synthesis
    syn = db.query(Synthesis).filter(Synthesis.workspace_id == ws.id).first()
    prisma = json.loads(syn.prisma_json) if (syn and syn.prisma_json) else None
    blocks = sorted(syn.blocks, key=lambda b: b.position) if syn else []
    shares = db.query(PublicShare).filter(PublicShare.workspace_id == ws.id,
                                          PublicShare.active == True).all()  # noqa: E712
    return render(request, "workspace_synthesis.html", {
        "user": user, "ws": ws, "tab": "synthesis", "syn": syn, "prisma": prisma,
        "blocks": blocks, "shares": shares, "has_key": bool(_user_api_key(user)),
        "is_owner": ws.owner_id == user.id or user.is_admin,
    })


@app.post("/w/{ws_id}/synthesis/run")
async def run_synthesis(ws_id: int, user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    import synthesis
    job = synthesis.get_job(ws.id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "Synthesis already in progress")
    synthesis.start_synthesis(ws.id, api_key, user.id)
    return RedirectResponse(f"/w/{ws_id}/synthesis", status_code=302)


@app.get("/w/{ws_id}/synthesis/status")
async def synthesis_status(ws_id: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    import synthesis
    return JSONResponse(synthesis.get_job(ws_id) or {"status": "idle"})


@app.post("/w/{ws_id}/synthesis/publish")
async def toggle_publish(ws_id: int, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Owner required")
    from models import Synthesis
    syn = db.query(Synthesis).filter(Synthesis.workspace_id == ws.id).first()
    if syn:
        syn.published = not syn.published
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/synthesis", status_code=302)


@app.get("/health")
async def health():
    return {"ok": True}
