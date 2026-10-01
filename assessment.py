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
# All prompt text lives in prompts.py; build_system stays exported under this name
# so callers keep working.
from prompts import assessment_system as build_system, assessment_user  # noqa: E402


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

# "Name (CODE)" with an ISO-style code only: "Decrease (crowding-out reported)"
# must not answer to a bare "Decrease", whose parenthesis carries the meaning.
_OPTION_CODE = re.compile(r"^(.*\S)\s*\(([A-Z]{2}(?:-[A-Z0-9]{1,3})?)\)$")


def _option_lookup(opts: list[str]) -> dict[str, str]:
    """Every spelling the model uses for an option, folded, → the option itself.

    The model writes "Netherlands" where the schema says "Netherlands (NL)", at
    random from one run to the next: about one country in four came back that
    way and was dropped without a word. So an option of the form "Name (CODE)",
    CODE being a country-style code, also answers to its name. Not to the bare code: "NA" written for "not
    available" would become Namibia. A spelling two options share answers to
    neither: guessing between them would invent a value."""
    seen: dict[str, set] = {}
    for o in opts:
        keys = {o.casefold()}
        m = _OPTION_CODE.match(o)
        if m:
            keys.add(m.group(1).casefold())
        for k in keys:
            seen.setdefault(k, set()).add(o)
    return {k: next(iter(v)) for k, v in seen.items() if len(v) == 1}


def _match_option(s: str, opts: list[str], lookup: dict[str, str]) -> str | None:
    if s in opts:
        return s
    return lookup.get(" ".join(s.split()).casefold())


def coerce_values(fields, raw: dict, dropped: list | None = None) -> dict:
    """Keep only what the schema allows: known keys, valid options, numeric
    numbers, and fields whose show_if condition holds. A select value is
    matched loosely (see _option_lookup); what still fits no option is
    appended to `dropped` as (field key, value), so the caller can say so
    instead of losing it."""
    from models import field_visible
    out = {}
    for f in fields:
        if not isinstance(raw, dict) or f.key not in raw:
            continue
        v = raw[f.key]
        opts = f.options()
        lookup = _option_lookup(opts) if opts else {}
        if f.field_type == "multiselect":
            vals = [str(x).strip() for x in v] if isinstance(v, list) else [str(v).strip()]
            kept = []
            for x in filter(None, vals):
                m = _match_option(x, opts, lookup) if opts else x
                if m is None:
                    if dropped is not None:
                        dropped.append((f.key, x))
                elif m not in kept:
                    kept.append(m)
            if kept:
                out[f.key] = kept
        elif f.field_type == "select":
            s = str(v).strip()
            m = (_match_option(s, opts, lookup) if opts else s) if s else None
            if m:
                out[f.key] = m
            elif s and dropped is not None:
                dropped.append((f.key, s))
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


