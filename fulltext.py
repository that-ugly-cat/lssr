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
import re
import threading
from pathlib import Path
from urllib.parse import quote, urljoin

import requests

DATA_ROOT = Path("data/fulltext")
UA = {"User-Agent": "Mozilla/5.0 (compatible; LSSR/1.0)"}
TIMEOUT = 20
MAX_CANDIDATES = 8   # bound the work per record

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


# ── Candidate PDF locations (step 6) ───────────────────────────────────────────
#
# Unpaywall's url_for_pdf alone misses a lot. For hybrid OA it is very often null
# — Unpaywall knows only a landing page — and best_oa_location tends to be the
# publisher's copy, which is exactly the one behind a bot wall. So: gather
# candidates from several providers, prefer repository copies (no bot walls), try
# every direct PDF before paying for a landing-page fetch, and read the PDF link
# out of landing pages via the citation_pdf_url meta tag most publishers emit
# (the same trick Zotero and Scholar use).


def _get(url: str, **kw):
    kw.setdefault("timeout", TIMEOUT)
    kw["headers"] = {**UA, **(kw.get("headers") or {})}
    return requests.get(url, **kw)


def _europepmc(doi: str):
    """Europe PMC's PDF routes 404 for us (including the one its own API
    advertises), but fullTextXML serves the whole article — no bot wall, no
    upload cap, and cleaner than anything we'd get back out of a PDF. For a
    PubMed-shaped corpus this is the highest-yield source, so we take the XML."""
    results = []
    try:
        r = _get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                 params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"})
        if r.status_code == 200:
            results = ((r.json().get("resultList") or {}).get("result") or [])[:1]
    except Exception:
        results = []
    for it in results:
        pmcid = it.get("pmcid")
        if pmcid and (it.get("isOpenAccess") == "Y" or it.get("inEPMC") == "Y"):
            yield ("xml", f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML")


def _unpaywall(doi: str, email: str):
    locs = []
    try:
        r = _get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email})
        if r.status_code == 200:
            locs = r.json().get("oa_locations") or []
    except Exception:
        locs = []
    repo_first = sorted(locs, key=lambda l: 0 if l.get("host_type") == "repository" else 1)
    for loc in repo_first:
        if loc.get("url_for_pdf"):
            yield ("pdf", loc["url_for_pdf"])
    for loc in repo_first:
        if loc.get("url_for_landing_page"):
            yield ("landing", loc["url_for_landing_page"])


def _openalex(doi: str, email: str):
    data = {}
    try:
        r = _get(f"https://api.openalex.org/works/doi:{doi}", params={"mailto": email})
        if r.status_code == 200:
            data = r.json()
    except Exception:
        data = {}
    locs = [l for l in (data.get("locations") or []) if l.get("is_oa")]
    repo_first = sorted(locs, key=lambda l: 0 if (l.get("source") or {}).get("type") == "repository" else 1)
    for loc in repo_first:
        if loc.get("pdf_url"):
            yield ("pdf", loc["pdf_url"])
    best = data.get("best_oa_location") or {}
    if best.get("pdf_url"):
        yield ("pdf", best["pdf_url"])
    for loc in repo_first:
        if loc.get("landing_page_url"):
            yield ("landing", loc["landing_page_url"])
    if best.get("landing_page_url"):
        yield ("landing", best["landing_page_url"])


_KIND_ORDER = {"xml": 0, "pdf": 1, "landing": 2}


def candidates(doi: str, email: str) -> list[tuple[str, str]]:
    """Ordered, de-duplicated (kind, url): full-text XML first (cleanest, always
    reachable), then direct PDFs, then landing pages (which cost an extra fetch)."""
    seen, out = set(), []
    for gen in (_europepmc(doi), _unpaywall(doi, email), _openalex(doi, email)):
        for kind, url in gen:
            if url and url not in seen:
                seen.add(url)
                out.append((kind, url))
    out.sort(key=lambda c: _KIND_ORDER[c[0]])   # stable — provider order kept within a kind
    return out[:MAX_CANDIDATES]


# ── JATS full text → markdown ──────────────────────────────────────────────────

_SKIP_TAGS = {"ref-list", "back", "fn-group", "table-wrap", "fig", "graphic",
              "supplementary-material", "table", "front", "journal-meta"}


