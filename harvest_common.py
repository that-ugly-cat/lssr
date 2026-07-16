"""
Shared job-tracking + ingest plumbing for the direct-harvest sources
(Europe PMC, OpenAlex, ERIC; PubMed keeps its own module but exposes the same
start/get_job interface). One run per (database, workspace) at a time.

Each source module implements a pure `search(query, year_from, year_to,
on_progress) -> list[ref dict]` that does HTTP + parsing only — it never touches
the ORM (the snapshot rule: worker code must not lazy-load ORM attributes across
threads). Ingest happens back on the single job thread here.

Job dict shape (same as pubmed.py, so poller.js handles it unchanged):
  status: 'searching' | 'downloading' | 'done' | 'error'
  message, total, downloaded, new_count, merged_count, error
"""
import threading

# Keyed by (database, workspace_id) so different sources for the same workspace
# never collide in the registry.
JOBS: dict[tuple, dict] = {}
_lock = threading.Lock()


def get_job(database: str, workspace_id: int) -> dict | None:
    with _lock:
        return JOBS.get((database, workspace_id))


def _set(database: str, workspace_id: int, data: dict):
    with _lock:
        JOBS[(database, workspace_id)] = data


def _update(database: str, workspace_id: int, **kw):
    with _lock:
        j = JOBS.get((database, workspace_id))
        if j is not None:
            j.update(kw)


def _run(database, workspace_id, query, year_from, year_to, user_id, search_fn, label):
    from models import SessionLocal, Workspace, current_iteration
    from ingest import ingest_references

    def on_progress(**kw):
        _update(database, workspace_id, **kw)

    _set(database, workspace_id, {"status": "searching",
                                  "message": f"Searching {label}…",
                                  "total": 0, "downloaded": 0})
    db = SessionLocal()
    try:
        refs = search_fn(query, year_from, year_to, on_progress)
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        it = current_iteration(db, ws)
        imp = ingest_references(db, ws, it, refs, database=database, fmt="api",
                                source_name=query[:120], user_id=user_id)
        _set(database, workspace_id, {
            "status": "done",
            "message": f"Done. {imp.new_count} new, {imp.merged_count} merged.",
            "total": len(refs), "downloaded": len(refs),
            "new_count": imp.new_count, "merged_count": imp.merged_count,
        })
    except Exception as exc:
        _set(database, workspace_id, {"status": "error", "message": str(exc), "error": str(exc)})
    finally:
        db.close()


def start(database, workspace_id, query, year_from, year_to, user_id, search_fn, label):
    _set(database, workspace_id, {"status": "searching", "message": "Starting…",
                                  "total": 0, "downloaded": 0})
    threading.Thread(
        target=_run,
        args=(database, workspace_id, query, year_from, year_to, user_id, search_fn, label),
        daemon=True,
    ).start()
