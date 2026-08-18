"""
Full-text acquisition (step 6) + paper2md conversion (step 7).

Pass 1 — FETCH walks a ladder of candidate locations for each included record
and **converts inline**, because a candidate can only be checked once it is
text: every converted candidate is verified against the record's own title, and
one that turns out to be a different paper is discarded and the ladder
continues. Deferring conversion would mean accepting the first PDF that
downloads, whatever it contains. Outcome per record: "converted" (markdown in
hand), "fetched" (PDF stored but paper2md was unavailable — pass 2 will finish
it), "url" (only an OA link, for manual retrieval), or "failed".

The ladder, in order: Europe PMC full-text XML → OA PDFs → OA landing pages read
for citation_pdf_url → publisher TDM APIs → OA siblings, i.e. same-titled
OpenAlex works under a different DOI, which is where the preprint copy of a
paywalled article lives (OpenAlex indexes the two versions as separate works, so
nothing earlier in the ladder can see them).

Between the passes the human uploads what the ladder could not reach — each
upload just stores the file (status "fetched"), no conversion yet.

Pass 2 — CONVERT (paper2md): send every stored-but-unconverted PDF to the
paper2md service (POST /convert) and store the clean markdown. No reimplementation
— paper2md already does the hard part. A title mismatch here is recorded as a
warning rather than discarded: this path also carries the human's own uploads.

Both passes run as background threads. JOBS are keyed by (workspace_id, kind)
where kind is "fetch" or "convert", so their progress bars poll independently.

Title verification and the sibling rung are backported from Contrarian, where a
Zenodo deposit was accepted for a different paper of a similar name.
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
MAX_SIBLINGS = 4     # …and the same for the sibling rung

_WORD = re.compile(r"[a-z0-9]+")
LINE_THRESHOLD = 0.65   # sequence similarity: title vs a head line (or 2–3 joined)
BAG_THRESHOLD = 0.90    # fallback: near-total word coverage across the head
SIBLING_THRESHOLD = 0.90  # title similarity for a work to count as the same paper


class Paper2mdUnavailable(RuntimeError):
    """paper2md itself is unreachable or timing out, as opposed to refusing this
    particular PDF. Worth distinguishing: when the service is down, walking the
    rest of the ladder converting candidates would burn one long timeout each."""

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


def sibling_candidates(doi: str, title: str, email: str) -> list[tuple[str, str, str]]:
    """(kind, url, sibling_doi) for OA copies of *other* OpenAlex works with the
    same title — usually the preprint sibling of a paywalled publisher record (an
    arXiv copy, a repository deposit). OpenAlex indexes preprint and publisher
    versions as separate works, so the main ladder never sees them. Title
    verification downstream still guards against a same-titled different paper."""
    from difflib import SequenceMatcher
    tnorm = " ".join(_WORD.findall((title or "").lower()))
    if len(tnorm) < 12:
        return []
    try:
        r = _get("https://api.openalex.org/works",
                 params={"filter": f"title.search:{tnorm}", "per-page": 8, "mailto": email})
        works = r.json().get("results", []) if r.status_code == 200 else []
    except Exception:
        works = []
    out = []
    for w in works:
        wdoi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
        if not wdoi or wdoi == doi.lower():
            continue
        wnorm = " ".join(_WORD.findall((w.get("title") or "").lower()))
        if SequenceMatcher(None, tnorm, wnorm).ratio() < SIBLING_THRESHOLD:
            continue
        best = w.get("best_oa_location") or {}
        if best.get("pdf_url"):
            out.append(("pdf", best["pdf_url"], wdoi))
        elif best.get("landing_page_url"):
            out.append(("landing", best["landing_page_url"], wdoi))
    return out[:MAX_SIBLINGS]


def title_matches(md: str, title: str) -> bool:
    """Does this text plausibly belong to a record with this title?

    Bag-of-words overlap is NOT enough: within one literature the generic domain
    words (organ, donation, consent…) appear in every paper, so a short title can
    'match' a different paper entirely. So the primary test demands the title as a
    contiguous thing — some line of the document head, or 2–3 adjacent lines for a
    wrapped title, must contain the normalized title verbatim or resemble it by
    sequence similarity. Near-total word coverage stays as a fallback for heavily
    mangled front matter."""
    from difflib import SequenceMatcher
    tnorm = " ".join(_WORD.findall((title or "").lower()))
    if len(tnorm) < 12:
        return True                      # too short to verify meaningfully
    head = (md or "")[:3000].lower()
    lines = [" ".join(_WORD.findall(l)) for l in head.splitlines()]
    lines = [l for l in lines if l]
    for i in range(len(lines)):
        for j in (1, 2, 3):
            cand = " ".join(lines[i:i + j])
            if tnorm in cand:
                return True
            if SequenceMatcher(None, tnorm, cand).ratio() >= LINE_THRESHOLD:
                return True
    words = [w for w in tnorm.split() if len(w) > 3]
    if not words:
        return True
    headwords = " ".join(_WORD.findall(head))
    return sum(1 for w in words if w in headwords) / len(words) >= BAG_THRESHOLD


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
    """POST the PDF to paper2md and keep the WHOLE text — references and back
    matter included — for the reader. Back matter is stripped later, only for the
    LLM (see strip_back_matter). PAPER2MD_API_KEY is optional but lifts the upload
    cap (10MB anonymous → 50MB keyed), so papers need it."""
    headers = {}
    key = os.environ.get("PAPER2MD_API_KEY", "").strip()
    if key:
        headers["X-API-Key"] = key
    resp = requests.post(
        f"{paper2md_url.rstrip('/')}/convert",
        files={"file": ("paper.pdf", pdf_bytes, "application/pdf")},
        data={"remove_references": "false", "remove_end_matter": "false", "format": "json"},
        headers=headers,
        timeout=360,
    )
    if not resp.ok:
        code = resp.status_code
        body = (resp.text or "").strip()
        # A Cloudflare/edge error page, not paper2md itself.
        if code == 524 or (code >= 520 and "<html" in body[:400].lower()):
            raise Paper2mdUnavailable(
                f"paper2md timed out at the proxy ({code}, Cloudflare's ~100s limit) — the "
                "PDF is large/slow to convert. Point PAPER2MD_URL at paper2md's internal "
                "address so the call skips the proxy.")
        if code in (502, 503, 504):
            raise Paper2mdUnavailable(f"paper2md unreachable ({code})")
        # surface paper2md's own complaint (bad key, too large, queue full…)
        raise RuntimeError(f"paper2md {code}: {body[:200]}")
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
    """Convert an already-stored PDF to markdown and persist it. Returns status.

    The title check here *warns* instead of discarding: this path also carries the
    human's own uploads, and a reviewer who deliberately attached a file outranks
    a heuristic. The warning still surfaces, so a wrong attachment is visible."""
    pdf_bytes = Path(rec.full_text_path).read_bytes()
    try:
        md = pdf_to_markdown(pdf_bytes, paper2md_url)
    except Exception as exc:
        rec.full_text_status = "fetched"  # PDF is here, conversion failed
        db.commit()
        raise RuntimeError(f"paper2md conversion failed: {exc}") from exc
    rec.full_text_md = md
    rec.full_text_status = "converted"
    if (rec.title or "").strip() and not title_matches(md, rec.title):
        rec.full_text_note = ("check this PDF: its text does not match this "
                              "record's title")
    db.commit()
    return "converted"


def store_uploaded_pdf(db, workspace_id: int, rec, pdf_bytes: bytes):
    """Manual upload path: store the PDF and mark it fetched. Conversion is
    deferred to the paper2md pass, so uploads work even if paper2md is down."""
    path = _store_pdf(workspace_id, rec.id, pdf_bytes)
    rec.full_text_path = str(path)
    rec.full_text_status = "fetched"
    rec.full_text_note = None      # the human chose this file; drop earlier notes
    db.commit()


def docx_to_markdown(docx_bytes: bytes) -> str:
    """.docx → markdown: paragraph text, with Heading/Title styles as #-levels."""
    import io
    import docx
    doc = docx.Document(io.BytesIO(docx_bytes))
    out = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        style = (p.style.name if p.style else "").lower()
        if style == "title":
            out.append("# " + t)
        elif style.startswith("heading"):
            lvl = "".join(c for c in style if c.isdigit())
            out.append("#" * (int(lvl) if lvl else 2) + " " + t)
        else:
            out.append(t)
    return "\n\n".join(out).strip()


