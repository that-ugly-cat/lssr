"""
Europe PMC direct harvest (an alternative step-1 source to PubMed).

Free REST search API, no key. resultType=core returns abstract + MeSH; paging via
cursorMark. Europe PMC is a superset of PubMed (adds PMC, preprints, Agricola),
and it retains MeSH — so its records fold cleanly into the same pool via the
shared dedup (union provenance). The workspace year window is applied here as a
PUB_YEAR range clause (the translated query itself carries no year clause).
"""
import json as _json
import time
from urllib.parse import urlencode

import urllib3

import harvest_common

http = urllib3.PoolManager()
BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE = 1000
RATE = 0.2  # seconds between page requests


def _parse(r: dict) -> dict:
    mesh = []
    for m in (r.get("meshHeadingList") or {}).get("meshHeading", []):
        d = m.get("descriptorName")
        if d:
            mesh.append(d)
    kws = [k for k in (r.get("keywordList") or {}).get("keyword", []) if k]
    year = r.get("pubYear")
    doi = r.get("doi", "") or ""
    src = r.get("source", "")     # MED, PMC, PPR, AGR…
    pmid = r.get("pmid") or r.get("id") or ""
    journal = ((r.get("journalInfo") or {}).get("journal") or {}).get("title", "")
    if doi:
        url = f"https://doi.org/{doi}"
    elif src and pmid:
        url = f"https://europepmc.org/article/{src}/{pmid}"
    else:
        url = ""
    return {
        "type": "article",
        "authors": r.get("authorString", "") or "",
        "year": int(year) if year and str(year).isdigit() else None,
        "title": r.get("title", "") or "",
        "abstract": r.get("abstractText", "") or "",
        "doi": doi,
        "url": url,
        "source": journal or src or "",
        "keywords": kws,
        "mesh": mesh,
        "language": r.get("language", "") or "",
        "database": "europepmc",
    }


def search(query: str, year_from: int, year_to: int, on_progress) -> list[dict]:
    q = f"({query}) AND (PUB_YEAR:[{year_from} TO {year_to}])"
    cursor, refs, total = "*", [], None
    while True:
        params = {"query": q, "format": "json", "pageSize": PAGE,
                  "cursorMark": cursor, "resultType": "core"}
        url = f"{BASE}?{urlencode(params)}"
        data = _json.loads(http.request("GET", url).data.decode("utf-8"))
        if total is None:
            total = data.get("hitCount", 0)
            on_progress(status="downloading",
                        message=f"Found {total} records. Downloading…",
                        total=total, downloaded=0)
        results = (data.get("resultList") or {}).get("result", [])
        for r in results:
            refs.append(_parse(r))
        on_progress(downloaded=len(refs))
        next_cursor = data.get("nextCursorMark")
        if not results or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
        time.sleep(RATE)
    return refs


def start(workspace_id: int, query: str, year_from: int, year_to: int, user_id: int | None):
    harvest_common.start("europepmc", workspace_id, query, year_from, year_to,
                         user_id, search, "Europe PMC")


def get_job(workspace_id: int) -> dict | None:
    return harvest_common.get_job("europepmc", workspace_id)
