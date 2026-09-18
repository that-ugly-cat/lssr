"""
LSSR — Living Systematic Scoping Review.

Three surfaces, three credentials:
- the web app, where the pipeline is run and every human decision is made
  (session cookie, or an SSO gate in front — see auth.py);
- `/r/{token}`, the public read-only dashboard of one review, which asks nobody
  who they are;
- `/mcp`, the model-facing read-only surface, gated by a per-user API key, with
  the `/mcp/k/{key}` capability-URL variant for clients that cannot send custom
  headers (see mcp_app.py). It reads exactly what its owner reads, and writes
  nothing.
"""
import contextlib
import json
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import (
    check_api_key, create_token, gateway_mode as auth_gateway_mode, get_current_user,
    get_user_or_none, hash_password, require_admin, set_caller, verify_password,
)
from authors import author_key, canonicalize, split_authors
from models import (
    ApiKey,
    DECIDING_KINDS,
    DATABASES, DB_LABELS, HARVEST_DBS, PIPELINE_STEPS, PRICING, SOURCE_DBS, Criterion,
    Import, PublicShare, RawReference, Record, SessionLocal, User, Workspace, WorkspaceMember,
    can_access,
    current_iteration, db_label, db_search_url, get_db, get_query, init_db,
    earmarks_by_record, my_earmark_ids, set_earmark,
    human_voted_subq, new_share_token, recompact_criteria, screen2_required,
    shadow_agreement, set_step_done,
    set_workspace_targets,
    DUPLICABLE_PARTS, delete_workspace, duplicate_workspace, workspace_footprint,
    upsert_query, user_workspaces, workspace_criteria, workspace_steps_done,
    workspace_target_dbs, workspace_years,
)

from mcp_app import mcp  # noqa: E402


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    """The MCP session manager has to be running for /mcp to answer at all."""
    async with mcp.session_manager.run():
        yield


BASE = Path(__file__).parent
app = FastAPI(title="LSSR", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _md(text: str) -> str:
    import markdown as _mdlib
    from markupsafe import Markup
    return Markup(_mdlib.markdown(text or "", extensions=["extra", "nl2br"]))


templates.env.filters["markdown"] = _md


def _fromjson(raw):
    try:
        return json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []


templates.env.filters["fromjson"] = _fromjson

from synthesis import compute_prisma as _compute_prisma  # noqa: E402
from synthesis import prisma_svg as _prisma_svg  # noqa: E402
templates.env.globals["prisma_svg"] = _prisma_svg

init_db()


# ── The MCP surface ─────────────────────────────────────────────────────────

# The MCP transport checks the Host header against DNS rebinding, so the public
# domain has to be allowed or every proxied request is refused.
def _allowed_hosts() -> list[str]:
    from urllib.parse import urlparse
    hosts = ["localhost:8013", "127.0.0.1:8013", "localhost", "127.0.0.1"]
    public = urlparse(os.environ.get("PUBLIC_URL", "")).netloc
    if public:
        hosts.append(public)
    return hosts


from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402

app.mount("/mcp", mcp.streamable_http_app(
    streamable_http_path="/", json_response=True, stateless_http=True,
    transport_security=TransportSecuritySettings(
        allowed_hosts=_allowed_hosts(),
        allowed_origins=[os.environ.get("PUBLIC_URL", "http://localhost:8013")])))


@app.middleware("http")
async def api_key_gate(request: Request, call_next):
    """
    Resolve the MCP caller, or refuse.

    Two ways in, one table. The header is the normal path; `/mcp/k/{key}`
    carries the same key as a path segment for clients that cannot set custom
    headers, and it is stripped before the mounted app sees it — so the MCP
    layer never learns how the caller authenticated.

    Nothing else on this app is touched: the cookie session still guards the web
    routes, and `/r/{token}` still guards nothing on purpose.
    """
    path = request.url.path
    if not path.startswith("/mcp"):
        return await call_next(request)

    if path.startswith("/mcp/k/"):
        key, _, rest = path[len("/mcp/k/"):].partition("/")
        request.scope["path"] = "/mcp/" + rest
        request.scope["raw_path"] = request.scope["path"].encode()
    else:
        key = request.headers.get("X-API-Key", "")

    # A client configured without the trailing slash would get a 307 from the
    # mount, and MCP clients do not follow redirects on POST. Worse, behind the
    # proxy the redirect comes back as `http`, because uvicorn does not know the
    # original scheme. Normalising here means both spellings of the endpoint
    # work. Same fix as Grant Radar's.
    if request.scope["path"] == "/mcp":
        request.scope["path"] = "/mcp/"
        request.scope["raw_path"] = b"/mcp/"

    db = SessionLocal()
    try:
        row = check_api_key(db, key)
        set_caller(row.user if row else None, bool(row and row.can_write))
    finally:
        db.close()
    if not row:
        return JSONResponse({"error": "missing or invalid API key"}, status_code=401)
    return await call_next(request)


# Unauthenticated HTML requests get bounced to /login instead of a raw 401 JSON.
@app.exception_handler(HTTPException)
async def auth_redirect(request: Request, exc: HTTPException):
    """Un 401 su una pagina, in un browser, diventa il login.

    **Tranne in `gateway`**, dove non deve. La' `/login` l'app lo spegne da se'
    e rimanda a `/app`: mandarci un 401 chiuderebbe un anello — `/app` -> 401
    -> `/login` -> `/app` — da cui non si esce. In produzione non si vede,
    perche' il gate intercetta prima che la richiesta arrivi qui; ma se il
    matcher del proxy fosse sbagliato si girerebbe a vuoto invece di ricevere
    un errore, e un anello e' molto piu' difficile da diagnosticare.

    Il messaggio e' quello di Onopedia: parla all'**operatore**, perche' in
    `gateway` una richiesta senza identita' significa che il gate non ha
    girato — un guasto di configurazione, non della persona.
    """
    accepts_html = "text/html" in request.headers.get("accept", "")
    if exc.status_code == status.HTTP_401_UNAUTHORIZED and accepts_html:
        if auth_gateway_mode():
            exc = HTTPException(status_code=503, detail=(
                "Gateway mode: no valid identity in the X-Borant-* headers. Check "
                "that the gate really sits in front of this app and that "
                "BORANT_TRUSTED_PROXY lists the address the proxy connects from."))
        else:
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


_PUBLISHER_FIELDS = {
    "elsevier": ("elsevier_key_encrypted", "ELSEVIER_API_KEY"),
    "elsevier_insttoken": ("elsevier_insttoken_encrypted", "ELSEVIER_INSTTOKEN"),
    "springer": ("springer_key_encrypted", "SPRINGER_API_KEY"),
    "wiley": ("wiley_token_encrypted", "WILEY_TDM_TOKEN"),
}


def _user_publisher_keys(user: User) -> dict:
    """The reviewer's own publisher TDM credentials, falling back to a
    server-wide env default. A publisher with no credential is left out, and
    fulltext skips it entirely."""
    import crypto
    keys = {}
    for name, (column, env) in _PUBLISHER_FIELDS.items():
        value = ""
        stored = getattr(user, column, None)
        if stored:
            try:
                value = crypto.decrypt(stored)
            except Exception:
                value = ""
        keys[name] = value or os.environ.get(env, "").strip()
    return keys


def _apply_record_filters(query, q: str, source: str, rtype: str, yf: str, yt: str,
                          sort: str, order: str):
    """Shared filter + sort for the Records and Screening tables."""
    from sqlalchemy import or_
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(Record.title.ilike(like), Record.authors.ilike(like),
                                 Record.abstract.ilike(like)))
    if source:
        query = query.filter(Record.source_dbs_json.like(f'%"{source}"%'))
    if rtype:
        query = query.filter(Record.type == rtype)
    if yf.isdigit():
        query = query.filter(Record.year >= int(yf))
    if yt.isdigit():
        query = query.filter(Record.year <= int(yt))
    col = {"year": Record.year, "title": Record.title, "id": Record.id}.get(sort, Record.year)
    direction = (col.desc() if order == "desc" else col.asc()).nullslast()
    return query.order_by(direction, Record.id.desc())


def _dbs_present(db, ws_id: int) -> list:
    return sorted({d for (raw,) in
                   db.query(Record.source_dbs_json).filter(Record.workspace_id == ws_id).all()
                   for d in _fromjson(raw)})


# ── Auth ────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: int = 0):
    # In gateway mode the app switches its own login off rather than relying on
    # the proxy to hide it: two sets of credentials for one review platform is
    # exactly what the SSO is there to remove.
    if auth_gateway_mode():
        return RedirectResponse("/app", status_code=302)
    return render(request, "login.html", {"user": None, "error": bool(error)})


@app.post("/login")
async def login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if auth_gateway_mode():
        return RedirectResponse("/app", status_code=302)
    user = db.query(User).filter(User.email == email.strip().lower(),
                                 User.is_active == True).first()  # noqa: E712
    if not user or not verify_password(password, user.hashed_password):
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/app", status_code=302)
    resp.set_cookie("session", create_token(user.id), httponly=True, samesite="lax",
                    max_age=86400 * 7)
    return resp


BORANT_LOGOUT_URL = os.environ.get("BORANT_LOGOUT_URL", "https://id.borant.eu/logout")


@app.get("/logout")
async def logout():
    # In gateway mode dropping the local cookie is not signing out: the gate
    # still holds the session, and the next click walks straight back in.
    target = BORANT_LOGOUT_URL if auth_gateway_mode() else "/login"
    resp = RedirectResponse(target, status_code=302)
    resp.delete_cookie("session")
    return resp


# ── Workspaces ──────────────────────────────────────────────────────────────

@app.get("/guide", response_class=HTMLResponse)
def guide(request: Request):
    """The user guide, rendered from docs/guide.md, and public like the landing.

    It is linked from the card on borant.eu/tools, where the reader has no
    account yet and is deciding whether the tool is worth asking for one. Behind
    the gate it would document the tool to the people who already use it, which
    is the wrong half of the audience. One source of truth: the markdown in the
    repository, so the page cannot drift from what ships.
    """
    import markdown
    md_text = open(BASE / "docs" / "guide.md", encoding="utf-8").read()
    return templates.TemplateResponse(request, "guide.html", {
        "guide_html": markdown.markdown(md_text, extensions=["tables", "fenced_code"]),
        "app_name": "LSSR",
        "app_url": "/app",
    })


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """La vetrina pubblica. Sta fuori dal gate e **non guarda mai chi sei**.

    Nessuna dipendenza da un utente e nessuna lettura dal database: sul ramo
    pubblico l'identita' viene tolta per costruzione, quindi un ramo su `user`
    sarebbe sempre falso in `gateway` e a volte vero in `local` — la stessa
    pagina con due comportamenti. E non mostra numeri interni: quante review,
    quanti record, quanti revisori.
    """
    return render(request, "landing.html", {"user": None})


