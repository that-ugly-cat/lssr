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

_SYSTEM = """\
You are screening records for a scoping review at the TITLE + ABSTRACT stage.

Research question:
{rq}

Exclude a record if it CLEARLY meets one or more of these exclusion criteria:
{criteria}

Rules:
- Screening 1 errs toward inclusion. If the abstract is missing or you are
  genuinely unsure, INCLUDE it (it can still be excluded later at full text).
- Exclude only when at least one exclusion criterion clearly applies.

Return ONLY a JSON object, no prose, no code fences:
  {{"decision": "include" | "exclude", "reason": "<one sentence; name the criterion if excluding>"}}"""


def build_system(research_question: str | None, exclusion_criteria: list) -> str:
    crit = "\n".join(f"- {c.label}: {c.description or ''}".rstrip() for c in exclusion_criteria)
    return _SYSTEM.format(rq=(research_question or "(not specified)").strip(),
                          criteria=crit or "(no exclusion criteria defined)")


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


def screen_record(client, system_prompt: str, title: str, abstract: str,
                  model: str) -> tuple[str, str, int, int]:
    """Returns (decision, reason, tokens_in, tokens_out). Defaults to include on
    a malformed response (screening 1 errs toward inclusion)."""
    user = f"Title: {title or '(no title)'}\n\nAbstract: {abstract or '(no abstract)'}"
    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _parse(text) or {}
    decision = "exclude" if str(parsed.get("decision", "")).lower() == "exclude" else "include"
    reason = parsed.get("reason", "") or ""
    return decision, reason, resp.usage.input_tokens, resp.usage.output_tokens


# ── Background job ─────────────────────────────────────────────────────────────

def _run(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False):
    from sqlalchemy import or_
    from models import (Record, SessionLocal, UserCostLog, Workspace,
                        calc_cost, workspace_criteria)
    import anthropic

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        model = ws.screening_model or "claude-haiku-4-5"
        system = build_system(ws.research_question, workspace_criteria(db, ws, "exclusion"))
        # never touch records a human decided; re-run also re-screens model-decided ones
        q = (db.query(Record)
               .filter(Record.workspace_id == workspace_id,
                       Record.is_removed == False,                  # noqa: E712
                       or_(Record.screen1_by.is_(None), Record.screen1_by != "user")))
        if not rerun:
            q = q.filter(Record.screen1_decision == "pending")
        pending = q.all()
        total = len(pending)
        _set(workspace_id, {"status": "running", "message": f"Screening {total} records…",
                            "total": total, "done": 0, "included": 0, "excluded": 0,
                            "cost_usd": 0.0})
        if total == 0:
            _set(workspace_id, {"status": "done", "message": "Nothing to screen.",
                                "total": 0, "done": 0, "included": 0, "excluded": 0, "cost_usd": 0.0})
            return

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        included = excluded = 0

        def work(rec):
            d, r, i, o = screen_record(client, system, rec.title or "", rec.abstract or "", model)
            return rec.id, d, r, i, o

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(work, rec): rec.id for rec in pending}
            done = 0
            for fut in as_completed(futures):
                rec_id = futures[fut]
                try:
                    _, decision, reason, i, o = fut.result()
                except Exception as exc:
                    decision, reason, i, o = "include", f"screening error: {exc}", 0, 0
                rec = db.query(Record).filter(Record.id == rec_id).first()
                rec.screen1_decision = decision
                rec.screen1_reason = reason
                rec.screen1_by = "model"
                rec.screen1_at = datetime.utcnow()
                tin += i
                tout += o
                if decision == "exclude":
                    excluded += 1
                else:
                    included += 1
                done += 1
                if done % 5 == 0:
                    db.commit()
                _update(workspace_id, done=done, included=included, excluded=excluded,
                        cost_usd=round(calc_cost(model, tin, tout), 4))
        db.commit()

        cost = calc_cost(model, tin, tout)
        db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="screen1",
                           input_tokens=tin, output_tokens=tout, cost_usd=cost))
        db.commit()
        _set(workspace_id, {"status": "done",
                            "message": f"Done. {included} included, {excluded} excluded.",
                            "total": total, "done": total, "included": included,
                            "excluded": excluded, "cost_usd": round(cost, 4)})
    except Exception as exc:
        _set(workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start_screen1(workspace_id: int, api_key: str, user_id: int | None, rerun: bool = False):
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id, rerun),
                     daemon=True).start()
