"""
Combined screening 2 + assessment on the full text (steps 8-9).

Per Spit's decision (SPEC §5b): ONE conditional LLM call per included record.
The model reads the full text once and returns the inclusion decision plus — only
if included — a finding + citation for each assessment criterion. Excluded records
cost no assessment tokens.

Background job, parallel workers, cost log (step "screen2"), sticky: only records
that passed screening 1 (include), have converted full text, and are still
screen2 == pending are processed.

JOBS keyed by workspace_id:
  status: 'running'|'done'|'error', message, total, done, included, excluded, cost_usd
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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


_SYSTEM = """\
You are conducting the FULL-TEXT stage of a scoping review.

Research question:
{rq}

STEP 1 — Inclusion decision. Include the study ONLY if it meets ALL of these
inclusion criteria; if any is not met, exclude it:
{inclusion}

STEP 2 — Assessment (only if included). For each assessment criterion below,
extract the study's finding relevant to that criterion, grounded in the text,
with a short supporting quote or locator as the citation. If the text does not
address a criterion, set finding to "not addressed".
{assessment}

Return ONLY a JSON object, no prose, no code fences:
  {{"inclusion_decision": "include" | "exclude",
    "inclusion_reason": "<one sentence>",
    "assessments": [ {{"criterion_id": <int>, "finding": "<text>", "citation": "<quote/locator>"}} ]}}
Populate "assessments" only when inclusion_decision is "include"; otherwise use []."""


def build_system(rq, inclusion_criteria, assessment_criteria) -> str:
    inc = "\n".join(f"- {c.label}: {c.description or ''}".rstrip() for c in inclusion_criteria)
    ass = "\n".join(f"- [id {c.id}] {c.label}: {c.description or ''}".rstrip() for c in assessment_criteria)
    return _SYSTEM.format(rq=(rq or "(not specified)").strip(),
                          inclusion=inc or "(no inclusion criteria defined)",
                          assessment=ass or "(no assessment criteria defined)")


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


def assess_record(client, system_prompt: str, full_text: str, model: str):
    """Returns (decision, reason, assessments_list, tokens_in, tokens_out)."""
    text = (full_text or "")[:MAX_TEXT_CHARS]
    resp = client.messages.create(
        model=model,
        max_tokens=2000,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"Full text:\n\n{text}"}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    parsed = _parse(raw) or {}
    decision = "include" if str(parsed.get("inclusion_decision", "")).lower() == "include" else "exclude"
    reason = parsed.get("inclusion_reason", "") or ""
    assessments = parsed.get("assessments") if isinstance(parsed.get("assessments"), list) else []
    return decision, reason, assessments, resp.usage.input_tokens, resp.usage.output_tokens


def _run(workspace_id: int, api_key: str, user_id: int | None):
    from models import (Assessment, Record, SessionLocal, UserCostLog, Workspace,
                        calc_cost, workspace_criteria)
    import anthropic

    db = SessionLocal()
    try:
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        model = ws.screening_model or "claude-haiku-4-5"
        inclusion = workspace_criteria(db, ws, "inclusion")
        assess_crit = workspace_criteria(db, ws, "assessment")
        valid_ids = {c.id for c in assess_crit}
        system = build_system(ws.research_question, inclusion, assess_crit)

        targets = (db.query(Record)
                     .filter(Record.workspace_id == workspace_id,
                             Record.is_removed == False,            # noqa: E712
                             Record.screen1_decision == "include",
                             Record.full_text_status == "converted",
                             Record.screen2_decision == "pending").all())
        total = len(targets)
        _set(workspace_id, {"status": "running", "message": f"Assessing {total} full texts…",
                            "total": total, "done": 0, "included": 0, "excluded": 0, "cost_usd": 0.0})
        if total == 0:
            _set(workspace_id, {"status": "done", "message": "Nothing to assess.",
                                "total": 0, "done": 0, "included": 0, "excluded": 0, "cost_usd": 0.0})
            return

        client = anthropic.Anthropic(api_key=api_key)
        tin = tout = 0
        included = excluded = 0
        it_id = targets[0].last_seen_iter_id

        def work(rec):
            d, r, a, i, o = assess_record(client, system, rec.full_text_md or "", model)
            return rec.id, d, r, a, i, o

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(work, rec): rec.id for rec in targets}
            done = 0
            for fut in as_completed(futures):
                rec_id = futures[fut]
                try:
                    _, decision, reason, assessments, i, o = fut.result()
                except Exception as exc:
                    decision, reason, assessments, i, o = "exclude", f"assessment error: {exc}", [], 0, 0
                rec = db.query(Record).filter(Record.id == rec_id).first()
                rec.screen2_decision = decision
                rec.screen2_reason = reason
                rec.screen2_by = "model"
                rec.screen2_at = datetime.utcnow()
                # regenerate assessments for this record
                db.query(Assessment).filter(Assessment.record_id == rec_id).delete()
                if decision == "include":
                    for a in assessments:
                        cid = a.get("criterion_id")
                        if cid in valid_ids:
                            db.add(Assessment(workspace_id=workspace_id, record_id=rec_id,
                                             criterion_id=cid, finding=a.get("finding", ""),
                                             citation=a.get("citation", ""), model=model))
                    included += 1
                else:
                    excluded += 1
                tin += i
                tout += o
                done += 1
                db.commit()
                _update(workspace_id, done=done, included=included, excluded=excluded,
                        cost_usd=round(calc_cost(model, tin, tout), 4))

        cost = calc_cost(model, tin, tout)
        db.add(UserCostLog(user_id=user_id, workspace_id=workspace_id, step="screen2",
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


def start_assessment(workspace_id: int, api_key: str, user_id: int | None):
    threading.Thread(target=_run, args=(workspace_id, api_key, user_id), daemon=True).start()
