"""
Full-text acquisition (step 6) + paper2md conversion (step 7).

Step 6: try Unpaywall for an OA PDF (adapted from RevMaster's pdf_fetch). If the
PDF downloads, store it; if only a URL is available, keep it as a fallback for
manual retrieval. Manual PDF upload covers everything Unpaywall can't reach.

Step 7: send the PDF to the paper2md service (POST /convert) and store the clean
markdown on the record. No reimplementation — paper2md already does the hard part.

Batch runs as a background thread over the included pool (screen1 = include) whose
full text isn't converted yet. JOBS keyed by workspace_id.
"""
import os
import threading
from pathlib import Path

import requests

DATA_ROOT = Path("data/fulltext")
UA = {"User-Agent": "Mozilla/5.0 (compatible; LSSR/1.0)"}

JOBS: dict[int, dict] = {}
_lock = threading.Lock()


def get_job(workspace_id: int) -> dict | None:
    with _lock:
        return JOBS.get(workspace_id)


def _set(workspace_id: int, data: dict):
    with _lock:
        JOBS[workspace_id] = data


def _update(workspace_id: int, **kw):
    with _lock:
        if workspace_id in JOBS:
            JOBS[workspace_id].update(kw)


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


# ── Single-record pipeline ─────────────────────────────────────────────────────

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


def ingest_uploaded_pdf(db, workspace_id: int, rec, pdf_bytes: bytes, paper2md_url: str):
    """Manual upload path (synchronous): store + convert one record's PDF."""
    path = _store_pdf(workspace_id, rec.id, pdf_bytes)
    rec.full_text_path = str(path)
    rec.full_text_status = "fetched"
    db.commit()
    return convert_stored_pdf(db, rec, paper2md_url)


def _process_record(db, workspace_id: int, rec, email: str, paper2md_url: str) -> str:
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
    try:
        return convert_stored_pdf(db, rec, paper2md_url)
    except Exception:
        return "fetched"


# ── Batch background job ───────────────────────────────────────────────────────

def _run(workspace_id: int, email: str, paper2md_url: str):
    from models import Record, SessionLocal
    db = SessionLocal()
    try:
        targets = (db.query(Record)
                     .filter(Record.workspace_id == workspace_id,
                             Record.is_removed == False,            # noqa: E712
                             Record.screen1_decision == "include",
                             Record.full_text_status.in_(["none", "failed", "url"])).all())
        total = len(targets)
        _set(workspace_id, {"status": "running", "message": f"Fetching {total} full texts…",
                            "total": total, "done": 0, "converted": 0, "url_only": 0, "failed": 0})
        converted = url_only = failed = 0
        for i, rec in enumerate(targets):
            outcome = _process_record(db, workspace_id, rec, email, paper2md_url)
            if outcome == "converted":
                converted += 1
            elif outcome == "url":
                url_only += 1
            elif outcome in ("failed", "fetched"):
                failed += 1
            _update(workspace_id, done=i + 1, converted=converted,
                    url_only=url_only, failed=failed)
        _set(workspace_id, {"status": "done",
                            "message": f"Done. {converted} converted, {url_only} URL-only, {failed} unresolved.",
                            "total": total, "done": total, "converted": converted,
                            "url_only": url_only, "failed": failed})
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_fulltext(workspace_id: int, email: str, paper2md_url: str):
    threading.Thread(target=_run, args=(workspace_id, email, paper2md_url),
                     daemon=True).start()


def paper2md_url() -> str:
    return os.environ.get("PAPER2MD_URL", "http://localhost:8008")
