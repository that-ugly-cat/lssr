"""
LSSR — Living Systematic Scoping Review.

Fase 0 (fondazione): auth + multiuser + multiworkspace + public read-only share.
The 10-step pipeline (SPEC.md §5) is added in later phases; the workspace page
shows the step scaffold with everything past the foundation marked "coming".
"""
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from auth import (
    create_token, get_current_user, get_user_or_none, hash_password, verify_password,
)
from models import (
    PublicShare, User, Workspace, WorkspaceMember, can_access, get_db, init_db,
    new_share_token, user_workspaces,
)

BASE = Path(__file__).parent
app = FastAPI(title="LSSR")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

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
    return render(request, "workspace_overview.html", {
        "user": user, "ws": ws,
        "is_owner": ws.owner_id == user.id or user.is_admin,
        "members": members, "shares": shares,
    })


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
    return render(request, "public_review.html", {"user": None, "ws": share.workspace})


@app.get("/health")
async def health():
    return {"ok": True}
