"""
Ingestion & deduplication for LSSR (steps 3-4).

Every source — PubMed API (pubmed.py), BibTeX/RIS file uploads, or manual entry —
produces a list of *normalized reference dicts* with this shape:

    {
      "type": "article" | "book" | "book_chapter" | "grey",
      "authors": str, "year": int | None, "title": str, "abstract": str,
      "doi": str, "url": str, "source": str,        # journal / publisher
      "keywords": [str], "mesh": [str], "language": str,
      "database": str,                               # provenance
    }

`merge_reference` folds one such dict into the workspace's persistent Record
pool: it deduplicates incrementally (DOI-exact, then fuzzy title+year) and, on a
duplicate, keeps the most complete version while merging provenance. This keeps
the pool "living" — new imports slot in without a rebuild, and human decisions on
existing records are never touched.
"""
import json
import re

from rapidfuzz import fuzz

from models import Import, Iteration, RawReference, Record

FUZZY_THRESHOLD = 95  # title similarity (same year) to treat as a duplicate

KEEP_FIELDS = ("type", "authors", "year", "title", "abstract", "doi", "url",
               "source", "keywords", "mesh", "language", "database")


# ── Normalization ──────────────────────────────────────────────────────────────

_DOI_PREFIXES = ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/",
                 "https://dx.doi.org/", "doi:")


def normalize_doi(doi: str | None) -> str:
    if not doi:
        return ""
    d = doi.strip().lower()
    for p in _DOI_PREFIXES:
        if d.startswith(p):
            d = d[len(p):]
    return d.strip().strip(".")


def _norm_title(title: str | None) -> str:
    if not title:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip()


def canonical_key(ref: dict) -> str:
    doi = normalize_doi(ref.get("doi"))
    if doi:
        return f"doi:{doi}"
    title = _norm_title(ref.get("title"))
    year = ref.get("year") or ""
    return f"ti:{title}|{year}" if title else ""


def completeness(ref: dict) -> int:
    """Higher = richer. Abstract and DOI weigh most (they drive screening)."""
    score = 0
    if ref.get("abstract"):
        score += 3
    if normalize_doi(ref.get("doi")):
        score += 2
    for f in ("title", "authors", "source", "language"):
        if ref.get(f):
            score += 1
    if ref.get("keywords"):
        score += 1
    if ref.get("mesh"):
        score += 1
    return score


def _as_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _record_as_ref(rec: Record) -> dict:
    return {
        "type": rec.type, "authors": rec.authors, "year": rec.year,
        "title": rec.title, "abstract": rec.abstract, "doi": rec.doi,
        "url": rec.url, "source": rec.source,
        "keywords": _as_list(rec.keywords_json), "mesh": _as_list(rec.mesh_json),
        "language": rec.language,
    }


# ── Dedup lookup ───────────────────────────────────────────────────────────────

def _find_duplicate(db, workspace_id: int, ref: dict, key: str) -> Record | None:
    if key.startswith("doi:"):
        hit = (db.query(Record)
                 .filter(Record.workspace_id == workspace_id,
                         Record.canonical_key == key,
                         Record.is_removed == False).first())  # noqa: E712
        if hit:
            return hit
    # exact key (covers title-based keys too)
    hit = (db.query(Record)
             .filter(Record.workspace_id == workspace_id,
                     Record.canonical_key == key,
                     Record.is_removed == False).first())  # noqa: E712
    if hit:
        return hit
    # fuzzy title match within same year, only when we have no DOI to trust
    if not key.startswith("doi:") and ref.get("title"):
        nt = _norm_title(ref["title"])
        candidates = (db.query(Record)
                        .filter(Record.workspace_id == workspace_id,
                                Record.year == ref.get("year"),
                                Record.is_removed == False)  # noqa: E712
                        .all())
        for c in candidates:
            if c.title and fuzz.ratio(nt, _norm_title(c.title)) >= FUZZY_THRESHOLD:
                return c
    return None


def _merge_into(rec: Record, ref: dict):
    """Keep the most complete: fill empties, prefer the longer abstract, union
    keywords/mesh/source_dbs. Decisions and identity are never overwritten."""
    incoming_richer = completeness(ref) > completeness(_record_as_ref(rec))

    if not rec.doi and ref.get("doi"):
        rec.doi = ref["doi"]
        if not rec.canonical_key or rec.canonical_key.startswith("ti:"):
            rec.canonical_key = canonical_key({"doi": ref["doi"]})
    if (ref.get("abstract") or "") and len(ref.get("abstract") or "") > len(rec.abstract or ""):
        rec.abstract = ref["abstract"]
    for field in ("authors", "url", "source", "language", "title", "year"):
        if not getattr(rec, field) and ref.get(field):
            setattr(rec, field, ref[field])
    if incoming_richer and ref.get("type"):
        rec.type = ref["type"]

    for field, col in (("keywords", "keywords_json"), ("mesh", "mesh_json")):
        merged = list(dict.fromkeys(_as_list(getattr(rec, col)) + (ref.get(field) or [])))
        if merged:
            setattr(rec, col, json.dumps(merged))

    dbs = _as_list(rec.source_dbs_json)
    if ref.get("database") and ref["database"] not in dbs:
        dbs.append(ref["database"])
        rec.source_dbs_json = json.dumps(dbs)


