"""
The model-facing surface of LSSR.

A scoping review is the kind of object you end up asking questions about far
more often than you edit it: how many records are still unscreened, what the
inclusion criteria actually say, which studies came out of Wales, what the
extraction says about study design. Those questions arrive in a conversation,
and until now the only way to answer them was to open the app, pick a tab and
count by eye. This is the same corpus, readable from where the questions are.

**Read-only, all of it.** No tool here writes: not a vote, not an extraction,
not a step marked done. That is a decision and not a stage — a screening
decision carries a reviewer's name and belongs to a person doing the reading,
and a surface where a model could cast one would quietly turn the reviewer into
an editor of its output. A leaked key therefore exposes a corpus and cannot
corrupt one. If writes ever come, they come one verb at a time, each with its
own reason.

**Access is the caller's own.** Every call resolves to the human who owns the
API key, and every review lookup goes through auth.mcp_review(), which uses the
same can_access() as the web app. A review the caller cannot reach reports "no
review" rather than "forbidden", so the model cannot enumerate what it cannot
see.

**Every voice is visible to every member, and that is deliberate.** The web app
hides other reviewers' votes until you have cast yours; here they are all
readable. Blinding is a discipline of the moment of voting, which happens in the
UI; this surface exists to read a corpus, and a reader that sees half the votes
mostly produces wrong totals. Worth saying plainly: a reviewer who reads here
before voting there has read ahead. What answers that is who holds a key, not a
filter that would make every count depend on its reader.

**Counts are computed, never narrated.** Same principle as the synthesis step:
extraction_summary and the PRISMA numbers come out of SQL, so the model is
handed figures it cannot have hallucinated. What it does with them is its
business; where they came from is not in doubt.

Errors are returned as {"error": ...} rather than raised: a tool that throws
gives the model a stack trace to hallucinate around, while a message it can read
lets it correct course.
"""
import json
import os
from collections import Counter
from datetime import datetime

from mcp.server.mcpserver import MCPServer
from sqlalchemy import distinct, func, or_

import auth
from models import (
    HARVEST_DBS, PIPELINE_STEPS, Extraction, Import, Iteration, PublicShare,
    Record, ScreenDecision, SearchQuery, SessionLocal, Synthesis, UserCostLog,
    authoritative_values, db_label, db_search_url, screen2_required,
    user_workspaces, workspace_criteria, workspace_extraction_fields,
    workspace_steps_done, workspace_target_dbs,
)
from synthesis import compute_prisma

mcp = MCPServer(
    name="lssr",
    instructions=(
        "Living systematic scoping reviews: the whole pipeline from query to "
        "synthesis, one workspace per review. Read-only — every decision, vote "
        "and extraction is made by a human in the web app, never here. Start "
        "with list_reviews, then get_review for the state of one and "
        "get_protocol for the criteria and the extraction schema it is being "
        "read against. Counts come from the database, so they are exact: "
        "prefer extraction_summary to counting records yourself. search_records "
        "is lexical, not semantic — no hit means those words are not in the "
        "title, abstract or authors, never that the corpus lacks the topic."
    ),
)

PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://localhost:8013")
FULLTEXT_CHUNK = 20_000
DECISIONS = ("pending", "include", "exclude", "maybe", "conflict")


def _fail(msg: str) -> dict:
    return {"error": msg}


def _j(raw) -> list:
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _d(dt) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M") if isinstance(dt, datetime) else None


def _live(db, ws_id: int):
    """The visible pool. The dedup merge soft-deletes its loser, so a query that
    does not filter is_removed counts merged duplicates as survivors."""
    return db.query(Record).filter(Record.workspace_id == ws_id,
                                   Record.is_removed == False)  # noqa: E712


def _role(user, ws) -> str:
    if ws.owner_id == user.id:
        return "owner"
    if user.is_admin:
        return "admin"
    return "member"