def ingest_upload(db, workspace_id: int, rec, filename: str, data: bytes) -> str:
    """Manual full-text upload of pdf / docx / md / txt. A PDF is stored for the
    convert pass; the text formats already are the full text, so they go straight
    to converted."""
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        store_uploaded_pdf(db, workspace_id, rec, data)
        return "fetched"
    if name.endswith(".docx") or data[:2] == b"PK":   # docx is a zip
        md = docx_to_markdown(data)
    else:                                             # md / markdown / txt / plain
        md = data.decode("utf-8", errors="replace")
    md = md.strip()
    if not md:
        raise ValueError("file is empty or unreadable")
    rec.full_text_md = md
    rec.full_text_path = None
    rec.full_text_status = "converted"
    rec.full_text_note = None      # the human chose this file
    db.commit()
    return "converted"


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


def _note(notes, text: str):
    if notes is not None:
        notes.add(text)


def _elsevier(doi: str, key: str, inst: str = "", notes=None) -> str | None:
    """ScienceDirect Article Retrieval.

    The key on its own is not enough — not even for open access. Elsevier grants
    full text only to an entitled *requestor*: a call from the institution's IP
    range, or one carrying X-ELS-Insttoken (which the library obtains from
    Elsevier). Off-network without a token every request 403s, metadata included.
    """
    if not key:
        return None
    headers = {"X-ELS-APIKey": key}
    if inst:
        headers["X-ELS-Insttoken"] = inst
    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    try:
        r = _get(url, headers={**headers, "Accept": "text/plain"}, timeout=45)
        if r.status_code == 200 and "text/plain" in r.headers.get("Content-Type", ""):
            text = r.text.strip()
            if len(text) > 500:
                return text
        if r.status_code in (401, 403):
            _note(notes, f"Elsevier refused the key ({r.status_code})"
                         + ("" if inst else " — needs an institutional token, or a call from the institution's network"))
            return None
        # some articles only come back structured — take the text out of the JSON
        r = _get(url, headers={**headers, "Accept": "application/json"}, timeout=45)
        if r.status_code in (401, 403):
            _note(notes, f"Elsevier refused the key ({r.status_code})")
            return None
        if r.status_code == 200:
            body = (r.json().get("full-text-retrieval-response") or {})
            text = (body.get("originalText") or "")
            if isinstance(text, str) and len(text.strip()) > 500:
                return text.strip()
    except Exception:
        pass
    return None


