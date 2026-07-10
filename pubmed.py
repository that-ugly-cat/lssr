"""
PubMed search & download (step 1), adapted from TopicTracker's pipeline.py.

Runs in a background thread; progress is tracked in the global JOBS dict keyed by
workspace_id (one PubMed run per workspace at a time). On completion the parsed
references are folded into the workspace's Record pool via ingest.ingest_references.

Job dict:
  status: 'searching' | 'downloading' | 'done' | 'error'
  message, total, downloaded, new_count, merged_count, error
"""
import re
import threading
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import urllib3

http = urllib3.PoolManager()
RATE_LIMIT = 0.4  # seconds between NCBI requests (E-utilities policy)

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


# ── NCBI helpers ───────────────────────────────────────────────────────────────

def _get_pmids(query: str) -> list[str]:
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
           f"?db=pubmed&retmax=100000&term={quote_plus(query)}")
    content = http.request("GET", url).data.decode("utf-8")
    root = ET.fromstring(content)
    return [x.text for x in root.findall("IdList/Id")]


def _fetch_medline(pmid: str) -> str:
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
           f"?db=pubmed&rettype=medline&id={pmid}")
    return http.request("GET", url).data.decode("utf-8")


def parse_medline(article: str) -> dict:
    """Parse one MEDLINE record into a normalized reference dict (ingest schema)."""
    article = re.sub(re.compile(r"\n\s{2,}", re.MULTILINE), " ", article)

    def find(pattern):
        m = re.search(pattern, article)
        return m.group(0).strip() if m else ""

    def findall(pattern):
        return [x.strip() for x in re.findall(re.compile(pattern), article)]

    pt = findall(r"(?<=PT\s\s-\s).*")
    if any("book chapter" in p.lower() for p in pt):
        rtype = "book_chapter"
    elif any(p.lower() == "book" for p in pt):
        rtype = "book"
    else:
        rtype = "article"

    year = find(r"(?<=DP\s\s-\s)\d{4}")
    doi = find(r"(?<=AID\s-\s).*(?=\s\[doi)")
    title = find(r"(?<=TI\s\s-\s).*") or find(r"(?<=BTI\s-\s).*")
    return {
        "type": rtype,
        "authors": ", ".join(findall(r"(?<=AU\s\s-\s).*")) or ", ".join(findall(r"(?<=ED\s\s-\s).*")),
        "year": int(year) if year else None,
        "title": title,
        "abstract": find(r"(?<=AB\s\s-\s).*") or find(r"(?<=OAB\s-\s).*"),
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else "",
        "source": find(r"(?<=JT\s\s-\s).*") or find(r"(?<=PB\s\s-\s).*"),
        "keywords": findall(r"(?<=OT\s\s-\s).*"),
        "mesh": findall(r"(?<=MH\s\s-\s).*"),
        "language": find(r"(?<=LA\s\s-\s).*"),
        "database": "pubmed",
    }


# ── Background job ─────────────────────────────────────────────────────────────

def _run(workspace_id: int, query: str, year_from: int, year_to: int, user_id: int | None):
    from models import SessionLocal, Workspace, current_iteration
    from ingest import ingest_references

    _set(workspace_id, {"status": "searching", "message": "Searching PubMed…",
                        "total": 0, "downloaded": 0})
    db = SessionLocal()
    try:
        all_ids: list[str] = []
        for year in range(year_from, year_to + 1):
            all_ids.extend(_get_pmids(f"{query} AND {year}[pdat]"))
            time.sleep(RATE_LIMIT)
        all_ids = list(dict.fromkeys(all_ids))
        total = len(all_ids)
        _set(workspace_id, {"status": "downloading",
                            "message": f"Found {total} records. Downloading…",
                            "total": total, "downloaded": 0})

        refs = []
        for i, pmid in enumerate(all_ids):
            try:
                ref = parse_medline(_fetch_medline(pmid))
                if ref.get("year") and year_from <= ref["year"] <= year_to:
                    refs.append(ref)
            except Exception:
                pass
            _update(workspace_id, downloaded=i + 1)
            time.sleep(RATE_LIMIT)

        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        it = current_iteration(db, ws)
        imp = ingest_references(db, ws, it, refs, database="pubmed", fmt="api",
                                source_name=query[:120], user_id=user_id)
        _set(workspace_id, {
            "status": "done",
            "message": f"Done. {imp.new_count} new, {imp.merged_count} merged.",
            "total": total, "downloaded": total,
            "new_count": imp.new_count, "merged_count": imp.merged_count,
        })
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_pubmed(workspace_id: int, query: str, year_from: int, year_to: int,
                 user_id: int | None):
    threading.Thread(
        target=_run, args=(workspace_id, query, year_from, year_to, user_id),
        daemon=True,
    ).start()