def _create_with_retry(client, *, tries: int = 3, **kw):
    """Anthropic call with a couple of retries for transient failures."""
    import time
    for attempt in range(tries):
        try:
            return client.messages.create(**kw)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def _stream_with_retry(client, *, tries: int = 3, **kw):
    """Same as _create_with_retry, but streamed: above ~16k output tokens a
    non-streaming request risks the SDK's HTTP timeout. The final message has
    the same shape as a create() response."""
    import time
    for attempt in range(tries):
        try:
            with client.messages.stream(**kw) as stream:
                return stream.get_final_message()
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def assess_record(client, system_prompt: str, full_text: str, model: str, meta: dict | None = None):
    """Returns (decision, reason, raw_fields, tokens_in, tokens_out). The reader
    keeps the whole full text; the model gets it without references/back matter.
    meta: the record's title, authors and year, which the prompt's STEP 0 checks
    the text against."""
    from fulltext import strip_back_matter
    from models import MAX_OUTPUT_TOKENS
    text = strip_back_matter(full_text or "")[:MAX_TEXT_CHARS]
    resp = _stream_with_retry(
        client,
        model=model,
        # Thinking counts against max_tokens, and on current models (Sonnet 5,
        # Opus 5) it runs by default when `thinking` is omitted. At 2000 the
        # reasoning used the whole budget and the JSON stopped after one line:
        # 13 of a 16-record pilot came back unparseable. 16000 then cut off two
        # long basic-science papers of 160, so 32000, which needs streaming;
        # now the shared ceiling.
        max_tokens=MAX_OUTPUT_TOKENS,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": assessment_user(text, **(meta or {}))}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _parse(raw) or {}
    d = str(parsed.get("inclusion_decision", "")).lower()
    decision = d if d in ("include", "exclude", "maybe") else "maybe"  # park the unparseable
    reason = parsed.get("inclusion_reason", "") or ""
    if not parsed:
        # say why a record was parked, or a reviewer sees a 'maybe' with no reason
        reason = ("(draft cut off at the output limit — re-draft this record)"
                  if resp.stop_reason == "max_tokens"
                  else "(the model's answer could not be read as JSON — re-draft this record)")
    fields = parsed.get("fields") if isinstance(parsed.get("fields"), dict) else {}
    return decision, reason, fields, resp.usage.input_tokens, resp.usage.output_tokens


# ── Background job ─────────────────────────────────────────────────────────────

def _run(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False,
         sample_pct: int | None = None):
    """sample_pct: draft only a random share of the pending records — a pilot.
    The drafts are ordinary ones (provisional, pre-filling the review form);
    what makes it a pilot is that people review those records before the rest
    is drafted, and draft_agreement then compares the two. A second sample
    widens the first, because a drafted record is no longer pending."""
    import math
    import random
    from models import (Extraction, Record, ScreenDecision, SessionLocal, UserCostLog, Workspace,
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
        if not rerun or sample_pct:
            q = q.filter(Record.screen2_decision == "pending")
        targets = [r for r in q.all() if r.id not in human_ids]
        if sample_pct and targets:
            pct = min(100, max(1, int(sample_pct)))
            targets = random.sample(targets, max(1, math.ceil(len(targets) * pct / 100)))
        total = len(targets)
        what = f"a {sample_pct}% sample: {total}" if sample_pct else str(total)
        _set(workspace_id, {"status": "running", "message": f"Drafting {what} full texts…",
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

        # Snapshot the full text on the job thread — reading it inside a worker
        # after a commit expired it would hit the shared session cross-thread.
        snaps = [(r.id, r.full_text_md or "",
                  {"title": r.title, "authors": r.authors, "year": r.year}) for r in targets]

        def work(snap):
            rid, md, meta = snap
            d, r, fl, i, o = assess_record(client, system, md, model, meta)
            return rid, d, r, fl, i, o

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(work, s): s[0] for s in snaps}
            done = 0
            for fut in as_completed(futures):
                rec_id = futures[fut]
                try:
                    _, decision, reason, raw_fields, i, o = fut.result()
                except Exception as exc:
                    decision, reason, raw_fields, i, o = "maybe", f"assessment error: {exc}", {}, 0, 0
                rec = db.query(Record).filter(Record.id == rec_id).first()
                values, dropped = None, []
                if decision == "include":
                    values = coerce_values(fields, raw_fields, dropped)
                if dropped:
                    # A value outside the schema is not data, but its absence is
                    # news: say it where the reviewer reads the draft.
                    reason = (reason + " [Dropped, not in the schema: "
                              + "; ".join(f"{k} = {v!r}" for k, v in dropped) + "]").strip()
                upsert_screen_decision(db, rec, "screen2", "model", None, decision, reason)
                recompute_record_screen2(db, ws, rec)
                if decision == "include":
                    upsert_extraction(db, ws, rec, "model", None, values)
                    included += 1
                else:
                    # A re-draft that no longer includes takes the old draft's
                    # extraction with it: left behind, it pre-filled the review
                    # form with the values of a paper this draft has just said
                    # is the wrong file, or does not belong in the review.
                    (db.query(Extraction)
                       .filter(Extraction.record_id == rec.id,
                               Extraction.reviewer_kind == "model").delete())
                    if decision == "maybe":
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


def start_assessment(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False,
                     sample_pct: int | None = None):
    # Mark running synchronously so the reloaded page's first status poll never
    # races the job's own setup and sees 'idle' (which stops the poller). Re-draft
    # scans every record, so its setup is slow enough to lose that race otherwise.
    _set(workspace_id, {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id, rerun, sample_pct),
                     daemon=True).start()
