"""
Synthesis (step 10): the public deliverable.

Builds the PRISMA flow counts from the DB and, for each free-text extraction
field (text/textarea), asks the LLM to aggregate what was extracted from each
included study into one narrative paragraph with inline citations. Structured
fields (select/multiselect/number) are not narrated — they are distributions,
not prose. Values come from each record's authoritative extraction (the curated
final row, else the latest reviewer's, else the model draft).

Stored as Synthesis + SynthesisBlock rows, shown on the public /r/{token} page
when published. Background job, JOBS keyed by workspace_id.
"""
import json
import re
import threading

JOBS: dict[int, dict] = {}
_lock = threading.Lock()


def get_job(workspace_id: int) -> dict | None:
    with _lock:
        return JOBS.get(workspace_id)


def _set(workspace_id: int, data: dict):
    with _lock:
        JOBS[workspace_id] = data


# ── PRISMA counts ──────────────────────────────────────────────────────────────

def compute_prisma(db, workspace_id: int) -> dict:
    from models import Record, RawReference
    R = Record
    def rc(*filters):
        return db.query(R).filter(R.workspace_id == workspace_id, *filters).count()

    identified = db.query(RawReference).filter(RawReference.workspace_id == workspace_id).count()
    records_total = rc()
    screened = rc(R.is_removed == False)                                   # noqa: E712
    included_s1 = rc(R.is_removed == False, R.screen1_decision == "include")  # noqa: E712
    retrieved = rc(R.is_removed == False, R.screen1_decision == "include",   # noqa: E712
                   R.full_text_status == "converted")
    included_final = rc(R.is_removed == False, R.screen2_decision == "include")  # noqa: E712
    return {
        "identified": identified,
        "duplicates_removed": max(identified - records_total, 0),
        "records_manually_removed": rc(R.is_removed == True),             # noqa: E712
        "screened": screened,
        "excluded_screen1": rc(R.is_removed == False, R.screen1_decision == "exclude"),  # noqa: E712
        "included_screen1": included_s1,
        "fulltext_sought": included_s1,
        "fulltext_retrieved": retrieved,
        "fulltext_not_retrieved": max(included_s1 - retrieved, 0),
        "assessed": rc(R.is_removed == False, R.screen1_decision == "include",  # noqa: E712
                       R.screen2_decision.in_(["include", "exclude"])),
        "excluded_screen2": rc(R.is_removed == False, R.screen2_decision == "exclude"),  # noqa: E712
        "included_final": included_final,
    }


# ── Citation helper ────────────────────────────────────────────────────────────

def ref_tag(rec) -> str:
    authors = (rec.authors or "").strip()
    year = rec.year or "n.d."
    if not authors:
        return f"({year})"
    first = authors.split(",")[0].split(";")[0].strip()
    surname = first.split()[0] if first else "Anon"
    multi = ("," in authors) or (";" in authors) or (" and " in authors)
    return f"{surname} et al. {year}" if multi else f"{surname} {year}"


# ── LLM narrative per criterion ────────────────────────────────────────────────

_SYSTEM = """\
You are writing the results section of a scoping review. For the theme below,
synthesize the provided per-study findings into one coherent narrative paragraph
(or a few, if warranted). Cite every source inline using its ref tag exactly as
given, e.g. (Rossi et al. 2021). Do not invent findings or citations; use only
the material provided. Be concise and neutral.

Return only the narrative prose, no headings, no preamble."""


def _narrative(client, model, rq, criterion, items):
    body = "\n\n".join(f"[{it['tag']}] {it['finding']}" +
                       (f"\n   citation: {it['citation']}" if it.get("citation") else "")
                       for it in items)
    user = (f"Research question: {rq or '(not specified)'}\n\n"
            f"Theme (assessment criterion): {criterion}\n\n"
            f"Findings to synthesize:\n{body}")
    resp = client.messages.create(
        model=model, max_tokens=1500,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    return text, resp.usage.input_tokens, resp.usage.output_tokens


def _run(workspace_id: int, api_key: str, user_id: int | None):
    from models import (Record, SessionLocal, Synthesis, SynthesisBlock, UserCostLog,
                        Workspace, authoritative_values, calc_cost, ensure_extraction_fields,
                        workspace_extraction_fields)
    import anthropic

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        model = ws.screening_model or "claude-haiku-4-5"
        ensure_extraction_fields(db, ws)
        narrative_fields = [f for f in workspace_extraction_fields(db, ws)
                            if f.field_type in ("text", "textarea")]
        _set(workspace_id, {"status": "running", "message": "Building synthesis…",
                            "total": len(narrative_fields), "done": 0})

        prisma = compute_prisma(db, workspace_id)

        # preserve prior published state; replace blocks
        syn = db.query(Synthesis).filter(Synthesis.workspace_id == workspace_id).first()
        published = syn.published if syn else False
        if syn:
            db.query(SynthesisBlock).filter(SynthesisBlock.synthesis_id == syn.id).delete()
            syn.prisma_json = json.dumps(prisma)
        else:
            syn = Synthesis(workspace_id=workspace_id, prisma_json=json.dumps(prisma),
                            published=published)
            db.add(syn)
        db.commit()
        db.refresh(syn)

        included = (db.query(Record)
                      .filter(Record.workspace_id == workspace_id,
                              Record.is_removed == False,               # noqa: E712
                              Record.screen2_decision == "include").all())
        extracted = {rec.id: authoritative_values(db, rec) for rec in included}

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        for pos, fld in enumerate(narrative_fields):
            items = []
            for rec in included:
                val = extracted.get(rec.id, {}).get(fld.key)
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                val = (val or "").strip() if isinstance(val, str) else ""
                if val and val.lower() != "not addressed":
                    items.append({"tag": ref_tag(rec), "finding": val})
            if items:
                narrative, i, o = _narrative(client, model, ws.research_question, fld.label, items)
                tin += i
                tout += o
            else:
                narrative = "_No included studies addressed this field._"
            db.add(SynthesisBlock(synthesis_id=syn.id, heading=fld.label,
                                  narrative=narrative, position=pos))
            db.commit()
            _set(workspace_id, {"status": "running", "message": f"Synthesizing {fld.label}…",
                                "total": len(narrative_fields), "done": pos + 1})

        if tin or tout:
            db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="synthesis",
                               input_tokens=tin, output_tokens=tout,
                               cost_usd=calc_cost(model, tin, tout)))
            db.commit()
        _set(workspace_id, {"status": "done", "message": "Synthesis ready.",
                            "total": len(narrative_fields), "done": len(narrative_fields)})
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_synthesis(workspace_id: int, api_key: str, user_id: int | None):
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id), daemon=True).start()
