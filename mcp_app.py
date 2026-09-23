"""
The model-facing surface of LSSR.

A scoping review is the kind of object you end up asking questions about far
more often than you edit it: how many records are still unscreened, what the
inclusion criteria actually say, which studies came out of Wales, what the
extraction says about study design. Those questions arrive in a conversation,
and until now the only way to answer them was to open the app, pick a tab and
count by eye. This is the same corpus, readable from where the questions are.

**A few verbs write, and the rest reads.** They are vote_screen1, set_earmark
and the protocol verbs at the bottom of this file, and the promise the first
version made still holds for everything else: no extraction, no adjudication,
no step marked done, no review created or deleted. The reason
writes were refused wholesale was that a screening decision carries a reviewer's
name and belongs to a person doing the reading, so a surface where a model could
cast one turns the reviewer into an editor of its own output. That objection was
never answered, only priced. Title-and-abstract screening at the scale these
reviews reach is thousands of judgements against a written criterion list, which
is work a conversation can genuinely share; and the vote it casts is a `user`
row like any other, signed with the key owner's name, resolved by the same
resolve_screen1 and counted in the same PRISMA. The trace is in the reason text,
which always carries `[via MCP]`, and in the capability: writing belongs to the
key, not to the person, so a key minted before this existed still cannot vote
and a leaked reader still cannot corrupt a corpus. Screening 2 is not here,
because in the app it happens in the same act as the extraction.

The second verb is cheaper and needed less argument. An earmark decides
nothing: it moves no count, enters no export, and comes off in one click. It is
here because the notes it holds were already being written — folded into the
reason text of votes, where an observation about a record ends up filed as a
reason for a decision it was not. It still needs a writing key, because the
mark is visible to the whole review and a read-only key that could annotate
seven hundred records is not read-only in any sense worth the name.

The protocol verbs came third, for a plainer reason: rewording a criterion or
correcting an extraction field's help was being done by copying text out of a
conversation into the settings page, one field at a time. They add and edit
criteria and fields and rewrite the research question; they never delete,
because deleting a criterion renumbers the ones every reason cites and deleting
a field discards what was extracted into it. They belong to the review's owner
and not to every member, and every change is logged with the text it replaced
(protocol_changes), from this surface and from the web app alike, because a
protocol amended mid-review has to be reported and the old wording is exactly
what used to be lost.

The consequence worth saying out loud: blinding. The web app hides other
reviewers' votes until you have cast yours; this surface shows them all, which
was harmless while nothing here could vote. It is not harmless now. A model that
reads a record and then votes on it has seen every other voice first, including
the model screening pass, so what it casts is not an independent second reading
and must not be counted as one. Use it to work through a pile against the
criteria, and adjudicate in the UI, where the blinding is.

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
    DECIDING_KINDS, HARVEST_DBS, PIPELINE_STEPS, Extraction, Import, Iteration, PublicShare,
    Record, ScreenDecision, SearchQuery, SessionLocal, Synthesis, UserCostLog,
    authoritative_values, db_label, db_search_url, earmarks_by_record, human_voted_subq,
    my_earmark_ids, screen2_required,
    user_workspaces, workspace_criteria, workspace_extraction_fields,
    workspace_steps_done, workspace_target_dbs,
)
from synthesis import compute_prisma

mcp = MCPServer(
    name="lssr",
    instructions=(
        "Living systematic scoping reviews: the whole pipeline from query to "
        "synthesis, one workspace per review. Start with list_reviews, then "
        "get_review for the state of one and get_protocol for the criteria and "
        "the extraction schema it is being read against. Counts come from the "
        "database, so they are exact: prefer extraction_summary to counting "
        "records yourself. search_records is lexical, not semantic — no hit "
        "means those words are not in the title, abstract or authors, never "
        "that the corpus lacks the topic. "
        "A few verbs write and the rest read. vote_screen1 casts a "
        "title-and-abstract vote in the key owner's name: read get_protocol "
        "first, because a vote not argued from the written criteria is noise "
        "in somebody's review, and confirm with the user before the first one. "
        "set_earmark writes a note in the margin of a record, which the whole "
        "team reads and which decides nothing — that is where an observation "
        "about a record belongs, and a verdict is not one. The protocol verbs "
        "(update_criterion, add_criterion, update_field, add_field, "
        "move_field, update_details) belong to the review's owner: confirm the exact "
        "wording with the user before writing, since every vote is argued from "
        "that text; nothing is deleted from here, and protocol_history shows "
        "what each change replaced. All writes need a key minted with writing "
        "enabled. Extraction, adjudication, screening 2, deleting criteria or "
        "fields, and marking a step done stay in the web app."
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
                         else "the model, dry run (does not count)" if v.reviewer_kind == "shadow"
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
    means two humans disagreeing — on a real corpus the gap is large.

    Shadow rows are left out: a dry run differing from a reviewer is the
    measurement being taken, not a record in dispute."""
    return (db.query(ScreenDecision.record_id)
              .filter(ScreenDecision.workspace_id == ws_id,
                      ScreenDecision.stage == stage,
                      ScreenDecision.reviewer_kind.in_(DECIDING_KINDS))
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

    The top-level counts are always the totals. When a review has records that
    arrived by some route other than searching a database — expert knowledge,
    citation chasing, grey literature — `prisma.arms` splits the same flow into
    the two columns PRISMA 2020 draws, `db` and `other`. A record counts as
    `other` only when none of its provenances is a database, so `arms.other`
    measures what those routes found *that the search missed*, and
    `arms.other.already_found_by_search` says how much of a nominated list the
    query had already caught. Reviews with no such records have no `arms` key.
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

        # `number` is what the settings page shows and what reasons cite, and
        # what update_criterion takes
        def crit(kind):
            return [{"number": i + 1, "label": c.label, "description": c.description}
                    for i, c in enumerate(workspace_criteria(db, ws, kind))]
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
                   earmarked: bool = False,
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
    earmarked: True returns only the records the caller has earmarked. Only
        ever the caller's own — everybody's marks are readable on a record
        through get_record, but what *other* people have flagged is a
        different question and not one this filter answers.
    full_text: none, url, fetched, converted, failed. Only `converted` is text
        a reviewer or the LLM actually reads.
    database: a source database key (pubmed, scopus, wos, …). Records carry
        every provenance that dedup merged into them.
    """
    db = SessionLocal()
    try:
        caller = auth.current_caller()
        ws = auth.mcp_review(db, review)
        rows = _live(db, ws.id)
        if earmarked:
            rows = rows.filter(Record.id.in_(
                my_earmark_ids(db, ws.id, caller.id) or {-1}))
        if screen2:
            rows = rows.filter(Record.screen1_decision == "include")
        for stage, val in (("screen1", screen1), ("screen2", screen2)):
            if not val:
                continue
            col = Record.screen1_decision if stage == "screen1" else Record.screen2_decision
            if val in DECISIONS:
                rows = rows.filter(col == val)
            elif val == "divergent":
                rows = rows.filter(Record.id.in_(_divergent_sub(db, ws.id, stage)))
            elif val == "modelonly":
                # the model has ruled and no person has yet. Filtering on `by`
                # returned the whole pool: it holds the resolved decision, which
                # stays 'model' while a lone human vote sits below quorum.
                rows = rows.filter(col != "pending",
                                   ~Record.id.in_(human_voted_subq(db, ws.id, stage)))
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
        caller = auth.current_caller()
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
            # The margin of the shared copy: every reviewer's earmark on this
            # record, theirs as well as the caller's. Notes, not decisions —
            # nothing here is counted anywhere, and a note arguing a verdict is
            # still only a note. `mine` says which one the caller could edit in
            # the web app; this surface does not write them.
            "earmarks": [{"reviewer": (m.user.name if m.user else "a reviewer"),
                          "mine": m.user_id == caller.id,
                          "note": m.note,
                          "marked": _d(m.created_at)}
                         for m in earmarks_by_record(db, ws.id, [r.id]).get(r.id, [])],
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

    `searched_not_found` is the human side: records somebody looked for by hand
    and marked as not found, with who, when and where they looked. They are
    still missing and still pending; `still_to_find` is the rest of the missing
    ones, the hand-search worklist. Records excluded at screening 2 are in
    neither, since nobody needs their full text.
    """
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        sought = _live(db, ws.id).filter(Record.screen1_decision == "include").all()
        counts = Counter(r.full_text_status or "none" for r in sought)
        missing = [r for r in sought if r.full_text_status != "converted"]
        wanted = [r for r in missing if r.screen2_decision != "exclude"]
        unfound = [r for r in wanted if r.full_text_unfound_at]
        names = {}
        from models import User
        for uid in {r.full_text_unfound_by for r in unfound if r.full_text_unfound_by}:
            u = db.get(User, uid)
            names[uid] = u.name if u else None
        n = max(1, min(int(limit or 30), 200))
        return {
            "review": ws.name,
            "sought": len(sought),
            "by_status": dict(counts),
            "converted": counts.get("converted", 0),
            "missing": len(missing),
            "searched_not_found": len(unfound),
            "still_to_find": len(wanted) - len(unfound),
            "missing_records": [{
                "id": r.id, "title": r.title, "year": r.year, "doi": r.doi,
                "status": r.full_text_status, "url": r.full_text_url,
                "note": r.full_text_note, "screen2": r.screen2_decision,
                "searched_not_found": ({
                    "by": names.get(r.full_text_unfound_by),
                    "at": _d(r.full_text_unfound_at),
                    "where": r.full_text_unfound_note,
                } if r.full_text_unfound_at else None),
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


@mcp.tool()
def vote_screen1(review: str, record_id: int, decision: str, reason: str = "") -> dict:
    """
    Cast or retract one title-and-abstract vote.

    decision: include, exclude, maybe, or `clear` to retract the vote you
        already cast. Retracting only ever removes your own row; an
        adjudicator's ruling and the model's pass are untouched.
    reason: why, in the review's own terms — which exclusion criterion the
        abstract trips, or what makes it eligible. Stored with `[via MCP]`
        appended, always, so the provenance survives into the UI and the
        screening export. Read get_protocol before the first vote: a vote
        argued from anything but the written criteria is noise in a corpus
        somebody else will have to trust.

    The vote is a `user` row signed with the key owner's name, identical to one
    cast in the browser and resolved by the same rules: an adjudicator outranks
    it, two humans differing make a conflict, and until the review's required
    number of reviewers have voted the model's provisional decision still
    stands. So the answer reports `record_decision` after the write as well as
    the vote itself, because casting a vote and settling a record are not the
    same event and on a two-reviewer review they usually are not the same day.

    One record per call, deliberately. There is no batch verb: a screening pass
    is hundreds of individual judgements and a tool that takes them in bulk is a
    tool that writes hundreds of them from one misreading.

    Needs a key minted with writing enabled, and it is not an independent second
    reading — see the note on blinding at the top of this module.
    """
    from models import recompute_record_screen1, upsert_screen_decision
    decision = (decision or "").strip().lower()
    if decision not in ("include", "exclude", "maybe", "clear"):
        return _fail("decision must be include, exclude, maybe, or clear")
    db = SessionLocal()
    try:
        user = auth.require_write()
        ws = auth.mcp_review(db, review)
        r = (db.query(Record).filter(Record.id == int(record_id),
                                     Record.workspace_id == ws.id).first())
        if r is None:
            return _fail(f"No record {record_id} in '{ws.name}'")
        if r.is_removed:
            return _fail(f"Record {record_id} was merged into another as a "
                         "duplicate and is not in the screening pool.")
        mine = (db.query(ScreenDecision)
                  .filter(ScreenDecision.record_id == r.id,
                          ScreenDecision.stage == "screen1",
                          ScreenDecision.reviewer_kind == "user",
                          ScreenDecision.reviewer_id == user.id).first())
        # Read before writing: the upsert mutates this very row, so asking it
        # afterwards what it used to say returns the answer we just put there.
        previous = mine.decision if mine else None
        if decision == "clear":
            if mine is None:
                return _fail(f"You have no screen-1 vote on record {record_id} "
                             "to retract.")
            db.delete(mine)
            db.flush()
        else:
            note = (reason or "").strip()
            upsert_screen_decision(db, r, "screen1", "user", user.id, decision,
                                   f"{note} [via MCP]" if note else "via MCP")
        recompute_record_screen1(db, ws, r)
        db.commit()
        return {
            "review": ws.name,
            "record": {"id": r.id, "title": r.title, "year": r.year},
            "your_vote": None if decision == "clear" else decision,
            "replaced": previous,
            "record_decision": r.screen1_decision,
            "decided_by": r.screen1_by,
            "reviewers_required": ws.screen1_reviewers_required or 1,
            "votes": _votes(db, [r.id], "screen1").get(r.id, []),
        }
    except (LookupError, PermissionError) as e:
        db.rollback()
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


@mcp.tool()
def set_earmark(review: str, record_id: int, note: str = "", on: bool = True) -> dict:
    """
    Put the caller's earmark on a record, change its note, or take it off.

    An earmark is the margin of the shared copy: a dot on a record, one per
    reviewer, with an optional line of why. Everyone in the review sees
    everyone's; this writes only the caller's own. It **decides nothing** — no
    screening decision, no PRISMA number, no extracted field, and it appears in
    no export. That is what makes it the right place for the things a vote's
    reason keeps being asked to carry: *this is the same study as 3286 under a
    different DOI*, *the abstract on this record belongs to another paper*,
    *the results of this protocol are already in the pool as 3257*.

    note: at most 280 characters, one line or two — newlines are kept, blank
        lines collapsed. Longer is cut rather than refused, and the answer
        says so in `truncated`. Write what a colleague could not work out on
        their own; a verdict belongs in the vote, where it is counted and
        attributed.
    on: False takes the caller's earmark off the record, note and all.

    **The call describes the end state, not a change to it.** `note` is written
    exactly as given, so omitting it on a record you have already annotated
    clears that note rather than leaving it alone. Read the record first if you
    mean to keep what is there.

    One record per call, like the vote. Not because a wrong note is expensive —
    it is one click to clear — but because each of these is about *this* paper,
    and a verb that took a list would mostly be used to write the same sentence
    on records that did not each earn it.

    Needs a key minted with writing enabled. An earmark cannot corrupt a review,
    but it is visible to the whole team, and a leaked read-only key that could
    write on seven hundred records is no longer a read-only key.

    Unlike a vote, the note carries no `[via MCP]` marker. A vote's provenance
    is part of a decision record somebody will have to defend; a margin note is
    signed with its author's name, decides nothing, and has 280 characters to
    say something in.
    """
    from models import EARMARK_NOTE_MAX, set_earmark as _set
    text = (note or "").strip()
    db = SessionLocal()
    try:
        user = auth.require_write()
        ws = auth.mcp_review(db, review)
        r = (db.query(Record).filter(Record.id == int(record_id),
                                     Record.workspace_id == ws.id).first())
        if r is None:
            return _fail(f"No record {record_id} in '{ws.name}'")
        row = _set(db, r, user.id, bool(on), text)
        db.commit()
        marks = earmarks_by_record(db, ws.id, [r.id]).get(r.id, [])
        return {
            "review": ws.name,
            "record": r.id,
            # Echoed so a misread id shows up as the wrong paper rather than as
            # a note that quietly landed somewhere else.
            "title": r.title,
            "earmarked": row is not None,
            "note": row.note if row else None,
            "truncated": len(text) > EARMARK_NOTE_MAX,
            "others": [{"reviewer": (m.user.name if m.user else "a reviewer"),
                        "note": m.note}
                       for m in marks if m.user_id != user.id],
        }
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()


# ── Protocol: criteria, extraction fields, research question ─────────────────
#
# The owner's, and only through a writing key: the protocol is what everybody
# else's votes are argued from, so a member who can read it may not reword it
# here. Nothing is deleted from this surface. Deleting a criterion renumbers
# the rest, and every reason that cites "criterion 4" starts pointing at
# another one; deleting a field throws away what was extracted into it. Both
# are done in the web app, with the protocol in front of you.
#
# Every change is logged in protocol_changes with its before and after, the
# same log the web app writes, and protocol_history reads it back.

CRITERION_KINDS = ("exclusion", "inclusion")
FIELD_TYPES = ("text", "textarea", "number", "select", "multiselect")


def _owner_review(db, review: str):
    """(user, ws) for a protocol write: a writing key, and the owner of the
    review or an admin. Raises PermissionError otherwise."""
    user = auth.require_write()
    ws = auth.mcp_review(db, review)
    if not (ws.owner_id == user.id or user.is_admin):
        raise PermissionError(
            f"Only the owner of '{ws.name}' can change its protocol. Members can "
            "read it with get_protocol.")
    return user, ws


def _criterion(db, ws, kind: str, number: int):
    rows = workspace_criteria(db, ws, kind)
    if not 1 <= int(number) <= len(rows):
        raise LookupError(f"'{ws.name}' has {len(rows)} {kind} criteria; there is "
                          f"no number {number}. get_protocol lists them.")
    return rows[int(number) - 1]


def _field(db, ws, key: str):
    from models import ensure_extraction_fields
    ensure_extraction_fields(db, ws)
    for f in workspace_extraction_fields(db, ws):
        if f.key == key:
            return f
    raise LookupError(f"No extraction field '{key}' in '{ws.name}'. get_protocol "
                      "lists the keys.")


def _option_use(db, ws, key: str) -> Counter:
    """How many extraction rows hold each value of one field: every row, every
    reviewer, the model's drafts included, since removing an option strands the
    value wherever it sits."""
    used = Counter()
    for e in db.query(Extraction).filter(Extraction.workspace_id == ws.id).all():
        v = e.values().get(key)
        for x in (v if isinstance(v, list) else [v] if v not in (None, "") else []):
            used[str(x)] += 1
    return used


def _clean_list(items) -> list:
    return [str(x).strip() for x in (items or []) if str(x).strip()]


def _show_if(db, ws, own_key: str | None, field: str, values):
    """Validate a show_if pair; returns (key, values), or (None, None) to clear."""
    field = (field or "").strip()
    # "none" as well as "": some clients cannot send an empty string, and
    # dropping the parameter means "leave it" rather than "clear it"
    if not field or field.lower() == "none":
        return None, None
    if field == own_key:
        raise ValueError("A field cannot be shown conditionally on itself.")
    parent = _field(db, ws, field)
    vals = _clean_list(values)
    if not vals:
        raise ValueError("show_if_values is needed with show_if_field: which "
                         f"values of '{field}' reveal this field?")
    if parent.options():
        unknown = [v for v in vals if v not in parent.options()]
        if unknown:
            raise ValueError(f"{unknown} are not options of '{field}': "
                             f"{parent.options()}")
    return parent.key, vals


@mcp.tool()
def update_criterion(review: str, kind: str, number: int, label: str = "",
                     description: str | None = None) -> dict:
    """
    Reword one screening criterion in place. Owner only, writing key.

    kind: exclusion (screening 1) or inclusion (screening 2).
    number: as get_protocol shows it, starting at 1. The criterion keeps its
        number and its place: only the words change.
    label: the new short name; empty leaves it as it is.
    description: the new full text, which reviewers and the model screen
        against. Omit to leave it; an empty string clears it.

    Votes already cast are not touched and are not re-read: a vote argued from
    the old wording stays argued from the old wording. That is why the change
    is logged with the text it replaced (protocol_history), and why a reworded
    criterion usually deserves a look at the records it decided.
    """
    from models import criterion_snapshot, log_protocol_change
    kind = (kind or "").strip().lower()
    if kind not in CRITERION_KINDS:
        return _fail("kind must be exclusion or inclusion")
    if not (label or "").strip() and description is None:
        return _fail("Nothing to change: give a label, a description, or both.")
    db = SessionLocal()
    try:
        user, ws = _owner_review(db, review)
        c = _criterion(db, ws, kind, number)
        before = criterion_snapshot(c)
        if (label or "").strip():
            c.label = label.strip()
        if description is not None:
            c.description = description.strip() or None
        after = criterion_snapshot(c)
        log_protocol_change(db, ws.id, user.id, "mcp", "criterion", "edit",
                            f"{kind} {after['number']}", before, after)
        db.commit()
        return {"review": ws.name, "changed": before != after,
                "before": before, "after": after}
    except (LookupError, PermissionError) as e:
        db.rollback()
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def add_criterion(review: str, kind: str, label: str, description: str = "") -> dict:
    """
    Add a screening criterion at the end of its list. Owner only, writing key.

    kind: exclusion (screening 1) or inclusion (screening 2).
    description: the full text reviewers and the model will screen against.
        Worth writing properly: a criterion whose description only repeats its
        label gives the pre-screener nothing to decide with.

    Records already screened are not re-screened against it; the answer says
    how many were decided without it.
    """
    from models import Criterion, criterion_snapshot, log_protocol_change, recompact_criteria
    kind = (kind or "").strip().lower()
    if kind not in CRITERION_KINDS:
        return _fail("kind must be exclusion or inclusion")
    if not (label or "").strip():
        return _fail("A criterion needs a label.")
    db = SessionLocal()
    try:
        user, ws = _owner_review(db, review)
        c = Criterion(workspace_id=ws.id, kind=kind, label=label.strip(),
                      description=(description or "").strip() or None,
                      position=len(workspace_criteria(db, ws, kind)))
        db.add(c)
        db.flush()
        recompact_criteria(db, ws.id, kind)
        after = criterion_snapshot(c)
        log_protocol_change(db, ws.id, user.id, "mcp", "criterion", "add",
                            f"{kind} {after['number']}", None, after)
        db.commit()
        col = Record.screen1_decision if kind == "exclusion" else Record.screen2_decision
        decided = _live(db, ws.id).filter(col != "pending").count()
        return {"review": ws.name, "added": after,
                "already_decided_without_it": decided}
    except (LookupError, PermissionError) as e:
        db.rollback()
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def update_field(review: str, key: str, label: str = "", help: str | None = None,
                 options: list[str] | None = None, show_if_field: str | None = None,
                 show_if_values: list[str] | None = None) -> dict:
    """
    Correct one extraction field in place. Owner only, writing key.

    key: the field's key from get_protocol. It never changes, so values already
        extracted stay attached.
    label: new display name; empty leaves it.
    help: the guidance reviewers see and the model drafts from. Omit to leave
        it; an empty string clears it.
    options: the complete new list, for select and multiselect fields. An
        option that some extraction already holds cannot be removed (or
        renamed, which is the same thing): the answer names it and says how
        many rows use it. Adding options and reordering them is always fine.
    show_if_field / show_if_values: ask this field only when another field
        holds one of these values. show_if_field="none" (or "")
        removes the condition.

    The type of a field cannot be changed here: values extracted as one type
    do not become another.
    """
    from models import field_snapshot, log_protocol_change
    if (not (label or "").strip() and help is None and options is None
            and show_if_field is None):
        return _fail("Nothing to change.")
    db = SessionLocal()
    try:
        user, ws = _owner_review(db, review)
        f = _field(db, ws, key)
        before = field_snapshot(f)
        if options is not None:
            if f.field_type not in ("select", "multiselect"):
                return _fail(f"'{key}' is a {f.field_type} field and has no options.")
            new = _clean_list(options)
            if not new:
                return _fail("A select field needs at least one option.")
            if len(set(new)) != len(new):
                return _fail("The option list has duplicates.")
            used = _option_use(db, ws, f.key)
            stranded = {o: used[o] for o in f.options() if o not in new and used[o]}
            if stranded:
                return _fail(f"These options are in use and would be stranded: "
                             f"{stranded} (rows per option). Keep them in the list, "
                             "or re-extract those records first.")
            f.options_json = json.dumps(new, ensure_ascii=False)
        if (label or "").strip():
            f.label = label.strip()
        if help is not None:
            f.help = help.strip() or None
        if show_if_field is not None:
            k, vals = _show_if(db, ws, f.key, show_if_field, show_if_values)
            f.show_if_key = k
            f.show_if_values_json = json.dumps(vals, ensure_ascii=False) if vals else None
        after = field_snapshot(f)
        log_protocol_change(db, ws.id, user.id, "mcp", "field", "edit", f.key, before, after)
        db.commit()
        return {"review": ws.name, "changed": before != after,
                "before": before, "after": after}
    except (LookupError, PermissionError, ValueError) as e:
        db.rollback()
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def add_field(review: str, label: str, type: str, help: str = "",
              options: list[str] | None = None, show_if_field: str = "",
              show_if_values: list[str] | None = None) -> dict:
    """
    Add an extraction field at the end of the form. Owner only, writing key.

    type: text, textarea, number, select or multiselect.
    options: required for select and multiselect, ignored otherwise.
    help: what the reviewer should put there. The model drafts extractions from
        it too, so it is the field's real definition.
    show_if_field / show_if_values: optional condition, as in update_field.

    The key is derived from the label and returned; it is what update_field
    and extraction_summary take.
    """
    from models import (ExtractionField, ensure_extraction_fields, field_snapshot,
                        log_protocol_change, slug_field_key)
    type = (type or "").strip().lower()
    if type not in FIELD_TYPES:
        return _fail(f"type must be one of {', '.join(FIELD_TYPES)}")
    if not (label or "").strip():
        return _fail("A field needs a label.")
    opts = _clean_list(options) if type in ("select", "multiselect") else []
    if type in ("select", "multiselect") and not opts:
        return _fail(f"A {type} field needs options.")
    if len(set(opts)) != len(opts):
        return _fail("The option list has duplicates.")
    db = SessionLocal()
    try:
        user, ws = _owner_review(db, review)
        ensure_extraction_fields(db, ws)
        existing = workspace_extraction_fields(db, ws)
        key = slug_field_key(label, {f.key for f in existing})
        k, vals = _show_if(db, ws, key, show_if_field, show_if_values)
        f = ExtractionField(
            workspace_id=ws.id, key=key, label=label.strip(),
            help=(help or "").strip() or None, field_type=type,
            options_json=json.dumps(opts, ensure_ascii=False) if opts else None,
            show_if_key=k,
            show_if_values_json=json.dumps(vals, ensure_ascii=False) if vals else None,
            builtin=False, position=max((x.position for x in existing), default=-1) + 1)
        db.add(f)
        after = field_snapshot(f)
        log_protocol_change(db, ws.id, user.id, "mcp", "field", "add", key, None, after)
        db.commit()
        return {"review": ws.name, "added": after}
    except (LookupError, PermissionError, ValueError) as e:
        db.rollback()
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def move_field(review: str, key: str, position: int = 0, after: str = "") -> dict:
    """
    Move one extraction field to another place in the form. Owner only,
    writing key.

    Give either `position` (1 = first, as get_protocol lists the fields) or
    `after`, the key of the field it should follow; `after` is usually what
    you mean ("put causal_design after methodology_time") and survives other
    fields being added in between.

    Only the order of the form changes: no value, key or definition moves, so
    this is not logged as a protocol change. Criteria have no equivalent on
    purpose: their order is their number, and reasons cite the number.
    """
    from models import ensure_extraction_fields
    if not position and not (after or "").strip():
        return _fail("Give a position (1 = first) or the key of the field to follow.")
    db = SessionLocal()
    try:
        user, ws = _owner_review(db, review)
        ensure_extraction_fields(db, ws)
        fields = workspace_extraction_fields(db, ws)
        f = _field(db, ws, key)
        rest = [x for x in fields if x.id != f.id]
        if (after or "").strip():
            anchor = _field(db, ws, after.strip())
            if anchor.id == f.id:
                return _fail("A field cannot follow itself.")
            idx = next(i for i, x in enumerate(rest) if x.id == anchor.id) + 1
        else:
            if not 1 <= int(position) <= len(fields):
                return _fail(f"position must be between 1 and {len(fields)}.")
            idx = int(position) - 1
        before = [x.key for x in fields]
        rest.insert(idx, f)
        for i, x in enumerate(rest):
            x.position = i
        db.commit()
        order = [x.key for x in rest]
        return {"review": ws.name, "moved": f.key, "changed": order != before,
                "position": order.index(f.key) + 1, "order": order}
    except (LookupError, PermissionError) as e:
        db.rollback()
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def update_details(review: str, research_question: str | None = None,
                   description: str | None = None) -> dict:
    """
    Rewrite the research question or the description of a review. Owner only,
    writing key.

    Omit a parameter to leave it as it is. The model reads the research
    question at every screening and assessment step, and the description is
    what the public page shows, so this is a protocol change like any other
    and is logged as one.
    """
    from models import log_protocol_change
    if research_question is None and description is None:
        return _fail("Nothing to change.")
    db = SessionLocal()
    try:
        user, ws = _owner_review(db, review)
        before = {"research_question": ws.research_question, "description": ws.description}
        if research_question is not None:
            ws.research_question = research_question.strip() or None
        if description is not None:
            ws.description = description.strip() or None
        after = {"research_question": ws.research_question, "description": ws.description}
        log_protocol_change(db, ws.id, user.id, "mcp", "details", "edit", None, before, after)
        db.commit()
        return {"review": ws.name, "changed": before != after,
                "before": before, "after": after}
    except (LookupError, PermissionError) as e:
        db.rollback()
        return _fail(str(e))
    finally:
        db.close()


@mcp.tool()
def protocol_history(review: str, limit: int = 50) -> dict:
    """
    Every recorded change to the protocol, newest first: who, when, through
    which door (web or mcp), and the criterion or field as it read before and
    after.

    The log starts on the day it was built. Edits made before then overwrote
    the old text without a trace, so a short history is not evidence that the
    protocol never changed.
    """
    from models import ProtocolChange
    db = SessionLocal()
    try:
        ws = auth.mcp_review(db, review)
        rows = (db.query(ProtocolChange)
                  .filter(ProtocolChange.workspace_id == ws.id)
                  .order_by(ProtocolChange.created_at.desc(), ProtocolChange.id.desc())
                  .limit(max(1, min(int(limit), 500))).all())
        return {
            "review": ws.name,
            "changes": [{
                "at": _d(r.created_at),
                "by": r.user.name if r.user else None,
                "via": r.via, "target": r.target, "action": r.action, "ref": r.ref,
                "before": json.loads(r.before_json) if r.before_json else None,
                "after": json.loads(r.after_json) if r.after_json else None,
            } for r in rows],
        }
    except (LookupError, PermissionError) as e:
        return _fail(str(e))
    finally:
        db.close()