def _brief(r: Record) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "authors": r.authors,
        "year": r.year,
        "type": r.type or "article",
        "doi": r.doi,
        "url": r.url,
        "journal": r.source,
        "databases": [db_label(d) for d in _j(r.source_dbs_json)],
        "screen1": r.screen1_decision,
        "screen1_by": r.screen1_by,
        "screen2": r.screen2_decision,
        "screen2_by": r.screen2_by,
        "full_text": r.full_text_status,
        "has_abstract": bool((r.abstract or "").strip()),
    }


def _votes(db, record_ids: list, stage: str) -> dict:
    """Every voice on these records at this stage, grouped by record — model,
    reviewers and adjudicator alike. See the note on blinding at the top."""
    out = {}
    if not record_ids:
        return out
    for v in (db.query(ScreenDecision)
                .filter(ScreenDecision.stage == stage,
                        ScreenDecision.record_id.in_(record_ids)).all()):
        out.setdefault(v.record_id, []).append({
            "reviewer": ("the model" if v.reviewer_kind == "model"
                         else (v.reviewer.name if v.reviewer else "unknown")),
            "kind": v.reviewer_kind,
            "decision": v.decision,
            "reason": v.reason,
            "at": _d(v.updated_at or v.created_at),
        })
    return out


def _divergent_sub(db, ws_id: int, stage: str):
    """Records where at least one voice differs from another, the model's and
    every 'maybe' included. Wider than decision == 'conflict', which only ever
    means two humans disagreeing — on a real corpus the gap is large."""
    return (db.query(ScreenDecision.record_id)
              .filter(ScreenDecision.workspace_id == ws_id,
                      ScreenDecision.stage == stage)
              .group_by(ScreenDecision.record_id)
              .having(func.count(distinct(ScreenDecision.decision)) > 1)
              .scalar_subquery())


def _extracted_ids(db, ws_id: int) -> set:
    """Records with a non-empty extraction. An existing but empty row counts as
    unextracted: from the synthesis's side they are the same hole."""
    return {e.record_id for e in
            db.query(Extraction).filter(Extraction.workspace_id == ws_id).all()
            if e.values()}


def _share_url(db, ws_id: int) -> str | None:
    row = (db.query(PublicShare)
             .filter(PublicShare.workspace_id == ws_id,
                     PublicShare.active == True)  # noqa: E712
             .order_by(PublicShare.id.desc()).first())
    return f"{PUBLIC_URL}/r/{row.token}" if row else None


# ── Reviews ───────────────────────────────────────────────────────────────────