def _springer(doi: str, key: str, notes=None) -> str | None:
    """Springer Nature's *Open Access* API returns JATS — the same shape Europe
    PMC gives us, so it reuses the same converter. Open-access content only;
    subscription content needs a separate TDM agreement."""
    if not key:
        return None
    try:
        r = _get("https://api.springernature.com/openaccess/jats",
                 params={"q": f"doi:{doi}", "api_key": key}, timeout=45)
        if r.status_code in (401, 403):
            _note(notes, f"Springer refused the key ({r.status_code}) — is it the Open Access API key?")
            return None
        if r.status_code == 200 and b"<" in r.content[:200]:
            md = jats_to_markdown(r.content)
            return md if len(md) > 500 else None
    except Exception:
        pass
    return None


def _wiley(doi: str, token: str, notes=None) -> bytes | None:
    """Wiley TDM serves a PDF. The client token comes from static.wiley.com/tdm
    under the institution's entitlement."""
    if not token:
        return None
    try:
        r = _get(f"https://api.wiley.com/onlinelibrary/tdm/v1/articles/{quote(doi, safe='')}",
                 headers={"Wiley-TDM-Client-Token": token}, timeout=60, allow_redirects=True)
        if r.status_code in (401, 403):
            _note(notes, f"Wiley refused the token ({r.status_code})")
            return None
        if r.status_code == 200 and r.content[:4] == b"%PDF" and len(r.content) > 10_000:
            return r.content
    except Exception:
        pass
    return None


def publisher_fulltext(doi: str, keys: dict | None = None,
                       notes=None) -> tuple[str | None, bytes | None]:
    """(markdown, pdf_bytes) from whichever publisher owns this DOI prefix.
    `keys` carries the reviewer's own credentials; a publisher with no key is
    skipped entirely, so an unconfigured LSSR never calls out. Credentials a
    publisher rejects are reported through `notes` — a misconfigured key is a
    thing to fix, not a paper that doesn't exist."""
    keys = keys or {}
    prefix = doi.split("/")[0]
    if prefix in ELSEVIER_PREFIXES:
        md = _elsevier(doi, keys.get("elsevier", ""), keys.get("elsevier_insttoken", ""), notes)
        if md:
            return md, None
    if prefix in SPRINGER_PREFIXES:
        md = _springer(doi, keys.get("springer", ""), notes)
        if md:
            return md, None
    if prefix in WILEY_PREFIXES:
        pdf = _wiley(doi, keys.get("wiley", ""), notes)
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


