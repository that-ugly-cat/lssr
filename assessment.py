"""
Combined screening 2 + structured extraction on the full text (steps 8-9).

ONE conditional LLM call per record: read the full text once, return the
screening-2 inclusion decision and — only if included — the extraction field
values. Excluded records cost no extraction tokens.

The call writes the model's *draft*, never the verdict: a ScreenDecision
(stage='screen2', reviewer_kind='model') resolved into Record.screen2_*, plus an
Extraction(reviewer_kind='model') that the review modal pre-fills from. Records a
human already voted on are never re-drafted — reviewers stay authoritative.

Returned values are validated against the field schema (allowed options, number
format, show_if conditions) before they are stored.

Background job, parallel workers, cost log (step "screen2"). JOBS by workspace_id.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

MAX_WORKERS = 4
MAX_TEXT_CHARS = 200_000

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


# ── Prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are conducting the FULL-TEXT stage of a scoping review.

Research question:
{rq}

STEP 1 — Inclusion decision. Include the study only if it meets ALL of these
inclusion criteria; exclude it if any is clearly not met. Use "maybe" only when
the full text genuinely does not settle it.
{inclusion}

STEP 2 — Data extraction (only when the decision is "include"). Fill the fields
below from the full text. Rules:
- Use the exact allowed values for select/multiselect fields; multiselect values
  must be a JSON array of those strings.
- Omit any field you cannot ground in the text — never guess.
- Fill a conditional field only when its stated condition holds.
- For free-text fields (text/textarea), where possible support your answer with a
  short EXACT quote from the article, copied verbatim inside «guillemets», after
  your answer.
{fields}

Return ONLY a JSON object, no prose, no code fences:
  {{"inclusion_decision": "include" | "exclude" | "maybe",
    "inclusion_reason": "<one sentence>",
    "fields": {{"<field key>": <value>, ...}}}}
Use an empty object for "fields" when the decision is not "include"."""


def _fields_spec(fields) -> str:
    lines = []
    for f in fields:
        parts = [f"- {f.key} ({f.field_type}) — {f.label}"]
        if f.help:
            parts.append(f"note: {f.help}")
        opts = f.options()
        if opts:
            parts.append("allowed values: " + " | ".join(opts))
        if f.show_if_key:
            parts.append(f"only when {f.show_if_key} is one of: " + ", ".join(f.show_if_values()))
        lines.append("\n    ".join(parts))
    return "\n".join(lines) or "(no extraction fields defined)"


def build_system(rq, inclusion_criteria, fields) -> str:
    inc = "\n".join(f"- {c.label}: {c.description or ''}".rstrip() for c in inclusion_criteria)
    return _SYSTEM.format(rq=(rq or "(not specified)").strip(),
                          inclusion=inc or "(no inclusion criteria defined)",
                          fields=_fields_spec(fields))


# ── Cost estimate (rough, ~4 chars/token; ignores prompt caching, upper bound) ──

CHARS_PER_TOKEN = 4
EST_OUTPUT_TOKENS = 500  # the JSON decision + a value per field


