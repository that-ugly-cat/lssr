"""
Screening 1 — title + abstract vs exclusion criteria (step 5).

Runs in a background thread. Records are screened in parallel (ThreadPoolExecutor);
worker threads only call the Anthropic API, all DB writes happen in the job thread
(the AutoCode execution model). Decisions are sticky: only records with
screen1_decision == 'pending' (and not removed) are processed, so re-runs and
later iterations never re-screen settled records.

Screening 1 errs toward inclusion: a record is excluded only if the model finds
it clearly meets an exclusion criterion.

JOBS keyed by workspace_id:
  status: 'running' | 'done' | 'error'
  message, total, done, included, excluded, cost_usd, error
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

MAX_WORKERS = 5

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
from prompts import screening_system as build_system, screening_user  # noqa: E402


# ── Cost estimate (rough, ~4 chars/token; ignores prompt caching, so it reads as
#    an upper bound) ─────────────────────────────────────────────────────────────

CHARS_PER_TOKEN = 4
EST_OUTPUT_TOKENS = 80  # the short include/exclude JSON reply


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


def _create_with_retry(client, *, tries: int = 3, **kw):
    """Anthropic call with a couple of retries for transient failures
    (rate limits, 5xx, network blips)."""
    import time
    for attempt in range(tries):
        try:
            return client.messages.create(**kw)
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def screen_record(client, system_prompt: str, title: str, abstract: str,
                  model: str) -> tuple[str, str, int, int]:
    """Returns (decision, reason, tokens_in, tokens_out). Defaults to include on
    a malformed response (screening 1 errs toward inclusion)."""
    user = screening_user(title, abstract)
    resp = _create_with_retry(
        client,
        model=model,
        max_tokens=300,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _parse(text) or {}
    d = str(parsed.get("decision", "")).lower()
    decision = d if d in ("include", "exclude", "maybe") else "maybe"  # park the unparseable
    reason = parsed.get("reason", "") or ""
    return decision, reason, resp.usage.input_tokens, resp.usage.output_tokens


# ── Background job ─────────────────────────────────────────────────────────────

def _run(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False):
    from models import (Record, ScreenDecision, SessionLocal, UserCostLog, Workspace,
                        calc_cost, recompute_record_screen1, upsert_screen_decision,
                        workspace_criteria)
    import anthropic

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        model = ws.screening_model or "claude-haiku-4-5"
        system = build_system(ws.research_question, workspace_criteria(db, ws, "exclusion"))
        # records any human already voted on (or an adjudicator resolved) are the
        # humans' call — the model never overrides them.
        human_ids = {rid for (rid,) in
                     db.query(ScreenDecision.record_id)
                       .filter(ScreenDecision.workspace_id == workspace_id,
                               ScreenDecision.stage == "screen1",
                               ScreenDecision.reviewer_kind.in_(["user", "adjudicator"])).all()}
        q = (db.query(Record)
               .filter(Record.workspace_id == workspace_id,
                       Record.is_removed == False))               # noqa: E712
        if not rerun:
            # default: only records with no decision yet (no model row, no human)
            q = q.filter(Record.screen1_decision == "pending")
        # re-run also re-screens the model's own past calls, but never human ones
        pending = [r for r in q.all() if r.id not in human_ids]
        total = len(pending)
        _set(workspace_id, {"status": "running", "message": f"Screening {total} records…",
                            "total": total, "done": 0, "included": 0, "excluded": 0, "maybe": 0,
                            "cost_usd": 0.0})
        if total == 0:
            _set(workspace_id, {"status": "done", "message": "Nothing to screen.",
                                "total": 0, "done": 0, "included": 0, "excluded": 0, "maybe": 0,
                                "cost_usd": 0.0})
            return

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        included = excluded = maybe = 0

        # Snapshot the text now, on the job thread. Reading rec.title/abstract
        # inside a worker would lazy-reload after a commit expired them, hitting
        # the shared SQLite session from another thread — the "prepared state" bug.
        snaps = [(r.id, r.title or "", r.abstract or "") for r in pending]

        def work(snap):
            rid, title, abstract = snap
            d, r, i, o = screen_record(client, system, title, abstract, model)
            return rid, d, r, i, o

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(work, s): s[0] for s in snaps}
            done = 0
            for fut in as_completed(futures):
                rec_id = futures[fut]
                try:
                    _, decision, reason, i, o = fut.result()
                except Exception as exc:
                    decision, reason, i, o = "maybe", f"screening error: {exc}", 0, 0
                rec = db.query(Record).filter(Record.id == rec_id).first()
                upsert_screen_decision(db, rec, "screen1", "model", None, decision, reason)
                recompute_record_screen1(db, ws, rec)
                tin += i
                tout += o
                if decision == "exclude":
                    excluded += 1
                elif decision == "maybe":
                    maybe += 1
                else:
                    included += 1
                done += 1
                if done % 5 == 0:
                    db.commit()
                _update(workspace_id, done=done, included=included, excluded=excluded,
                        maybe=maybe, cost_usd=round(calc_cost(model, tin, tout), 4))
        db.commit()

        cost = calc_cost(model, tin, tout)
        db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="screen1",
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


def start_screen1(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False):
    # Mark running synchronously so the reloaded page's first status poll never
    # races the job's own setup and sees 'idle' (which stops the poller).
    _set(workspace_id, {"status": "running", "message": "Starting…", "total": 0, "done": 0})
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id, rerun),
                     daemon=True).start()