def _fetch_record(db, workspace_id: int, rec, email: str, keys: dict,
                  paper2md_url: str, notes=None) -> str:
    """Walk the candidate ladder, converting each candidate as it arrives and
    keeping only one that verifies against this record's own title. A candidate
    that converts to a different paper is discarded and the walk continues — the
    check is the whole point of converting inline. If every candidate is
    unreachable (publisher bot walls, mostly) keep the best URL we saw, so the
    record lands in the manual-upload queue with a link instead of a dead end."""
    doi = (rec.doi or "").strip()
    if not doi:
        rec.full_text_status = "failed"
        db.commit()
        return "failed"

    expected = (rec.title or "").strip()
    own: set = set()          # notes about this record, kept on the record itself
    if not expected:
        own.add("no title on this record — retrieved content could not be verified")

    fallback = None           # best OA URL seen, for the manual queue
    held_pdf = None           # a PDF we have but could not convert (paper2md down)
    p2m_down = False

    def _accept(md: str, provider: str, url: str, pdf: bytes | None) -> bool:
        """Persist this candidate if it really is this record's paper."""
        if expected and not title_matches(md, expected):
            own.add(f"discarded {provider}: the text retrieved is a different "
                    f"paper from this record's title ({url})")
            return False
        if pdf is not None:
            rec.full_text_path = str(_store_pdf(workspace_id, rec.id, pdf))
        rec.full_text_md = md
        rec.full_text_url = url
        rec.full_text_status = "converted"
        rec.full_text_note = "; ".join(sorted(own)) or None
        db.commit()
        return True

    def _convert(pdf: bytes) -> str | None:
        """Convert, distinguishing 'paper2md is down' from 'this PDF failed'. A
        PDF is held for pass 2 only when *conversion* failed: one that converted
        to the wrong paper must not be kept, or pass 2 would accept it later and
        undo the check we just made."""
        nonlocal p2m_down, held_pdf
        try:
            return pdf_to_markdown(pdf, paper2md_url)
        except Paper2mdUnavailable as exc:
            p2m_down = True
            held_pdf = held_pdf or pdf
            _note(notes, str(exc))
            own.add(str(exc))
            return None
        except RuntimeError as exc:
            _note(notes, str(exc))
            own.add(str(exc))
            return None

    def _walk(rungs, provider: str = "oa_pdf") -> bool:
        nonlocal fallback
        for kind, url in rungs:
            if kind == "xml":
                # Full text straight from Europe PMC: no PDF, no paper2md round trip.
                md = _fetch_jats(url)
                if md and _accept(md, "europepmc_xml", url, None):
                    return True
                continue
            if p2m_down:
                continue          # nothing downstream can be converted right now
            if kind == "pdf":
                fallback = fallback or url
                pdf = _download_pdf(url)
            else:
                pdf, pdf_url = pdf_from_landing(url)
                fallback = fallback or pdf_url or url
                if pdf is None and pdf_url:
                    pdf = _download_pdf(pdf_url)
            if not pdf:
                continue
            md = _convert(pdf)
            if md and _accept(md, provider, url, pdf):
                return True
        return False

    if _walk(candidates(doi, email)):
        return "converted"

    # Next rung: the publisher's own TDM API, for what OA channels can't reach.
    if not p2m_down:
        md, pdf = publisher_fulltext(doi, keys, notes)
        doi_url = f"https://doi.org/{doi}"
        if md and _accept(md, "publisher_tdm", doi_url, None):
            return "converted"
        if pdf:
            md = _convert(pdf)
            if md and _accept(md, "publisher_tdm", doi_url, pdf):
                return "converted"

    # Last rung: OA siblings — same-titled OpenAlex works under another DOI, where
    # the preprint copy of a paywalled article lives. Needs a title to match on.
    if expected and not p2m_down:
        for kind, url, wdoi in sibling_candidates(doi, expected, email):
            own.add(f"OA sibling tried: {wdoi} (same title, different DOI)")
            if _walk([(kind, url)], provider=f"oa_sibling {wdoi}"):
                return "converted"

    # Nothing verified. If we hold a PDF, keep it: pass 2 converts it once
    # paper2md is back, rather than throwing away a download we already paid for.
    if held_pdf is not None:
        rec.full_text_path = str(_store_pdf(workspace_id, rec.id, held_pdf))
        rec.full_text_status = "fetched"
        rec.full_text_note = "; ".join(sorted(own)) or None
        db.commit()
        return "fetched"

    rec.full_text_note = "; ".join(sorted(own)) or None
    if fallback:
        rec.full_text_url = fallback      # keep the OA URL for manual retrieval
        rec.full_text_status = "url"
        db.commit()
        return "url"
    rec.full_text_status = "failed"
    db.commit()
    return "failed"