def _itext(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _walk_jats(el, level: int, out: list):
    for child in el:
        tag = child.tag.split("}")[-1]
        if tag in _SKIP_TAGS:
            continue
        if tag == "sec":
            title = child.find("title")
            if title is not None:
                heading = _itext(title)
                if heading:
                    out.append("#" * min(level, 6) + " " + heading)
            _walk_jats(child, level + 1, out)
        elif tag == "title":
            continue                      # emitted by the parent sec
        elif tag in ("p", "caption"):
            text = _itext(child)
            if text:
                out.append(text)
        else:
            _walk_jats(child, level, out)


def _heading_level(block: str) -> int:
    return len(block) - len(block.lstrip("#"))


def _prune_empty_sections(blocks: list) -> list:
    """Drop headings left with nothing under them — dropping a ref-list leaves its
    'References' title behind. A heading followed by a *deeper* one still has
    content (its subsections), so only same-or-higher level (or the end) counts as
    empty. Walking backwards makes it cascade."""
    kept = []
    for b in reversed(blocks):
        if b.startswith("#"):
            nxt = kept[-1] if kept else None
            if nxt is None or (nxt.startswith("#") and _heading_level(nxt) <= _heading_level(b)):
                continue
        kept.append(b)
    return list(reversed(kept))


def jats_to_markdown(xml_bytes: bytes) -> str:
    """JATS full text → markdown. References, figures and tables are dropped —
    the same shape paper2md returns for a PDF."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_bytes)
    out = []
    title = root.find(".//article-title")
    if title is not None and _itext(title):
        out.append("# " + _itext(title))
    abstract = root.find(".//abstract")
    if abstract is not None:
        out.append("## Abstract")
        _walk_jats(abstract, 3, out)
    body = root.find(".//body")
    if body is not None:
        _walk_jats(body, 2, out)
    return "\n\n".join(_prune_empty_sections(out)).strip()


_PDF_META = re.compile(
    r'<meta[^>]*?(?:name|property)=["\']citation_pdf_url["\'][^>]*?content=["\']([^"\']+)["\']', re.I)
_PDF_META_REV = re.compile(
    r'<meta[^>]*?content=["\']([^"\']+)["\'][^>]*?(?:name|property)=["\']citation_pdf_url["\']', re.I)


def pdf_from_landing(url: str) -> tuple[bytes | None, str | None]:
    """Resolve a landing page to a PDF. Returns (pdf_bytes, pdf_url): a landing
    page may redirect straight to the PDF, otherwise we read citation_pdf_url."""
    try:
        r = _get(url, allow_redirects=True, timeout=25)
    except Exception:
        return None, None
    if not r.ok:
        return None, None
    ctype = r.headers.get("Content-Type", "")
    if "pdf" in ctype or r.content[:4] == b"%PDF":
        return (r.content if len(r.content) > 10_000 else None), r.url
    if "html" not in ctype:
        return None, None
    m = _PDF_META.search(r.text) or _PDF_META_REV.search(r.text)
    return None, urljoin(r.url, m.group(1)) if m else None


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
    """POST the PDF to paper2md. PAPER2MD_API_KEY is optional but lifts the
    upload cap (10MB anonymous → 50MB keyed), so papers need it."""
    headers = {}
    key = os.environ.get("PAPER2MD_API_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key
    resp = requests.post(
        f"{paper2md_url.rstrip('/')}/convert",
        files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
        data={"remove_references": "true", "format": "json"},
        headers=headers,
        timeout=360,
    )
    if not resp.ok:
        # surface paper2md's own complaint (bad key, too large, queue full…)
        raise RuntimeError(f"paper2md {resp.status_code}: {resp.text[:200]}")
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

# ── Publisher TDM APIs (last layer) ────────────────────────────────────────────
#
# What the OA channels can't reach is, mostly, the publisher's own copy behind a
# bot wall. The sanctioned way in is each publisher's text-and-data-mining API,
# which is built for exactly this: systematic full-text retrieval for research,
# under the subscription an institution already pays for. Each provider is tried
# only when its key is configured and only for DOIs with that publisher's prefix,
# so we never spend a call we know will fail.

ELSEVIER_PREFIXES = {"10.1016", "10.1006", "10.1053", "10.1054", "10.1078", "10.5555"}
WILEY_PREFIXES    = {"10.1002", "10.1111", "10.1046", "10.1034", "10.1049"}
SPRINGER_PREFIXES = {"10.1007", "10.1186", "10.1038", "10.1140", "10.1057", "10.1245"}


def _elsevier(doi: str) -> str | None:
    """ScienceDirect Article Retrieval. The API key alone reaches open-access
    articles; entitled subscription content also needs X-ELS-Insttoken (issued to
    the institution) or a call from its IP range."""
    key = os.environ.get("ELSEVIER_API_KEY", "").strip()
    if not key:
        return None
    headers = {"X-ELS-APIKey": key}
    inst = os.environ.get("ELSEVIER_INSTTOKEN", "").strip()
    if inst:
        headers["X-ELS-Insttoken"] = inst
    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    try:
        r = _get(url, headers={**headers, "Accept": "text/plain"}, timeout=45)
        if r.status_code == 200 and "text/plain" in r.headers.get("Content-Type", ""):
            text = r.text.strip()
            if len(text) > 500:
                return text
        # some articles only come back structured — take the text out of the JSON
        r = _get(url, headers={**headers, "Accept": "application/json"}, timeout=45)
        if r.status_code == 200:
            body = (r.json().get("full-text-retrieval-response") or {})
            text = (body.get("originalText") or "")
            if isinstance(text, str) and len(text.strip()) > 500:
                return text.strip()
    except Exception:
        pass
    return None


def _springer(doi: str) -> str | None:
    """Springer Nature's open-access API returns JATS — the same shape Europe PMC
    gives us, so it reuses the same converter. Subscription content needs a
    separate TDM agreement and is not covered here."""
    key = os.environ.get("SPRINGER_API_KEY", "").strip()
    if not key:
        return None
    try:
        r = _get("https://api.springernature.com/openaccess/jats",
                 params={"q": f"doi:{doi}", "api_key": key}, timeout=45)
        if r.status_code == 200 and b"<" in r.content[:200]:
            md = jats_to_markdown(r.content)
            return md if len(md) > 500 else None
    except Exception:
        pass
    return None


def _wiley(doi: str) -> bytes | None:
    """Wiley TDM serves a PDF. The client token is issued from a Wiley Online
    Library account with the institution's entitlement."""
    token = os.environ.get("WILEY_TDM_TOKEN", "").strip()
    if not token:
        return None
    try:
        r = _get(f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{quote(doi, safe='')}",
                 headers={"Wiley-TDM-Client-Token": token}, timeout=60, allow_redirects=True)
        if r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 10_000:
            return r.content
    except Exception:
        pass
    return None


def publisher_fulltext(doi: str) -> tuple[str | None, bytes | None]:
    """(markdown, pdf_bytes) from whichever publisher owns this DOI prefix."""
    prefix = doi.split("/")[0]
    if prefix in ELSEVIER_PREFIXES:
        md = _elsevier(doi)
        if md:
            return md, None
    if prefix in SPRINGER_PREFIXES:
        md = _springer(doi)
        if md:
            return md, None
    if prefix in WILEY_PREFIXES:
        pdf = _wiley(doi)
        if pdf:
            return None, pdf
    return None, None


def _fetch_jats(url: str) -> str | None:
    try:
        r = _get(url, timeout=30)
        if r.status_code != 200:
            return None
        # Europe PMC serves some articles with an XML declaration and others with
        # a bare DOCTYPE — sniff for either rather than one exact prefix.
        head = r.content.lstrip()[:120]
        if not head.startswith((b"<?xml", b"<!DOCTYPE", b"<article")):
            return None
        md = jats_to_markdown(r.content)
        return md if len(md) > 500 else None
    except Exception:
        return None


def _fetch_record(db, workspace_id: int, rec, email: str) -> str:
    """Walk the candidate ladder until a real PDF comes back. If every candidate
    is unreachable (publisher bot walls, mostly) keep the best URL we saw so the
    record lands in the manual-upload queue with a link instead of a dead end."""
    doi = (rec.doi or "").strip()
    if not doi:
        rec.full_text_status = "failed"
        db.commit()
        return "failed"

    fallback = None
    for kind, url in candidates(doi, email):
        pdf = None
        if kind == "xml":
            # Full text straight from Europe PMC: no PDF to store, no paper2md
            # round trip — this record is already done.
            md = _fetch_jats(url)
            if md:
                rec.full_text_md = md
                rec.full_text_url = url
                rec.full_text_status = "converted"
                db.commit()
                return "converted"
            continue
        if kind == "pdf":
            fallback = fallback or url
            pdf = _download_pdf(url)
        else:
            pdf, pdf_url = pdf_from_landing(url)
            fallback = fallback or pdf_url or url
            if pdf is None and pdf_url:
                pdf = _download_pdf(pdf_url)
        if pdf:
            path = _store_pdf(workspace_id, rec.id, pdf)
            rec.full_text_path = str(path)
            rec.full_text_status = "fetched"
            db.commit()
            return "fetched"

    # Last layer: the publisher's own TDM API, for what OA channels can't reach.
    md, pdf = publisher_fulltext(doi)
    if md:
        rec.full_text_md = md
        rec.full_text_status = "converted"
        db.commit()
        return "converted"
    if pdf:
        path = _store_pdf(workspace_id, rec.id, pdf)
        rec.full_text_path = str(path)
        rec.full_text_status = "fetched"
        db.commit()
        return "fetched"

    if fallback:
        rec.full_text_url = fallback      # keep the OA URL for manual retrieval
        rec.full_text_status = "url"
        db.commit()
        return "url"
    rec.full_text_status = "failed"
    db.commit()
    return "failed"


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
        _set(workspace_id, "fetch", {"status": "running", "message": f"Fetching {total} full texts…",
                                     "total": total, "done": 0, "fetched": 0, "converted": 0,
                                     "url_only": 0, "failed": 0})
        fetched = converted = url_only = failed = 0
        for i, rec in enumerate(targets):
            outcome = _fetch_record(db, workspace_id, rec, email)
            if outcome == "fetched":
                fetched += 1
            elif outcome == "converted":
                converted += 1
            elif outcome == "url":
                url_only += 1
            else:
                failed += 1
            _update(workspace_id, "fetch", done=i + 1, fetched=fetched, converted=converted,
                    url_only=url_only, failed=failed)
        msg = (f"Done. {converted} full texts straight from Europe PMC, {fetched} PDFs fetched, "
               f"{url_only} OA link only, {failed} not found.")
        if fetched:
            msg += " Convert the PDFs next."
        _set(workspace_id, "fetch", {"status": "done", "message": msg,
                                     "total": total, "done": total, "fetched": fetched,
                                     "converted": converted, "url_only": url_only, "failed": failed})
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