def merge_reference(db, workspace_id: int, iteration: Iteration, ref: dict,
                    import_id: int | None = None) -> str:
    """Fold one normalized ref into the pool. Returns 'new' or 'merged'.
    Caller commits."""
    key = canonical_key(ref)
    dup = _find_duplicate(db, workspace_id, ref, key) if key else None

    if dup is not None:
        _merge_into(dup, ref)
        dup.last_seen_iter_id = iteration.id
        db.add(RawReference(workspace_id=workspace_id, import_id=import_id,
                            record_id=dup.id, database=ref.get("database"),
                            canonical_key=key, raw_json=json.dumps(ref)))
        return "merged"

    rec = Record(
        workspace_id=workspace_id,
        type=ref.get("type") or "article",
        authors=ref.get("authors"), year=ref.get("year"), title=ref.get("title"),
        abstract=ref.get("abstract"), doi=normalize_doi(ref.get("doi")) or None,
        url=ref.get("url"), source=ref.get("source"),
        keywords_json=json.dumps(ref.get("keywords") or []),
        mesh_json=json.dumps(ref.get("mesh") or []),
        language=ref.get("language"),
        source_dbs_json=json.dumps([ref["database"]] if ref.get("database") else []),
        canonical_key=key or None,
        first_seen_iter_id=iteration.id, last_seen_iter_id=iteration.id,
    )
    db.add(rec)
    db.flush()  # assign rec.id for the RawReference FK
    db.add(RawReference(workspace_id=workspace_id, import_id=import_id,
                        record_id=rec.id, database=ref.get("database"),
                        canonical_key=key, raw_json=json.dumps(ref)))
    return "new"


def ingest_references(db, workspace, iteration, refs: list[dict], database: str,
                      fmt: str, source_name: str, user_id: int | None) -> Import:
    """Create an Import row and fold every ref into the pool. Commits once."""
    imp = Import(workspace_id=workspace.id, iteration_id=iteration.id,
                 database=database, fmt=fmt, source_name=source_name,
                 created_by_id=user_id)
    db.add(imp)
    db.flush()
    new_n = merged_n = 0
    for ref in refs:
        ref.setdefault("database", database)
        outcome = merge_reference(db, workspace.id, iteration, ref, import_id=imp.id)
        if outcome == "new":
            new_n += 1
        else:
            merged_n += 1
    imp.raw_count = len(refs)
    imp.new_count = new_n
    imp.merged_count = merged_n
    db.commit()
    db.refresh(imp)
    return imp


# ── File parsers ───────────────────────────────────────────────────────────────

_BIBTEX_TYPES = {"article": "article", "book": "book", "inbook": "book_chapter",
                 "incollection": "book_chapter", "conference": "grey",
                 "inproceedings": "grey", "misc": "grey", "techreport": "grey",
                 "phdthesis": "grey", "mastersthesis": "grey", "unpublished": "grey"}


def _split_terms(raw: str | None) -> list:
    if not raw:
        return []
    parts = re.split(r"[;\n]|,(?![^(]*\))", raw)
    return [p.strip() for p in parts if p and p.strip()]


def parse_bibtex(text: str) -> list[dict]:
    import bibtexparser
    from bibtexparser.bparser import BibTexParser
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    db = bibtexparser.loads(text, parser=parser)
    refs = []
    for e in db.entries:
        etype = (e.get("ENTRYTYPE") or "misc").lower()
        year = e.get("year", "")
        year = int(re.sub(r"[^0-9]", "", year)[:4]) if re.search(r"\d{4}", year) else None
        refs.append({
            "type": _BIBTEX_TYPES.get(etype, "grey"),
            "authors": re.sub(r"\s+and\s+", ", ", e.get("author", "")).strip(),
            "year": year,
            "title": re.sub(r"[{}]", "", e.get("title", "")).strip(),
            "abstract": e.get("abstract", "").strip(),
            "doi": e.get("doi", "").strip(),
            "url": e.get("url", "").strip(),
            "source": (e.get("journal") or e.get("booktitle") or e.get("publisher") or "").strip(),
            "keywords": _split_terms(e.get("keywords")),
            "mesh": [],
            "language": e.get("language", "").strip(),
        })
    return refs


_RIS_TYPES = {"JOUR": "article", "BOOK": "book", "CHAP": "book_chapter",
              "RPRT": "grey", "CONF": "grey", "CPAPER": "grey", "THES": "grey",
              "UNPB": "grey", "GEN": "grey"}


