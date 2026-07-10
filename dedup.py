"""
Manual dedup pass over the existing record pool (records page).

Import-time dedup (ingest.merge_reference) already collapses obvious duplicates,
but manual additions and cross-source title variants slip through. This module
runs a pass on demand:

- auto_dedup: merges the CERTAIN duplicates — same normalized DOI, same URL, or
  identical normalized title + first-author surname + year. The most complete
  record survives; the others are folded in (provenance, keywords/mesh, sticky
  decisions, full text carried over) and soft-removed as "merged into #survivor".
- uncertain_clusters: groups the DOUBTFUL ones — high fuzzy title similarity that
  isn't certain — for the user to resolve, minus pairs they've dismissed.
"""
import json

from rapidfuzz import fuzz

from ingest import (_as_list, _merge_into, _norm_title, _record_as_ref,
                    completeness, normalize_doi)
from models import DedupDismissal, Record

UNCERTAIN_THRESHOLD = 85   # title ratio to flag as a possible duplicate
BLOCK_PREFIX = 8           # block fuzzy comparisons by normalized-title prefix


def _norm_url(url: str | None) -> str:
    if not url:
        return ""
    u = url.strip().lower()
    for p in ("https://", "http://"):
        if u.startswith(p):
            u = u[len(p):]
    return u.rstrip("/")


def _first_surname(authors: str | None) -> str:
    if not authors:
        return ""
    first = authors.split(";")[0].split(",")[0].strip()
    return first.split()[0].lower() if first else ""


def pair_key(a_id: int, b_id: int) -> str:
    lo, hi = sorted((a_id, b_id))
    return f"{lo}-{hi}"


# ── Union-find ─────────────────────────────────────────────────────────────────

class _UF:
    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb

    def clusters(self):
        out = {}
        for i in self.p:
            out.setdefault(self.find(i), []).append(i)
        return [c for c in out.values() if len(c) > 1]


# ── Merge two existing records ─────────────────────────────────────────────────

def merge_records(db, survivor: Record, dup: Record):
    """Fold `dup` into `survivor` and soft-remove `dup`."""
    _merge_into(survivor, _record_as_ref(dup))
    dbs = set(_as_list(survivor.source_dbs_json)) | set(_as_list(dup.source_dbs_json))
    survivor.source_dbs_json = json.dumps(sorted(dbs))

    # carry sticky decisions if the survivor hasn't been decided
    if survivor.screen1_decision == "pending" and dup.screen1_decision != "pending":
        survivor.screen1_decision = dup.screen1_decision
        survivor.screen1_reason = dup.screen1_reason
        survivor.screen1_by = dup.screen1_by
        survivor.screen1_at = dup.screen1_at
    if survivor.screen2_decision == "pending" and dup.screen2_decision != "pending":
        survivor.screen2_decision = dup.screen2_decision
        survivor.screen2_reason = dup.screen2_reason
        survivor.screen2_by = dup.screen2_by
        survivor.screen2_at = dup.screen2_at

    # carry full text if the survivor lacks it
    if survivor.full_text_status in (None, "none", "failed", "url") and \
            dup.full_text_status in ("fetched", "converted"):
        survivor.full_text_status = dup.full_text_status
        survivor.full_text_path = dup.full_text_path
        survivor.full_text_md = dup.full_text_md
        survivor.full_text_url = dup.full_text_url or survivor.full_text_url

    for rr in list(dup.raw_refs):
        rr.record_id = survivor.id
    dup.is_removed = True
    dup.removed_reason = f"merged into record #{survivor.id}"


def _pick_survivor(records: list[Record]) -> Record:
    return max(records, key=lambda r: (completeness(_record_as_ref(r)), -r.id))


# ── Auto dedup (certain) ───────────────────────────────────────────────────────

def _active(db, workspace_id):
    return (db.query(Record)
              .filter(Record.workspace_id == workspace_id,
                      Record.is_removed == False).all())  # noqa: E712


def auto_dedup(db, workspace_id: int) -> int:
    records = _active(db, workspace_id)
    uf = _UF([r.id for r in records])
    seen: dict[tuple, int] = {}
    for r in records:
        keys = []
        doi = normalize_doi(r.doi)
        if doi:
            keys.append(("doi", doi))
        url = _norm_url(r.url)
        if url:
            keys.append(("url", url))
        nt, sur = _norm_title(r.title), _first_surname(r.authors)
        if nt and sur and r.year:
            keys.append(("tay", nt, sur, r.year))
        for k in keys:
            if k in seen:
                uf.union(seen[k], r.id)
            else:
                seen[k] = r.id

    by_id = {r.id: r for r in records}
    merged = 0
    for cluster in uf.clusters():
        recs = [by_id[i] for i in cluster]
        survivor = _pick_survivor(recs)
        for r in recs:
            if r.id != survivor.id:
                merge_records(db, survivor, r)
                merged += 1
    if merged:
        db.commit()
    return merged


# ── Uncertain clusters (doubtful) ──────────────────────────────────────────────

def uncertain_clusters(db, workspace_id: int) -> list[list[Record]]:
    records = _active(db, workspace_id)
    dismissed = {d.pair_key for d in
                 db.query(DedupDismissal).filter(DedupDismissal.workspace_id == workspace_id).all()}
    by_id = {r.id: r for r in records}
    uf = _UF(list(by_id))

    blocks: dict[str, list[Record]] = {}
    for r in records:
        nt = _norm_title(r.title)
        if nt:
            blocks.setdefault(nt[:BLOCK_PREFIX], []).append(r)

    for group in blocks.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if pair_key(a.id, b.id) in dismissed:
                    continue
                if fuzz.ratio(_norm_title(a.title), _norm_title(b.title)) >= UNCERTAIN_THRESHOLD:
                    uf.union(a.id, b.id)

    clusters = [[by_id[i] for i in c] for c in uf.clusters()]
    clusters.sort(key=lambda c: min(r.id for r in c))
    return clusters


def dismiss_cluster(db, workspace_id: int, record_ids: list[int]):
    existing = {d.pair_key for d in
                db.query(DedupDismissal).filter(DedupDismissal.workspace_id == workspace_id).all()}
    for i in range(len(record_ids)):
        for j in range(i + 1, len(record_ids)):
            pk = pair_key(record_ids[i], record_ids[j])
            if pk not in existing:
                db.add(DedupDismissal(workspace_id=workspace_id, pair_key=pk))
    db.commit()
