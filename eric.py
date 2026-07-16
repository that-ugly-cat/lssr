"""
ERIC direct harvest (an alternative step-1 source for education reviews).

IES ERIC API, Solr-backed JSON, no key. Offset paging (start/rows). The year
window is applied as a publicationdateyear range clause. Records are ED* (grey /
documents) or EJ* (journal articles); the id resolves to the ERIC record page.
Solr multi-valued fields come back as lists, so string fields are coerced.
"""
import json as _json
import time
from urllib.parse import urlencode

import urllib3

import harvest_common

http = urllib3.PoolManager()
BASE = "https://api.ies.ed.gov/eric/"
ROWS = 200
RATE = 0.15
FIELDS = "id,title,author,description,publicationdateyear,source,subject,publicationtype"


def _s(v) -> str:
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if x)
    return str(v) if v is not None else ""


def _list(v) -> list:
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [str(v)] if v else []


def _parse(d: dict) -> dict:
    eid = _s(d.get("id"))
    year = d.get("publicationdateyear")
    return {
        "type": "article" if eid.startswith("EJ") else "grey",
        "authors": ", ".join(_list(d.get("author"))),
        "year": int(year) if year and str(year).isdigit() else None,
        "title": _s(d.get("title")),
        "abstract": _s(d.get("description")),
        "doi": "",
        "url": f"https://eric.ed.gov/?id={eid}" if eid else "",
        "source": _s(d.get("source")),
        "keywords": _list(d.get("subject")),
        "mesh": [],
        "language": "",
        "database": "eric",
    }


def search(query: str, year_from: int, year_to: int, on_progress) -> list[dict]:
    q = f"({query}) AND publicationdateyear:[{year_from} TO {year_to}]"
    start, refs, total = 0, [], None
    while True:
        params = {"search": q, "format": "json", "rows": ROWS, "start": start, "fields": FIELDS}
        url = f"{BASE}?{urlencode(params)}"
        data = _json.loads(http.request("GET", url).data.decode("utf-8"))
        resp = data.get("response", {})
        if total is None:
            total = resp.get("numFound", 0)
            on_progress(status="downloading",
                        message=f"Found {total} records. Downloading…",
                        total=total, downloaded=0)
        docs = resp.get("docs", [])
        for d in docs:
            refs.append(_parse(d))
        on_progress(downloaded=len(refs))
        start += len(docs)
        if not docs or start >= (total or 0):
            break
        time.sleep(RATE)
    return refs


def start(workspace_id: int, query: str, year_from: int, year_to: int, user_id: int | None):
    harvest_common.start("eric", workspace_id, query, year_from, year_to,
                         user_id, search, "ERIC")


def get_job(workspace_id: int) -> dict | None:
    return harvest_common.get_job("eric", workspace_id)
