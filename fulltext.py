"""
Full-text acquisition (step 6) + paper2md conversion (step 7), as two separate
on-demand passes so the human can slot manual PDFs in between.

Pass 1 — FETCH (Unpaywall, adapted from RevMaster's pdf_fetch): for every
included record without a PDF yet, ask Unpaywall for an OA PDF. If it downloads,
store it (status "fetched"); if only a URL is available, keep it as a fallback
for manual retrieval (status "url"); otherwise "failed". No paper2md here.

Between the passes the human uploads the PDFs Unpaywall couldn't reach — each
upload just stores the file (status "fetched"), no conversion yet.

Pass 2 — CONVERT (paper2md): send every stored-but-unconverted PDF to the
paper2md service (POST /convert) and store the clean markdown. No reimplementation
— paper2md already does the hard part.

Both passes run as background threads. JOBS are keyed by (workspace_id, kind)
where kind is "fetch" or "convert", so their progress bars poll independently.
"""
import os
import threading
from pathlib import Path

import requests

DATA_ROOT = Path("data/fulltext")
UA = {"User-Agent": "Mozilla/5.0 (compatible; LSSR/1.0)"}

JOBS: dict[tuple[int, str], dict] = {}
_lock = threading.Lock()


def get_job(workspace_id: int, kind: str) -> dict | None:
    with _lock:
        return JOBS.get((workspace_id, kind))


def _set(workspace_id: int, kind: str, data: dict):
    with _lock:
        JOBS[(workspace_id, kind)] = data


def _update(workspace_id: int, kind: str, **kw):
    with _lock:
        if (workspace_id, kind) in JOBS:
            JOBS[(workspace_id, kind)].update(kw)


# ── Unpaywall (step 6) ─────────────────────────────────────────────────────────

def _oa_pdf_url(doi: str, email: str) -> str | None:
    try:
        r = requests.get(f"https://api.unpaywall.org/v2/{doi}?email={email}",
                         timeout=10, headers=UA)
        if r.status_code != 200:
            return None
        data = r.json()
        best = data.get("best_oa_location") or {}
        if best.get("url_for_pdf"):
            return best["url_for_pdf"]
        for loc in data.get("oa_locations", []):
            if loc.get("url_for_pdf"):
                return loc["url_for_pdf"]
    except Exception:
        pass
    return None


def _download_pdf(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=30, allow_redirects=True, headers=UA)
        if r.status_code == 200 and len(r.content) > 10_000:
            if "pdf" in r.headers.get("Content-Type", "") or r.content[:4] == b"%PDF":
                return r.content
    except Exception:
        pass
    return None


# ── paper2md (step 7) ──────────────────────────────────────────────────────────