@mcp.tool()
def list_reviews() -> dict:
    """Reviews the caller can reach, newest first. Start here: every other tool
    takes a review by the `id` or the `name` returned in this list."""
    db = SessionLocal()
    try:
        user = auth.current_caller()
        out = []
        for ws in user_workspaces(db, user):
            done = workspace_steps_done(ws)
            out.append({
                "id": ws.id,
                "name": ws.name,
                "research_question": ws.research_question,
                "role": _role(user, ws),
                "records": _live(db, ws.id).count(),
                "steps_done": [s for s in PIPELINE_STEPS if s in done],
                "created": _d(ws.created_at),
            })
        return {"you": user.name, "count": len(out), "reviews": out}
    except PermissionError as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def get_review(review: str) -> dict:
    """
    The state of one review: configuration, progress, live PRISMA counts, and
    what the LLM steps have cost so far.

    The PRISMA numbers are computed from the pool as it stands right now, not
    from a snapshot taken when the synthesis was last generated — so they move
    as the review moves, and the stages whose step is not marked done are
    provisional by design.
    """
    db = SessionLocal()
    try:
        user = auth.current_caller()
        ws = auth.mcp_review(db, review)
        done = workspace_steps_done(ws)
        pool = _live(db, ws.id)
        s1 = {d: pool.filter(Record.screen1_decision == d).count() for d in DECISIONS}
        s1_incl = pool.filter(Record.screen1_decision == "include")
        s2 = {d: s1_incl.filter(Record.screen2_decision == d).count() for d in DECISIONS}
        costs, total = {}, 0.0
        for step, n, ti, to, c in (db.query(
                UserCostLog.step, func.count(), func.sum(UserCostLog.input_tokens),
                func.sum(UserCostLog.output_tokens), func.sum(UserCostLog.cost_usd))
                .filter(UserCostLog.workspace_id == ws.id)
                .group_by(UserCostLog.step).all()):
            costs[step] = {"runs": n, "input_tokens": int(ti or 0),
                           "output_tokens": int(to or 0), "usd": round(c or 0.0, 4)}
            total += c or 0.0
        syn = db.query(Synthesis).filter(Synthesis.workspace_id == ws.id).first()
        iters = (db.query(Iteration).filter(Iteration.workspace_id == ws.id)
                   .order_by(Iteration.number.desc()).all())
        return {
            "id": ws.id,
            "name": ws.name,
            "description": ws.description,
            "research_question": ws.research_question,
            "owner": ws.owner.name if ws.owner else None,
            "your_role": _role(user, ws),
            "members": sorted({ws.owner.name if ws.owner else "?"}
                              | {m.user.name for m in ws.members if m.user}),
            "created": _d(ws.created_at),
            "steps": PIPELINE_STEPS,
            "steps_done": [s for s in PIPELINE_STEPS if s in done],
            "config": {
                "primary_db": db_label(ws.primary_db or "pubmed"),
                "year_from": ws.year_from, "year_to": ws.year_to,
                "target_dbs": [db_label(d) for d in workspace_target_dbs(ws)],
                "screening_model": ws.screening_model,
                "screen1_reviewers_required": ws.screen1_reviewers_required or 1,
                "screen2_reviewers_required": screen2_required(ws),
            },
            "counts": {
                "records": pool.count(),
                "screen1": s1,
                "screen2": s2,
                "full_text_converted": s1_incl.filter(
                    Record.full_text_status == "converted").count(),
                "extracted": len(_extracted_ids(db, ws.id)),
                "criteria_exclusion": len(workspace_criteria(db, ws, "exclusion")),
                "criteria_inclusion": len(workspace_criteria(db, ws, "inclusion")),
                "extraction_fields": len(workspace_extraction_fields(db, ws)),
            },
            "iterations": [{"number": it.number, "status": it.status} for it in iters],
            "prisma": compute_prisma(db, ws.id),
            "llm_cost": {"by_step": costs, "total_usd": round(total, 4)},
            "synthesis": {"exists": syn is not None,
                          "published": bool(syn and syn.published),
                          "generated": _d(syn.generated_at) if syn else None},
            "public_url": _share_url(db, ws.id),
        }
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def list_iterations(review: str) -> dict:
    """
    The living history: every iteration, and every import that fed it.

    An iteration is the unit of "living" — a refresh re-runs the searches and
    re-deduplicates, screening only what is new, and past decisions stay sticky.
    The imports carry the numbers PRISMA is built from: raw references parsed,
    records newly created, references merged into records already in the pool.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        first_seen = dict(db.query(Record.first_seen_iter_id, func.count())
                            .filter(Record.workspace_id == ws.id,
                                    Record.is_removed == False)  # noqa: E712
                            .group_by(Record.first_seen_iter_id).all())
        imports = {}
        for im in (db.query(Import).filter(Import.workspace_id == ws.id)
                     .order_by(Import.created_at).all()):
            imports.setdefault(im.iteration_id, []).append({
                "database": db_label(im.database), "format": im.fmt,
                "source": im.source_name, "references": im.raw_count,
                "new_records": im.new_count, "merged": im.merged_count,
                "at": _d(im.created_at),
            })
        out = []
        for it in (db.query(Iteration).filter(Iteration.workspace_id == ws.id)
                     .order_by(Iteration.number).all()):
            out.append({
                "number": it.number, "status": it.status, "note": it.note,
                "started": _d(it.started_at), "completed": _d(it.completed_at),
                "records_first_seen_here": first_seen.get(it.id, 0),
                "imports": imports.get(it.id, []),
            })
        return {"review": ws.name, "count": len(out), "iterations": out,
                "imports_without_iteration": imports.get(None, [])}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Search strategy (steps 1–2) ───────────────────────────────────────────────

@mcp.tool()
def get_queries(review: str) -> dict:
    """
    The canonical query and its translations, one per database.

    The primary database is where the query is authored; every other query is a
    translation out of it, editable by hand and often edited. `harvestable`
    marks the four databases with a free API that LSSR pulls records from
    directly (PubMed, Europe PMC, OpenAlex, ERIC); for the rest the query is
    copied into the database's own interface and the export imported back, so a
    translation existing does not mean records were ever collected from there.
    list_iterations is what says whether they were.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        primary = ws.primary_db or "pubmed"
        rows = db.query(SearchQuery).filter(SearchQuery.workspace_id == ws.id).all()
        by_db = {q.database: q for q in rows}
        order = [primary] + [d for d in workspace_target_dbs(ws) if d != primary]
        order += [d for d in by_db if d not in order]
        queries = []
        for d in order:
            q = by_db.get(d)
            queries.append({
                "database": db_label(d), "key": d,
                "primary": d == primary,
                "harvestable": d in HARVEST_DBS,
                "query": q.query_string if q else None,
                "updated": _d(q.updated_at) if q else None,
                "search_url": db_search_url(d),
            })
        return {"review": ws.name, "primary_db": db_label(primary),
                "years": {"from": ws.year_from, "to": ws.year_to},
                "count": len(queries), "queries": queries}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Protocol: criteria + extraction schema (steps 4, 6) ───────────────────────

