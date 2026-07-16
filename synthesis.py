"""
Synthesis (step 10): the public deliverable.

Builds the PRISMA flow counts, then a sequence of blocks:
  • Block 0 — "Study characteristics": a procedural distribution summary of the
    structured "fixed variable" fields (select/multiselect/number: country, study
    year, study type, methodology axes…). No LLM, so no miscounted figures.
  • One block per assessment criterion (text/textarea field): the LLM aggregates
    the per-study findings into a narrative paragraph. Citations are NOT authored
    by the LLM — it only inserts a study token ([S1], [S2]…) which we substitute
    procedurally with a citation built from the record (Surname et al., Year,
    DOI/link), so a citation can never be hallucinated.

Values come from each record's authoritative extraction (curated final row, else
the latest reviewer's, else the model draft). Stored as Synthesis + SynthesisBlock
rows, shown on the public /r/{token} page when published. Background job, JOBS
keyed by workspace_id.
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


# ── Citations (procedural — never authored by the LLM) ──────────────────────────

def citation(rec) -> str:
    """Full inline citation built from the record's own fields:
    'Surname et al., Year, https://doi.org/…'. The LLM never writes this — it only
    emits a study token that we substitute here, so citations can't be hallucinated."""
    authors = (rec.authors or "").strip()
    year = rec.year or "n.d."
    if authors:
        first = authors.split(",")[0].split(";")[0].strip()
        surname = first.split()[0] if first else "Anon"
        multi = ("," in authors) or (";" in authors) or (" and " in authors)
        who = f"{surname} et al." if multi else surname
    else:
        who = "Anon"
    link = f"https://doi.org/{rec.doi}" if rec.doi else (rec.url or "")
    parts = [who, str(year)] + ([link] if link else [])
    return ", ".join(parts)


_TOKEN_RE = re.compile(r"\[(S\d+)\]")


def _substitute_citations(text: str, token_cite: dict) -> str:
    """Replace each [S#] study token the LLM placed with the procedural citation;
    drop any token that isn't in the map (a hallucinated reference)."""
    out = _TOKEN_RE.sub(lambda m: f"({token_cite[m.group(1)]})"
                        if m.group(1) in token_cite else "", text)
    return re.sub(r" {2,}", " ", out).strip()


# ── General block: structured "fixed variables" (procedural, no LLM) ────────────

def general_narrative(structured_fields, extracted, included) -> str:
    """A deterministic distribution summary of the structured extraction fields
    across the included studies. No LLM, so no risk of a miscounted figure."""
    import statistics
    from collections import Counter
    from models import field_visible

    if not included:
        return "_No studies were included in the synthesis._"
    lines = [f"**{len(included)} studies** were included in the synthesis."]
    for fld in structured_fields:
        counts: Counter = Counter()
        nums: list = []
        for rec in included:
            vals = extracted.get(rec.id, {})
            if not field_visible(fld, vals):
                continue
            v = vals.get(fld.key)
            if v in (None, "") or (isinstance(v, list) and not v):
                continue
            if fld.field_type == "number":
                try:
                    nums.append(float(v))
                except (TypeError, ValueError):
                    pass
            elif isinstance(v, list):
                for x in v:
                    if x not in (None, ""):
                        counts[str(x)] += 1
            else:
                counts[str(v)] += 1
        if fld.field_type == "number" and nums:
            lo, hi = int(min(nums)), int(max(nums))
            med = statistics.median(nums)
            med = int(med) if med == int(med) else round(med, 1)
            span = f"{lo}" if lo == hi else f"{lo}–{hi}"
            lines.append(f"- **{fld.label}:** {span} (median {med}, n={len(nums)})")
        elif counts:
            parts = ", ".join(f"{k} ({n})" for k, n in counts.most_common())
            lines.append(f"- **{fld.label}:** {parts}")
    return "\n".join(lines)


# ── LLM narrative per assessment criterion (text/textarea fields) ───────────────

_SYSTEM = """\
You are writing the results section of a scoping review. For the theme below,
synthesize the provided per-study findings into one coherent narrative paragraph
(or a few, if warranted). Cite each study you draw on by inserting ITS TOKEN
exactly as given, in square brackets, e.g. [S1]; put the token right after the
statement it supports. Do NOT write author names, years, DOIs, or links yourself —
only the token. Do not invent findings or tokens; use only the material provided.
Be concise and neutral.

Return only the narrative prose, no headings, no preamble."""


def _narrative(client, model, rq, criterion, items):
    body = "\n\n".join(f"[{it['token']}] {it['finding']}" for it in items)
    user = (f"Research question: {rq or '(not specified)'}\n\n"
            f"Theme (assessment criterion): {criterion}\n\n"
            f"Findings to synthesize (each prefixed by its study token):\n{body}")
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
        fields = workspace_extraction_fields(db, ws)
        structured_fields = [f for f in fields if f.field_type in ("select", "multiselect", "number")]
        narrative_fields = [f for f in fields if f.field_type in ("text", "textarea")]
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
        # stable per-study token → procedural citation (LLM only ever sees the token)
        tokens = {rec.id: f"S{i + 1}" for i, rec in enumerate(included)}
        token_cite = {tokens[rec.id]: citation(rec) for rec in included}

        # Block 0: the general "fixed variables" summary — procedural, no LLM.
        db.add(SynthesisBlock(synthesis_id=syn.id, heading="Study characteristics",
                              narrative=general_narrative(structured_fields, extracted, included),
                              position=0))
        db.commit()

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        for i, fld in enumerate(narrative_fields):
            items = []
            for rec in included:
                val = extracted.get(rec.id, {}).get(fld.key)
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                val = (val or "").strip() if isinstance(val, str) else ""
                if val and val.lower() != "not addressed":
                    items.append({"token": tokens[rec.id], "finding": val})
            if items:
                raw, ti, to = _narrative(client, model, ws.research_question, fld.label, items)
                narrative = _substitute_citations(raw, token_cite)
                tin += ti
                tout += to
            else:
                narrative = "_No included studies addressed this field._"
            db.add(SynthesisBlock(synthesis_id=syn.id, heading=fld.label,
                                  narrative=narrative, position=i + 1))
            db.commit()
            _set(workspace_id, {"status": "running", "message": f"Synthesizing {fld.label}…",
                                "total": len(narrative_fields), "done": i + 1})

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
    _set(workspace_id, {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id), daemon=True).start()