def estimate_cost(model: str, system_prompt: str, n: int, content_chars: int) -> float:
    from models import calc_cost
    if n <= 0:
        return 0.0
    sys_tokens = max(1, len(system_prompt) // CHARS_PER_TOKEN)
    tokens_in = n * sys_tokens + content_chars // CHARS_PER_TOKEN
    tokens_out = n * EST_OUTPUT_TOKENS
    return calc_cost(model, tokens_in, tokens_out)


def _parse(content: str) -> dict | None:
    content = content.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
    if m:
        content = m.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", content)
    if m:
        content = m.group(0)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return None


# ── Validation against the field schema ────────────────────────────────────────

def coerce_values(fields, raw: dict) -> dict:
    """Keep only what the schema allows: known keys, valid options, numeric
    numbers, and fields whose show_if condition holds."""
    from models import field_visible
    out = {}
    for f in fields:
        if not isinstance(raw, dict) or f.key not in raw:
            continue
        v = raw[f.key]
        opts = f.options()
        if f.field_type == "multiselect":
            vals = [str(x).strip() for x in v] if isinstance(v, list) else [str(v).strip()]
            vals = [x for x in vals if x and (not opts or x in opts)]
            if vals:
                out[f.key] = vals
        elif f.field_type == "select":
            s = str(v).strip()
            if s and (not opts or s in opts):
                out[f.key] = s
        elif f.field_type == "number":
            s = str(v).strip()
            if re.fullmatch(r"-?\d+(\.\d+)?", s):
                out[f.key] = s
        else:
            s = str(v).strip()
            if s:
                out[f.key] = s
    by_key = {f.key: f for f in fields}
    return {k: v for k, v in out.items() if field_visible(by_key[k], out)}


def assess_record(client, system_prompt: str, full_text: str, model: str):
    """Returns (decision, reason, raw_fields, tokens_in, tokens_out)."""
    text = (full_text or "")[:MAX_TEXT_CHARS]
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Full text:\n\n{text}"}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _parse(raw) or {}
    d = str(parsed.get("inclusion_decision", "")).lower()
    decision = d if d in ("include", "exclude", "maybe") else "maybe"  # park the unparseable
    reason = parsed.get("inclusion_reason", "") or ""
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    return decision, reason, fields, resp.usage.input_tokens, resp.usage.output_tokens


# ── Background job ─────────────────────────────────────────────────────────────

def _run(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False):
    from models import (Record, ScreenDecision, SessionLocal, UserCostLog, Workspace,
                        calc_cost, ensure_extraction_fields, recompute_record_screen2,
                        upsert_extraction, upsert_screen_decision, workspace_criteria,
                        workspace_extraction_fields)
    import anthropic

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        model = ws.screening_model or "claude-haiku-4-5"
        ensure_extraction_fields(db, ws)
        fields = workspace_extraction_fields(db, ws)
        inclusion = workspace_criteria(db, ws, "inclusion")
        system = build_system(ws.research_question, inclusion, fields)

        # a record a reviewer already voted on is theirs — never re-drafted
        human_ids = {rid for (rid,) in
                     db.query(ScreenDecision.record_id)
                       .filter(ScreenDecision.workspace_id == workspace_id,
                               ScreenDecision.stage == "screen2",
                               ScreenDecision.reviewer_kind.in_(["user", "adjudicator"])).all()}
        q = (db.query(Record)
               .filter(Record.workspace_id == workspace_id,
                       Record.is_removed == False,                # noqa: E712
                       Record.screen1_decision == "include",
                       Record.full_text_status == "converted"))
        if not rerun:
            q = q.filter(Record.screen2_decision == "pending")
        targets = [r for r in q.all() if r.id not in human_ids]
        total = len(targets)
        _set(workspace_id, {"status": "running", "message": f"Drafting {total} full texts…",
                            "total": total, "done": 0, "included": 0, "excluded": 0,
                            "maybe": 0, "cost_usd": 0.0})
        if total == 0:
            _set(workspace_id, {"status": "done", "message": "Nothing to draft.",
                                "total": 0, "done": 0, "included": 0, "excluded": 0,
                                "maybe": 0, "cost_usd": 0.0})
            return

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        included = excluded = maybe = 0

        def work(rec):
            d, r, fl, i, o = assess_record(client, system, rec.full_text_md or "", model)
            return rec.id, d, r, fl, i, o

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(work, rec): rec.id for rec in targets}
            done = 0
            for fut in as_completed(futures):
                rec_id = futures[fut]
                try:
                    _, decision, reason, raw_fields, i, o = fut.result()
                except Exception as exc:
                    decision, reason, raw_fields, i, o = "maybe", f"assessment error: {exc}", {}, 0, 0
                rec = db.query(Record).filter(Record.id == rec_id).first()
                upsert_screen_decision(db, rec, "screen2", "model", None, decision, reason)
                recompute_record_screen2(db, ws, rec)
                if decision == "include":
                    upsert_extraction(db, ws, rec, "model", None, coerce_values(fields, raw_fields))
                    included += 1
                elif decision == "maybe":
                    maybe += 1
                else:
                    excluded += 1
                tin += i
                tout += o
                done += 1
                db.commit()
                _update(workspace_id, done=done, included=included, excluded=excluded,
                        maybe=maybe, cost_usd=round(calc_cost(model, tin, tout), 4))

        cost = calc_cost(model, tin, tout)
        db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="screen2",
                           input_tokens=tin, output_tokens=tout, cost_usd=cost))
        db.commit()
        _set(workspace_id, {"status": "done",
                            "message": f"Done. {included} included, {excluded} excluded, {maybe} maybe.",
                            "total": total, "done": total, "included": included,
                            "excluded": excluded, "maybe": maybe, "cost_usd": round(cost, 4)})
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_assessment(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False):
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id, rerun),
                     daemon=True).start()