@mcp.tool()
def get_protocol(review: str) -> dict:
    """
    What this review reads against: the two criterion sets and the extraction
    schema.

    Exclusion criteria drive screening 1 (title and abstract); inclusion
    criteria drive screening 2, which happens on the full text in the same act
    as the extraction. The extraction fields are the columns of the review's
    data matrix — `type` and `options` are what a value is allowed to be, and
    `show_if` says a field is asked only when another field has certain values,
    so a blank there is a question not asked rather than an answer missing.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)

        def crit(kind):
            return [{"label": c.label, "description": c.description}
                    for c in workspace_criteria(db, ws, kind)]
        fields = []
        for f in workspace_extraction_fields(db, ws):
            fields.append({
                "key": f.key, "label": f.label, "help": f.help,
                "type": f.field_type, "options": f.options(),
                "builtin": bool(f.builtin),
                "show_if": ({"field": f.show_if_key, "values": f.show_if_values()}
                            if f.show_if_key else None),
            })
        return {"review": ws.name,
                "research_question": ws.research_question,
                "exclusion_criteria": crit("exclusion"),
                "inclusion_criteria": crit("inclusion"),
                "extraction_fields": fields}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Records (step 3) ──────────────────────────────────────────────────────────

@mcp.tool()
def search_records(review: str, q: str = "", screen1: str = "", screen2: str = "",
                   full_text: str = "", database: str = "", year_from: int = 0,
                   year_to: int = 0, limit: int = 50, offset: int = 0) -> dict:
    """
    Records in the pool, newest publication year first.

    q: substring of title, authors or abstract. Lexical and case-insensitive —
        a miss means those characters are absent, not that the topic is.
    screen1 / screen2: one of pending, include, exclude, maybe, conflict; plus
        two filters that are questions rather than states — `divergent` (at
        least one voice differing from another, the model's and every 'maybe'
        included, which is much wider than 'conflict') and `modelonly` (the
        model voted and no human has yet). screen2 also takes `empty`: included
        on full text with nothing extracted, i.e. in the review and
        contributing to no field of the synthesis. Any screen2 filter implies
        the screen-1 included pool, the only place screening 2 happens.
    full_text: none, url, fetched, converted, failed. Only `converted` is text
        a reviewer or the LLM actually reads.
    database: a source database key (pubmed, scopus, wos, …). Records carry
        every provenance that dedup merged into them.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        rows = _live(db, ws.id)
        if screen2:
            rows = rows.filter(Record.screen1_decision == "include")
        for stage, val in (("screen1", screen1), ("screen2", screen2)):
            if not val:
                continue
            col = Record.screen1_decision if stage == "screen1" else Record.screen2_decision
            by = Record.screen1_by if stage == "screen1" else Record.screen2_by
            if val in DECISIONS:
                rows = rows.filter(col == val)
            elif val == "divergent":
                rows = rows.filter(Record.id.in_(_divergent_sub(db, ws.id, stage)))
            elif val == "modelonly":
                rows = rows.filter(by == "model")
            elif val == "empty" and stage == "screen2":
                ids = {r.id for r in _live(db, ws.id)
                       .filter(Record.screen2_decision == "include").all()
                       } - _extracted_ids(db, ws.id)
                rows = rows.filter(Record.id.in_(ids or [-1]))
            else:
                return _fail(f"Unknown {stage} filter '{val}'. One of: "
                             + ", ".join(DECISIONS) + ", divergent, modelonly"
                             + (", empty" if stage == "screen2" else ""))
        if q.strip():
            like = f"%{q.strip()}%"
            rows = rows.filter(or_(Record.title.ilike(like), Record.authors.ilike(like),
                                   Record.abstract.ilike(like)))
        if full_text:
            rows = rows.filter(Record.full_text_status == full_text)
        if database:
            rows = rows.filter(Record.source_dbs_json.like(f'%"{database}"%'))
        if year_from:
            rows = rows.filter(Record.year >= int(year_from))
        if year_to:
            rows = rows.filter(Record.year <= int(year_to))
        total = rows.count()
        n = max(1, min(int(limit or 50), 200))
        off = max(0, int(offset or 0))
        hits = (rows.order_by(Record.year.desc().nullslast(), Record.id.desc())
                    .offset(off).limit(n).all())
        return {"review": ws.name, "lexical": True, "matched": total,
                "offset": off, "returned": len(hits),
                "records": [_brief(r) for r in hits]}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def get_record(review: str, record_id: int) -> dict:
    """
    One record in full: metadata, abstract, provenance, every screening vote at
    both stages, and every extraction row.

    The extraction comes as `authoritative` plus the rows it was chosen from.
    That priority is the review's, not this tool's: the owner-curated `final`
    row wins, else the most recently saved reviewer's, else the model's draft.
    Reading the rows separately is how you see a model draft nobody has
    confirmed, or two reviewers who extracted the same paper differently.

    The full text is not here even when there is one — see get_fulltext.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        r = (db.query(Record).filter(Record.id == int(record_id),
                                     Record.workspace_id == ws.id).first())
        if r is None:
            return _fail(f"No record {record_id} in '{ws.name}'")
        out = _brief(r)
        out.update({
            "abstract": r.abstract,
            "keywords": _j(r.keywords_json),
            "mesh": _j(r.mesh_json),
            "language": r.language,
            "added_manually": bool(r.added_manually),
            "removed_as_duplicate": bool(r.is_removed),
            "full_text": {
                "status": r.full_text_status,
                "url": r.full_text_url,
                "note": r.full_text_note,
                "chars": len(r.full_text_md or ""),
            },
            "screen1_votes": _votes(db, [r.id], "screen1").get(r.id, []),
            "screen1_reason": r.screen1_reason,
            "screen2_votes": _votes(db, [r.id], "screen2").get(r.id, []),
            "screen2_reason": r.screen2_reason,
        })
        rows = db.query(Extraction).filter(Extraction.record_id == r.id).all()
        out["extractions"] = [{
            "kind": e.reviewer_kind,
            "reviewer": ("the model" if e.reviewer_kind == "model"
                         else (e.reviewer.name if e.reviewer else "unknown")),
            "updated": _d(e.updated_at),
            "values": e.values(),
        } for e in rows]
        out["authoritative"] = authoritative_values(db, r)
        return out
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Full text (step 5) ────────────────────────────────────────────────────────

@mcp.tool()
def get_fulltext(review: str, record_id: int, offset: int = 0,
                 limit: int = FULLTEXT_CHUNK) -> dict:
    """
    The retrieved full text of one record, as markdown, in slices.

    Separate from get_record and paginated on purpose: a paper is tens of
    thousands of characters and a review has hundreds of them, so this is the
    one call on this surface that can fill a context by itself. `next_offset`
    is null once the end has been reached.

    This is publisher content retrieved under the reviewer's own entitlement.
    It is readable here because whoever holds the key can already read it in the
    app; it is not a redistribution channel.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        r = (db.query(Record).filter(Record.id == int(record_id),
                                     Record.workspace_id == ws.id).first())
        if r is None:
            return _fail(f"No record {record_id} in '{ws.name}'")
        text = r.full_text_md or ""
        if not text:
            return {"record_id": r.id, "title": r.title,
                    "status": r.full_text_status, "note": r.full_text_note,
                    "chars": 0, "text": "",
                    "message": ("No converted full text for this record "
                                f"(retrieval status '{r.full_text_status}'). A "
                                "reviewer can still have decided it on other "
                                "grounds — an unobtainable report is a decided "
                                "record, not a pending one.")}
        off = max(0, int(offset or 0))
        n = max(1_000, min(int(limit or FULLTEXT_CHUNK), 80_000))
        chunk = text[off:off + n]
        end = off + len(chunk)
        return {"record_id": r.id, "title": r.title, "doi": r.doi,
                "status": r.full_text_status, "chars": len(text),
                "offset": off, "returned": len(chunk),
                "next_offset": end if end < len(text) else None,
                "text": chunk}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def fulltext_status(review: str, limit: int = 30) -> dict:
    """
    How full-text retrieval went across the records that need one.

    The denominator is the screen-1 included pool, because those are the papers
    the review sought. `converted` is text a reviewer and the LLM can read;
    `url` means an open-access location was found but the document itself was
    never converted; `failed` and `none` are the holes. The note on a missing
    one is the retrieval ladder's own account of what it tried — candidates
    discarded, titles that did not match, a publisher refusing without an
    institutional token.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        sought = _live(db, ws.id).filter(Record.screen1_decision == "include").all()
        counts = Counter(r.full_text_status or "none" for r in sought)
        missing = [r for r in sought if r.full_text_status != "converted"]
        n = max(1, min(int(limit or 30), 200))
        return {
            "review": ws.name,
            "sought": len(sought),
            "by_status": dict(counts),
            "converted": counts.get("converted", 0),
            "missing": len(missing),
            "missing_records": [{
                "id": r.id, "title": r.title, "year": r.year, "doi": r.doi,
                "status": r.full_text_status, "url": r.full_text_url,
                "note": r.full_text_note, "screen2": r.screen2_decision,
            } for r in missing[:n]],
        }
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Screening (steps 4, 6) ────────────────────────────────────────────────────

@mcp.tool()
def list_conflicts(review: str, stage: str = "screen1", wide: bool = True,
                   limit: int = 50) -> dict:
    """
    Records whose reviewers do not agree, with every vote, so that what has to
    be adjudicated is visible instead of counted.

    stage: screen1 (title and abstract) or screen2 (full text).
    wide: with True (the default) this is the `divergent` set — any voice
        differing from another, the model's and every 'maybe' included. With
        False it is only the narrow `conflict` state, two humans disagreeing.
        The gap between the two is usually large, and that is the point: a
        corpus can show zero conflicts and hundreds of divergences.

    Resolving one is an adjudication, which is a human act in the web app.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        if stage not in ("screen1", "screen2"):
            return _fail("stage must be 'screen1' or 'screen2'")
        col = Record.screen1_decision if stage == "screen1" else Record.screen2_decision
        rows = _live(db, ws.id)
        if stage == "screen2":
            rows = rows.filter(Record.screen1_decision == "include")
        rows = (rows.filter(Record.id.in_(_divergent_sub(db, ws.id, stage))) if wide
                else rows.filter(col == "conflict"))
        total = rows.count()
        n = max(1, min(int(limit or 50), 200))
        hits = rows.order_by(Record.id.desc()).limit(n).all()
        votes = _votes(db, [r.id for r in hits], stage)
        required = (ws.screen1_reviewers_required or 1 if stage == "screen1"
                    else screen2_required(ws))
        return {"review": ws.name, "stage": stage,
                "set": "divergent" if wide else "conflict",
                "reviewers_required": required,
                "matched": total, "returned": len(hits),
                "records": [dict(_brief(r), votes=votes.get(r.id, [])) for r in hits]}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Extraction (step 6) ───────────────────────────────────────────────────────