@app.get("/app", response_class=HTMLResponse)
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


@app.post("/w/{ws_id}/duplicate")
async def duplicate_review(ws_id: int, name: str = Form(...),
                           parts: list[str] = Form([]),
                           user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    """Start a new review from an existing one: the protocol, never the corpus.

    Which parts come along is the caller's choice (see DUPLICABLE_PARTS); the
    records and every human decision taken on them stay behind by construction,
    so a duplicate is an empty review that already knows what it is looking for.
    """
    ws = _load_ws(db, user, ws_id)
    if not name.strip():
        raise HTTPException(400, "Name required")
    unknown = [p for p in parts if p not in DUPLICABLE_PARTS]
    if unknown:
        raise HTTPException(400, f"Unknown part: {unknown[0]}")
    new_ws = duplicate_workspace(db, ws, user, name, parts)
    return RedirectResponse(f"/w/{new_ws.id}", status_code=302)


@app.post("/w/{ws_id}/delete")
async def delete_review(ws_id: int, confirm_name: str = Form(""),
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    """Delete a review and everything in it. Owner only, and irreversible.

    The confirmation is the review's own name, typed: a review is deleted about
    once in its life and the click that does it sits under settings people open
    for other reasons, so the guard has to cost more than a reflex.
    """
    ws = db.query(Workspace).filter(Workspace.id == ws_id).first()
    if not ws or not can_access(db, user, ws):
        raise HTTPException(404, "Workspace not found")
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Owner required")
    if confirm_name.strip() != (ws.name or "").strip():
        return RedirectResponse(f"/w/{ws_id}/settings?delete_error=1", status_code=302)
    delete_workspace(db, ws)
    return RedirectResponse("/app", status_code=302)


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
        "members": members, "shares": shares, "steps_done": workspace_steps_done(ws),
        "iterations": iterations, "iter_new": iter_new,
        "prisma": _compute_prisma(db, ws.id),
    })