def parse_ris(text: str) -> list[dict]:
    import rispy
    entries = rispy.loads(text, skip_unknown_tags=True)
    refs = []
    for e in entries:
        ty = (e.get("type_of_reference") or "GEN").upper()
        year = e.get("year") or e.get("publication_year") or ""
        year = int(re.sub(r"[^0-9]", "", str(year))[:4]) if re.search(r"\d{4}", str(year)) else None
        authors = e.get("authors") or e.get("first_authors") or []
        urls = e.get("urls") or []
        source = (e.get("journal_name") or e.get("secondary_title")
                  or e.get("alternate_title3") or e.get("publisher") or "")
        refs.append({
            "type": _RIS_TYPES.get(ty, "grey"),
            "authors": ", ".join(authors) if isinstance(authors, list) else str(authors),
            "year": year,
            "title": (e.get("title") or e.get("primary_title") or "").strip(),
            "abstract": (e.get("abstract") or e.get("notes_abstract") or "").strip(),
            "doi": (e.get("doi") or "").strip(),
            "url": (urls[0] if isinstance(urls, list) and urls else e.get("url", "")).strip(),
            "source": source.strip(),
            "keywords": e.get("keywords") or [],
            "mesh": [],
            "language": (e.get("language") or "").strip(),
        })
    return refs


# ── Excel (column-mapped) ───────────────────────────────────────────────────────

# Target fields the user maps spreadsheet columns onto. title is the anchor.
EXCEL_FIELDS = [
    ("title", "Title"), ("authors", "Authors"), ("year", "Year"),
    ("doi", "DOI"), ("abstract", "Abstract"), ("source", "Source / journal"),
    ("url", "URL"), ("keywords", "Keywords"), ("language", "Language"),
]

_TYPE_ALIASES = {"article": "article", "paper": "article", "journal": "article",
                 "book": "book", "book chapter": "book_chapter", "chapter": "book_chapter",
                 "book_chapter": "book_chapter"}


def _norm_type(raw: str) -> str:
    v = (raw or "").strip().lower()
    # longest alias first, so "book chapter" wins over "book"
    for k in sorted(_TYPE_ALIASES, key=len, reverse=True):
        if k in v:
            return _TYPE_ALIASES[k]
    return "grey" if v else "article"


def read_excel(file_bytes: bytes) -> tuple[list[str], list[list], int]:
    """Return (column headers, up to 5 sample rows, total row count) for the
    mapping UI. Everything read as strings so numbers/DOIs survive intact."""
    import io
    import pandas as pd
    df = pd.read_excel(io.BytesIO(file_bytes), dtype=str).fillna("")
    cols = [str(c) for c in df.columns]
    sample = [[str(v) for v in row] for row in df.head(5).values.tolist()]
    return cols, sample, len(df)


def excel_to_refs(file_bytes: bytes, mapping: dict, type_col: str | None,
                  default_type: str) -> list[dict]:
    """Turn spreadsheet rows into normalized ref dicts using a field→column map.
    `mapping` keys are EXCEL_FIELDS names; values are column headers ('' = skip)."""
    import io
    import pandas as pd
    df = pd.read_excel(io.BytesIO(file_bytes), dtype=str).fillna("")
    cols = set(str(c) for c in df.columns)
    df.columns = [str(c) for c in df.columns]
    refs = []
    for _, row in df.iterrows():
        def g(field):
            col = mapping.get(field)
            return str(row[col]).strip() if col and col in cols else ""
        title, doi = g("title"), g("doi")
        if not title and not doi:
            continue  # skip blank rows
        ym = re.search(r"\d{4}", g("year"))
        rtype = _norm_type(str(row[type_col])) if (type_col and type_col in cols) else default_type
        refs.append({
            "type": rtype,
            "authors": g("authors"),
            "year": int(ym.group()) if ym else None,
            "title": title,
            "abstract": g("abstract"),
            "doi": doi,
            "url": g("url"),
            "source": g("source"),
            "keywords": _split_terms(g("keywords")),
            "mesh": [],
            "language": g("language"),
        })
    return refs


def parse_file(filename: str, text: str) -> list[dict]:
    name = filename.lower()
    if name.endswith((".ris", ".nbib")) or text.lstrip().startswith("TY  -"):
        return parse_ris(text)
    if name.endswith((".bib", ".bibtex")) or text.lstrip().startswith("@"):
        return parse_bibtex(text)
    # last resort: sniff
    if text.lstrip().startswith("@"):
        return parse_bibtex(text)
    return parse_ris(text)


# ── Query refinement helper (step 1) ───────────────────────────────────────────

def term_frequencies(db, workspace_id: int, top_n: int = 40) -> dict:
    """Count MeSH terms and author keywords across the non-removed pool, so the
    user can decide what to add to or exclude from the query."""
    from collections import Counter
    mesh, kw = Counter(), Counter()
    for rec in (db.query(Record)
                  .filter(Record.workspace_id == workspace_id,
                          Record.is_removed == False).all()):  # noqa: E712
        for m in _as_list(rec.mesh_json):
            mesh[m] += 1
        for k in _as_list(rec.keywords_json):
            kw[k.lower()] += 1
    return {"mesh": mesh.most_common(top_n), "keywords": kw.most_common(top_n)}