def pdf_to_markdown(pdf_bytes: bytes, paper2md_url: str) -> str:
    resp = requests.post(
        f"{paper2md_url.rstrip('/')}/convert",
        files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
        data={"remove_references": "true", "format": "json"},
        timeout=360,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("markdown") or data.get("text") or ""


# ── PDF storage + single-record conversion ──────────────────────────────────────

def _store_pdf(workspace_id: int, record_id: int, pdf_bytes: bytes) -> Path:
    d = DATA_ROOT / str(workspace_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{record_id}.pdf"
    path.write_bytes(pdf_bytes)
    return path


def convert_stored_pdf(db, rec, paper2md_url: str) -> str:
    """Convert an already-stored PDF to markdown and persist it. Returns status."""
    pdf_bytes = Path(rec.full_text_path).read_bytes()
    try:
        md = pdf_to_markdown(pdf_bytes, paper2md_url)
    except Exception as exc:
        rec.full_text_status = "fetched"  # PDF is here, conversion failed
        db.commit()
        raise RuntimeError(f"paper2md conversion failed: {exc}") from exc
    rec.full_text_md = md
    rec.full_text_status = "converted"
    db.commit()
    return "converted"


def store_uploaded_pdf(db, workspace_id: int, rec, pdf_bytes: bytes):
    """Manual upload path: store the PDF and mark it fetched. Conversion is
    deferred to the paper2md pass, so uploads work even if paper2md is down."""
    path = _store_pdf(workspace_id, rec.id, pdf_bytes)
    rec.full_text_path = str(path)
    rec.full_text_status = "fetched"
    db.commit()


# ── Pass 1: fetch (Unpaywall) ───────────────────────────────────────────────────

def _fetch_record(db, workspace_id: int, rec, email: str) -> str:
    doi = (rec.doi or "").strip()
    if not doi:
        rec.full_text_status = "failed"
        db.commit()
        return "failed"
    url = _oa_pdf_url(doi, email)
    if not url:
        rec.full_text_status = "failed"
        db.commit()
        return "failed"
    pdf = _download_pdf(url)
    if not pdf:
        rec.full_text_url = url          # keep OA URL for manual retrieval
        rec.full_text_status = "url"
        db.commit()
        return "url"
    path = _store_pdf(workspace_id, rec.id, pdf)
    rec.full_text_path = str(path)
    rec.full_text_status = "fetched"
    db.commit()
    return "fetched"


def _run_fetch(workspace_id: int, email: str):
    from models import Record, SessionLocal
    db = SessionLocal()
    try:
        targets = (db.query(Record)
                     .filter(Record.workspace_id == workspace_id,
                             Record.is_removed == False,            # noqa: E712
                             Record.screen1_decision == "include",
                             Record.full_text_status.in_(["none", "failed", "url"])).all())
        total = len(targets)
        _set(workspace_id, "fetch", {"status": "running", "message": f"Fetching {total} OA PDFs…",
                                     "total": total, "done": 0, "fetched": 0, "url_only": 0, "failed": 0})
        fetched = url_only = failed = 0
        for i, rec in enumerate(targets):
            outcome = _fetch_record(db, workspace_id, rec, email)
            if outcome == "fetched":
                fetched += 1
            elif outcome == "url":
                url_only += 1
            else:
                failed += 1
            _update(workspace_id, "fetch", done=i + 1, fetched=fetched,
                    url_only=url_only, failed=failed)
        _set(workspace_id, "fetch", {"status": "done",
                                     "message": f"Done. {fetched} PDFs fetched, {url_only} OA link only, {failed} not found. Upload the rest, then convert.",
                                     "total": total, "done": total, "fetched": fetched,
                                     "url_only": url_only, "failed": failed})
    except Exception as exc:
        _set(workspace_id, "fetch", {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_fetch(workspace_id: int, email: str):
    threading.Thread(target=_run_fetch, args=(workspace_id, email), daemon=True).start()


# ── Pass 2: convert (paper2md) ──────────────────────────────────────────────────

def _run_convert(workspace_id: int, paper2md_url: str):
    from models import Record, SessionLocal
    db = SessionLocal()
    try:
        targets = (db.query(Record)
                     .filter(Record.workspace_id == workspace_id,
                             Record.is_removed == False,            # noqa: E712
                             Record.screen1_decision == "include",
                             Record.full_text_status == "fetched").all())
        total = len(targets)
        _set(workspace_id, "convert", {"status": "running", "message": f"Converting {total} PDFs…",
                                       "total": total, "done": 0, "converted": 0, "failed": 0})
        converted = failed = 0
        first_error = None
        for i, rec in enumerate(targets):
            try:
                convert_stored_pdf(db, rec, paper2md_url)
                converted += 1
            except Exception as exc:
                failed += 1          # PDF kept, status reverted to "fetched"
                if first_error is None:
                    first_error = str(exc)
            _update(workspace_id, "convert", done=i + 1, converted=converted, failed=failed)
        if converted == 0 and failed > 0:
            # Nothing came back — usually paper2md is unreachable. Say so instead
            # of reporting a quiet "done" the user can't act on.
            _set(workspace_id, "convert", {
                "status": "error",
                "message": f"No PDFs converted ({failed} failed). paper2md at {paper2md_url} — {first_error}",
                "error": first_error, "total": total, "done": total,
                "converted": 0, "failed": failed})
            return
        msg = f"Done. {converted} converted, {failed} failed."
        if first_error:
            msg += f" First failure: {first_error}"
        _set(workspace_id, "convert", {"status": "done", "message": msg,
                                       "total": total, "done": total,
                                       "converted": converted, "failed": failed})
    except Exception as exc:
        _set(workspace_id, "convert", {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_convert(workspace_id: int, paper2md_url: str):
    threading.Thread(target=_run_convert, args=(workspace_id, paper2md_url), daemon=True).start()


def paper2md_url() -> str:
    return os.environ.get("PAPER2MD_URL", "http://localhost:8008")