@mcp.tool()
def extraction_summary(review: str, field: str = "", population: str = "included",
                       top: int = 25) -> dict:
    """
    The distribution of extracted values, computed from the database.

    field: an extraction field key (see get_protocol). Empty summarises every
        field at once, which is the fastest way to see the shape of the corpus.
    population: `included` (the finally included papers — what the synthesis
        describes) or `screen1` (everything that survived title and abstract,
        which shows the extraction still in progress).

    Each field reports how many records answered it and how many did not, then
    the value counts. Multiselect values are counted once per option chosen, so
    those counts can exceed the number of records. Free-text fields report
    length rather than a tally, because tallying prose would invent categories.
    Values come from the authoritative extraction per record: `final` if the
    owner curated one, else the last reviewer to save, else the model's draft.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        if population not in ("included", "screen1"):
            return _fail("population must be 'included' or 'screen1'")
        pool = _live(db, ws.id)
        recs = (pool.filter(Record.screen2_decision == "include").all()
                if population == "included"
                else pool.filter(Record.screen1_decision == "include").all())
        fields = workspace_extraction_fields(db, ws)
        if field:
            fields = [f for f in fields if f.key == field]
            if not fields:
                return _fail(f"No extraction field '{field}'. See get_protocol.")
        values = {r.id: authoritative_values(db, r) for r in recs}
        out = []
        for f in fields:
            answered, counts, lengths = 0, Counter(), []
            for r in recs:
                v = values[r.id].get(f.key)
                if isinstance(v, list):
                    v = [x for x in v if str(x).strip() != ""]
                    if not v:
                        continue
                    answered += 1
                    for x in v:
                        counts[str(x)] += 1
                elif v is not None and str(v).strip() != "":
                    answered += 1
                    if f.field_type == "textarea":
                        lengths.append(len(str(v)))
                    else:
                        counts[str(v)] += 1
            item = {"key": f.key, "label": f.label, "type": f.field_type,
                    "answered": answered, "unanswered": len(recs) - answered}
            if f.field_type == "textarea":
                item["mean_chars"] = round(sum(lengths) / len(lengths)) if lengths else 0
            else:
                item["distinct_values"] = len(counts)
                item["values"] = [{"value": v, "n": c} for v, c
                                  in counts.most_common(max(1, int(top or 25)))]
            out.append(item)
        return {"review": ws.name, "population": population,
                "records": len(recs), "computed_from_database": True,
                "fields": out}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Synthesis (step 7) ────────────────────────────────────────────────────────

@mcp.tool()
def get_synthesis(review: str) -> dict:
    """
    The narrative synthesis as it stands, block by block, plus its PRISMA
    counts and the public link if the review has one.

    The citations inside a block are procedural: the model that wrote the prose
    only ever placed a token, and the author-year-DOI you read was substituted
    from the record's own fields afterwards. That is why a reference here cannot
    be invented — the model chose where a citation goes, never what it says.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        syn = db.query(Synthesis).filter(Synthesis.workspace_id == ws.id).first()
        if syn is None:
            return {"review": ws.name, "exists": False,
                    "message": "No synthesis generated yet.",
                    "prisma": compute_prisma(db, ws.id)}
        blocks = sorted(syn.blocks, key=lambda b: (b.position or 0, b.id))
        return {"review": ws.name, "exists": True,
                "published": bool(syn.published),
                "generated": _d(syn.generated_at),
                "public_url": _share_url(db, ws.id),
                "prisma": compute_prisma(db, ws.id),
                "blocks": [{"heading": b.heading, "narrative": b.narrative}
                           for b in blocks]}
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()
