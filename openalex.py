"""
OpenAlex direct harvest (an alternative step-1 source).

Free REST API, no key (polite pool via OPENALEX_MAILTO env if set). Boolean search
over title+abstract through the `title_and_abstract.search` filter; the year window
is applied as from/to_publication_date filters (not in the query string). Cursor
paging. Abstracts arrive as an inverted index and are reconstructed to plain text.
"""
import json as _json
import os
import time
from urllib.parse import urlencode

import urllib3

import harvest_common

http = urllib3.PoolManager()
BASE = "https://api.openalex.org/works"
PER_PAGE = 200
RATE = 0.15

_TYPE_MAP = {"article": "article", "book": "book", "book-chapter": "book_chapter"}


def _reconstruct_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    positions = [(i, word) for word, idxs in inv.items() for i in idxs]
    positions.sort()
    return " ".join(w for _, w in positions)


def _parse(r: dict) -> dict:
    doi = (r.get("doi") or "").replace("https://doi.org/", "")
    authors = ", ".join(
        (a.get("author") or {}).get("display_name", "")
        for a in r.get("authorships", []) if a.get("author"))
    mesh = [m.get("descriptor_name", "") for m in r.get("mesh", []) if m.get("descriptor_name")]
    kws = [k.get("display_name", "") for k in r.get("keywords", []) if k.get("display_name")]
    src = ((r.get("primary_location") or {}).get("source") or {}).get("display_name", "") or ""
    ptype = r.get("type", "") or ""
    rtype = _TYPE_MAP.get(ptype, "article" if ptype in ("article", "review", "preprint") else "grey")
    year = r.get("publication_year")
    return {
        "type": rtype,
        "authors": authors,
        "year": int(year) if year else None,
        "title": r.get("title") or r.get("display_name") or "",
        "abstract": _reconstruct_abstract(r.get("abstract_inverted_index")),
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else (r.get("id", "") or ""),
        "source": src,
        "keywords": kws,
        "mesh": mesh,
        "language": r.get("language", "") or "",
        "database": "openalex",
    }


def search(query: str, year_from: int, year_to: int, on_progress) -> list[dict]:
    filt = (f"title_and_abstract.search:{query},"
            f"from_publication_date:{year_from}-01-01,"
            f"to_publication_date:{year_to}-12-31")
    cursor, refs, total = "*", [], None
    mailto = os.environ.get("OPENALEX_MAILTO", "").strip()
    while True:
        params = {"filter": filt, "per-page": PER_PAGE, "cursor": cursor}
        if mailto:
            params["mailto"] = mailto
        url = f"{BASE}?{urlencode(params)}"
        resp = http.request("GET", url)
        data = _json.loads(resp.data.decode("utf-8"))
        if resp.status >= 400 or "error" in data:
            msg = data.get("message") or data.get("error") or f"HTTP {resp.status}"
            raise RuntimeError(f"OpenAlex rejected the query: {msg}")
        meta = data.get("meta", {})
        if total is None:
            total = meta.get("count", 0)
            on_progress(status="downloading",
                        message=f"Found {total} records. Downloading…",
                        total=total, downloaded=0)
        results = data.get("results", [])
        for r in results:
            refs.append(_parse(r))
        on_progress(downloaded=len(refs))
        cursor = meta.get("next_cursor")
        if not results or not cursor:
            break
        time.sleep(RATE)
    return refs


def start(workspace_id: int, query: str, year_from: int, year_to: int, user_id: int | None):
    harvest_common.start("openalex", workspace_id, query, year_from, year_to,
                         user_id, search, "OpenAlex")


def get_job(workspace_id: int) -> dict | None:
    return harvest_common.get_job("openalex", workspace_id)