@app.post("/w/{ws_id}/steps/{step}/toggle")
async def toggle_step_done(ws_id: int, step: str, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if step not in PIPELINE_STEPS:
        raise HTTPException(400, "Unknown step")
    set_step_done(db, ws, step, step not in workspace_steps_done(ws))
    return RedirectResponse(f"/w/{ws_id}/{step}", status_code=302)


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


def _year_hist(years):
    """Counter of year→count → a filled, contiguous histogram."""
    if not years:
        return [], None, None
    ymin, ymax = min(years), max(years)
    peak = max(years.values())
    hist = [{"year": y, "count": years.get(y, 0),
             "pct": round(years.get(y, 0) / peak * 100) if peak else 0}
            for y in range(ymin, ymax + 1)]
    return hist, ymin, ymax


def _bars(counter, top_n=None):
    """Counter → sorted [{label, count, pct}] for a horizontal bar chart."""
    items = counter.most_common(top_n) if top_n else counter.most_common()
    peak = items[0][1] if items else 1
    return [{"label": str(k), "count": c, "pct": round(c / peak * 100)} for k, c in items]


def _public_stats(db, ws_id: int) -> dict:
    """Everything the public page charts: records, screening, full-text, and the
    extraction aggregates over the finally-included papers."""
    from collections import Counter

    from models import authoritative_values
    recs = db.query(Record).filter(Record.workspace_id == ws_id,
                                   Record.is_removed == False).all()  # noqa: E712

    # ── records per source database (pie) ──
    src = Counter(d for r in recs for d in _fromjson(r.source_dbs_json))
    palette = ["#63b3ed", "#68d391", "#f6ad55", "#b794f4", "#fc8181",
               "#4fd1c5", "#f687b3", "#90cdf4", "#fbd38d", "#a0aec0"]
    src_total = sum(src.values())
    sources, acc = [], 0.0
    for i, (d, c) in enumerate(src.most_common()):
        start = acc
        acc += c / src_total * 100
        sources.append({"label": "Manual entry" if d == "manual" else db_label(d),
                        "count": c, "color": palette[i % len(palette)],
                        "start": round(start, 2), "end": round(acc, 2)})

    # ── records ──
    year_hist, ymin, ymax = _year_hist(Counter(r.year for r in recs if r.year))
    type_lbl = {"article": "papers", "book": "books",
                "book_chapter": "book chapters", "grey": "grey literature"}
    type_counts = [{"label": type_lbl.get(t, t), "count": c}
                   for t, c in Counter(r.type or "article" for r in recs).most_common()]
    kw = Counter()
    author_counts = Counter()   # merge key → count ('Mario Rossi' + 'Rossi M')
    author_forms = {}           # merge key → Counter of the forms seen
    for r in recs:
        for k in _fromjson(r.keywords_json):
            term = (k or "").strip().lower()
            if term:
                kw[term] += 1
        for a in split_authors(r.authors):
            if len(a) > 1:
                key = author_key(a)
                author_counts[key] += 1
                author_forms.setdefault(key, Counter())[a] += 1
    authors = Counter()
    for key, c in author_counts.items():
        authors[author_forms[key].most_common(1)[0][0]] += c
    top = kw.most_common(45)
    kwpeak = top[0][1] if top else 1
    keywords = [{"term": t, "count": c, "size": round(0.85 + 1.75 * (c / kwpeak), 2)}
                for t, c in top]

    # ── screening 1 & 2 ──
    def sc(field, d):
        return sum(1 for r in recs if getattr(r, field) == d)
    screen = {d: sc("screen1_decision", d) for d in ("include", "maybe", "exclude", "conflict", "pending")}
    screen["total"] = len(recs)
    s1_incl = [r for r in recs if r.screen1_decision == "include"]
    screen2 = {d: sum(1 for r in s1_incl if r.screen2_decision == d)
               for d in ("include", "maybe", "exclude", "conflict", "pending")}
    screen2["total"] = len(s1_incl)

    # ── full-text pie (over screen-1 included) ──
    ftc = Counter(("converted" if r.full_text_status == "converted"
                   else "oalink" if r.full_text_status == "url"
                   else "none") for r in s1_incl)
    fulltext = {"converted": ftc.get("converted", 0), "oalink": ftc.get("oalink", 0),
                "none": ftc.get("none", 0), "total": len(s1_incl)}

    # ── extraction aggregates over the finally-included papers ──
    included = [r for r in recs if r.screen2_decision == "include"]
    ext = {r.id: authoritative_values(db, r) for r in included}
    def fcount(key):
        c = Counter()
        for r in included:
            v = ext[r.id].get(key)
            if isinstance(v, list):
                for x in v:
                    c[str(x)] += 1
            elif v not in (None, ""):
                c[str(v)] += 1
        return c
    study_years = Counter()
    for r in included:
        v = str(ext[r.id].get("study_year", "")).strip()
        m = re.search(r"\d{4}", v)
        if m:
            study_years[int(m.group())] += 1
    sy_hist, sy_min, sy_max = _year_hist(study_years)
    assessment = {
        "n": len(included),
        "country": _bars(fcount("country"), 20),
        "study_type": _bars(fcount("study_type")),
        "design": _bars(fcount("methodology_design")),
        "data": _bars(fcount("methodology_data")),
        "time": _bars(fcount("methodology_time")),
        "study_year_hist": sy_hist, "study_year_min": sy_min, "study_year_max": sy_max,
    }
    return {"n_records": len(recs), "year_hist": year_hist, "year_min": ymin,
            "year_max": ymax, "type_counts": type_counts, "keywords": keywords,
            "sources": sources, "sources_overlap": src_total > len(recs),
            "authors": _bars(authors, 25), "screen": screen, "screen2": screen2,
            "fulltext": fulltext, "assessment": assessment}


@app.get("/r/{token}", response_class=HTMLResponse)
async def public_review(token: str, request: Request, db: Session = Depends(get_db)):
    """Public, no-login view. Shows a progress line and — per step marked done —
    the queries, record stats, screening breakdown, and the published synthesis."""
    share = db.query(PublicShare).filter(PublicShare.token == token,
                                         PublicShare.active == True).first()  # noqa: E712
    if not share:
        raise HTTPException(404, "Not found")
    ws = share.workspace
    steps_done = workspace_steps_done(ws)
    from models import Synthesis
    syn = db.query(Synthesis).filter(Synthesis.workspace_id == ws.id,
                                     Synthesis.published == True).first()  # noqa: E712
    prisma = _compute_prisma(db, ws.id)
    blocks = sorted(syn.blocks, key=lambda b: b.position) if syn else []
    queries = []
    if "query" in steps_done:
        queries = [(q.database, q.query_string) for q in
                   sorted(ws.queries, key=lambda q: (q.database != "pubmed", q.database))
                   if q.query_string]
    stats = _public_stats(db, ws.id)
    # The two criterion sets are protocol, not results: they are shown from the
    # start, like the research question, rather than gated on a step being done.
    crits = sorted(ws.criteria, key=lambda c: (c.position or 0, c.id))
    criteria = {k: [c for c in crits if c.kind == k] for k in ("exclusion", "inclusion")}
    # Read-only, and deliberately not ensure_extraction_fields(): seeding is a
    # write, and this route is unauthenticated. A workspace whose Settings page
    # has never been opened simply shows no extraction box.
    from models import workspace_extraction_fields
    fields = workspace_extraction_fields(db, ws)
    field_labels = {f.key: f.label for f in fields}
    return render(request, "public_review.html", {
        "user": None, "ws": ws, "syn": syn, "prisma": prisma, "blocks": blocks,
        "steps": PIPELINE_STEPS, "steps_done": steps_done,
        "queries": queries, "stats": stats, "criteria": criteria,
        "fields": fields, "field_labels": field_labels,
    })


# ── Profile (Anthropic API key) ─────────────────────────────────────────────

@app.get("/profile", response_class=HTMLResponse)
async def profile(request: Request, user: User = Depends(get_current_user),
                  db: Session = Depends(get_db)):
    # which publisher credentials this user has, and which fall back to the server's
    pub = {name: {"user": bool(getattr(user, column, None)),
                  "env": bool(os.environ.get(env, "").strip())}
           for name, (column, env) in _PUBLISHER_FIELDS.items()}
    keys = (db.query(ApiKey).filter(ApiKey.user_id == user.id)
              .order_by(ApiKey.created_at.desc()).all())
    return render(request, "profile.html", {
        "user": user, "has_key": bool(user.api_key_encrypted), "pub": pub,
        "keys": keys,
        "public_url": os.environ.get("PUBLIC_URL", "http://localhost:8013"),
    })


@app.post("/profile/keys")
async def create_mcp_key(name: str = Form(...), can_write: str = Form(""),
                         user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Mint an MCP key for yourself. Keys belong to people and never to the
    deployment: one reaches exactly the reviews its owner is a member of, and
    removing them from a review removes the key's reach with it.

    Writing is decided here and never afterwards. There is no route that flips
    an existing key, on purpose: a capability that can be added later is one
    that can be added to a key already sitting in somebody's config file, and
    the checkbox in front of you is the moment where that gets thought about."""
    db.add(ApiKey(user_id=user.id, name=name.strip() or "mcp",
                  can_write=bool(can_write)))
    db.commit()
    return RedirectResponse("/profile", status_code=302)


@app.post("/profile/keys/{key_id}/revoke")
async def revoke_mcp_key(key_id: int, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    row = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.user_id == user.id).first()
    if row:
        row.active = False
        db.commit()
    return RedirectResponse("/profile", status_code=302)


@app.post("/profile/publisher-key/{name}")
async def set_publisher_key(name: str, value: str = Form(""),
                            user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    """Save one publisher credential; blank clears it — same contract as the
    Anthropic key, so each credential is edited independently."""
    if name not in _PUBLISHER_FIELDS:
        raise HTTPException(400, "Unknown credential")
    import crypto
    v = value.strip()
    setattr(user, _PUBLISHER_FIELDS[name][0], crypto.encrypt(v) if v else None)
    db.commit()
    return RedirectResponse("/profile", status_code=302)


@app.post("/profile/api-key")
async def set_api_key(api_key: str = Form(...), user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    key = api_key.strip()
    import crypto
    user.api_key_encrypted = crypto.encrypt(key) if key else None
    db.commit()
    return RedirectResponse("/profile", status_code=302)


# ── Admin: user management ──────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request, error: str = "", user: User = Depends(require_admin),
                     db: Session = Depends(get_db)):
    users = db.query(User).order_by(User.id).all()
    owned = {u.id: db.query(Workspace).filter(Workspace.owner_id == u.id).count() for u in users}
    return render(request, "admin.html", {"user": user, "users": users, "owned": owned,
                                          "error": error})


@app.post("/admin/users")
async def admin_create_user(email: str = Form(...), name: str = Form(...),
                            password: str = Form(...), is_admin: str = Form(""),
                            user: User = Depends(require_admin), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if not email or not name.strip() or len(password) < 8:
        return RedirectResponse("/admin?error=Email,+name+and+an+8%2B+char+password+are+required",
                                status_code=302)
    if db.query(User).filter(User.email == email).first():
        return RedirectResponse("/admin?error=That+email+already+exists", status_code=302)
    db.add(User(email=email, name=name.strip(), hashed_password=hash_password(password),
                is_admin=bool(is_admin), is_active=True))
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{uid}/toggle-active")
async def admin_toggle_active(uid: int, user: User = Depends(require_admin),
                              db: Session = Depends(get_db)):
    if uid == user.id:
        return RedirectResponse("/admin?error=You+can%27t+deactivate+yourself", status_code=302)
    target = db.query(User).filter(User.id == uid).first()
    if target:
        target.is_active = not target.is_active
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{uid}/toggle-admin")
async def admin_toggle_admin(uid: int, user: User = Depends(require_admin),
                             db: Session = Depends(get_db)):
    if uid == user.id:
        return RedirectResponse("/admin?error=You+can%27t+change+your+own+admin+flag", status_code=302)
    target = db.query(User).filter(User.id == uid).first()
    if target:
        target.is_admin = not target.is_admin
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/users/{uid}/reset-password")
async def admin_reset_password(uid: int, password: str = Form(...),
                               user: User = Depends(require_admin), db: Session = Depends(get_db)):
    if len(password) < 8:
        return RedirectResponse("/admin?error=Password+must+be+8%2B+characters", status_code=302)
    target = db.query(User).filter(User.id == uid).first()
    if target:
        target.hashed_password = hash_password(password)
        db.commit()
    return RedirectResponse("/admin", status_code=302)


# ── Query strategy (steps 1-2) ──────────────────────────────────────────────

def _harvest_module(database: str):
    """Resolve a harvestable database key to its source module (uniform
    start/get_job interface). None for translation-only databases."""
    import eric
    import europepmc
    import openalex
    import pubmed
    return {"pubmed": pubmed, "europepmc": europepmc,
            "openalex": openalex, "eric": eric}.get(database)


def _harvest_state(ws_id: int, database: str) -> dict:
    mod = _harvest_module(database)
    return (mod.get_job(ws_id) if mod else None) or {"status": "idle"}


@app.get("/w/{ws_id}/query", response_class=HTMLResponse)
async def query_page(ws_id: int, request: Request, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from ingest import term_frequencies
    primary_db = ws.primary_db or "pubmed"
    primary = get_query(db, ws, primary_db)
    targets = workspace_target_dbs(ws)                       # active targets, minus primary
    yf, yt = workspace_years(ws)
    # start-from is limited to the two source databases; keep a legacy primary
    # visible if some older workspace points elsewhere.
    source_keys = SOURCE_DBS + ([primary_db] if primary_db not in SOURCE_DBS else [])
    db_options = [(d, db_label(d)) for d in source_keys]
    target_choices = [(d, db_label(d), d in targets, d in HARVEST_DBS)
                      for d in DATABASES if d != primary_db]
    # translation windows for the selected targets, each with its saved query
    translations = [(d, db_label(d), get_query(db, ws, d), d in HARVEST_DBS, db_search_url(d))
                    for d in targets]
    # harvest job state for every active harvestable source (primary + targets)
    harvest_states = {d: _harvest_state(ws.id, d)
                      for d in ([primary_db] + targets) if d in HARVEST_DBS}
    freqs = term_frequencies(db, ws.id, top_n=30)
    return render(request, "workspace_query.html", {
        "user": user, "ws": ws, "tab": "query", "steps_done": workspace_steps_done(ws),
        "primary_db": primary_db, "primary_label": db_label(primary_db), "primary": primary,
        "primary_search_url": db_search_url(primary_db),
        "year_from": yf, "year_to": yt,
        "db_options": db_options, "target_choices": target_choices,
        "translations": translations, "harvest_states": harvest_states,
        "harvest_dbs": HARVEST_DBS, "freqs": freqs,
        "has_key": bool(_user_api_key(user)),
    })


@app.post("/w/{ws_id}/query/primary")
async def save_primary_query(ws_id: int, primary_db: str = Form(...), query: str = Form(""),
                             year_from: int = Form(...), year_to: int = Form(...),
                             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if primary_db not in SOURCE_DBS:
        raise HTTPException(400, "Start-from must be PubMed or OpenAlex")
    if year_from > year_to:
        year_from, year_to = year_to, year_from
    ws.primary_db = primary_db
    ws.year_from, ws.year_to = year_from, year_to
    db.commit()
    upsert_query(db, ws, primary_db, query.strip())
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.post("/w/{ws_id}/query/targets")
async def save_targets(ws_id: int, targets: list[str] = Form(default=[]),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    set_workspace_targets(db, ws, targets)
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.post("/w/{ws_id}/harvest/{database}/run")
async def run_harvest(ws_id: int, database: str, user: User = Depends(get_current_user),
                      db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if database not in HARVEST_DBS:
        raise HTTPException(400, "This database can't be harvested directly")
    q = get_query(db, ws, database)
    if not q or not q.query_string:
        raise HTTPException(400, f"Save a {db_label(database)} query first")
    # one harvest at a time per workspace (shared SQLite writer)
    for hdb in HARVEST_DBS:
        j = _harvest_state(ws.id, hdb)
        if j.get("status") in ("searching", "downloading"):
            raise HTTPException(409, "A harvest run is already in progress")
    yf, yt = workspace_years(ws)
    _harvest_module(database).start(ws.id, q.query_string, yf, yt, user.id)
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.get("/w/{ws_id}/harvest/{database}/status")
async def harvest_status(ws_id: int, database: str, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    return JSONResponse(_harvest_state(ws_id, database))


@app.post("/w/{ws_id}/query/{database}/save")
async def save_translation(ws_id: int, database: str, query: str = Form(""),
                           user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if database not in DATABASES or database == (ws.primary_db or "pubmed"):
        raise HTTPException(400, "Invalid database")
    upsert_query(db, ws, database, query.strip())
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


@app.post("/w/{ws_id}/query/{database}/translate")
async def translate_route(ws_id: int, database: str, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    source_db = ws.primary_db or "pubmed"
    source = get_query(db, ws, source_db)
    if not source or not source.query_string:
        raise HTTPException(400, f"Save a {db_label(source_db)} query first")
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    from translate import translate_query
    yf, yt = workspace_years(ws)
    try:
        translated = translate_query(api_key, source.query_string, database,
                                     source_db=source_db, year_from=yf, year_to=yt,
                                     apply_years=(database not in HARVEST_DBS))
    except Exception as exc:
        raise HTTPException(502, f"Translation failed: {exc}")
    upsert_query(db, ws, database, translated)
    return RedirectResponse(f"/w/{ws_id}/query", status_code=302)


# ── Records pool (steps 3-4) ────────────────────────────────────────────────

@app.get("/w/{ws_id}/records", response_class=HTMLResponse)
async def records_page(ws_id: int, request: Request,
                       q: str = "", source: str = "", rtype: str = "",
                       yf: str = "", yt: str = "", sort: str = "year", order: str = "desc",
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    query = db.query(Record).filter(Record.workspace_id == ws.id,
                                    Record.is_removed == False)  # noqa: E712
    query = _apply_record_filters(query, q, source, rtype, yf, yt, sort, order)
    records = query.limit(500).all()

    active_n = db.query(Record).filter(Record.workspace_id == ws.id,
                                       Record.is_removed == False).count()  # noqa: E712
    imports = db.query(Import).filter(Import.workspace_id == ws.id).order_by(
        Import.created_at.desc()).limit(10).all()
    dbs_present = _dbs_present(db, ws.id)
    return render(request, "workspace_records.html", {
        "user": user, "ws": ws, "tab": "records", "steps_done": workspace_steps_done(ws),
        "records": records, "active_n": active_n,
        "imports": imports, "dbs_present": dbs_present,
        "db_options": [(d, db_label(d)) for d in DATABASES],
        "filters": {"q": q, "source": source, "rtype": rtype,
                    "yf": yf, "yt": yt, "sort": sort, "order": order},
    })


def _db_label(database: str, other_name: str) -> str:
    """Resolve the provenance label: a custom name when 'other' is picked."""
    if database == "other" and other_name.strip():
        return other_name.strip()[:60]
    return database


@app.post("/w/{ws_id}/records/import")
async def import_file(ws_id: int, file: UploadFile = File(...), database: str = Form("scopus"),
                      other_name: str = Form(""), via_other: str = Form(""), note: str = Form(""),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    # utf-8-sig, not utf-8: Web of Science exports carry a BOM, and a leading
    # U+FEFF makes rispy miss the first record's "TY  - " and drop it silently.
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    from ingest import ingest_references, parse_file
    try:
        refs = parse_file(file.filename or "upload", raw)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse file: {exc}")
    it = current_iteration(db, ws)
    name = (file.filename or "").lower()
    if name.endswith(".nbib") or raw.lstrip().startswith("PMID- "):
        fmt = "medline"
    elif name.endswith(".ris") or raw.lstrip().startswith("TY  -"):
        fmt = "ris"
    else:
        fmt = "bibtex"
    ingest_references(db, ws, it, refs, database=_db_label(database, other_name), fmt=fmt,
                      source_name=file.filename, user_id=user.id,
                      via_other_methods=bool(via_other), note=note.strip() or None)
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


# ── Excel import: upload → map columns → ingest ─────────────────────────────

_IMPORT_TMP = Path("data/import_tmp")


@app.post("/w/{ws_id}/records/import/excel")
async def import_excel_preview(ws_id: int, request: Request, file: UploadFile = File(...),
                               database: str = Form("scopus"), other_name: str = Form(""),
                               via_other: str = Form(""), note: str = Form(""),
                               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    data = await file.read()
    from ingest import EXCEL_FIELDS, read_excel
    try:
        cols, sample, total = read_excel(data)
    except Exception as exc:
        raise HTTPException(400, f"Could not read spreadsheet: {exc}")
    # stash the file server-side between the two steps; clean up stale ones first
    import time
    import uuid
    tmp_dir = _IMPORT_TMP / str(ws.id)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for old in tmp_dir.glob("*.xlsx"):
        if time.time() - old.stat().st_mtime > 3600:
            old.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    (tmp_dir / f"{token}.xlsx").write_bytes(data)
    # best-effort auto-mapping: match our field names against the headers
    lc = {c.lower(): c for c in cols}
    guess = {}
    for field, _ in EXCEL_FIELDS:
        for cand in (field, field.rstrip("s"), {"source": "journal", "authors": "author"}.get(field, field)):
            if cand in lc:
                guess[field] = lc[cand]
                break
    return render(request, "workspace_import_map.html", {
        "user": user, "ws": ws, "tab": "records", "steps_done": workspace_steps_done(ws),
        "token": token,
        "columns": cols, "sample": sample, "total": total,
        "fields": EXCEL_FIELDS, "guess": guess,
        "database": database, "other_name": other_name,
        "via_other": via_other, "note": note,
    })


@app.post("/w/{ws_id}/records/import/excel/apply")
async def import_excel_apply(ws_id: int, request: Request, token: str = Form(...),
                             database: str = Form("scopus"), other_name: str = Form(""),
                             via_other: str = Form(""), note: str = Form(""),
                             type_col: str = Form(""), default_type: str = Form("article"),
                             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if not token.isalnum():
        raise HTTPException(400, "Bad token")
    path = _IMPORT_TMP / str(ws.id) / f"{token}.xlsx"
    if not path.exists():
        raise HTTPException(400, "Upload expired — please re-upload the file")
    from ingest import EXCEL_FIELDS, excel_to_refs, ingest_references
    form = await request.form()
    mapping = {field: (form.get(f"map_{field}") or "").strip() for field, _ in EXCEL_FIELDS}
    if not mapping.get("title"):
        raise HTTPException(400, "Map a column to Title first")
    try:
        refs = excel_to_refs(path.read_bytes(), mapping, type_col or None, default_type)
    except Exception as exc:
        raise HTTPException(400, f"Could not read spreadsheet: {exc}")
    it = current_iteration(db, ws)
    ingest_references(db, ws, it, refs, database=_db_label(database, other_name), fmt="excel",
                      source_name=None, user_id=user.id,
                      via_other_methods=bool(via_other), note=note.strip() or None)
    path.unlink(missing_ok=True)
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


def _parse_year(year: str):
    return int("".join(c for c in year if c.isdigit())[:4]) if any(c.isdigit() for c in year) else None


@app.post("/w/{ws_id}/records/add")
async def add_record(ws_id: int, request: Request, title: str = Form(...), authors: str = Form(""),
                     year: str = Form(""), doi: str = Form(""), url: str = Form(""),
                     abstract: str = Form(""), source: str = Form(""), type: str = Form("article"),
                     note: str = Form(""),
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    it = current_iteration(db, ws)
    from ingest import canonical_key, normalize_doi
    yr = _parse_year(year)
    ref = {"type": type, "authors": canonicalize(authors), "year": yr, "title": title.strip(),
           "abstract": abstract.strip(), "doi": doi.strip(), "url": url.strip(),
           "source": source.strip(), "keywords": [], "mesh": [], "language": ""}
    rec = Record(workspace_id=ws.id, type=type, authors=canonicalize(authors) or None, year=yr,
                 title=title.strip(), abstract=abstract.strip() or None,
                 doi=normalize_doi(doi) or None, url=url.strip() or None, source=source.strip() or None,
                 keywords_json="[]", mesh_json="[]",
                 source_dbs_json=json.dumps(["manual"]), canonical_key=canonical_key(ref) or None,
                 added_manually=True, first_seen_iter_id=it.id, last_seen_iter_id=it.id)
    db.add(rec)
    db.flush()
    # A record typed in by hand is the other-methods arm in miniature, and it
    # used to leave no RawReference at all: it was counted in screening but was
    # invisible in Identification, so 'without duplicates' and 'screened' did
    # not agree and the difference looked like dedup. One reference row per
    # hand-added record fixes the arithmetic and puts it in the right arm.
    imp = Import(workspace_id=ws.id, iteration_id=it.id, database="manual", fmt="manual",
                 source_name=(title.strip()[:120] or None), raw_count=1, new_count=1,
                 via_other_methods=True, note=note.strip() or None, created_by_id=user.id)
    db.add(imp)
    db.flush()
    db.add(RawReference(workspace_id=ws.id, import_id=imp.id, record_id=rec.id,
                        database="manual", via_other_methods=True,
                        canonical_key=rec.canonical_key, raw_json=json.dumps(ref)))
    db.commit()
    if _from_script(request):
        return JSONResponse({"id": rec.id, "title": rec.title})
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


@app.post("/w/{ws_id}/records/{rid}/edit")
async def edit_record(ws_id: int, rid: int, request: Request, title: str = Form(...), authors: str = Form(""),
                      year: str = Form(""), doi: str = Form(""), url: str = Form(""),
                      abstract: str = Form(""), source: str = Form(""), type: str = Form("article"),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec:
        raise HTTPException(404, "Record not found")
    from ingest import canonical_key, normalize_doi
    if type not in ("article", "book", "book_chapter", "grey"):
        type = "article"
    rec.title = title.strip()
    rec.authors = canonicalize(authors) or None
    rec.year = _parse_year(year)
    rec.doi = normalize_doi(doi) or None
    rec.url = url.strip() or None
    rec.source = source.strip() or None
    rec.type = type
    rec.abstract = abstract.strip() or None
    rec.canonical_key = canonical_key({"doi": rec.doi, "title": rec.title, "year": rec.year}) or None
    db.commit()
    if _from_script(request):
        return JSONResponse({"id": rec.id, "title": rec.title})
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


def _delete_records(db, ws, recs) -> str | None:
    """Hard-delete records and their orphaned screen/extraction rows.

    Returns an undo token: the rows are written out before they go, so the ten
    seconds after a mis-click are recoverable without making the deletion itself
    any softer. See undo.py for why the restore keeps the original ids.
    """
    rids = [r.id for r in recs]
    if not rids:
        return None
    import undo
    token = undo.stash(ws.id, undo.snapshot(db, ws.id, recs))
    from models import Earmark, Extraction, ScreenDecision
    db.query(ScreenDecision).filter(ScreenDecision.record_id.in_(rids)).delete(synchronize_session=False)
    db.query(Extraction).filter(Extraction.record_id.in_(rids)).delete(synchronize_session=False)
    db.query(Earmark).filter(Earmark.record_id.in_(rids)).delete(synchronize_session=False)
    for r in recs:
        db.delete(r)     # raw_refs cascade via the relationship
    db.commit()
    return token


@app.post("/w/{ws_id}/records/{rid}/remove")
async def remove_record(ws_id: int, rid: int, request: Request,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Hard delete — at the records stage a removal leaves no trace (removals are
    tracked from screening onward, as exclude decisions)."""
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    token = _delete_records(db, ws, [rec]) if rec else None
    if _from_script(request):
        return JSONResponse({"deleted": 1 if rec else 0, "undo": token})
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


@app.post("/w/{ws_id}/records/remove-batch")
async def remove_records_batch(ws_id: int, request: Request, ids: str = Form(...),
                               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    recs = (db.query(Record).filter(Record.id.in_(id_list), Record.workspace_id == ws.id).all()
            if id_list else [])
    token = _delete_records(db, ws, recs)
    if _from_script(request):
        return JSONResponse({"deleted": len(recs), "undo": token})
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


@app.post("/w/{ws_id}/records/undo-delete")
async def undo_delete(ws_id: int, request: Request, token: str = Form(...),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Put back what the last delete took. Unknown tokens are not an error: an
    undo clicked twice, or after the snapshot expired, has simply nothing to do
    and must not look like a failure."""
    ws = _load_ws(db, user, ws_id)
    import undo
    result = undo.restore(db, ws.id, token)
    if _from_script(request):
        return JSONResponse(result or {"records": 0, "renumbered": 0})
    return RedirectResponse(f"/w/{ws_id}/records", status_code=302)


# ── Manual dedup pass (step 4, on demand) ───────────────────────────────────

@app.post("/w/{ws_id}/records/dedup/run")
async def dedup_run(ws_id: int, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    import dedup
    merged = dedup.auto_dedup(db, ws.id)
    return RedirectResponse(f"/w/{ws_id}/records/dedup?merged={merged}", status_code=302)


@app.get("/w/{ws_id}/records/dedup", response_class=HTMLResponse)
async def dedup_page(ws_id: int, request: Request, merged: int = -1,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    import dedup
    from ingest import _record_as_ref, completeness
    raw = dedup.uncertain_clusters(db, ws.id)
    clusters = []
    for c in raw:
        best = max(c, key=lambda r: (completeness(_record_as_ref(r)), -r.id))
        clusters.append({"records": c, "survivor_id": best.id,
                         "ids": ",".join(str(r.id) for r in c)})
    return render(request, "workspace_dedup.html", {
        "user": user, "ws": ws, "tab": "records", "steps_done": workspace_steps_done(ws),
        "clusters": clusters, "merged": merged,
    })


@app.post("/w/{ws_id}/records/dedup/merge")
async def dedup_merge(ws_id: int, survivor: int = Form(...), ids: str = Form(...),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    recs = db.query(Record).filter(Record.id.in_(id_list), Record.workspace_id == ws.id,
                                   Record.is_removed == False).all()  # noqa: E712
    survivor_rec = next((r for r in recs if r.id == survivor), None)
    if survivor_rec:
        import dedup
        for r in recs:
            if r.id != survivor_rec.id:
                dedup.merge_records(db, survivor_rec, r)
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/records/dedup", status_code=302)


@app.post("/w/{ws_id}/records/dedup/keep")
async def dedup_keep(ws_id: int, ids: str = Form(...),
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
    import dedup
    dedup.dismiss_cluster(db, ws.id, id_list)
    return RedirectResponse(f"/w/{ws_id}/records/dedup", status_code=302)


# ── Settings: criteria & model (steps 5, 8, 9) ──────────────────────────────

@app.get("/w/{ws_id}/settings", response_class=HTMLResponse)
async def settings_page(ws_id: int, request: Request, member_error: int = 0,
                        delete_error: int = 0,
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import ensure_extraction_fields, workspace_extraction_fields
    ensure_extraction_fields(db, ws)
    members = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws.id).all()
    fields = workspace_extraction_fields(db, ws)
    return render(request, "workspace_settings.html", {
        "user": user, "ws": ws, "tab": "settings", "steps_done": workspace_steps_done(ws),
        "exclusion": workspace_criteria(db, ws, "exclusion"),
        "inclusion": workspace_criteria(db, ws, "inclusion"),
        "fields": fields, "field_keys": [f.key for f in fields],
        "field_types": ["text", "textarea", "number", "select", "multiselect"],
        "models": list(PRICING.keys()),
        "members": members, "owner": ws.owner, "member_error": bool(member_error),
        "delete_error": bool(delete_error), "footprint": workspace_footprint(db, ws),
        "is_owner": ws.owner_id == user.id or user.is_admin,
    })


# ── Extraction fields (step 9 schema) ───────────────────────────────────────

@app.post("/w/{ws_id}/fields/add")
async def add_field(ws_id: int, label: str = Form(...), description: str = Form(""),
                    field_type: str = Form("text"), options: str = Form(""),
                    show_if_key: str = Form(""), show_if_values: str = Form(""),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import ExtractionField, slug_field_key, workspace_extraction_fields
    if field_type not in ("text", "textarea", "number", "select", "multiselect") or not label.strip():
        raise HTTPException(400, "Invalid field")
    existing = workspace_extraction_fields(db, ws)
    key = slug_field_key(label, {f.key for f in existing})
    opts = [o.strip() for o in options.splitlines() if o.strip()] \
        if field_type in ("select", "multiselect") else []
    sivals = [v.strip() for v in show_if_values.split(",") if v.strip()]
    pos = max((f.position for f in existing), default=-1) + 1
    db.add(ExtractionField(
        workspace_id=ws.id, key=key, label=label.strip(), help=description.strip() or None,
        field_type=field_type,
        options_json=json.dumps(opts) if opts else None,
        show_if_key=show_if_key.strip() or None,
        show_if_values_json=json.dumps(sivals) if sivals else None,
        builtin=False, position=pos))
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/fields/{fid}/delete")
async def delete_field(ws_id: int, fid: int, user: User = Depends(get_current_user),
                       db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import ExtractionField
    f = db.query(ExtractionField).filter(ExtractionField.id == fid,
                                         ExtractionField.workspace_id == ws.id).first()
    if f:
        db.delete(f)
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/fields/{fid}/move")
async def move_field(ws_id: int, fid: int, dir: str = Form(...),
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import workspace_extraction_fields
    fields = workspace_extraction_fields(db, ws)
    idx = next((i for i, f in enumerate(fields) if f.id == fid), None)
    if idx is not None:
        swap = idx - 1 if dir == "up" else idx + 1
        if 0 <= swap < len(fields):
            fields[idx].position, fields[swap].position = fields[swap].position, fields[idx].position
            db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/settings/model")
async def set_model(ws_id: int, screening_model: str = Form(...),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if screening_model in PRICING:
        ws.screening_model = screening_model
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/settings/details")
async def set_details(ws_id: int, description: str = Form(""), research_question: str = Form(""),
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    ws.description = description.strip() or None
    ws.research_question = research_question.strip() or None
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/settings/screening")
async def set_screening_config(ws_id: int, reviewers_required: int = Form(...),
                               reviewers_required_2: str = Form(""),
                               dry_run_enabled: str = Form(""),
                               user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Owner required")
    # Same rule as stage 2 below, and it was missing here: the cached decision
    # on every record was resolved against the OLD number, so changing it
    # without re-resolving leaves the setting saying one thing and the whole
    # table, the counts, the PRISMA and the export saying another. It is the
    # asymmetry that matters most in exactly the case people change this for —
    # a review configured for two reviewers that has been screened by one,
    # where every decision is still standing on the model's fallback.
    before1 = ws.screen1_reviewers_required
    ws.screen1_reviewers_required = max(1, min(10, reviewers_required))
    if ws.screen1_reviewers_required != before1:
        from models import recompute_record_screen1
        for rec in db.query(Record).filter(Record.workspace_id == ws.id,
                                           Record.is_removed == False).all():  # noqa: E712
            recompute_record_screen1(db, ws, rec)
    # an unchecked checkbox sends nothing, so absence is the off signal
    ws.dry_run_enabled = bool(dry_run_enabled)
    # empty means "same as screening 1", the behaviour before the two stages
    # could be set apart. Changing it re-resolves stage 2 for every record, since
    # the cached decision was computed against the old number.
    raw = (reviewers_required_2 or "").strip()
    before = ws.screen2_reviewers_required
    ws.screen2_reviewers_required = max(1, min(10, int(raw))) if raw.isdigit() else None
    if ws.screen2_reviewers_required != before:
        from models import recompute_record_screen2
        for rec in db.query(Record).filter(Record.workspace_id == ws.id,
                                           Record.is_removed == False).all():  # noqa: E712
            recompute_record_screen2(db, ws, rec)
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/members/add")
async def add_member(ws_id: int, email: str = Form(...),
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Owner required")
    target = db.query(User).filter(User.email == email.strip().lower(),
                                   User.is_active == True).first()  # noqa: E712
    if not target:
        return RedirectResponse(f"/w/{ws_id}/settings?member_error=1", status_code=302)
    if target.id != ws.owner_id:
        exists = db.query(WorkspaceMember).filter(
            WorkspaceMember.workspace_id == ws.id, WorkspaceMember.user_id == target.id).first()
        if not exists:
            db.add(WorkspaceMember(workspace_id=ws.id, user_id=target.id))
            db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/members/{uid}/remove")
async def remove_member(ws_id: int, uid: int,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Owner required")
    m = db.query(WorkspaceMember).filter(WorkspaceMember.workspace_id == ws.id,
                                         WorkspaceMember.user_id == uid).first()
    if m:
        db.delete(m)
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/criteria/add")
async def add_criterion(ws_id: int, kind: str = Form(...), label: str = Form(...),
                        description: str = Form(""), user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if kind not in ("exclusion", "inclusion") or not label.strip():
        raise HTTPException(400, "Invalid criterion")
    pos = db.query(Criterion).filter(Criterion.workspace_id == ws.id,
                                     Criterion.kind == kind).count()
    db.add(Criterion(workspace_id=ws.id, kind=kind, label=label.strip(),
                     description=description.strip() or None, position=pos))
    db.flush()
    recompact_criteria(db, ws.id, kind)
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/criteria/{cid}/edit")
async def edit_criterion(ws_id: int, cid: int, label: str = Form(...),
                         description: str = Form(""), user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Change a criterion's wording in place. Without this the only way to reword
    one was to delete it and add it back, which lost its position and its id."""
    ws = _load_ws(db, user, ws_id)
    c = db.query(Criterion).filter(Criterion.id == cid, Criterion.workspace_id == ws.id).first()
    if not c:
        raise HTTPException(404, "No such criterion")
    if not label.strip():
        raise HTTPException(400, "Label required")
    c.label = label.strip()
    c.description = description.strip() or None
    db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


@app.post("/w/{ws_id}/criteria/{cid}/delete")
async def delete_criterion(ws_id: int, cid: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    c = db.query(Criterion).filter(Criterion.id == cid, Criterion.workspace_id == ws.id).first()
    if c:
        kind = c.kind
        db.delete(c)
        db.flush()
        recompact_criteria(db, ws.id, kind)
        db.commit()
    return RedirectResponse(f"/w/{ws_id}/settings", status_code=302)


# ── Screening 1 (step 5) ────────────────────────────────────────────────────

@app.get("/w/{ws_id}/screening", response_class=HTMLResponse)
async def screening_page(ws_id: int, request: Request, decision: str = "pending",
                         q: str = "", source: str = "", rtype: str = "",
                         yf: str = "", yt: str = "", sort: str = "year", order: str = "desc",
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)

    def _count(d):
        return db.query(Record).filter(Record.workspace_id == ws.id,
                                       Record.is_removed == False,  # noqa: E712
                                       Record.screen1_decision == d).count()
    counts = {d: _count(d) for d in ("pending", "include", "exclude", "maybe", "conflict")}
    counts["total"] = sum(counts[d] for d in ("pending", "include", "exclude", "maybe", "conflict"))
    # records the model may (re-)screen: everything already decided, minus what a
    # person has voted on. Read off Record.screen1_by this undercounts the humans
    # badly — that column stays 'model' until reviewers_required votes are in, so
    # a two-reviewer workspace reported 0 reviewed records and offered to re-screen
    # work people had already done.
    _human = human_voted_subq(db, ws.id, "screen1")
    model_n = db.query(Record).filter(Record.workspace_id == ws.id,
                                      Record.is_removed == False,   # noqa: E712
                                      Record.screen1_decision != "pending",
                                      ~Record.id.in_(_human)).count()
    manual_n = db.query(Record).filter(Record.workspace_id == ws.id,
                                       Record.is_removed == False,  # noqa: E712
                                       Record.id.in_(_human)).count()
    # dry run: the model against the reviewers, on the records it may not decide.
    # Switched off, the page must show neither the button nor the results, so the
    # numbers are not computed at all rather than computed and hidden in the
    # template — one place to be wrong instead of several.
    if ws.dry_run_enabled:
        shadow_matrix, shadow_stats = shadow_agreement(db, ws.id, "screen1")
    else:
        shadow_matrix, shadow_stats = {}, {"pairs": 0, "agree": 0, "disagree": 0,
                                           "pct": None, "shadow_n": 0, "disagree_ids": []}
    # Records where at least one voice differs from another, the model included.
    # Wider than screen1_decision == 'conflict', which only ever means two humans
    # disagreeing: 'maybe' against 'include' counts, and so does the model against
    # everyone. Open to every member of the workspace; the votes themselves stay
    # behind the blind rule, so this says that a record is contested, not who said
    # what.
    from sqlalchemy import distinct, func
    from models import ScreenDecision
    is_owner = ws.owner_id == user.id or user.is_admin
    # DECIDING_KINDS, not every row: a shadow vote is a dry run and disagreeing
    # with it is the measurement, not a contested record. Without this filter the
    # divergent count would jump by however many the dry run happened to differ on.
    divergent_sub = (db.query(ScreenDecision.record_id)
                       .filter(ScreenDecision.workspace_id == ws.id,
                               ScreenDecision.stage == "screen1",
                               ScreenDecision.reviewer_kind.in_(DECIDING_KINDS))
                       .group_by(ScreenDecision.record_id)
                       .having(func.count(distinct(ScreenDecision.decision)) > 1)
                       .scalar_subquery())
    divergent_n = (db.query(Record)
                     .filter(Record.workspace_id == ws.id,
                             Record.is_removed == False,          # noqa: E712
                             Record.id.in_(divergent_sub)).count())

    mine_marked = my_earmark_ids(db, ws.id, user.id)

    tq = db.query(Record).filter(Record.workspace_id == ws.id, Record.is_removed == False)  # noqa: E712
    if decision in ("pending", "include", "exclude", "maybe", "conflict"):
        tq = tq.filter(Record.screen1_decision == decision)
    elif decision == "divergent":
        tq = tq.filter(Record.id.in_(divergent_sub))
    elif decision == "modelonly":
        # standing on the model's word alone: it voted, no human has yet. Empty
        # once a corpus is fully screened, and filling again after each refresh,
        # which is exactly when it is worth looking at.
        # Not screen1_by == 'model': that is the resolved decision, and it stays
        # 'model' while a lone human vote sits below reviewers_required, so the
        # filter used to return records reviewers had already worked through.
        tq = tq.filter(Record.screen1_decision != "pending", ~Record.id.in_(_human))
    elif decision == "earmarked":
        # Mine, never everybody's: a list of what other people find interesting
        # is a different question, and at screening 1 it would also be an
        # aggregate over attention the blind rule keeps out of sight.
        tq = tq.filter(Record.id.in_(mine_marked or {-1}))
    elif decision == "shadow_disagrees":
        # the dry run's whole point: walk the records where the model, run again
        # under the criteria as they stand, would not have voted as a reviewer did.
        tq = tq.filter(Record.id.in_(shadow_stats["disagree_ids"] or [-1]))
    tq = _apply_record_filters(tq, q, source, rtype, yf, yt, sort, order)
    records = tq.limit(500).all()

    # per-record screen-1 votes, for the reviewer table (blind) + adjudication.
    rec_ids = [r.id for r in records]
    all_votes = (db.query(ScreenDecision)
                   .filter(ScreenDecision.stage == "screen1",
                           ScreenDecision.record_id.in_(rec_ids)).all() if rec_ids else [])
    votes = {}
    for v in all_votes:
        # with the dry run switched off its rows are dropped here rather than
        # skipped in each of the three places the template renders a vote, so
        # the per-record 🌓 chips cannot leak what the hidden panel would have said
        if v.reviewer_kind == "shadow" and not ws.dry_run_enabled:
            continue
        votes.setdefault(v.record_id, []).append(v)
    # One order for the voices on a row, so the column reads the same way on
    # every record: whoever closed it, then the model, then the people, and last
    # the dry run — a voice that decides nothing belongs at the bottom.
    _voice_rank = {"adjudicator": 0, "model": 1, "user": 2, "shadow": 3}
    for _vs in votes.values():
        _vs.sort(key=lambda v: (_voice_rank.get(v.reviewer_kind, 2),
                                (v.reviewer.name if v.reviewer else "") or ""))
    # records the current reviewer has already voted on → their votes are revealed
    my_voted = {v.record_id for v in all_votes
                if v.reviewer_kind == "user" and v.reviewer_id == user.id}
    # same definition as divergent_sub, for the marker on the rows on screen
    divergent_ids = {rid for rid, vs in votes.items()
                     if len({v.decision for v in vs}) > 1}
    # The blind rule for the votes on a row, in one place. The template used to
    # carry two that disagreed: the compact vote strip under the decision was
    # revealed to the owner on every record, while the named list beside the
    # buttons was revealed to them only on conflicts — the same data, two rules.
    # This is the one the page's own help text states: after you have voted, or
    # once an adjudicator has closed the record.
    revealed = {r.id for r in records
                if is_owner or r.id in my_voted or r.screen1_by == "adjudicator"}

    earmarks = earmarks_by_record(db, ws.id, rec_ids)

    # cost estimate for the screening buttons
    import screening
    model = ws.screening_model or "claude-haiku-4-5"
    system = screening.build_system(ws.research_question, workspace_criteria(db, ws, "exclusion"))

    def _chars(*filters):
        expr = func.coalesce(func.length(Record.title), 0) + func.coalesce(func.length(Record.abstract), 0)
        return db.query(func.coalesce(func.sum(expr), 0)).filter(
            Record.workspace_id == ws.id, Record.is_removed == False, *filters).scalar() or 0  # noqa: E712
    pending_chars = _chars(Record.screen1_decision == "pending")
    # same set model_n counts, so the price quoted matches the number on the button
    model_chars = _chars(Record.screen1_decision != "pending", ~Record.id.in_(_human))

    def _fmt(x):
        if x <= 0:
            return "$0.00"
        return "<$0.01" if x < 0.01 else f"${x:.2f}"
    shadow_chars = _chars(Record.id.in_(_human))
    est_shadow = _fmt(screening.estimate_cost(model, system, manual_n, shadow_chars))
    est_pending = _fmt(screening.estimate_cost(model, system, counts["pending"], pending_chars))
    est_rerun = _fmt(screening.estimate_cost(model, system, counts["pending"] + model_n,
                                             pending_chars + model_chars))
    return render(request, "workspace_screening.html", {
        "user": user, "ws": ws, "tab": "screening", "steps_done": workspace_steps_done(ws),
        "counts": counts,
        "model_n": model_n, "manual_n": manual_n, "model": model,
        "est_pending": est_pending, "est_rerun": est_rerun, "est_shadow": est_shadow,
        "shadow_matrix": shadow_matrix, "shadow": shadow_stats,
        "records": records, "decision": decision,
        "votes": votes, "revealed": revealed,
        "divergent_n": divergent_n, "divergent_ids": divergent_ids,
        "earmarks": earmarks, "mine_marked": mine_marked,
        "reviewers_required": ws.screen1_reviewers_required or 1,
        "is_owner": is_owner,
        "dbs_present": _dbs_present(db, ws.id),
        "filters": {"decision": decision, "q": q, "source": source, "rtype": rtype,
                    "yf": yf, "yt": yt, "sort": sort, "order": order},
        "n_exclusion": len(workspace_criteria(db, ws, "exclusion")),
        "exclusion_criteria": workspace_criteria(db, ws, "exclusion"),
        "has_key": bool(_user_api_key(user)),
    })


@app.post("/w/{ws_id}/screening/run")
async def run_screening(ws_id: int, mode: str = Form(""), rerun: str = Form(""),
                        user: User = Depends(get_current_user),
                        db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    # `rerun` is the old field name, still honoured so a bookmarked POST keeps
    # working. An unknown mode falls back to the safest one rather than erroring.
    mode = mode or ("rerun" if rerun else "pending")
    if mode not in ("pending", "rerun", "shadow"):
        mode = "pending"
    # the button is hidden when the setting is off, but hiding a control is not
    # the same as refusing the request it would have sent
    if mode == "shadow" and not ws.dry_run_enabled:
        raise HTTPException(403, "The dry run is switched off for this review")
    from screening import get_job, start_screen1
    job = get_job(ws.id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "Screening already in progress")
    start_screen1(ws.id, api_key, user.id, mode=mode)
    return RedirectResponse(f"/w/{ws_id}/screening", status_code=302)


@app.get("/w/{ws_id}/screening/status")
async def screening_status(ws_id: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    from screening import get_job
    return JSONResponse(get_job(ws_id) or {"status": "idle"})


def _xlsx_response(data: bytes, ws, suffix: str) -> Response:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", (ws.name or "workspace")).strip("_")[:60] or "workspace"
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}-{suffix}.xlsx"'},
    )


@app.get("/w/{ws_id}/screening/export.xlsx")
async def export_screening(ws_id: int, user: User = Depends(get_current_user),
                           db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from export import screening_xlsx
    return _xlsx_response(screening_xlsx(db, ws), ws, "screening")


@app.get("/w/{ws_id}/records/export.xlsx")
async def export_records(ws_id: int, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from export import records_xlsx
    return _xlsx_response(records_xlsx(db, ws), ws, "records")


def _from_script(request: Request) -> bool:
    """Is the page asking for this itself, rather than a browser navigating?

    The screening table votes by fetch and redraws in place, so it wants the
    record's resolved state back, not a redirect it would have to follow and
    then throw away. Without the header — no JavaScript, or a form posted the
    old way — nothing changes: the route redirects as it always did.
    """
    return request.headers.get("x-requested-with") == "fetch"


@app.post("/w/{ws_id}/records/{rid}/screen1/vote")
async def vote_screen1(ws_id: int, rid: int, request: Request,
                       decision: str = Form(...), reason: str = Form(""),
                       set_reason: str = Form(""), back: str = Form("pending"),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The current reviewer's independent screen-1 vote. 'clear' retracts it."""
    ws = _load_ws(db, user, ws_id)
    if decision not in ("include", "exclude", "maybe", "clear"):
        raise HTTPException(400, "Invalid decision")
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if rec:
        from models import ScreenDecision, recompute_record_screen1, upsert_screen_decision
        if decision == "clear":
            row = (db.query(ScreenDecision)
                     .filter(ScreenDecision.record_id == rec.id, ScreenDecision.stage == "screen1",
                             ScreenDecision.reviewer_kind == "user",
                             ScreenDecision.reviewer_id == user.id).first())
            if row:
                db.delete(row)
                db.flush()
        else:
            # Changing your mind with a plain click must not wipe an argument
            # you wrote earlier, so "leave it alone" and "blank it" have to be
            # different messages — and they cannot be told apart by looking at
            # `reason`, because an empty form field arrives here as None just
            # like an absent one. Hence the separate flag: `set_reason` present
            # means the box was used and `reason` is the new text, empty or
            # not. Measured, not assumed: reason= and no reason at all were
            # indistinguishable at the route.
            mine = (db.query(ScreenDecision)
                      .filter(ScreenDecision.record_id == rec.id,
                              ScreenDecision.stage == "screen1",
                              ScreenDecision.reviewer_kind == "user",
                              ScreenDecision.reviewer_id == user.id).first())
            text = reason if set_reason else (mine.reason if mine else None)
            upsert_screen_decision(db, rec, "screen1", "user", user.id, decision,
                                   (text or "").strip() or "manual vote")
        recompute_record_screen1(db, ws, rec)
        db.commit()
    if _from_script(request):
        # What the vote *did*, which is not what was clicked: below the required
        # number of reviewers the record still stands on the model's decision,
        # and two reviewers who differ make a conflict nobody voted for.
        return JSONResponse({"vote": decision,
                             "decision": rec.screen1_decision if rec else None,
                             "by": rec.screen1_by if rec else None})
    return RedirectResponse(f"/w/{ws_id}/screening?decision={back}", status_code=302)


@app.post("/w/{ws_id}/records/{rid}/earmark")
async def earmark(ws_id: int, rid: int, request: Request,
                  on: str = Form(""), note: str | None = Form(None),
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Raise, clear or annotate the caller's dot on a record.

    Any member, because a mark is not a permission and needs only the access
    the workspace already grants — and only ever the caller's own row: marking
    a record *for somebody else* is a different feature with a different
    question behind it, so it is not half-built here.

    `on` empty means toggle, which is what the dot sends. Nothing is logged and
    no decision is recomputed: this must not be able to move a record.
    """
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec:
        raise HTTPException(404, "Record not found")
    want = None if on == "" else (on not in ("0", "false", ""))
    row = set_earmark(db, rec, user.id, want, note)
    db.commit()
    if _from_script(request):
        return JSONResponse({"on": row is not None, "note": row.note if row else None})
    return RedirectResponse(request.headers.get("referer") or f"/w/{ws_id}/screening",
                            status_code=302)


@app.post("/w/{ws_id}/records/{rid}/screen1/adjudicate")
async def adjudicate_screen1(ws_id: int, rid: int, request: Request, decision: str = Form(...),
                             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Owner/admin resolves a conflicted record. 'clear' removes the ruling."""
    ws = _load_ws(db, user, ws_id)
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Adjudicator (owner/admin) required")
    if decision not in ("include", "exclude", "maybe", "clear"):
        raise HTTPException(400, "Invalid decision")
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if rec:
        from models import ScreenDecision, recompute_record_screen1, upsert_screen_decision
        if decision == "clear":
            for row in (db.query(ScreenDecision)
                          .filter(ScreenDecision.record_id == rec.id,
                                  ScreenDecision.stage == "screen1",
                                  ScreenDecision.reviewer_kind == "adjudicator").all()):
                db.delete(row)
            db.flush()
        else:
            upsert_screen_decision(db, rec, "screen1", "adjudicator", user.id, decision,
                                   "adjudication")
        recompute_record_screen1(db, ws, rec)
        db.commit()
    if _from_script(request):
        return JSONResponse({"vote": decision,
                             "decision": rec.screen1_decision if rec else None,
                             "by": rec.screen1_by if rec else None})
    return RedirectResponse(f"/w/{ws_id}/screening?decision=conflict", status_code=302)


# ── Full text: fetch (Unpaywall) + convert (paper2md), two passes (steps 6-7) ─

_FT_STATUS_LABELS = [("converted", "converted"), ("fetched", "PDF only"),
                     ("url", "OA link"), ("failed", "not found"), ("none", "—")]


@app.get("/w/{ws_id}/fulltext", response_class=HTMLResponse)
async def fulltext_page(ws_id: int, request: Request, status: str = "all",
                        q: str = "", source: str = "", rtype: str = "",
                        yf: str = "", yt: str = "", sort: str = "year", order: str = "desc",
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    base = db.query(Record).filter(Record.workspace_id == ws.id, Record.is_removed == False,  # noqa: E712
                                   Record.screen1_decision == "include")
    all_included = base.all()  # the whole included pool, for counts + the action buttons
    # Records excluded at screening 2 are skipped by both passes (see fulltext.py),
    # so they must not be counted as work left to do either.
    pending = [r for r in all_included if r.screen2_decision != "exclude"]
    ft = {"included": len(all_included),
          "to_fetch": sum(1 for r in pending if r.full_text_status in ("none", "failed", "url")),
          "fetched": sum(1 for r in pending if r.full_text_status == "fetched"),
          "converted": sum(1 for r in all_included if r.full_text_status == "converted")}
    # full-text status nav (counts over the whole pool, like screening's decision nav)
    status_nav = [("all", "all", len(all_included))] + [
        (k, lbl, sum(1 for r in all_included if r.full_text_status == k))
        for k, lbl in _FT_STATUS_LABELS]

    tq = base
    if status in ("converted", "fetched", "url", "failed", "none"):
        tq = tq.filter(Record.full_text_status == status)
    tq = _apply_record_filters(tq, q, source, rtype, yf, yt, sort, order)
    records = tq.limit(500).all()

    return render(request, "workspace_fulltext.html", {
        "user": user, "ws": ws, "tab": "fulltext", "steps_done": workspace_steps_done(ws),
        "records": records, "ft": ft, "status": status, "status_nav": status_nav,
        "dbs_present": _dbs_present(db, ws.id),
        "filters": {"status": status, "q": q, "source": source, "rtype": rtype,
                    "yf": yf, "yt": yt, "sort": sort, "order": order},
    })


@app.post("/w/{ws_id}/fulltext/fetch")
async def run_fulltext_fetch(ws_id: int, user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    import fulltext
    job = fulltext.get_job(ws.id, "fetch")
    if job and job.get("status") == "running":
        raise HTTPException(409, "Full-text fetch already in progress")
    email = os.environ.get("UNPAYWALL_EMAIL") or ws.owner.email
    fulltext.start_fetch(ws.id, email, _user_publisher_keys(user), fulltext.paper2md_url())
    return RedirectResponse(f"/w/{ws_id}/fulltext", status_code=302)


@app.get("/w/{ws_id}/fulltext/fetch/status")
async def fulltext_fetch_status(ws_id: int, user: User = Depends(get_current_user),
                                db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    import fulltext
    return JSONResponse(fulltext.get_job(ws_id, "fetch") or {"status": "idle"})


@app.post("/w/{ws_id}/fulltext/convert")
async def run_fulltext_convert(ws_id: int, user: User = Depends(get_current_user),
                               db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    import fulltext
    job = fulltext.get_job(ws.id, "convert")
    if job and job.get("status") == "running":
        raise HTTPException(409, "Conversion already in progress")
    fulltext.start_convert(ws.id, fulltext.paper2md_url())
    return RedirectResponse(f"/w/{ws_id}/fulltext", status_code=302)


@app.get("/w/{ws_id}/fulltext/convert/status")
async def fulltext_convert_status(ws_id: int, user: User = Depends(get_current_user),
                                  db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    import fulltext
    return JSONResponse(fulltext.get_job(ws_id, "convert") or {"status": "idle"})


@app.get("/w/{ws_id}/records/{rid}/pdf")
async def record_pdf(ws_id: int, rid: int, user: User = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec or not rec.full_text_path:
        raise HTTPException(404, "No stored PDF for this record")
    p = Path(rec.full_text_path)
    if not p.exists():
        raise HTTPException(404, "PDF file missing")
    return FileResponse(str(p), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="record-{rid}.pdf"'})


@app.post("/w/{ws_id}/records/{rid}/fulltext/upload")
async def upload_fulltext(ws_id: int, rid: int, request: Request, file: UploadFile = File(...),
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec:
        raise HTTPException(404, "Record not found")
    data = await file.read()
    import fulltext
    try:
        fulltext.ingest_upload(db, ws.id, rec, file.filename or "", data)
    except Exception as exc:
        raise HTTPException(400, f"Could not read file: {exc}")
    if _from_script(request):
        # Only the status. There is deliberately no warning to pass on: an
        # upload clears the record's note, because the reviewer chose this file,
        # and a PDF whose text turns out to belong to another paper is flagged
        # later, by the convert pass, not here.
        return JSONResponse({"status": rec.full_text_status})
    return RedirectResponse(f"/w/{ws_id}/fulltext", status_code=302)


# ── Assessment: combined screening 2 + assessment (steps 8-9) ───────────────

@app.get("/w/{ws_id}/assessment", response_class=HTMLResponse)
async def assessment_page(ws_id: int, request: Request, decision: str = "all",
                          q: str = "", source: str = "", rtype: str = "",
                          yf: str = "", yt: str = "", sort: str = "year", order: str = "desc",
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import ScreenDecision, ensure_extraction_fields, workspace_extraction_fields
    ensure_extraction_fields(db, ws)

    base = db.query(Record).filter(Record.workspace_id == ws.id, Record.is_removed == False,  # noqa: E712
                                   Record.screen1_decision == "include")
    all_recs = base.all()  # the whole screen-1 included pool, for counts & the draft job

    def _c2(d):
        return sum(1 for r in all_recs if r.screen2_decision == d)
    counts = {d: _c2(d) for d in ("pending", "include", "exclude", "maybe", "conflict")}
    counts["total"] = len(all_recs)

    # Included on full text but with nothing extracted: the record is in the
    # review and contributes to no field of the synthesis. Counts an existing but
    # empty extraction as unextracted too — from the reader's side they are the
    # same hole.
    from models import Extraction
    extracted = {e.record_id for e in
                 db.query(Extraction).filter(Extraction.workspace_id == ws.id).all()
                 if e.values()}
    empty_ids = {r.id for r in all_recs
                 if r.screen2_decision == "include" and r.id not in extracted}
    counts["empty"] = len(empty_ids)

    mine_marked = my_earmark_ids(db, ws.id, user.id)

    # the visible table: apply the screen-2 decision filter + shared filters
    tq = base
    if decision in ("pending", "include", "exclude", "maybe", "conflict"):
        tq = tq.filter(Record.screen2_decision == decision)
    elif decision == "empty":
        tq = tq.filter(Record.id.in_(empty_ids or [-1]))
    elif decision == "modelonly":
        # the model drafted a full-text decision and nobody has confirmed it: the
        # same idea as on screening 1, and it matters more here, since the draft
        # also pre-fills the extraction the reviewer will be looking at.
        tq = tq.filter(Record.screen2_by == "model")
    elif decision == "earmarked":
        tq = tq.filter(Record.id.in_(mine_marked or {-1}))
    tq = _apply_record_filters(tq, q, source, rtype, yf, yt, sort, order)
    records = tq.limit(500).all()

    rec_ids = [r.id for r in records]
    votes = {}
    my_reviewed = set()
    for v in (db.query(ScreenDecision)
                .filter(ScreenDecision.stage == "screen2",
                        ScreenDecision.record_id.in_(rec_ids)).all() if rec_ids else []):
        votes.setdefault(v.record_id, []).append(v)
        if v.reviewer_kind == "user" and v.reviewer_id == user.id:
            my_reviewed.add(v.record_id)

    earmarks = earmarks_by_record(db, ws.id, rec_ids)

    n_fields = len(workspace_extraction_fields(db, ws))
    n_converted = sum(1 for r in all_recs if r.full_text_status == "converted")
    # records the model may draft: converted, still pending, untouched by a human
    s2_human = {rid for (rid,) in db.query(ScreenDecision.record_id)
                .filter(ScreenDecision.workspace_id == ws.id, ScreenDecision.stage == "screen2",
                        ScreenDecision.reviewer_kind.in_(["user", "adjudicator"])).all()}
    ready_recs = [r for r in all_recs if r.full_text_status == "converted"
                  and r.screen2_decision == "pending" and r.id not in s2_human]
    ready = len(ready_recs)
    redraft_recs = [r for r in all_recs if r.full_text_status == "converted" and r.id not in s2_human]
    drafted = sum(1 for r in all_recs if r.screen2_by == "model")

    import assessment
    model = ws.screening_model or "claude-haiku-4-5"
    system = assessment.build_system(ws.research_question,
                                     workspace_criteria(db, ws, "inclusion"),
                                     workspace_extraction_fields(db, ws))

    def _fmt(recs):
        e = assessment.estimate_cost(model, system, len(recs),
                                     sum(len(r.full_text_md or "") for r in recs))
        return "$0.00" if e <= 0 else ("<$0.01" if e < 0.01 else f"${e:.2f}")
    return render(request, "workspace_assessment.html", {
        "user": user, "ws": ws, "tab": "assessment", "steps_done": workspace_steps_done(ws),
        "records": records, "votes": votes, "my_reviewed": my_reviewed,
        "earmarks": earmarks, "mine_marked": mine_marked,
        "counts": counts, "decision": decision, "dbs_present": _dbs_present(db, ws.id),
        "filters": {"decision": decision, "q": q, "source": source, "rtype": rtype,
                    "yf": yf, "yt": yt, "sort": sort, "order": order},
        "n_converted": n_converted, "n_fields": n_fields,
        "ready": ready, "drafted": drafted, "est": _fmt(ready_recs), "est_redraft": _fmt(redraft_recs),
        "model": model,
        "has_key": bool(_user_api_key(user)),
        "n_inclusion": len(workspace_criteria(db, ws, "inclusion")),
        "reviewers_required": screen2_required(ws),
        "is_owner": ws.owner_id == user.id or user.is_admin,
    })


@app.post("/w/{ws_id}/assessment/run")
async def run_assessment(ws_id: int, rerun: str = Form(""),
                         user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    api_key = _user_api_key(user)
    if not api_key:
        raise HTTPException(400, "Set your Anthropic API key in your profile first")
    import assessment
    job = assessment.get_job(ws.id)
    if job and job.get("status") == "running":
        raise HTTPException(409, "Assessment already in progress")
    assessment.start_assessment(ws.id, api_key, user.id, rerun=bool(rerun))
    return RedirectResponse(f"/w/{ws_id}/assessment", status_code=302)


@app.get("/w/{ws_id}/assessment/status")
async def assessment_status(ws_id: int, user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    _load_ws(db, user, ws_id)
    import assessment
    return JSONResponse(assessment.get_job(ws_id) or {"status": "idle"})


@app.get("/w/{ws_id}/assessment/export.xlsx")
async def export_assessment(ws_id: int, user: User = Depends(get_current_user),
                            db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import ensure_extraction_fields
    ensure_extraction_fields(db, ws)
    from export import assessment_xlsx
    return _xlsx_response(assessment_xlsx(db, ws), ws, "assessment")


# ── Assessment review modal: screen 2 + extraction in one save ──────────────

@app.get("/w/{ws_id}/assessment/{rid}/review", response_class=HTMLResponse)
async def review_fragment(ws_id: int, rid: int, request: Request,
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec:
        raise HTTPException(404, "Record not found")
    from models import (Extraction, ScreenDecision, ensure_extraction_fields,
                        workspace_extraction_fields)
    ensure_extraction_fields(db, ws)
    fields = workspace_extraction_fields(db, ws)

    def _vals(kind, rid_):
        row = (db.query(Extraction)
                 .filter(Extraction.record_id == rec.id, Extraction.reviewer_kind == kind,
                         Extraction.reviewer_id == rid_).first())
        return row.values() if row else None

    mine = _vals("user", user.id)
    draft = _vals("model", None)
    values = mine if mine is not None else (draft or {})  # AI draft pre-fill
    from_draft = mine is None and draft is not None
    my_vote = (db.query(ScreenDecision)
                 .filter(ScreenDecision.record_id == rec.id, ScreenDecision.stage == "screen2",
                         ScreenDecision.reviewer_kind == "user",
                         ScreenDecision.reviewer_id == user.id).first())
    other_votes = (db.query(ScreenDecision)
                     .filter(ScreenDecision.record_id == rec.id, ScreenDecision.stage == "screen2")
                     .all())

    # What the other reviewers extracted, read-only. Deliberately NOT used to
    # pre-fill the form: adopting someone else's answers with one click would
    # turn a second independent extraction into a copy of the first.
    from models import field_visible
    is_owner = ws.owner_id == user.id or user.is_admin
    other_extractions = []
    if is_owner:
        rows = (db.query(Extraction)
                  .filter(Extraction.record_id == rec.id,
                          Extraction.reviewer_kind.in_(["user", "final"]))
                  .all())
        # final first, then oldest reviewer row first; id is monotonic so it
        # orders them without needing the timestamps to be populated
        for row in sorted(rows, key=lambda r: (r.reviewer_kind != "final", r.id)):
            if row.reviewer_kind == "user" and row.reviewer_id == user.id:
                continue
            vals = row.values()
            pairs = [(f.label, "; ".join(vals[f.key]) if isinstance(vals[f.key], list)
                      else str(vals[f.key]))
                     for f in fields
                     if vals.get(f.key) and field_visible(f, vals)]
            if pairs:
                who = ("final version" if row.reviewer_kind == "final"
                       else (row.reviewer.name if row.reviewer else "a reviewer"))
                other_extractions.append({"who": who, "pairs": pairs,
                                          "when": row.updated_at})

    marks = earmarks_by_record(db, ws.id, [rec.id]).get(rec.id, [])
    return render(request, "_review_form.html", {
        "user": user, "ws": ws, "rec": rec, "fields": fields,
        "marks": marks, "my_mark": next((m for m in marks if m.user_id == user.id), None),
        "inclusion": workspace_criteria(db, ws, "inclusion"),
        "values": values, "my_vote": my_vote, "other_votes": other_votes,
        "from_draft": from_draft, "model_vote": next(
            (v for v in other_votes if v.reviewer_kind == "model"), None),
        "is_owner": is_owner,
        "other_extractions": other_extractions,
        "has_final": _vals("final", None) is not None,
    })


@app.post("/w/{ws_id}/assessment/{rid}/review")
async def save_review(ws_id: int, rid: int, request: Request,
                      user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if not rec:
        raise HTTPException(404, "Record not found")
    from models import (ensure_extraction_fields, field_visible, recompute_record_screen2,
                        upsert_extraction, upsert_screen_decision, workspace_extraction_fields)
    ensure_extraction_fields(db, ws)
    fields = workspace_extraction_fields(db, ws)
    form = await request.form()
    raw = {}
    for f in fields:
        if f.field_type == "multiselect":
            vals = [v for v in form.getlist(f"f_{f.key}") if v]
            if vals:
                raw[f.key] = vals
        else:
            v = (form.get(f"f_{f.key}") or "").strip()
            if v:
                raw[f.key] = v
    values = {f.key: raw[f.key] for f in fields if f.key in raw and field_visible(f, raw)}
    # Saving an empty form must not blank a record someone else has extracted:
    # authoritative_values takes the most recently saved reviewer row, so a blank
    # new row would quietly empty the record in exports and synthesis. Clearing
    # your OWN existing row stays possible — that is a deliberate act.
    from models import Extraction
    existing = (db.query(Extraction)
                  .filter(Extraction.record_id == rec.id,
                          Extraction.reviewer_kind == "user").all())
    mine_exists = any(r.reviewer_id == user.id for r in existing)
    others_have = any(r.reviewer_id != user.id and r.values() for r in existing)
    if values or mine_exists or not others_have:
        upsert_extraction(db, ws, rec, "user", user.id, values)
    if form.get("set_final") and (ws.owner_id == user.id or user.is_admin):
        upsert_extraction(db, ws, rec, "final", None, values)
    decision = form.get("screen2_decision") or ""
    if decision in ("include", "exclude", "maybe"):
        upsert_screen_decision(db, rec, "screen2", "user", user.id, decision,
                               (form.get("screen2_reason") or "").strip() or "full-text review")
        recompute_record_screen2(db, ws, rec)
    db.commit()
    if _from_script(request):
        # What was saved, so the toast can tell a review that only extracted
        # from one that also decided — and, when it decided, where that left the
        # record, which below quorum is not where the vote pointed.
        return JSONResponse({"vote": decision or None,
                             "decision": rec.screen2_decision,
                             "by": rec.screen2_by,
                             "fields": len(values),
                             "final": bool(form.get("set_final"))})
    # Without the query string, so the plain path lands on the default filter —
    # which is the reason the redraw above exists.
    return RedirectResponse(f"/w/{ws_id}/assessment", status_code=302)


@app.post("/w/{ws_id}/records/{rid}/screen2/adjudicate")
async def adjudicate_screen2(ws_id: int, rid: int, request: Request, decision: str = Form(...),
                             user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    if not (ws.owner_id == user.id or user.is_admin):
        raise HTTPException(403, "Adjudicator (owner/admin) required")
    if decision not in ("include", "exclude", "maybe", "clear"):
        raise HTTPException(400, "Invalid decision")
    rec = db.query(Record).filter(Record.id == rid, Record.workspace_id == ws.id).first()
    if rec:
        from models import ScreenDecision, recompute_record_screen2, upsert_screen_decision
        if decision == "clear":
            for row in (db.query(ScreenDecision)
                          .filter(ScreenDecision.record_id == rec.id,
                                  ScreenDecision.stage == "screen2",
                                  ScreenDecision.reviewer_kind == "adjudicator").all()):
                db.delete(row)
            db.flush()
        else:
            upsert_screen_decision(db, rec, "screen2", "adjudicator", user.id, decision,
                                   "adjudication")
        recompute_record_screen2(db, ws, rec)
        db.commit()
    if _from_script(request):
        return JSONResponse({"vote": decision,
                             "decision": rec.screen2_decision if rec else None,
                             "by": rec.screen2_by if rec else None})
    return RedirectResponse(f"/w/{ws_id}/assessment", status_code=302)


# ── Synthesis (step 10) ─────────────────────────────────────────────────────

@app.get("/w/{ws_id}/synthesis", response_class=HTMLResponse)
async def synthesis_page(ws_id: int, request: Request, user: User = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    ws = _load_ws(db, user, ws_id)
    from models import Synthesis
    syn = db.query(Synthesis).filter(Synthesis.workspace_id == ws.id).first()
    blocks = sorted(syn.blocks, key=lambda b: b.position) if syn else []
    shares = db.query(PublicShare).filter(PublicShare.workspace_id == ws.id,
                                          PublicShare.active == True).all()  # noqa: E712
    return render(request, "workspace_synthesis.html", {
        "user": user, "ws": ws, "tab": "synthesis", "steps_done": workspace_steps_done(ws),
        "syn": syn,
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