# A record excluded at screening 2 is decided: its full text is no longer needed,
# so both passes skip it. Without this a re-run keeps walking the OA ladder for
# papers the reviewer has already dropped — and keeps them in the "to fetch" count.
def _not_dropped_at_screen2():
    """SQLAlchemy clause for "not excluded at screening 2", NULL-safe: a plain
    `!= "exclude"` drops rows whose decision is NULL, which would quietly shrink
    the pool instead of failing."""
    from sqlalchemy import or_
    from models import Record
    return or_(Record.screen2_decision.is_(None),
               Record.screen2_decision != "exclude")


def _run_fetch(workspace_id: int, email: str, keys: dict | None = None,
               p2m_url: str | None = None):
    from models import Record, SessionLocal
    db = SessionLocal()
    p2m_url = p2m_url or paper2md_url()
    try:
        targets = (db.query(Record)
                     .filter(Record.workspace_id == workspace_id,
                             Record.is_removed == False,            # noqa: E712
                             Record.screen1_decision == "include",
                             _not_dropped_at_screen2(),
                             Record.full_text_status.in_(["none", "failed", "url"])).all())
        total = len(targets)
        _set(workspace_id, "fetch", {"status": "running", "message": f"Fetching {total} full texts…",
                                     "total": total, "done": 0, "fetched": 0, "converted": 0,
                                     "url_only": 0, "failed": 0, "mismatched": 0})
        fetched = converted = url_only = failed = mismatched = 0
        notes = set()
        for i, rec in enumerate(targets):
            outcome = _fetch_record(db, workspace_id, rec, email, keys or {}, p2m_url, notes)
            if outcome == "fetched":
                fetched += 1
            elif outcome == "converted":
                converted += 1
            elif outcome == "url":
                url_only += 1
            else:
                failed += 1
            if "different paper" in (rec.full_text_note or ""):
                mismatched += 1
            _update(workspace_id, "fetch", done=i + 1, fetched=fetched, converted=converted,
                    url_only=url_only, failed=failed, mismatched=mismatched)
        msg = (f"Done. {converted} full texts retrieved and verified, {fetched} PDFs stored "
               f"but not converted, {url_only} OA link only, {failed} not found.")
        if mismatched:
            msg += (f" {mismatched} record(s) had a candidate discarded for belonging to a "
                    f"different paper — see the note on the record.")
        if fetched:
            msg += " Convert the stored PDFs next."
        if notes:
            msg += " ⚠ " + " ".join(sorted(notes))
        _set(workspace_id, "fetch", {"status": "done", "message": msg,
                                     "total": total, "done": total, "fetched": fetched,
                                     "converted": converted, "url_only": url_only,
                                     "failed": failed, "mismatched": mismatched})
    except Exception as exc:
        _set(workspace_id, "fetch", {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_fetch(workspace_id: int, email: str, keys: dict | None = None,
                p2m_url: str | None = None):
    _set(workspace_id, "fetch", {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run_fetch,
                     args=(workspace_id, email, keys or {}, p2m_url or paper2md_url()),
                     daemon=True).start()


# ── Pass 2: convert (paper2md) ──────────────────────────────────────────────────

def _run_convert(workspace_id: int, paper2md_url: str):
    from models import Record, SessionLocal
    db = SessionLocal()
    try:
        targets = (db.query(Record)
                     .filter(Record.workspace_id == workspace_id,
                             Record.is_removed == False,            # noqa: E712
                             Record.screen1_decision == "include",
                             _not_dropped_at_screen2(),
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
    _set(workspace_id, "convert", {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run_convert, args=(workspace_id, paper2md_url), daemon=True).start()


def paper2md_url() -> str:
    return os.environ.get("PAPER2MD_URL", "http://localhost:8008")


_BACK_MATTER = re.compile(
    r"(?im)^\s*#{1,6}\s*(references|bibliography|works cited|literature cited|"
    r"acknowledge?ments?|funding|conflicts? of interest|competing interests|"
    r"declaration of competing interest|declarations?|author contributions?|"
    r"supplementary( material)?|data availability)\b")


def strip_back_matter(md: str) -> str:
    """Drop everything from the first references/back-matter heading onward, so
    the LLM reads the article, not its bibliography. Kept out of what the reader
    sees. Guarded against a false positive that would gut the text (a match in the
    first 40% is ignored)."""
    if not md:
        return md
    m = _BACK_MATTER.search(md)
    if m and m.start() >= len(md) * 0.4:
        return md[:m.start()].rstrip()
    return md
